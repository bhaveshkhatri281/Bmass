"""Tests for scheduling logic, ramp calculations, windows, and timezone mapping."""

import datetime
from zoneinfo import ZoneInfo
import pytest

from mailer.config import Settings
from mailer.db import open_conn
from mailer.scheduling import (
    has_queued_us_leads,
    in_app_window,
    in_recipient_window,
    is_us_lead,
    non_us_allowance,
    non_us_sent_last_24h,
    parse_us_state_tz,
    queued_leads,
    reservation_is_open,
    save_country_settings,
    sent_last_24h,
    thin_window_leads,
    todays_cap,
    window_overlap_minutes,
)


def test_ramp_cap_progression():
    inbox = {
        "ramp_start_date": "2026-10-01",
        "ramp_start_cap": 10,
        "ramp_increment": 5,
        "ramp_every_days": 3,
        "max_cap": 80,
    }

    # Day 0: start cap = 10
    assert todays_cap(inbox, target_date=datetime.date(2026, 10, 1), hard_daily_cap=150) == 10
    # Day 1: still 10
    assert todays_cap(inbox, target_date=datetime.date(2026, 10, 2), hard_daily_cap=150) == 10
    # Day 2: still 10
    assert todays_cap(inbox, target_date=datetime.date(2026, 10, 3), hard_daily_cap=150) == 10
    # Day 3: 10 + 5 = 15
    assert todays_cap(inbox, target_date=datetime.date(2026, 10, 4), hard_daily_cap=150) == 15
    # Day 6: 10 + 10 = 20
    assert todays_cap(inbox, target_date=datetime.date(2026, 10, 7), hard_daily_cap=150) == 20
    # Day 60: would be 10 + 5*20 = 110, but capped at max_cap = 80
    assert todays_cap(inbox, target_date=datetime.date(2026, 11, 30), hard_daily_cap=150) == 80
    # Hard daily cap overrides if lower than max_cap
    assert todays_cap(inbox, target_date=datetime.date(2026, 11, 30), hard_daily_cap=50) == 50


def test_app_window_across_midnight():
    cfg = Settings()
    cfg.worker.app_window_ist.start = "18:00"
    cfg.worker.app_window_ist.end = "00:30"

    # 18:30 IST is inside window
    dt_inside_early = datetime.datetime(2026, 10, 31, 13, 0, tzinfo=datetime.timezone.utc)  # 18:30 IST
    assert in_app_window(dt_inside_early, settings=cfg) is True

    # 00:15 IST is inside window (next calendar day IST)
    dt_inside_late = datetime.datetime(2026, 10, 31, 18, 45, tzinfo=datetime.timezone.utc)  # 00:15 IST next day
    assert in_app_window(dt_inside_late, settings=cfg) is True

    # 01:00 IST is outside window
    dt_outside = datetime.datetime(2026, 10, 31, 19, 30, tzinfo=datetime.timezone.utc)  # 01:00 IST next day
    assert in_app_window(dt_outside, settings=cfg) is False

    # 12:00 IST is outside window
    dt_afternoon = datetime.datetime(2026, 10, 31, 6, 30, tzinfo=datetime.timezone.utc)  # 12:00 IST
    assert in_app_window(dt_afternoon, settings=cfg) is False


def test_us_state_tz_parsing():
    assert parse_us_state_tz("California, United States") == "America/Los_Angeles"
    assert parse_us_state_tz("Austin, Texas") == "America/Chicago"
    assert parse_us_state_tz("Denver, Colorado") == "America/Denver"
    assert parse_us_state_tz("Phoenix, Arizona") == "America/Phoenix"
    assert parse_us_state_tz("Honolulu, Hawaii") == "Pacific/Honolulu"
    assert parse_us_state_tz("Miami, Florida") == "America/New_York"
    assert parse_us_state_tz("") == "America/New_York"


