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


# ------------------------------------------------------------------------------
# US reservation
# ------------------------------------------------------------------------------
# 15:00 UTC is 20:30 IST: Toronto is open, Honolulu is closed, and the reservation holds.
# 17:30 UTC is 23:00 IST: Toronto is still open, Honolulu still closed, reservation over.
AFTER_RESERVATION = datetime.datetime(2026, 11, 4, 17, 30, tzinfo=datetime.timezone.utc)


def add_held_us_lead():
    return add_lead("hawaii@acme.com", country="United States", location="Hawaii, United States")


def add_canada_leads(count):
    for i in range(count):
        add_lead(f"ca{i}@acme.com", country="Canada")


def tick_repeatedly(settings, client, count, now=NOW):
    results = []
    for _ in range(count):
        results.append(run_tick(settings, client, now))
        clear_gap()
    return results


def test_reservation_holds_back_half_the_cap(settings, client):
    add_inbox(client, ramp_start_cap=10, max_cap=10)
    add_held_us_lead()
    add_canada_leads(8)

    assert tick_repeatedly(settings, client, 7) == [True] * 5 + [False] * 2


def test_reserved_share_opens_after_the_cutoff(settings, client):
    add_inbox(client, ramp_start_cap=10, max_cap=10)
    add_held_us_lead()
    add_canada_leads(12)

    tick_repeatedly(settings, client, 6)
    assert len(log_rows()) == 5

    assert tick_repeatedly(settings, client, 6, AFTER_RESERVATION) == [True] * 5 + [False]
    assert len(log_rows()) == 10


def test_no_us_leads_queued_means_no_reservation(settings, client):
    add_inbox(client, ramp_start_cap=10, max_cap=10)
    add_canada_leads(12)

    assert tick_repeatedly(settings, client, 11) == [True] * 10 + [False]


def test_us_leads_that_are_no_longer_queued_do_not_reserve(settings, client):
    add_inbox(client, ramp_start_cap=10, max_cap=10)
    add_lead("gone@acme.com", country="United States", location="Hawaii, United States", status="replied")
    add_canada_leads(7)

    assert all(tick_repeatedly(settings, client, 7))


def test_us_leads_can_use_the_whole_cap(settings, client):
    add_inbox(client, ramp_start_cap=4, max_cap=4)
    for i in range(6):
        add_lead(f"ny{i}@acme.com", country="United States", location="New York, United States")

    assert tick_repeatedly(settings, client, 5) == [True] * 4 + [False]


def test_reserved_share_goes_to_us_leads_when_they_open(settings, client):
    add_inbox(client, ramp_start_cap=10, max_cap=10)
    add_held_us_lead()
    for i in range(5):
        add_lead(f"ny{i}@acme.com", country="United States", location="New York, United States")
    add_canada_leads(8)
    with open_conn() as conn:
        conn.execute("UPDATE country_settings SET sort_order = 0 WHERE country = 'Canada'")

    tick_repeatedly(settings, client, 12)

    emails = logged_emails()
    assert len(emails) == 10
    assert all(e.startswith("ca") for e in emails[:5])
    assert all(e.startswith("ny") for e in emails[5:])


def test_reservation_percent_is_editable(settings, client):
    settings.worker.us_reservation.percent = 80
    add_inbox(client, ramp_start_cap=10, max_cap=10)
    add_held_us_lead()
    add_canada_leads(4)

    assert tick_repeatedly(settings, client, 3) == [True, True, False]


def test_reservation_cutoff_is_editable(settings, client):
    settings.worker.us_reservation.until_ist = "20:00"  # already over at 20:30 IST
    add_inbox(client, ramp_start_cap=4, max_cap=4)
    add_held_us_lead()
    add_canada_leads(5)

    assert tick_repeatedly(settings, client, 5) == [True] * 4 + [False]


def test_zero_percent_turns_the_reservation_off(settings, client):
    settings.worker.us_reservation.percent = 0
    add_inbox(client, ramp_start_cap=4, max_cap=4)
    add_held_us_lead()
    add_canada_leads(5)

    assert tick_repeatedly(settings, client, 5) == [True] * 4 + [False]


def test_reservation_does_not_hold_back_follow_ups(settings, client):
    inbox_id = add_inbox(client, ramp_start_cap=4, max_cap=4)
    add_held_us_lead()
    add_canada_leads(2)
    tick_repeatedly(settings, client, 2)
    add_lead("due@acme.com", country="Canada", status="in_sequence", step=1, inbox_id=inbox_id, next_due_at="2026-11-04T14:00:00Z")

    assert run_tick(settings, client) is True
    assert logged_emails()[-1] == "due@acme.com"


