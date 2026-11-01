"""One worker tick: pick an inbox and a lead, render, and record the send."""

from __future__ import annotations

import datetime
import logging
import random
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from mailer import gmail_client
from mailer.config import Settings, load_settings
from mailer.db import get_active_steps, get_app_state, open_conn, set_app_state, to_iso, transaction
from mailer.scheduling import (
    IST,
    due_follow_ups,
    in_recipient_window,
    queued_leads,
    sent_last_24h,
    todays_cap,
)
from mailer.templating import TemplateError, render

logger = logging.getLogger("engine")

LAST_INBOX_KEY = "last_inbox_id"
DRY_RUN_REPEAT_HOURS = 24

# Live sending arrives in Milestone 5. Until then a tick in LIVE mode sends nothing.
LIVE_SENDING_READY = False

_live_warning_logged = False


def _next_step(current_step: int, active_steps: Sequence[int]) -> Optional[int]:
    """Return the next active step after current_step, or None if the sequence is over."""
    return next((step for step in active_steps if step > current_step), None)


def _auto_resume(now: datetime.datetime, conn: sqlite3.Connection) -> None:
    """Resume inboxes whose timed pause has passed."""
    rows = conn.execute(
        "SELECT id, email FROM inboxes WHERE paused_until IS NOT NULL AND paused_until <= ?",
        (to_iso(now),),
    ).fetchall()
    for row in rows:
        with transaction(conn):
            conn.execute("UPDATE inboxes SET paused_reason = NULL, paused_until = NULL WHERE id = ?", (row["id"],))
        logger.info("Inbox %s resumed: pause ended.", row["email"])


def candidate_inboxes(settings: Settings, client: Any, now: datetime.datetime, conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Return inboxes allowed to send right now."""
    now_iso = to_iso(now)
    today = now.astimezone(IST).date()
    rows = conn.execute(
        """SELECT * FROM inboxes
           WHERE role = 'send' AND active = 1 AND paused_reason IS NULL
             AND (next_send_after IS NULL OR next_send_after <= ?)
           ORDER BY id""",
        (now_iso,),
    ).fetchall()

    candidates = []
    for inbox in rows:
        if not client.has_token(inbox["email"]):
            continue
        cap = todays_cap(inbox, today, settings.worker.hard_daily_cap)
        sent = sent_last_24h(inbox["id"], conn, include_dry_run=settings.mode.dry_run, now_utc=now)
        if sent < cap:
            candidates.append(inbox)
    return candidates


def _round_robin(candidates: Sequence[sqlite3.Row], conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Order inboxes so the one after the last used inbox goes first."""
    last_used = int(get_app_state(LAST_INBOX_KEY, "0", conn) or 0)
    later = [inbox for inbox in candidates if inbox["id"] > last_used]
    earlier = [inbox for inbox in candidates if inbox["id"] <= last_used]
    return later + earlier


def _recent_dry_runs(now: datetime.datetime, conn: sqlite3.Connection) -> Set[Tuple[int, int]]:
    """Return (lead_id, step_no) pairs already dry-run in the last 24 hours."""
    cutoff = to_iso(now - datetime.timedelta(hours=DRY_RUN_REPEAT_HOURS))
    rows = conn.execute(
        "SELECT lead_id, step_no FROM send_log WHERE result = 'dry_run' AND ts >= ?",
        (cutoff,),
    ).fetchall()
    return {(row["lead_id"], row["step_no"]) for row in rows}


def pick_lead(
    inbox: sqlite3.Row,
    settings: Settings,
    now: datetime.datetime,
    conn: sqlite3.Connection,
) -> Optional[Tuple[sqlite3.Row, int]]:
    """Return the next (lead, step_no) for this inbox: due follow-ups first, then new leads."""
    active_steps = [step["step_no"] for step in get_active_steps(conn)]
    if not active_steps:
        return None

    suppressed = {row["email"].lower() for row in conn.execute("SELECT email FROM suppression")}
    already_logged = _recent_dry_runs(now, conn) if settings.mode.dry_run else set()
    # Thousands of leads share a country or a state, so each pair is checked once.
    open_windows: Dict[Tuple[str, str], bool] = {}

    def can_send(lead: sqlite3.Row, step_no: int) -> bool:
        if (lead["id"], step_no) in already_logged or lead["email"].lower() in suppressed:
            return False
        place = (lead["country"], lead["location"])
        if place not in open_windows:
            open_windows[place] = in_recipient_window(lead, now, settings, conn)
        return open_windows[place]

    for lead in due_follow_ups(inbox["id"], now, conn):
        step_no = _next_step(lead["step"], active_steps)
        if step_no is not None and can_send(lead, step_no):
            return lead, step_no

    first_step = active_steps[0]
    for lead in queued_leads(conn):
        if can_send(lead, first_step):
            return lead, first_step

    return None


def _write_log(
    inbox: sqlite3.Row,
    lead: sqlite3.Row,
    step_no: int,
    subject: Optional[str],
    result: str,
    error_text: Optional[str],
    delay_seconds: int,
    now: datetime.datetime,
    conn: sqlite3.Connection,
) -> None:
    next_send_after = to_iso(now + datetime.timedelta(seconds=delay_seconds))
    with transaction(conn):
        conn.execute(
            """INSERT INTO send_log (ts, inbox_id, lead_id, lead_email, step_no, subject, result, error_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (to_iso(now), inbox["id"], lead["id"], lead["email"], step_no, subject, result, error_text),
        )
        conn.execute("UPDATE inboxes SET next_send_after = ? WHERE id = ?", (next_send_after, inbox["id"]))


def tick(
    settings: Optional[Settings] = None,
    client: Any = gmail_client,
    now: Optional[datetime.datetime] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Handle at most one email. Returns True if one was logged as sent or dry-run."""
    if conn is None:
        with open_conn() as c:
            return tick(settings, client, now, c)

    global _live_warning_logged
    cfg = settings or load_settings()
    moment = now or datetime.datetime.now(datetime.timezone.utc)

    if not cfg.mode.dry_run and not LIVE_SENDING_READY:
        if not _live_warning_logged:
            logger.warning("LIVE mode is on, but live sending is not built yet. Nothing will be sent.")
            _live_warning_logged = True
        return False

    _auto_resume(moment, conn)

    delays = cfg.worker.delay_between_sends_seconds
    for inbox in _round_robin(candidate_inboxes(cfg, client, moment, conn), conn):
        picked = pick_lead(inbox, cfg, moment, conn)
        if picked is None:
            continue

        lead, step_no = picked
        delay = random.randint(delays.min, delays.max)
        set_app_state(LAST_INBOX_KEY, str(inbox["id"]), conn)

        try:
            email = render(lead, step_no, inbox, cfg, conn)
        except TemplateError as e:
            logger.error("Cannot render step %d for %s: %s", step_no, lead["email"], e)
            _write_log(inbox, lead, step_no, None, "error", str(e), delay, moment, conn)
            return False

        _write_log(inbox, lead, step_no, email.subject, "dry_run", None, delay, moment, conn)
        logger.info("Dry run: inbox=%s lead=%s step=%d next_gap=%ds", inbox["email"], lead["email"], step_no, delay)
        logger.debug("Dry run subject: %s", email.subject)
        return True

    return False
