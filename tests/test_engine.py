"""Tests for one worker tick, using FakeGmailClient. Live sending arrives in Milestone 5."""

import datetime

import pytest

from mailer import engine
from mailer.config import Settings
from mailer.db import add_suppression, open_conn, set_app_state, to_iso
from tests.fakes import FakeGmailClient

# Wednesday: 10:00 New York, 07:00 Los Angeles, 15:00 London, 16:00 Paris, 02:00 Sydney.
NOW = datetime.datetime(2026, 11, 4, 15, 0, tzinfo=datetime.timezone.utc)
SATURDAY = datetime.datetime(2026, 11, 7, 15, 0, tzinfo=datetime.timezone.utc)
STAMP = "2026-11-01T00:00:00Z"


@pytest.fixture
def settings():
    cfg = Settings()
    cfg.sender.business_address = "Jaipur"
    cfg.sender.website = "mobisa.in"
    cfg.sender.portfolio_link = "https://mobisa.in"
    cfg.worker.delay_between_sends_seconds.min = 90
    cfg.worker.delay_between_sends_seconds.max = 240
    return cfg


@pytest.fixture
def client():
    return FakeGmailClient()


def add_inbox(client, email="beedu@mobisa.in", connect=True, **overrides):
    values = {
        "email": email,
        "display_name": "Beedu",
        "role": "send",
        "active": 1,
        "ramp_start_date": "2026-11-04",
        "ramp_start_cap": 100,
        "max_cap": 100,
        "created_at": STAMP,
        **overrides,
    }
    with open_conn() as conn:
        cursor = conn.execute(
            f"INSERT INTO inboxes ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
            list(values.values()),
        )
        inbox_id = cursor.lastrowid
    if connect:
        client.connect_inbox(email)
    return inbox_id


def add_lead(email, country="United Kingdom", **overrides):
    values = {
        "email": email,
        "business_name": "Acme",
        "category": "skincare",
        "country": country,
        "language": "fr" if country == "France" else "en",
        "created_at": STAMP,
        "updated_at": STAMP,
        **overrides,
    }
    with open_conn() as conn:
        cursor = conn.execute(
            f"INSERT INTO leads ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
            list(values.values()),
        )
        return cursor.lastrowid


def run_tick(settings, client, now=NOW):
    return engine.tick(settings, client, now)


def clear_gap():
    with open_conn() as conn:
        conn.execute("UPDATE inboxes SET next_send_after = NULL")


def log_rows():
    with open_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM send_log ORDER BY id")]


def logged_emails():
    return [row["lead_email"] for row in log_rows()]


def lead_row(lead_id):
    with open_conn() as conn:
        return dict(conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())


def test_dry_run_changes_nothing_on_leads(settings, client):
    add_inbox(client)
    lead_id = add_lead("a@acme.com")
    before = lead_row(lead_id)

    assert run_tick(settings, client) is True

    assert lead_row(lead_id) == before
    assert client.sent_messages == []
    assert client.send_attempts == 0


def test_dry_run_writes_a_log_row_with_the_rendered_subject(settings, client):
    inbox_id = add_inbox(client)
    lead_id = add_lead("a@acme.com")

    run_tick(settings, client)

    row = log_rows()[0]
    assert row["result"] == "dry_run"
    assert row["inbox_id"] == inbox_id
    assert row["lead_id"] == lead_id
    assert row["lead_email"] == "a@acme.com"
    assert row["step_no"] == 1
    assert row["subject"].endswith("for Acme")
    assert row["ts"] == to_iso(NOW)


def test_at_most_one_email_per_tick(settings, client):
    add_inbox(client)
    add_inbox(client, "second@mobisa.in")
    add_lead("a@acme.com")
    add_lead("b@acme.com")

    run_tick(settings, client)

    assert len(log_rows()) == 1


def test_dry_run_uses_the_random_delay(settings, client):
    add_inbox(client)
    add_lead("a@acme.com")
    gaps = set()

    for i in range(30):
        add_lead(f"lead{i}@acme.com")
        clear_gap()
        run_tick(settings, client)
        with open_conn() as conn:
            next_send = conn.execute("SELECT next_send_after FROM inboxes").fetchone()[0]
        gap = datetime.datetime.fromisoformat(next_send.replace("Z", "+00:00")) - NOW
        gaps.add(gap.total_seconds())

    assert all(90 <= gap <= 240 for gap in gaps)
    assert len(gaps) > 5


def test_inbox_waits_for_its_gap(settings, client):
    add_inbox(client)
    add_lead("a@acme.com")
    add_lead("b@acme.com")

    assert run_tick(settings, client) is True
    assert run_tick(settings, client) is False
    assert run_tick(settings, client, NOW + datetime.timedelta(seconds=241)) is True
    assert len(log_rows()) == 2


def test_dry_run_rows_count_toward_the_cap(settings, client):
    add_inbox(client, ramp_start_cap=2, max_cap=2)
    for i in range(5):
        add_lead(f"lead{i}@acme.com")

    assert run_tick(settings, client) is True
    clear_gap()
    assert run_tick(settings, client) is True
    clear_gap()
    assert run_tick(settings, client) is False
    assert len(log_rows()) == 2