def test_reservation_applies_per_inbox(settings, client):
    add_inbox(client, "first@mobisa.in", ramp_start_cap=2, max_cap=2)
    add_inbox(client, "second@mobisa.in", ramp_start_cap=2, max_cap=2)
    add_held_us_lead()
    add_canada_leads(4)

    assert tick_repeatedly(settings, client, 3) == [True, True, False]


# ------------------------------------------------------------------------------
# Live sending
# ------------------------------------------------------------------------------

@pytest.fixture
def live(settings):
    settings.mode.dry_run = False
    return settings


def add_test_lead(email="me+a@example.com", **overrides):
    return add_lead(email, is_test=1, **overrides)


def test_real_leads_are_never_emailed_while_the_lock_is_on(live, client):
    add_inbox(client)
    lead_id = add_lead("real@acme.com")
    before = lead_row(lead_id)

    assert run_tick(live, client) is False

    assert client.sent_messages == []
    assert log_rows() == []
    assert lead_row(lead_id) == before


def test_lock_passes_over_real_leads_to_reach_test_leads(live, client):
    add_inbox(client)
    add_lead("real@acme.com", country="United States", location="New York, United States", priority=1)
    add_test_lead(country="France")

    assert run_tick(live, client) is True

    assert [m["to"] for m in client.sent_messages] == ["me+a@example.com"]


def test_lock_blocks_a_real_follow_up_too(live, client):
    inbox_id = add_inbox(client)
    add_lead("real@acme.com", status="in_sequence", step=1, inbox_id=inbox_id, thread_id="t", next_due_at="2026-11-04T14:00:00Z")

    assert run_tick(live, client) is False
    assert client.sent_messages == []


def test_real_leads_send_only_after_the_lock_is_turned_off(live, client):
    live.mode.test_leads_only = False
    add_inbox(client)
    add_lead("real@acme.com")

    assert run_tick(live, client) is True
    assert [m["to"] for m in client.sent_messages] == ["real@acme.com"]


def test_dry_run_never_sends_even_for_test_leads(settings, client):
    add_inbox(client)
    add_test_lead()

    assert run_tick(settings, client) is True
    assert client.sent_messages == []


@pytest.mark.parametrize("blank", ["business_address", "portfolio_link", "website"])
def test_live_refuses_with_missing_settings(live, client, blank):
    setattr(live.sender, blank, "")
    add_inbox(client)
    add_test_lead()

    assert run_tick(live, client) is False
    assert client.sent_messages == []


def test_live_send_sets_thread_and_next_due(live, client):
    inbox_id = add_inbox(client)
    lead_id = add_test_lead()

    assert run_tick(live, client) is True

    sent = client.sent_messages[0]
    lead = lead_row(lead_id)
    assert lead["status"] == "in_sequence"
    assert lead["step"] == 1
    assert lead["inbox_id"] == inbox_id
    assert lead["thread_id"] == sent["thread_id"]
    assert lead["last_message_id"] == sent["rfc_message_id"]
    assert lead["first_subject"] == sent["subject"]
    assert lead["last_sent_at"] == to_iso(NOW)
    assert lead["next_due_at"] == to_iso(NOW + datetime.timedelta(days=3))

    row = log_rows()[0]
    assert row["result"] == "sent"
    assert row["gmail_message_id"] == sent["gmail_message_id"]
    assert row["thread_id"] == sent["thread_id"]


def test_live_send_passes_sender_and_unsubscribe(live, client):
    add_inbox(client)
    add_test_lead()

    run_tick(live, client)

    sent = client.sent_messages[0]
    assert sent["from"] == "beedu@mobisa.in"
    assert sent["display_name"] == "Beedu"
    assert sent["list_unsubscribe"] == "unsubscribe@mobisa.in"
    assert sent["in_reply_to"] is None


def test_gap_is_random_after_a_live_send(live, client):
    add_inbox(client)
    add_test_lead()

    run_tick(live, client)

    with open_conn() as conn:
        next_send = conn.execute("SELECT next_send_after FROM inboxes").fetchone()[0]
    gap = datetime.datetime.fromisoformat(next_send.replace("Z", "+00:00")) - NOW
    assert 90 <= gap.total_seconds() <= 240