def test_recipient_window_weekdays_and_hours():
    cfg = Settings()
    cfg.worker.recipient_window.start = "09:00"
    cfg.worker.recipient_window.end = "17:00"
    cfg.worker.recipient_window.weekdays_only = True

    lead_ny = {"country": "United States", "location": "New York, NY"}

    # Wednesday 14:00 UTC = 10:00 AM EDT (in window)
    wed_utc = datetime.datetime(2026, 10, 28, 14, 0, tzinfo=datetime.timezone.utc)
    assert in_recipient_window(lead_ny, now_utc=wed_utc, settings=cfg) is True

    # Wednesday 23:00 UTC = 19:00 EDT (outside window)
    wed_night_utc = datetime.datetime(2026, 10, 28, 23, 0, tzinfo=datetime.timezone.utc)
    assert in_recipient_window(lead_ny, now_utc=wed_night_utc, settings=cfg) is False

    # Saturday 14:00 UTC = Weekend (excluded)
    sat_utc = datetime.datetime(2026, 10, 31, 14, 0, tzinfo=datetime.timezone.utc)
    assert in_recipient_window(lead_ny, now_utc=sat_utc, settings=cfg) is False


def test_same_utc_time_differs_by_recipient():
    cfg = Settings()
    # 10:00 New York, 07:00 Los Angeles, 16:00 Paris
    moment = datetime.datetime(2026, 11, 4, 15, 0, tzinfo=datetime.timezone.utc)

    new_york = {"country": "United States", "location": "New York, New York, United States"}
    los_angeles = {"country": "United States", "location": "Los Angeles, California, United States"}
    paris = {"country": "France", "location": "Paris, France"}

    assert in_recipient_window(new_york, moment, cfg) is True
    assert in_recipient_window(los_angeles, moment, cfg) is False
    assert in_recipient_window(paris, moment, cfg) is True

    # 18:00 Paris is closed while New York stays open.
    later = datetime.datetime(2026, 11, 4, 17, 0, tzinfo=datetime.timezone.utc)
    assert in_recipient_window(paris, later, cfg) is False
    assert in_recipient_window(new_york, later, cfg) is True


def test_friday_evening_in_ist_is_still_friday_for_recipient():
    cfg = Settings()
    # 00:15 Saturday IST is 13:45 Friday in New York.
    moment = datetime.datetime(2026, 11, 6, 18, 45, tzinfo=datetime.timezone.utc)
    lead = {"country": "United States", "location": "New York, United States"}

    assert in_recipient_window(lead, moment, cfg) is True


@pytest.mark.parametrize(
    "location, expected",
    [
        ("Washington, DC, United States", "America/New_York"),
        ("Washington, D.C.", "America/New_York"),
        ("Washington DC, District of Columbia, United States", "America/New_York"),
        ("Washington, District of Columbia, United States", "America/New_York"),
        ("Washingtonville, New York, United States", "America/New_York"),
        ("Washington Heights, New York, United States", "America/New_York"),
        ("Oregon, Ohio, United States", "America/New_York"),
        ("Washington, United States", "America/Los_Angeles"),
        ("Seattle, Washington, United States", "America/Los_Angeles"),
        ("Arkansas, United States", "America/Chicago"),
        ("Kansas City, Missouri, United States", "America/Chicago"),
        ("New Mexico, United States", "America/Denver"),
        ("Georgia, United States", "America/New_York"),
        ("TX, United States", "America/New_York"),
        ("United States", "America/New_York"),
    ],
)
def test_us_state_is_the_part_before_the_country(location, expected):
    assert parse_us_state_tz(location) == expected


def test_queue_order_is_stable_on_ties():
    stamp = "2026-11-01T00:00:00Z"
    with open_conn() as conn:
        for email, country, priority, quality in [
            ("c@x.com", "France", 1, "Good"),
            ("b@x.com", "United Kingdom", 2, "Good"),
            ("a@x.com", "United Kingdom", 1, "Weak"),
            ("d@x.com", "United Kingdom", 1, ""),
            ("e@x.com", "United Kingdom", 1, "Good"),
        ]:
            conn.execute(
                "INSERT INTO leads (email, country, priority, quality, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (email, country, priority, quality, stamp, stamp),
            )
        order = [row["email"] for row in queued_leads(conn)]

    assert order == ["e@x.com", "a@x.com", "d@x.com", "b@x.com", "c@x.com"]


