"""Safety rules: pausing and resuming inboxes, bounce auto-pause, and the global kill switch."""

from __future__ import annotations

import datetime
import logging
import sqlite3
from typing import List, Optional

from mailer.config import Settings
from mailer.db import get_app_state, set_app_state, to_iso, transaction

logger = logging.getLogger("safety")

PAUSED_ALL_KEY = "paused_all"


def _bounce_reset_key(inbox_id: int) -> str:
    return f"bounce_reset_{inbox_id}"


def is_paused_all(conn: sqlite3.Connection) -> bool:
    """Return True while the global kill switch is on."""
    return get_app_state(PAUSED_ALL_KEY, "0", conn) == "1"


def set_paused_all(paused: bool, conn: sqlite3.Connection) -> None:
    """Turn the global kill switch on or off."""
    set_app_state(PAUSED_ALL_KEY, "1" if paused else "0", conn)
    logger.info("Global pause %s.", "on" if paused else "off")


def pause_inbox(
    inbox_id: int,
    reason: str,
    conn: sqlite3.Connection,
    until: Optional[datetime.datetime] = None,
) -> None:
    """Pause an inbox, until a time or until someone resumes it."""
    paused_until = to_iso(until) if until else None
    with transaction(conn):
        conn.execute(
            "UPDATE inboxes SET paused_reason = ?, paused_until = ? WHERE id = ?",
            (reason, paused_until, inbox_id),
        )
    logger.warning("Inbox %s paused: %s (until %s).", inbox_id, reason, paused_until or "resumed by hand")


def resume_inbox(inbox_id: int, conn: sqlite3.Connection, now: Optional[datetime.datetime] = None) -> None:
    """Resume an inbox. Bounces before this moment no longer count, or it would pause again at once."""
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    with transaction(conn):
        conn.execute("UPDATE inboxes SET paused_reason = NULL, paused_until = NULL WHERE id = ?", (inbox_id,))
    set_app_state(_bounce_reset_key(inbox_id), to_iso(moment), conn)
    logger.info("Inbox %s resumed by hand.", inbox_id)


def recent_bounces(inbox_id: int, window: int, conn: sqlite3.Connection) -> int:
    """Count bounces of leads since the inbox's last `window` sends began."""
    sends = conn.execute(
        "SELECT ts FROM send_log WHERE inbox_id = ? AND result = 'sent' ORDER BY id DESC LIMIT ?",
        (inbox_id, window),
    ).fetchall()
    if not sends:
        return 0

    since = max(sends[-1]["ts"], get_app_state(_bounce_reset_key(inbox_id), "", conn) or "")
    row = conn.execute(
        """SELECT COUNT(*) FROM events
           WHERE inbox_id = ? AND type = 'bounce' AND lead_id IS NOT NULL AND ts >= ?""",
        (inbox_id, since),
    ).fetchone()
    return int(row[0])


def evaluate_all_inboxes(settings: Settings, conn: sqlite3.Connection) -> List[str]:
    """Pause inboxes with too many recent bounces and return their addresses."""
    threshold = settings.safety.bounce_pause_threshold
    window = settings.safety.bounce_pause_window
    paused: List[str] = []

    for inbox in conn.execute("SELECT id, email FROM inboxes WHERE role = 'send' AND paused_reason IS NULL").fetchall():
        bounces = recent_bounces(inbox["id"], window, conn)
        if bounces >= threshold:
            pause_inbox(inbox["id"], f"Too many bounces ({bounces} in the last {window} sends)", conn)
            paused.append(inbox["email"])

    return paused