def test_cap_is_a_rolling_24_hours(settings, client):
    add_inbox(client, ramp_start_cap=1, max_cap=1)
    add_lead("a@acme.com")
    add_lead("b@acme.com")

    run_tick(settings, client)
    clear_gap()

    assert run_tick(settings, client, NOW + datetime.timedelta(hours=23)) is False
    assert run_tick(settings, client, NOW + datetime.timedelta(hours=24, minutes=1)) is True


def test_hard_cap_overrides_ramp(settings, client):
    settings.worker.hard_daily_cap = 1
    add_inbox(client)
    add_lead("a@acme.com")
    add_lead("b@acme.com")

    run_tick(settings, client)
    clear_gap()

    assert run_tick(settings, client) is False


def test_cap_reached_inbox_is_skipped_for_the_next_one(settings, client):
    add_inbox(client, "first@mobisa.in", ramp_start_cap=1, max_cap=1)
    second_id = add_inbox(client, "second@mobisa.in")
    add_lead("a@acme.com")
    add_lead("b@acme.com")
    add_lead("c@acme.com")

    for _ in range(3):
        run_tick(settings, client)
        clear_gap()

    assert [row["inbox_id"] for row in log_rows()][1:] == [second_id, second_id]


def test_lead_and_step_are_skipped_for_24_hours_after_a_dry_run(settings, client):
    add_inbox(client)
    add_lead("a@acme.com")
    add_lead("b@acme.com")

    run_tick(settings, client)
    clear_gap()
    run_tick(settings, client)
    clear_gap()

    assert logged_emails() == ["a@acme.com", "b@acme.com"]
    assert run_tick(settings, client) is False

    assert run_tick(settings, client, NOW + datetime.timedelta(hours=24, minutes=1)) is True
    assert logged_emails()[-1] == "a@acme.com"


def test_inboxes_rotate(settings, client):
    first_id = add_inbox(client, "first@mobisa.in")
    second_id = add_inbox(client, "second@mobisa.in")
    third_id = add_inbox(client, "third@mobisa.in")
    for i in range(7):
        add_lead(f"lead{i}@acme.com")

    for _ in range(7):
        run_tick(settings, client)
        clear_gap()

    assert [row["inbox_id"] for row in log_rows()] == [
        first_id, second_id, third_id, first_id, second_id, third_id, first_id,
    ]


def test_rotation_continues_after_last_used_inbox(settings, client):
    add_inbox(client, "first@mobisa.in")
    second_id = add_inbox(client, "second@mobisa.in")
    with open_conn() as conn:
        set_app_state(engine.LAST_INBOX_KEY, "1", conn)
    add_lead("a@acme.com")

    run_tick(settings, client)

    assert log_rows()[0]["inbox_id"] == second_id


@pytest.mark.parametrize(
    "overrides, connect",
    [
        ({"role": "monitor"}, True),
        ({"active": 0}, True),
        ({"paused_reason": "Manually paused"}, True),
        ({"next_send_after": "2026-11-04T15:01:00Z"}, True),
        ({}, False),
    ],
)
def test_inbox_that_may_not_send_is_skipped(settings, client, overrides, connect):
    add_inbox(client, connect=connect, **overrides)
    add_lead("a@acme.com")

    assert run_tick(settings, client) is False
    assert log_rows() == []


def test_timed_pause_ends_and_clears_the_reason(settings, client):
    add_inbox(client, paused_reason="Rate limited", paused_until="2026-11-04T14:59:00Z")
    add_lead("a@acme.com")

    assert run_tick(settings, client) is True

    with open_conn() as conn:
        inbox = conn.execute("SELECT paused_reason, paused_until FROM inboxes").fetchone()
    assert tuple(inbox) == (None, None)


def test_timed_pause_still_running_blocks_sending(settings, client):
    add_inbox(client, paused_reason="Rate limited", paused_until="2026-11-04T15:30:00Z")
    add_lead("a@acme.com")

    assert run_tick(settings, client) is False

    with open_conn() as conn:
        assert conn.execute("SELECT paused_reason FROM inboxes").fetchone()[0] == "Rate limited"


def test_queue_order_country_priority_quality_id(settings, client):
    add_inbox(client)
    add_lead("france@acme.com", country="France", priority=1, quality="Good")
    add_lead("uk-weak@acme.com", priority=1, quality="Weak")
    add_lead("uk-p2@acme.com", priority=2, quality="Good")
    add_lead("uk-good-1@acme.com", priority=1, quality="Good")
    add_lead("uk-good-2@acme.com", priority=1, quality="Good")
    add_lead("us@acme.com", country="United States", location="New York, United States", priority=5, quality="Weak")
    add_lead("unknown@acme.com", country="Atlantis", priority=1, quality="Good")

    for _ in range(7):
        run_tick(settings, client)
        clear_gap()

    assert logged_emails() == [
        "us@acme.com",
        "uk-good-1@acme.com",
        "uk-good-2@acme.com",
        "uk-weak@acme.com",
        "uk-p2@acme.com",
        "france@acme.com",
        "unknown@acme.com",
    ]