def test_sent_last_24h_counts_only_what_was_asked():
    now = datetime.datetime(2026, 11, 4, 15, 0, tzinfo=datetime.timezone.utc)
    with open_conn() as conn:
        conn.execute("INSERT INTO inboxes (id, email, display_name, ramp_start_date, created_at) VALUES (1, 'a@b.c', 'A', '2026-11-01', 'x')")
        rows = [
            ("2026-11-04T14:00:00Z", "sent"),
            ("2026-11-04T14:00:00Z", "dry_run"),
            ("2026-11-04T14:00:00Z", "test"),
            ("2026-11-04T14:00:00Z", "error"),
            ("2026-11-03T14:59:00Z", "sent"),
        ]
        conn.executemany(
            "INSERT INTO send_log (ts, inbox_id, lead_email, step_no, result) VALUES (?, 1, 'x@y.z', 1, ?)",
            rows,
        )
        assert sent_last_24h(1, conn, now_utc=now) == 1
        assert sent_last_24h(1, conn, include_dry_run=True, now_utc=now) == 2


@pytest.mark.parametrize(
    "tz_name, minimum, maximum",
    [
        ("Australia/Sydney", 0, 0),
        ("Pacific/Honolulu", 0, 59),
        ("America/New_York", 240, 400),
        ("Europe/Paris", 100, 300),
    ],
)
def test_window_overlap(tz_name, minimum, maximum):
    minutes = window_overlap_minutes(tz_name, Settings(), datetime.date(2026, 11, 4))
    assert minimum <= minutes <= maximum


def test_thin_window_leads_reports_only_poor_timezones():
    stamp = "2026-11-01T00:00:00Z"
    with open_conn() as conn:
        for email, country, location, status in [
            ("a@x.com", "Australia", "Sydney", "queued"),
            ("b@x.com", "Australia", "Perth", "queued"),
            ("c@x.com", "United States", "Hawaii, United States", "queued"),
            ("d@x.com", "United States", "Texas, United States", "queued"),
            ("e@x.com", "France", "Paris", "queued"),
            ("f@x.com", "Australia", "Sydney", "finished"),
        ]:
            conn.execute(
                "INSERT INTO leads (email, country, location, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (email, country, location, status, stamp, stamp),
            )
        assert thin_window_leads(Settings(), conn) == {"Australia/Sydney": 2, "Pacific/Honolulu": 1}