def advance_to_follow_up(lead_id, hours=73):
    return NOW + datetime.timedelta(hours=hours)


def test_follow_up_uses_the_same_thread_and_in_reply_to(live, client):
    add_inbox(client)
    lead_id = add_test_lead()
    run_tick(live, client)
    first = client.sent_messages[0]
    clear_gap()

    # Saturday would be closed, so step 2 runs on the next working day after the 3 day delay.
    later = datetime.datetime(2026, 11, 9, 15, 0, tzinfo=datetime.timezone.utc)
    assert run_tick(live, client, later) is True

    second = client.sent_messages[1]
    assert second["thread_id"] == first["thread_id"]
    assert second["in_reply_to"] == first["rfc_message_id"]
    assert second["subject"] == f"Re: {first['subject']}"
    assert lead_row(lead_id)["step"] == 2
    assert lead_row(lead_id)["first_subject"] == first["subject"]


def test_delay_zero_sends_the_whole_sequence_in_one_thread(live, client):
    add_inbox(client)
    lead_id = add_test_lead()
    with open_conn() as conn:
        conn.execute("UPDATE steps SET delay_days = 0")

    for i in range(4):
        run_tick(live, client, NOW + datetime.timedelta(minutes=i))
        clear_gap()

    assert len(client.sent_messages) == 3
    assert len({m["thread_id"] for m in client.sent_messages}) == 1
    assert client.sent_messages[2]["in_reply_to"] == client.sent_messages[1]["rfc_message_id"]
    assert lead_row(lead_id)["step"] == 3


def test_last_step_waits_four_days_then_finishes(live, client):
    from mailer.db import finish_completed_leads

    inbox_id = add_inbox(client)
    lead_id = add_test_lead(status="in_sequence", step=2, inbox_id=inbox_id, thread_id="t", first_subject="idea",
                            last_message_id="<m@x>", next_due_at="2026-11-04T14:00:00Z")

    run_tick(live, client)

    assert lead_row(lead_id)["next_due_at"] == to_iso(NOW + datetime.timedelta(days=4))
    with open_conn() as conn:
        assert finish_completed_leads(to_iso(NOW + datetime.timedelta(days=3)), conn) == 0
        assert finish_completed_leads(to_iso(NOW + datetime.timedelta(days=4, seconds=1)), conn) == 1
    assert lead_row(lead_id)["status"] == "finished"


def test_finish_leaves_leads_with_steps_remaining(live):
    from mailer.db import finish_completed_leads

    lead_id = add_test_lead(status="in_sequence", step=1, next_due_at="2026-11-04T14:00:00Z")

    with open_conn() as conn:
        assert finish_completed_leads(to_iso(NOW), conn) == 0
    assert lead_row(lead_id)["status"] == "in_sequence"


def test_follow_ups_take_priority_in_live(live, client):
    inbox_id = add_inbox(client)
    add_test_lead("new@example.com", country="United States", location="New York, United States", priority=1)
    add_test_lead("due@example.com", country="France", status="in_sequence", step=1, inbox_id=inbox_id, thread_id="t",
                  first_subject="idea", last_message_id="<m@x>", next_due_at="2026-11-04T14:00:00Z")

    run_tick(live, client)

    assert client.sent_messages[0]["to"] == "due@example.com"


@pytest.mark.parametrize("status", ["replied", "unsubscribed", "bounced", "finished", "skipped"])
def test_stopped_lead_gets_no_live_email(live, client, status):
    inbox_id = add_inbox(client)
    add_test_lead(status=status, step=1, inbox_id=inbox_id, thread_id="t", next_due_at="2026-11-04T14:00:00Z")

    assert run_tick(live, client) is False
    assert client.sent_messages == []


def test_suppressed_lead_is_marked_skipped_and_never_sent(live, client):
    add_inbox(client)
    lead_id = add_test_lead()
    add_suppression("ME+A@example.com", "unsubscribed")

    assert run_tick(live, client) is False

    assert client.sent_messages == []
    assert lead_row(lead_id)["status"] == "skipped"


def test_suppression_stops_a_running_sequence(live, client):
    inbox_id = add_inbox(client)
    lead_id = add_test_lead(status="in_sequence", step=1, inbox_id=inbox_id, thread_id="t", next_due_at="2026-11-04T14:00:00Z")
    add_suppression("me+a@example.com", "manual")

    assert run_tick(live, client) is False
    assert client.sent_messages == []
    assert lead_row(lead_id)["status"] == "skipped"


