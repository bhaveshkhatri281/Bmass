"""Tests for pausing inboxes, the bounce rule, and the kill switch."""

import datetime

from mailer.config import Settings
from mailer.db import open_conn, to_iso
from mailer.safety import (
    evaluate_all_inboxes,
    is_paused_all,
    pause_inbox,
    recent_bounces,
    resume_inbox,
    set_paused_all,
)

NOW = datetime.datetime(2026, 11, 4, 15, 0, tzinfo=datetime.timezone.utc)


def add_inbox(conn, inbox_id=1, role="send"):
    conn.execute(
        "INSERT INTO inboxes (id, email, display_name, role, ramp_start_date, created_at) VALUES (?, ?, 'B', ?, '2026-11-01', 'x')",
        (inbox_id, f"inbox{inbox_id}@mobisa.in", role),
    )


def add_lead(conn, lead_id):
    conn.execute(
        "INSERT INTO leads (id, email, created_at, updated_at) VALUES (?, ?, 'x', 'x')",
        (lead_id, f"lead{lead_id}@acme.com"),
    )


def add_sends(conn, count, start=NOW, inbox_id=1):
    for i in range(count):
        ts = to_iso(start + datetime.timedelta(minutes=i))
        conn.execute("INSERT INTO send_log (ts, inbox_id, lead_email, step_no, result) VALUES (?, ?, 'x@y.z', 1, 'sent')", (ts, inbox_id))


def add_bounces(conn, count, when, inbox_id=1, with_lead=True):
    for i in range(count):
        lead_id = (inbox_id * 1000 + i) if with_lead else None
        if with_lead:
            existing = conn.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if not existing:
                add_lead(conn, lead_id)
        conn.execute(
            "INSERT INTO events (ts, inbox_id, lead_id, type, gmail_message_id) VALUES (?, ?, ?, 'bounce', ?)",
            (to_iso(when), inbox_id, lead_id, f"m{inbox_id}{i}{when.timestamp()}"),
        )


def reason(conn, inbox_id=1):
    return conn.execute("SELECT paused_reason FROM inboxes WHERE id = ?", (inbox_id,)).fetchone()[0]


def test_three_bounces_in_the_window_pause_the_inbox():
    with open_conn() as conn:
        add_inbox(conn)
        add_sends(conn, 10)
        add_bounces(conn, 3, NOW + datetime.timedelta(minutes=20))

        assert evaluate_all_inboxes(Settings(), conn) == ["inbox1@mobisa.in"]
        assert "Too many bounces (3 in the last 50 sends)" == reason(conn)


def test_two_bounces_do_not_pause():
    with open_conn() as conn:
        add_inbox(conn)
        add_sends(conn, 10)
        add_bounces(conn, 2, NOW + datetime.timedelta(minutes=20))

        assert evaluate_all_inboxes(Settings(), conn) == []
        assert reason(conn) is None


def test_old_bounces_outside_the_last_sends_do_not_count():
    with open_conn() as conn:
        add_inbox(conn)
        add_sends(conn, 60)
        add_bounces(conn, 3, NOW + datetime.timedelta(minutes=5))

        assert recent_bounces(1, 50, conn) == 0


def test_bounces_without_a_lead_do_not_count():
    with open_conn() as conn:
        add_inbox(conn)
        add_sends(conn, 5)
        add_bounces(conn, 3, NOW + datetime.timedelta(minutes=20), with_lead=False)

        assert recent_bounces(1, 50, conn) == 0


def test_no_sends_means_no_pause():
    with open_conn() as conn:
        add_inbox(conn)
        add_bounces(conn, 3, NOW)

        assert evaluate_all_inboxes(Settings(), conn) == []


def test_other_inboxes_are_not_paused():
    with open_conn() as conn:
        add_inbox(conn, 1)
        add_inbox(conn, 2)
        add_sends(conn, 5, inbox_id=1)
        add_sends(conn, 5, inbox_id=2)
        add_bounces(conn, 3, NOW + datetime.timedelta(minutes=20), inbox_id=1)

        evaluate_all_inboxes(Settings(), conn)

        assert reason(conn, 2) is None


def test_resume_forgives_old_bounces_so_it_does_not_pause_again():
    with open_conn() as conn:
        add_inbox(conn)
        add_sends(conn, 5)
        add_bounces(conn, 3, NOW + datetime.timedelta(minutes=20))
        evaluate_all_inboxes(Settings(), conn)

        resume_inbox(1, conn, NOW + datetime.timedelta(minutes=30))

        assert evaluate_all_inboxes(Settings(), conn) == []
        assert reason(conn) is None


def test_new_bounces_after_resume_pause_again():
    with open_conn() as conn:
        add_inbox(conn)
        add_sends(conn, 5)
        resume_inbox(1, conn, NOW + datetime.timedelta(minutes=30))
        add_bounces(conn, 3, NOW + datetime.timedelta(minutes=40))

        assert evaluate_all_inboxes(Settings(), conn) == ["inbox1@mobisa.in"]


def test_pause_with_and_without_an_end_time():
    with open_conn() as conn:
        add_inbox(conn)

        pause_inbox(1, "Rate limited", conn, NOW)
        assert conn.execute("SELECT paused_until FROM inboxes").fetchone()[0] == to_iso(NOW)

        pause_inbox(1, "Reconnect inbox", conn)
        assert conn.execute("SELECT paused_until FROM inboxes").fetchone()[0] is None


def test_kill_switch():
    with open_conn() as conn:
        assert is_paused_all(conn) is False
        set_paused_all(True, conn)
        assert is_paused_all(conn) is True
        set_paused_all(False, conn)
        assert is_paused_all(conn) is False