def ist(hour, minute=0):
    return datetime.datetime(2026, 11, 4, hour, minute, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.mark.parametrize(
    "moment, expected",
    [
        (ist(17, 59), False),
        (ist(18, 0), True),
        (ist(22, 29), True),
        (ist(22, 30), False),
        (ist(23, 59), False),
        (ist(0, 15), False),
    ],
)
def test_reservation_runs_from_app_start_to_cutoff(moment, expected):
    assert reservation_is_open(moment, Settings()) is expected


def test_reservation_cutoff_after_midnight():
    cfg = Settings()
    cfg.worker.us_reservation.until_ist = "00:15"

    assert reservation_is_open(ist(23, 30), cfg) is True
    assert reservation_is_open(ist(0, 10), cfg) is True
    assert reservation_is_open(ist(0, 20), cfg) is False


def test_zero_percent_is_never_open():
    cfg = Settings()
    cfg.worker.us_reservation.percent = 0

    assert reservation_is_open(ist(19), cfg) is False


@pytest.mark.parametrize("cap, percent, expected", [(10, 50, 5), (15, 50, 7), (1, 50, 0), (10, 0, 10), (10, 100, 0), (80, 25, 60)])
def test_non_us_allowance(cap, percent, expected):
    assert non_us_allowance(cap, percent) == expected


def test_has_queued_us_leads_and_is_us_lead():
    stamp = "x"
    with open_conn() as conn:
        assert has_queued_us_leads(conn) is False
        conn.execute("INSERT INTO leads (email, country, status, created_at, updated_at) VALUES ('a@x.com', 'France', 'queued', ?, ?)", (stamp, stamp))
        conn.execute("INSERT INTO leads (email, country, status, created_at, updated_at) VALUES ('b@x.com', 'United States', 'replied', ?, ?)", (stamp, stamp))
        assert has_queued_us_leads(conn) is False

        conn.execute("INSERT INTO leads (email, country, status, created_at, updated_at) VALUES ('c@x.com', ' USA ', 'queued', ?, ?)", (stamp, stamp))
        assert has_queued_us_leads(conn) is True
        assert has_queued_us_leads(conn, test_leads_only=True) is False

        conn.execute("UPDATE leads SET is_test = 1 WHERE email = 'c@x.com'")
        assert has_queued_us_leads(conn, test_leads_only=True) is True

    assert is_us_lead({"country": "United States"}) is True
    assert is_us_lead({"country": "France"}) is False
    assert is_us_lead({"country": None}) is False


def test_non_us_sent_counts_only_non_us_sends():
    now = datetime.datetime(2026, 11, 4, 15, 0, tzinfo=datetime.timezone.utc)
    with open_conn() as conn:
        conn.execute("INSERT INTO inboxes (id, email, display_name, ramp_start_date, created_at) VALUES (1, 'a@b.c', 'A', '2026-11-01', 'x')")
        conn.execute("INSERT INTO leads (id, email, country, created_at, updated_at) VALUES (1, 'uk@x.com', 'United Kingdom', 'x', 'x')")
        conn.execute("INSERT INTO leads (id, email, country, created_at, updated_at) VALUES (2, 'us@x.com', 'United States', 'x', 'x')")
        conn.executemany(
            "INSERT INTO send_log (ts, inbox_id, lead_id, lead_email, step_no, result) VALUES (?, 1, ?, 'x', ?, ?)",
            [
                ("2026-11-04T14:00:00Z", 1, 2, "sent"),
                ("2026-11-04T14:00:00Z", 2, 2, "sent"),
                ("2026-11-04T14:00:00Z", 1, 3, "dry_run"),
                ("2026-11-03T14:00:00Z", 1, 1, "sent"),
            ],
        )

        assert non_us_sent_last_24h(1, conn, now_utc=now) == 1
        assert non_us_sent_last_24h(1, conn, include_dry_run=True, now_utc=now) == 2


def country_rows(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM country_settings ORDER BY sort_order")]


def test_country_table_saves_and_adds_a_country():
    with open_conn() as conn:
        rows = country_rows(conn)
        rows[0]["sort_order"] = 7
        rows.append({"country": "Malta", "language": "en", "timezone": "Europe/Malta", "skip": False, "sort_order": 50})
        rows.append({"country": None, "language": None, "timezone": None, "skip": None, "sort_order": None})

        assert save_country_settings(rows, conn) == []
        assert conn.execute("SELECT timezone FROM country_settings WHERE country = 'Malta'").fetchone()[0] == "Europe/Malta"
        assert conn.execute("SELECT sort_order FROM country_settings WHERE country = 'United States'").fetchone()[0] == 7


@pytest.mark.parametrize(
    "change, error",
    [
        ({"timezone": "Mars/Base"}, "unknown timezone"),
        ({"language": "xx"}, "unknown language"),
        ({"sort_order": "first"}, "sort_order must be a number"),
        ({"country": ""}, "needs a country"),
    ],
)
def test_country_table_rejects_bad_rows(change, error):
    with open_conn() as conn:
        rows = country_rows(conn)
        rows[0].update(change)
        errors = save_country_settings(rows, conn)

        assert any(error in message for message in errors)
        assert conn.execute("SELECT COUNT(*) FROM country_settings").fetchone()[0] == 35

