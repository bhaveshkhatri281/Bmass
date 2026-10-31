"""Tests for scheduling logic, ramp calculations, windows, and timezone mapping."""

import datetime
from zoneinfo import ZoneInfo
import pytest

from mailer.config import Settings
from mailer.scheduling import (
    in_app_window,
    in_recipient_window,
    parse_us_state_tz,
    todays_cap,
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

