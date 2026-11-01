"""One worker tick: pick an inbox and a lead, render, send or log, and record it."""

from __future__ import annotations

import datetime
import logging
import random
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from mailer import gmail_client, safety
from mailer.config import Settings, live_blockers, load_settings
from mailer.db import (
    add_suppression,
    get_active_steps,
    get_app_state,
    open_conn,
    set_app_state,
    set_lead_status,
    to_iso,
    transaction,
)
from mailer.gmail_client import (
    AuthExpired,
    GmailClientError,
    PermanentSendError,
    QuotaExceeded,
    RateLimited,
    TransientError,
)
from mailer.scheduling import (
    IST,
    due_follow_ups,
    has_queued_us_leads,
    in_recipient_window,
    is_us_lead,
    non_us_allowance,
    non_us_sent_last_24h,
    queued_leads,
    reservation_is_open,
    sent_last_24h,
    todays_cap,
)
from mailer.templating import RenderedEmail, TemplateError, render

logger = logging.getLogger("engine")

LAST_INBOX_KEY = "last_inbox_id"
DRY_RUN_REPEAT_HOURS = 24
RATE_LIMIT_PAUSE_HOURS = 1
GMAIL_ERROR_PAUSE_HOURS = 1
# After the last step, a lead waits this long for a reply before it is marked finished.
FINISH_AFTER_DAYS = 4

_blockers_logged = False


def _next_step(current_step: int, active_steps: Sequence[int]) -> Optional[int]:
    """Return the next active step after current_step, or None if the sequence is over."""
    return next((step for step in active_steps if step > current_step), None)


def _test_leads_only(settings: Settings) -> bool:
    """Return True when only test leads may be emailed."""
    return not settings.mode.dry_run and settings.mode.test_leads_only


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


def _skip_suppressed(now: datetime.datetime, conn: sqlite3.Connection) -> None:
    """Mark leads on the suppression list as skipped, so they never reach the queue."""
    with transaction(conn):
        conn.execute(
            """UPDATE leads SET status = 'skipped', updated_at = ?
               WHERE status IN ('queued', 'in_sequence')
                 AND lower(email) IN (SELECT lower(email) FROM suppression)""",
            (to_iso(now),),
        )


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


def _us_only_for_now(inbox: sqlite3.Row, settings: Settings, now: datetime.datetime, conn: sqlite3.Connection) -> bool:
    """Return True if this inbox has used its non-US share while the US reservation is open."""
    if not reservation_is_open(now, settings):
        return False
    if not has_queued_us_leads(conn, _test_leads_only(settings)):
        return False

    cap = todays_cap(inbox, now.astimezone(IST).date(), settings.worker.hard_daily_cap)
    allowance = non_us_allowance(cap, settings.worker.us_reservation.percent)
    used = non_us_sent_last_24h(inbox["id"], conn, include_dry_run=settings.mode.dry_run, now_utc=now)
    return used >= allowance


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
    test_only = _test_leads_only(settings)
    # Thousands of leads share a country or a state, so each pair is checked once.
    open_windows: Dict[Tuple[str, str], bool] = {}

    def can_send(lead: sqlite3.Row, step_no: int) -> bool:
        if test_only and not lead["is_test"]:
            return False
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

    us_only = _us_only_for_now(inbox, settings, now, conn)
    first_step = active_steps[0]
    for lead in queued_leads(conn):
        if us_only and not is_us_lead(lead):
            continue
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


def _next_due_at(step_no: int, now: datetime.datetime, conn: sqlite3.Connection) -> str:
    """Return when the step after step_no is due, or when a finished sequence closes."""
    steps = {step["step_no"]: step["delay_days"] for step in get_active_steps(conn)}
    following = _next_step(step_no, sorted(steps))
    days = FINISH_AFTER_DAYS if following is None else steps[following]
    return to_iso(now + datetime.timedelta(days=days))


def _repair_already_sent(lead: sqlite3.Row, step_no: int, now: datetime.datetime, conn: sqlite3.Connection) -> bool:
    """Catch a lead whose step was sent but never recorded. Returns True if that happened."""
    row = conn.execute(
        "SELECT thread_id FROM send_log WHERE lead_id = ? AND step_no = ? AND result = 'sent'",
        (lead["id"], step_no),
    ).fetchone()
    if row is None:
        return False

    logger.warning("Step %d for %s was already sent. Not sending again.", step_no, lead["email"])
    with transaction(conn):
        conn.execute(
            """UPDATE leads SET step = ?, status = 'in_sequence', thread_id = COALESCE(thread_id, ?),
                                next_due_at = ?, updated_at = ? WHERE id = ?""",
            (step_no, row["thread_id"], _next_due_at(step_no, now, conn), to_iso(now), lead["id"]),
        )
    return True