def test_cap_reached_stops_live_sends(live, client):
    add_inbox(client, ramp_start_cap=1, max_cap=1)
    add_test_lead("a@example.com")
    add_test_lead("b@example.com")

    run_tick(live, client)
    clear_gap()

    assert run_tick(live, client) is False
    assert len(client.sent_messages) == 1


def test_dry_run_rows_do_not_count_toward_the_live_cap(live, client):
    inbox_id = add_inbox(client, ramp_start_cap=1, max_cap=1)
    add_test_lead()
    with open_conn() as conn:
        conn.execute(
            "INSERT INTO send_log (ts, inbox_id, lead_email, step_no, result) VALUES (?, ?, 'x@y.z', 1, 'dry_run')",
            (to_iso(NOW), inbox_id),
        )

    assert run_tick(live, client) is True


def test_same_lead_and_step_is_never_sent_twice(live, client):
    inbox_id = add_inbox(client)
    lead_id = add_test_lead()
    with open_conn() as conn:
        conn.execute(
            "INSERT INTO send_log (ts, inbox_id, lead_id, lead_email, step_no, result, thread_id) VALUES (?, ?, ?, 'me+a@example.com', 1, 'sent', 'th_old')",
            (to_iso(NOW), inbox_id, lead_id),
        )

    assert run_tick(live, client) is False

    assert client.sent_messages == []
    lead = lead_row(lead_id)
    assert (lead["step"], lead["status"], lead["thread_id"]) == (1, "in_sequence", "th_old")


def paused_state():
    with open_conn() as conn:
        return tuple(conn.execute("SELECT paused_reason, paused_until FROM inboxes").fetchone())


def test_quota_exceeded_pauses_the_inbox(live, client):
    from mailer.gmail_client import QuotaExceeded

    add_inbox(client)
    lead_id = add_test_lead()
    client.should_raise = QuotaExceeded("limit")

    assert run_tick(live, client) is False

    reason, until = paused_state()
    assert "quota" in reason.lower()
    assert until == to_iso(NOW + datetime.timedelta(hours=24))
    assert log_rows()[0]["result"] == "error"
    assert lead_row(lead_id)["status"] == "queued"


def test_rate_limit_pauses_for_one_hour(live, client):
    from mailer.gmail_client import RateLimited

    add_inbox(client)
    add_test_lead()
    client.should_raise = RateLimited("slow")

    run_tick(live, client)

    assert paused_state()[1] == to_iso(NOW + datetime.timedelta(hours=1))


def test_expired_auth_pauses_until_reconnected(live, client):
    from mailer.gmail_client import AuthExpired

    add_inbox(client)
    add_test_lead()
    client.should_raise = AuthExpired("expired")

    run_tick(live, client)

    assert paused_state() == ("Reconnect inbox", None)


def test_rejected_address_is_bounced_and_suppressed(live, client):
    from mailer.db import is_suppressed
    from mailer.gmail_client import PermanentSendError

    add_inbox(client)
    lead_id = add_test_lead()
    client.should_raise = PermanentSendError("Invalid To header")

    run_tick(live, client)

    assert lead_row(lead_id)["status"] == "bounced"
    assert is_suppressed("me+a@example.com")
    assert paused_state()[0] is None


def test_transient_error_keeps_lead_and_inbox_for_a_retry(live, client):
    from mailer.gmail_client import TransientError

    add_inbox(client)
    lead_id = add_test_lead()
    client.should_raise = TransientError("down")

    assert run_tick(live, client) is False

    assert lead_row(lead_id)["status"] == "queued"
    assert paused_state()[0] is None
    assert log_rows()[0]["result"] == "error"

    client.should_raise = None
    clear_gap()
    assert run_tick(live, client) is True


def test_unknown_gmail_error_pauses_rather_than_burning_through_leads(live, client):
    from mailer.gmail_client import GmailClientError

    add_inbox(client)
    lead_id = add_test_lead()
    client.should_raise = GmailClientError("Insufficient Permission")

    run_tick(live, client)

    assert "Insufficient Permission" in paused_state()[0]
    assert lead_row(lead_id)["status"] == "queued"


def test_unwritable_address_skips_the_lead_without_crashing(live, client):
    add_inbox(client)
    lead_id = add_test_lead()
    client.should_raise = ValueError("bad header")

    assert run_tick(live, client) is False
    assert lead_row(lead_id)["status"] == "skipped"
