"""Inbox detection: classify messages as bounce, auto-reply, opt-out or reply, and act on them."""

from __future__ import annotations

import datetime
import email.utils
import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from mailer import gmail_client, safety
from mailer.config import Settings, load_settings
from mailer.db import add_suppression, finish_completed_leads, open_conn, set_app_state, set_lead_status, to_iso, transaction
from mailer.gmail_client import AuthExpired, GmailClientError, LABELS, MessageMeta

logger = logging.getLogger("detection")

LAST_RUN_KEY = "last_detection_run"
BASE_QUERY = "newer_than:3d -from:me"

BOUNCE_FROM_RE = re.compile(r"mailer-daemon@|postmaster@", re.IGNORECASE)
BOUNCE_SUBJECT_RE = re.compile(
    r"Delivery Status Notification|Undeliverable|Mail delivery failed|Address not found|Returned mail",
    re.IGNORECASE,
)
AUTO_REPLY_SUBJECT_RE = re.compile(
    r"Automatic reply|Auto-reply|Out of office|Réponse automatique|Absence",
    re.IGNORECASE,
)
EMAIL_ADDRESS_RE = re.compile(r"[^@\s<>\"'(),;:]+@[^@\s<>\"'(),;:]+\.[A-Za-z]{2,}")

OPT_OUT_PHRASES = (
    "stop",
    "unsubscribe",
    "remove me",
    "opt out",
    "désabonner",
    "désinscrire",
    "ne plus recevoir",
)
# Hyphens count as part of a word, so "non-stop" is not an opt-out.
OPT_OUT_RE = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in OPT_OUT_PHRASES) + r")(?![\w-])",
    re.IGNORECASE,
)