def test_follow_ups_take_priority_over_new_leads(settings, client):
    inbox_id = add_inbox(client)
    add_lead("new@acme.com", country="United States", location="New York, United States", priority=1)
    add_lead(
        "due@acme.com", country="France", status="in_sequence", step=1, inbox_id=inbox_id,
        first_subject="idea for Acme", next_due_at="2026-11-04T14:00:00Z",
    )

    run_tick(settings, client)

    row = log_rows()[0]
    assert (row["lead_email"], row["step_no"]) == ("due@acme.com", 2)
    assert row["subject"] == "Re: idea for Acme"


def test_follow_up_not_due_yet_waits(settings, client):
    inbox_id = add_inbox(client)
    add_lead("later@acme.com", status="in_sequence", step=1, inbox_id=inbox_id, next_due_at="2026-11-04T16:00:00Z")

    assert run_tick(settings, client) is False


def test_follow_up_goes_only_through_its_own_inbox(settings, client):
    first_id = add_inbox(client, "first@mobisa.in")
    add_inbox(client, "second@mobisa.in")
    with open_conn() as conn:
        set_app_state(engine.LAST_INBOX_KEY, str(first_id), conn)
    add_lead("due@acme.com", status="in_sequence", step=1, inbox_id=first_id, next_due_at="2026-11-04T14:00:00Z")

    run_tick(settings, client)

    assert log_rows()[0]["inbox_id"] == first_id


def test_last_step_sent_gets_no_more_emails(settings, client):
    inbox_id = add_inbox(client)
    add_lead("done@acme.com", status="in_sequence", step=3, inbox_id=inbox_id, next_due_at="2026-11-04T14:00:00Z")

    assert run_tick(settings, client) is False


def test_inactive_step_is_jumped_over(settings, client):
    inbox_id = add_inbox(client)
    with open_conn() as conn:
        conn.execute("UPDATE steps SET active = 0 WHERE step_no = 2")
    add_lead("due@acme.com", status="in_sequence", step=1, inbox_id=inbox_id, next_due_at="2026-11-04T14:00:00Z")

    run_tick(settings, client)

    assert log_rows()[0]["step_no"] == 3


@pytest.mark.parametrize("status", ["replied", "unsubscribed", "bounced", "finished", "skipped"])
def test_stopped_lead_gets_no_more_emails(settings, client, status):
    inbox_id = add_inbox(client)
    add_lead("stopped@acme.com", status=status, step=1, inbox_id=inbox_id, next_due_at="2026-11-04T14:00:00Z")

    assert run_tick(settings, client) is False


def test_suppressed_lead_is_never_picked_and_stays_unchanged_in_dry_run(settings, client):
    add_inbox(client)
    lead_id = add_lead("gone@acme.com")
    before = lead_row(lead_id)
    add_suppression("GONE@acme.com", "unsubscribed")

    assert run_tick(settings, client) is False
    assert lead_row(lead_id) == before


def test_country_skipped_after_import_is_not_sent(settings, client):
    add_inbox(client)
    add_lead("a@acme.com", country="Germany")

    assert run_tick(settings, client) is False


def test_lead_outside_its_window_is_passed_over(settings, client):
    add_inbox(client)
    add_lead("la@acme.com", country="United States", location="California, United States")
    add_lead("ny@acme.com", country="United States", location="New York, United States")

    run_tick(settings, client)

    assert logged_emails() == ["ny@acme.com"]


def test_sydney_is_outside_the_window(settings, client):
    add_inbox(client)
    add_lead("au@acme.com", country="Australia")

    assert run_tick(settings, client) is False


def test_weekend_sends_nothing(settings, client):
    add_inbox(client, ramp_start_date="2026-11-01")
    add_lead("a@acme.com")

    assert run_tick(settings, client, SATURDAY) is False


def test_weekend_sends_when_weekdays_only_is_off(settings, client):
    settings.worker.recipient_window.weekdays_only = False
    add_inbox(client, ramp_start_date="2026-11-01")
    add_lead("a@acme.com")

    assert run_tick(settings, client, SATURDAY) is True


def test_no_inbox_means_no_work(settings, client):
    add_lead("a@acme.com")

    assert run_tick(settings, client) is False


def test_no_active_steps_means_no_work(settings, client):
    add_inbox(client)
    add_lead("a@acme.com")
    with open_conn() as conn:
        conn.execute("UPDATE steps SET active = 0")

    assert run_tick(settings, client) is False


def test_render_failure_logs_an_error_and_keeps_going(settings, client):
    add_inbox(client)
    add_lead("a@acme.com")
    with open_conn() as conn:
        conn.execute("UPDATE templates SET body = 'Hi {{nope}}' WHERE step_no = 1")

    assert run_tick(settings, client) is False

    row = log_rows()[0]
    assert row["result"] == "error"
    assert "nope" in row["error_text"]


def test_live_mode_sends_nothing_until_milestone_5(settings, client):
    settings.mode.dry_run = False
    add_inbox(client)
    add_lead("a@acme.com")

    assert run_tick(settings, client) is False
    assert log_rows() == []
    assert client.sent_messages == []
