"""Scheduling rules: timezone mapping, send windows, ramp caps, and queue ordering."""

from __future__ import annotations

import datetime
import math
import sqlite3
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mailer.config import RecipientWindowSettings, Settings, load_settings
from mailer.db import open_conn, to_iso, transaction

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_US_TIMEZONE = "America/New_York"
# Below this many shared minutes a day, a timezone is reported on the Overview page.
THIN_WINDOW_MINUTES = 60

US_COUNTRY_NAMES = ("united states", "usa", "us")

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
    """Map a location like 'Austin, Texas, United States' to a timezone.

    The state is the last part before the country. Matching only that part keeps
    "Washington, District of Columbia" and "Washingtonville, New York" out of Pacific time.
    """
    parts = [p.strip().lower() for p in (location_str or "").split(",") if p.strip()]
    while parts and parts[-1] in US_COUNTRY_NAMES:
        parts.pop()
    return US_STATE_TIMEZONES.get(parts[-1], DEFAULT_US_TIMEZONE) if parts else DEFAULT_US_TIMEZONE


def recipient_tz(lead: Dict[str, Any] | sqlite3.Row, conn: Optional[sqlite3.Connection] = None) -> ZoneInfo:
    """Determine the ZoneInfo for a given lead based on country and location rules."""
    country = str(lead["country"] or "").strip()
    location = str(lead["location"] or "").strip()

    if country.lower() in US_COUNTRY_NAMES:
        tz_name = parse_us_state_tz(location)
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return ZoneInfo(DEFAULT_US_TIMEZONE)

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


def _in_local_window(local_dt: datetime.datetime, recip_win: RecipientWindowSettings) -> bool:
    """Check a local datetime against the recipient window."""
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


def in_recipient_window(
    lead: Dict[str, Any] | sqlite3.Row,
    now_utc: Optional[datetime.datetime] = None,
    settings: Optional[Settings] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Check if recipient's local time is within their business hours window."""
    cfg = settings or load_settings()
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    return _in_local_window(now.astimezone(recipient_tz(lead, conn=conn)), cfg.worker.recipient_window)


def in_app_window(
    now_utc: Optional[datetime.datetime] = None,
    settings: Optional[Settings] = None,
) -> bool:
    """Check if IST time is within app_window_ist (handles overnight spans)."""
    cfg = settings or load_settings()
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    now_ist = now.astimezone(IST).time()

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


def sent_last_24h(
    inbox_id: int,
    conn: Optional[sqlite3.Connection] = None,
    include_dry_run: bool = False,
    now_utc: Optional[datetime.datetime] = None,
) -> int:
    """Count sends for an inbox in the rolling last 24h, with dry-run rows when asked."""
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    cutoff = to_iso(now - datetime.timedelta(hours=24))
    results = "('sent', 'dry_run')" if include_dry_run else "('sent')"

    def _count(c: sqlite3.Connection) -> int:
        cur = c.execute(
            f"SELECT COUNT(*) FROM send_log WHERE inbox_id = ? AND result IN {results} AND ts >= ?",
            (inbox_id, cutoff),
        )
        return int(cur.fetchone()[0])

    if conn is not None:
        return _count(conn)
    with open_conn() as c:
        return _count(c)


def queued_leads(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    """Yield new leads in queue order: country order, priority, Good before Weak, then id."""
    return conn.execute(
        """SELECT l.* FROM leads l
           LEFT JOIN country_settings c ON c.country = l.country
           WHERE l.status = 'queued' AND COALESCE(c.skip, 0) = 0
           ORDER BY COALESCE(c.sort_order, 100),
                    l.priority,
                    CASE l.quality WHEN 'Good' THEN 0 WHEN 'Weak' THEN 1 ELSE 2 END,
                    l.id"""
    )


def due_follow_ups(inbox_id: int, now_utc: datetime.datetime, conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    """Yield leads assigned to this inbox whose next step is due, oldest first."""
    return conn.execute(
        """SELECT l.* FROM leads l
           LEFT JOIN country_settings c ON c.country = l.country
           WHERE l.status = 'in_sequence' AND l.inbox_id = ? AND l.next_due_at <= ? AND COALESCE(c.skip, 0) = 0
           ORDER BY l.next_due_at, l.id""",
        (inbox_id, to_iso(now_utc)),
    )


def window_overlap_minutes(
    tz_name: str,
    settings: Settings,
    day: Optional[datetime.date] = None,
) -> int:
    """Count the minutes in a weekday when the app window and this timezone's recipient window both hold."""
    today = day or datetime.date.today()
    # Wednesday, so weekends never hide a timezone that is fine on working days.
    wednesday = today + datetime.timedelta(days=(2 - today.weekday()) % 7)
    midnight = datetime.datetime.combine(wednesday, datetime.time(0), tzinfo=datetime.timezone.utc)
    zone = ZoneInfo(tz_name)

    minutes = 0
    for offset in range(24 * 60):
        moment = midnight + datetime.timedelta(minutes=offset)
        if in_app_window(moment, settings) and _in_local_window(moment.astimezone(zone), settings.worker.recipient_window):
            minutes += 1
    return minutes


def thin_window_leads(settings: Settings, conn: sqlite3.Connection) -> Dict[str, int]:
    """Return queued lead counts per timezone that barely overlaps the app window."""
    rows = conn.execute(
        """SELECT country,
                  CASE WHEN lower(country) IN ('united states', 'usa', 'us') THEN location ELSE '' END AS location,
                  COUNT(*) AS leads
           FROM leads WHERE status = 'queued' GROUP BY 1, 2"""
    ).fetchall()

    overlap: Dict[str, int] = {}
    counts: Dict[str, int] = {}
    for row in rows:
        tz_name = recipient_tz(row, conn=conn).key
        if tz_name not in overlap:
            overlap[tz_name] = window_overlap_minutes(tz_name, settings)
        if overlap[tz_name] < THIN_WINDOW_MINUTES:
            counts[tz_name] = counts.get(tz_name, 0) + row["leads"]
    return counts


def save_country_settings(rows: List[Dict[str, Any]], conn: sqlite3.Connection) -> List[str]:
    """Validate and replace the country table, returning errors."""
    languages = {row["code"] for row in conn.execute("SELECT code FROM languages")}
    errors: List[str] = []
    cleaned: Dict[str, tuple] = {}

    for row in rows:
        country = str(row.get("country") or "").strip()
        language = str(row.get("language") or "").strip()
        timezone = str(row.get("timezone") or "").strip()
        if not country and not language and not timezone:
            continue
        if not country:
            errors.append("Every row needs a country.")
            continue
        if country in cleaned:
            errors.append(f"Duplicate country: {country}")
        if language not in languages:
            errors.append(f"{country}: unknown language '{language}'.")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            errors.append(f"{country}: unknown timezone '{timezone}'.")
        try:
            sort_order = int(row.get("sort_order"))
        except (TypeError, ValueError):
            errors.append(f"{country}: sort_order must be a number.")
            continue
        cleaned[country] = (language, timezone, 1 if row.get("skip") else 0, sort_order)

    if errors:
        return errors

    with transaction(conn):
        conn.execute("DELETE FROM country_settings")
        conn.executemany(
            "INSERT INTO country_settings (country, language, timezone, skip, sort_order) VALUES (?, ?, ?, ?, ?)",
            [(country, *values) for country, values in cleaned.items()],
        )
    return []


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