def _record_send(
    inbox: sqlite3.Row,
    lead: sqlite3.Row,
    step_no: int,
    email: RenderedEmail,
    result: gmail_client.SentResult,
    delay_seconds: int,
    now: datetime.datetime,
    conn: sqlite3.Connection,
) -> None:
    """Write the log row, the lead and the inbox in one transaction."""
    now_iso = to_iso(now)
    with transaction(conn):
        conn.execute(
            """INSERT INTO send_log (ts, inbox_id, lead_id, lead_email, step_no, subject, gmail_message_id, thread_id, result)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sent')""",
            (now_iso, inbox["id"], lead["id"], lead["email"], step_no, email.subject, result.gmail_message_id, result.thread_id),
        )
        conn.execute(
            """UPDATE leads SET step = ?, status = 'in_sequence', inbox_id = ?, thread_id = ?,
                                first_subject = COALESCE(first_subject, ?), last_message_id = ?,
                                last_sent_at = ?, next_due_at = ?, updated_at = ?
               WHERE id = ?""",
            (
                step_no,
                inbox["id"],
                result.thread_id,
                email.subject,
                result.rfc_message_id or lead["last_message_id"],
                now_iso,
                _next_due_at(step_no, now, conn),
                now_iso,
                lead["id"],
            ),
        )
        conn.execute(
            "UPDATE inboxes SET next_send_after = ? WHERE id = ?",
            (to_iso(now + datetime.timedelta(seconds=delay_seconds)), inbox["id"]),
        )


def _handle_send_error(
    error: Exception,
    inbox: sqlite3.Row,
    lead: sqlite3.Row,
    step_no: int,
    subject: str,
    delay_seconds: int,
    settings: Settings,
    now: datetime.datetime,
    conn: sqlite3.Connection,
) -> None:
    """Log a failed send and react. Nothing here may stop the worker."""
    logger.error("Send failed: inbox=%s lead=%s step=%d: %s", inbox["email"], lead["email"], step_no, error)
    _write_log(inbox, lead, step_no, subject, "error", str(error), delay_seconds, now, conn)

    if isinstance(error, RateLimited):
        until = now + datetime.timedelta(hours=RATE_LIMIT_PAUSE_HOURS)
        safety.pause_inbox(inbox["id"], "Rate limited by Gmail", conn, until)
    elif isinstance(error, QuotaExceeded):
        until = now + datetime.timedelta(hours=settings.safety.pause_on_quota_hours)
        safety.pause_inbox(inbox["id"], "Gmail sending quota reached", conn, until)
    elif isinstance(error, AuthExpired):
        safety.pause_inbox(inbox["id"], "Reconnect inbox", conn)
    elif isinstance(error, PermanentSendError):
        add_suppression(lead["email"], "bounced", conn)
        set_lead_status(lead["id"], "bounced", conn)
    elif isinstance(error, TransientError):
        pass  # The gap above spaces out the next try.
    elif isinstance(error, GmailClientError):
        # Gmail refused for a reason that is not about this lead, so more sends would fail too.
        until = now + datetime.timedelta(hours=GMAIL_ERROR_PAUSE_HOURS)
        safety.pause_inbox(inbox["id"], f"Gmail error: {error}"[:200], conn, until)
    else:
        set_lead_status(lead["id"], "skipped", conn)  # The address cannot be written into an email.


def _live_send(
    inbox: sqlite3.Row,
    lead: sqlite3.Row,
    step_no: int,
    email: RenderedEmail,
    delay_seconds: int,
    settings: Settings,
    client: Any,
    now: datetime.datetime,
    conn: sqlite3.Connection,
) -> bool:
    """Send one real email and record it. Returns True if it was sent."""
    if _test_leads_only(settings) and not lead["is_test"]:
        logger.error("Blocked a send to %s: not a test lead.", lead["email"])
        return False
    if _repair_already_sent(lead, step_no, now, conn):
        return False

    is_follow_up = step_no > 1 and lead["thread_id"]
    try:
        result = client.send_message(
            email_addr=inbox["email"],
            to=lead["email"],
            subject=email.subject,
            body=email.body,
            display_name=inbox["display_name"],
            thread_id=lead["thread_id"] if is_follow_up else None,
            in_reply_to=lead["last_message_id"] if is_follow_up else None,
            list_unsubscribe=settings.sender.list_unsubscribe_address,
        )
    except (GmailClientError, ValueError) as e:
        _handle_send_error(e, inbox, lead, step_no, email.subject, delay_seconds, settings, now, conn)
        return False

    _record_send(inbox, lead, step_no, email, result, delay_seconds, now, conn)
    logger.info("Sent: inbox=%s lead=%s step=%d", inbox["email"], lead["email"], step_no)
    return True


def tick(
    settings: Optional[Settings] = None,
    client: Any = gmail_client,
    now: Optional[datetime.datetime] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Handle at most one email. Returns True if one was sent or logged as a dry run."""
    if conn is None:
        with open_conn() as c:
            return tick(settings, client, now, c)

    global _blockers_logged
    cfg = settings or load_settings()
    moment = now or datetime.datetime.now(datetime.timezone.utc)

    if not cfg.mode.dry_run:
        blockers = live_blockers(cfg)
        if blockers:
            if not _blockers_logged:
                logger.error("LIVE mode refused: %s", " ".join(blockers))
                _blockers_logged = True
            return False
        _skip_suppressed(moment, conn)

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

        if not cfg.mode.dry_run:
            return _live_send(inbox, lead, step_no, email, delay, cfg, client, moment, conn)

        _write_log(inbox, lead, step_no, email.subject, "dry_run", None, delay, moment, conn)
        logger.info("Dry run: inbox=%s lead=%s step=%d next_gap=%ds", inbox["email"], lead["email"], step_no, delay)
        logger.debug("Dry run subject: %s", email.subject)
        return True

    return False
