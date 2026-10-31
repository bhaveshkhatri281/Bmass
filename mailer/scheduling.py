"""Scheduling rules: timezone mapping, send windows, ramp caps, and queue ordering."""

from __future__ import annotations

import datetime
import math
import sqlite3
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from mailer.config import Settings, load_settings
from mailer.db import open_conn, utc_now_iso

# US State mapping as per Section 7
US_STATE_TIMEZONES = {
    # Pacific (America/Los_Angeles)
    "california": "America/Los_Angeles",
    "washington": "America/Los_Angeles",
    "oregon": "America/Los_Angeles",
    "nevada": "America/Los_Angeles",
    # Mountain (America/Denver)
    "colorado": "America/Denver",
    "utah": "America/Denver",
    "new mexico": "America/Denver",
    "wyoming": "America/Denver",
    "montana": "America/Denver",
    "idaho": "America/Denver",
    # Arizona (America/Phoenix)
    "arizona": "America/Phoenix",
    # Central (America/Chicago)
    "texas": "America/Chicago",
    "illinois": "America/Chicago",
    "minnesota": "America/Chicago",
    "wisconsin": "America/Chicago",
    "missouri": "America/Chicago",
    "iowa": "America/Chicago",
    "kansas": "America/Chicago",
    "nebraska": "America/Chicago",
    "oklahoma": "America/Chicago",
    "arkansas": "America/Chicago",
    "louisiana": "America/Chicago",
    "mississippi": "America/Chicago",
    "alabama": "America/Chicago",
    "tennessee": "America/Chicago",
    "north dakota": "America/Chicago",
    "south dakota": "America/Chicago",
    # Alaska & Hawaii
    "alaska": "America/Anchorage",
    "hawaii": "Pacific/Honolulu",
}


def parse_us_state_tz(location_str: str) -> str:
    """Parse state from location string like 'Texas, United States' and map to IANA timezone."""
    if not location_str:
        return "America/New_York"

    # Split by comma or whitespace
    parts = [p.strip().lower() for p in location_str.split(",")]
    for part in parts:
        if part in US_STATE_TIMEZONES:
            return US_STATE_TIMEZONES[part]

    # Check words inside location string
    loc_lower = location_str.lower()
    for state_name, tz_name in US_STATE_TIMEZONES.items():
        if state_name in loc_lower:
            return tz_name

    return "America/New_York"


def recipient_tz(lead: Dict[str, Any] | sqlite3.Row, conn: Optional[sqlite3.Connection] = None) -> ZoneInfo:
    """Determine the ZoneInfo for a given lead based on country and location rules."""
    country = str(lead["country"] or "").strip()
    location = str(lead["location"] or "").strip()

    if country.lower() in ("united states", "usa", "us"):
        tz_name = parse_us_state_tz(location)
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return ZoneInfo("America/New_York")

    # Look up in country_settings table
    def _lookup(c: sqlite3.Connection) -> str:
        cur = c.execute("SELECT timezone FROM country_settings WHERE lower(country) = lower(?)", (country,))
        row = cur.fetchone()
        return row["timezone"] if row else "Europe/London"

    if conn is not None:
        tz_name = _lookup(conn)
    else:
        with open_conn() as c:
            tz_name = _lookup(c)

    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Europe/London")


def in_recipient_window(
    lead: Dict[str, Any] | sqlite3.Row,
    now_utc: Optional[datetime.datetime] = None,
    settings: Optional[Settings] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Check if recipient's local time is within their business hours window."""
    cfg = settings or load_settings()
    recip_win = cfg.worker.recipient_window
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)

    recip_timezone = recipient_tz(lead, conn=conn)
    local_dt = now.astimezone(recip_timezone)

    if recip_win.weekdays_only and local_dt.weekday() >= 5:  # 5=Sat, 6=Sun
        return False

    sh, sm = map(int, recip_win.start.split(":"))
    eh, em = map(int, recip_win.end.split(":"))
    start_t = datetime.time(sh, sm)
    end_t = datetime.time(eh, em)

    local_t = local_dt.time()
    if start_t <= end_t:
        return start_t <= local_t <= end_t
    else:
        return local_t >= start_t or local_t <= end_t


def in_app_window(
    now_utc: Optional[datetime.datetime] = None,
    settings: Optional[Settings] = None,
) -> bool:
    """Check if IST time is within app_window_ist (handles overnight spans)."""
    cfg = settings or load_settings()
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    ist_tz = ZoneInfo("Asia/Kolkata")
    now_ist = now.astimezone(ist_tz).time()

    sh, sm = map(int, cfg.worker.app_window_ist.start.split(":"))
    eh, em = map(int, cfg.worker.app_window_ist.end.split(":"))
    start_t = datetime.time(sh, sm)
    end_t = datetime.time(eh, em)

    if start_t <= end_t:
        return start_t <= now_ist <= end_t
    else:
        return now_ist >= start_t or now_ist <= end_t


def todays_cap(
    inbox: Dict[str, Any] | sqlite3.Row,
    target_date: Optional[datetime.date] = None,
    hard_daily_cap: int = 150,
) -> int:
    """Calculate daily sending cap based on inbox ramp formula and hard limits."""
    today = target_date or datetime.date.today()
    ramp_start_str = inbox["ramp_start_date"]
    if isinstance(ramp_start_str, str):
        ramp_start = datetime.date.fromisoformat(ramp_start_str.split("T")[0])
    elif isinstance(ramp_start_str, datetime.date):
        ramp_start = ramp_start_str
    else:
        ramp_start = today

    days = max(0, (today - ramp_start).days)
    start_cap = int(inbox["ramp_start_cap"])
    increment = int(inbox["ramp_increment"])
    every_days = max(1, int(inbox["ramp_every_days"]))
    max_cap = int(inbox["max_cap"])

    ramp_steps = math.floor(days / every_days)
    calculated_cap = start_cap + (increment * ramp_steps)
    return min(max_cap, hard_daily_cap, calculated_cap)


def sent_last_24h(inbox_id: int, conn: Optional[sqlite3.Connection] = None) -> int:
    """Count emails dispatched with result='sent' for an inbox in the rolling last 24h."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _count(c: sqlite3.Connection) -> int:
        cur = c.execute(
            "SELECT COUNT(*) FROM send_log WHERE inbox_id = ? AND result = 'sent' AND ts >= ?",
            (inbox_id, cutoff),
        )
        return int(cur.fetchone()[0])

    if conn is not None:
        return _count(conn)
    with open_conn() as c:
        return _count(c)


def get_ramp_schedule(
    inbox: Dict[str, Any] | sqlite3.Row,
    days: int = 30,
    hard_daily_cap: int = 150,
) -> List[Dict[str, Any]]:
    """Generate a projection table of daily caps for the next N days."""
    today = datetime.date.today()
    schedule = []
    for d in range(days):
        dt = today + datetime.timedelta(days=d)
        cap = todays_cap(inbox, target_date=dt, hard_daily_cap=hard_daily_cap)
        schedule.append({
            "Date": dt.isoformat(),
            "Day Offset": f"+{d}d" if d > 0 else "Today",
            "Cap": cap,
        })
    return schedule