# Where quoted text begins. The attribution line can wrap onto a second line.
QUOTE_START_RE = re.compile(
    r"^\s*(?:"
    r"On\b[^\n]*(?:\n[^\n]*)?\bwrote:"
    r"|Le\b[^\n]*(?:\n[^\n]*)?\ba\s+écrit\s*:"
    r"|-{2,}\s*(?:Original Message|Message d'origine)\s*-{2,}"
    r"|(?:From|De)\s*:[^\n]*\n\s*(?:Sent|Date|Envoyé)\s*:"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def strip_quoted_text(text: str) -> str:
    """Remove quoted lines and everything after the start of a quoted message."""
    match = QUOTE_START_RE.search(text)
    kept = text[:match.start()] if match else text
    return "\n".join(line for line in kept.splitlines() if not line.lstrip().startswith(">"))


def is_opt_out(body: str, footer_lines: Sequence[str] = ()) -> bool:
    """Return True if the person's own words ask to opt out. Unsure means a normal reply."""
    own_words = [
        line for line in strip_quoted_text(body).splitlines()
        if not any(footer in line for footer in footer_lines)
    ]
    return OPT_OUT_RE.search("\n".join(own_words)) is not None


def is_bounce(message: MessageMeta) -> bool:
    """Return True for a delivery failure notice."""
    return bool(BOUNCE_FROM_RE.search(message.from_) or BOUNCE_SUBJECT_RE.search(message.subject))


def is_auto_reply(message: MessageMeta) -> bool:
    """Return True for an out-of-office or other automatic message."""
    auto_submitted = (message.auto_submitted or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if message.x_autoreply is not None or message.x_autorespond is not None:
        return True
    if (message.precedence or "").strip().lower() in ("auto_reply", "bulk"):
        return True
    return AUTO_REPLY_SUBJECT_RE.search(message.subject) is not None


def _sender(message: MessageMeta) -> str:
    return email.utils.parseaddr(message.from_)[1].lower()


def _lead_by_email(address: str, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM leads WHERE email = ?", (address.strip().lower(),)).fetchone()


def _lead_by_thread(thread_id: str, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM leads WHERE thread_id = ?", (thread_id,)).fetchone() if thread_id else None


def _bounced_lead(
    message: MessageMeta,
    inbox_email: str,
    client: Any,
    conn: sqlite3.Connection,
) -> Tuple[Optional[sqlite3.Row], str]:
    """Find who bounced: the X-Failed-Recipients header, else the first lead address in the text."""
    named = [address for _, address in email.utils.getaddresses([message.x_failed_recipients or ""]) if address]
    for address in named:
        lead = _lead_by_email(address, conn)
        if lead is not None:
            return lead, address.lower()

    text = client.get_message_text(inbox_email, message.id)
    for address in EMAIL_ADDRESS_RE.findall(text):
        lead = _lead_by_email(address, conn)
        if lead is not None:
            return lead, address.lower()

    return None, named[0].lower() if named else ""


def _record_event(
    event_type: str,
    message: MessageMeta,
    inbox_id: int,
    lead: Optional[sqlite3.Row],
    address: str,
    now: datetime.datetime,
    conn: sqlite3.Connection,
) -> bool:
    """Store an event. Returns False if this message was already processed."""
    with transaction(conn):
        cursor = conn.execute(
            """INSERT OR IGNORE INTO events (ts, inbox_id, lead_id, email, type, gmail_message_id, subject, snippet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (to_iso(now), inbox_id, lead["id"] if lead else None, address, event_type, message.id, message.subject, message.snippet),
        )
    return cursor.rowcount > 0


def _apply_label(inbox_email: str, message_id: str, label: str, client: Any) -> None:
    """Label a message. A failed label must not undo the detection."""
    try:
        client.add_label(inbox_email, message_id, client.ensure_label(inbox_email, label))
    except GmailClientError as e:
        logger.warning("Could not label message %s in %s: %s", message_id, inbox_email, e)


def _footer_lines(conn: sqlite3.Connection) -> List[str]:
    """Return fixed footer text, like our own 'Reply stop' line, which a person's reply may repeat."""
    lines: List[str] = []
    for row in conn.execute("SELECT footer_text FROM languages"):
        lines += [line.strip() for line in row["footer_text"].splitlines() if line.strip() and "{{" not in line]
    return lines


def process_message(
    message: MessageMeta,
    inbox: sqlite3.Row,
    alias: str,
    footer_lines: Sequence[str],
    client: Any,
    now: datetime.datetime,
    conn: sqlite3.Connection,
) -> Optional[str]:
    """Classify one message and act on it. Returns the event type, or None if it is ignored."""
    inbox_email = inbox["email"]

    if is_bounce(message):
        lead, address = _bounced_lead(message, inbox_email, client, conn)
        if not _record_event("bounce", message, inbox["id"], lead, address, now, conn):
            return None
        if lead is not None:
            add_suppression(lead["email"], "bounced", conn)
            if lead["status"] != "unsubscribed":
                set_lead_status(lead["id"], "bounced", conn)
            _apply_label(inbox_email, message.id, LABELS["bounced"], client)
        logger.info("Bounce: inbox=%s address=%s", inbox_email, address)
        return "bounce"

    sender = _sender(message)
    lead = _lead_by_thread(message.thread_id, conn) or _lead_by_email(sender, conn)

    if is_auto_reply(message):
        _record_event("auto_reply_ignored", message, inbox["id"], lead, sender, now, conn)
        logger.info("Auto-reply ignored: inbox=%s from=%s", inbox_email, sender)
        return "auto_reply_ignored"

    if lead is None:
        return None

    to_alias = bool(alias) and alias.lower() in [address.lower() for _, address in email.utils.getaddresses([message.to])]
    if to_alias or is_opt_out(client.get_message_text(inbox_email, message.id), footer_lines):
        if not _record_event("unsubscribe", message, inbox["id"], lead, sender, now, conn):
            return None
        add_suppression(lead["email"], "unsubscribed", conn)
        set_lead_status(lead["id"], "unsubscribed", conn)
        _apply_label(inbox_email, message.id, LABELS["unsubscribed"], client)
        logger.info("Opt-out: inbox=%s lead=%s", inbox_email, lead["email"])
        return "unsubscribe"

    if not _record_event("reply", message, inbox["id"], lead, sender, now, conn):
        return None
    if lead["status"] not in ("unsubscribed", "bounced"):
        set_lead_status(lead["id"], "replied", conn)
    _apply_label(inbox_email, message.id, LABELS["replied"], client)
    logger.info("Reply: inbox=%s lead=%s", inbox_email, lead["email"])
    return "reply"


def run_inbox(
    inbox: sqlite3.Row,
    settings: Settings,
    footer_lines: Sequence[str],
    client: Any,
    now: datetime.datetime,
    conn: sqlite3.Connection,
) -> Dict[str, int]:
    """Check one inbox and return counts per event type."""
    alias = settings.sender.list_unsubscribe_address.strip()
    query = f"in:inbox {BASE_QUERY}"
    if alias and inbox["email"].lower() == settings.sender.unsubscribe_inbox:
        query = f"{{in:inbox to:{alias}}} {BASE_QUERY}"

    seen = {row["gmail_message_id"] for row in conn.execute("SELECT gmail_message_id FROM events")}
    counts: Dict[str, int] = {}
    for message in client.list_recent_messages(inbox["email"], query):
        if message.id in seen:
            continue
        try:
            event_type = process_message(message, inbox, alias, footer_lines, client, now, conn)
        except (AuthExpired, gmail_client.RateLimited, gmail_client.QuotaExceeded):
            raise
        except GmailClientError as e:
            # Left without an event, so the next run tries this message again.
            logger.warning("Could not process message %s in %s: %s", message.id, inbox["email"], e)
            continue
        if event_type:
            counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def run_all_inboxes(
    settings: Optional[Settings] = None,
    client: Any = gmail_client,
    now: Optional[datetime.datetime] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, int]:
    """Check every connected inbox, close finished sequences, and return total counts."""
    if conn is None:
        with open_conn() as c:
            return run_all_inboxes(settings, client, now, c)

    cfg = settings or load_settings()
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    footer_lines = _footer_lines(conn)
    totals: Dict[str, int] = {}

    for inbox in conn.execute("SELECT * FROM inboxes WHERE active = 1 ORDER BY id").fetchall():
        if not client.has_token(inbox["email"]):
            continue
        try:
            counts = run_inbox(inbox, cfg, footer_lines, client, moment, conn)
        except AuthExpired:
            safety.pause_inbox(inbox["id"], "Reconnect inbox", conn)
            continue
        except GmailClientError as e:
            logger.warning("Detection skipped %s: %s", inbox["email"], e)
            continue
        for event_type, count in counts.items():
            totals[event_type] = totals.get(event_type, 0) + count

    finish_completed_leads(to_iso(moment), conn)
    set_app_state(LAST_RUN_KEY, to_iso(moment), conn)
    logger.info("Detection finished: %s", totals)
    return totals
