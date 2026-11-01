"""Shared UI components, DB caching, and status banners for Streamlit."""

from __future__ import annotations

import datetime
import sqlite3

import streamlit as st

from mailer.config import Settings, load_settings
from mailer.db import get_conn, init_db
from mailer.logs import setup_logging
from mailer.scheduling import in_app_window
from mailer.seed import seed_database


@st.cache_resource
def get_cached_connection() -> sqlite3.Connection:
    """Return a cached SQLite connection for the Streamlit process."""
    setup_logging()
    init_db()
    seed_database()
    return get_conn(check_same_thread=False)


def render_top_banners(conn: sqlite3.Connection, settings: Settings) -> None:
    """Render DRY RUN / LIVE banner, paused inbox alerts, and worker heartbeat warning."""
    # 1. Mode banner
    if settings.mode.dry_run:
        st.warning("⚠️ **DRY RUN MODE ACTIVE** — Sends are simulated and logged. No actual emails will be dispatched.")
    else:
        st.success("🟢 **LIVE MODE ACTIVE** — Real emails will be dispatched from connected inboxes.")

    # 2. Paused inboxes banner
    paused_rows = conn.execute(
        "SELECT email, paused_reason FROM inboxes WHERE active = 1 AND paused_reason IS NOT NULL AND paused_reason != ''"
    ).fetchall()
    if paused_rows:
        reasons = ", ".join(f"{r['email']} ({r['paused_reason']})" for r in paused_rows)
        st.error(f"🚨 **Paused Inboxes:** {reasons}")

    # 3. Worker heartbeat banner (check during app window)
    if in_app_window(settings=settings):
        cur = conn.execute("SELECT value FROM app_state WHERE key = 'worker_heartbeat'")
        row = cur.fetchone()
        heartbeat_str = row["value"] if row else None
        worker_down = True
        if heartbeat_str:
            try:
                hb_time = datetime.datetime.fromisoformat(heartbeat_str.replace("Z", "+00:00"))
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                age_seconds = (now_utc - hb_time).total_seconds()
                if age_seconds <= 120:  # within 2 minutes
                    worker_down = False
            except ValueError:
                worker_down = True

        if worker_down:
            st.error("🚨 **Worker is not running!** Current time is within the IST sending window, but no active worker heartbeat was detected in the last 2 minutes.")


def setup_page(title: str) -> tuple[sqlite3.Connection, Settings]:
    """Configure page settings and render the top banners."""
    st.set_page_config(
        page_title=f"Mobisa Mailer · {title}",
        page_icon="✉️",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    conn = get_cached_connection()
    settings = load_settings()
    render_top_banners(conn, settings)
    st.title(f"✉️ Mobisa Mailer — {title}")
    st.markdown("---")
    return conn, settings

