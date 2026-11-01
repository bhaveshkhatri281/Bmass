"""Overview page: inboxes summary, lead funnel metrics, and operational controls."""

import datetime
import subprocess
import sys

import streamlit as st

from mailer.config import PROJECT_ROOT, Settings
from mailer.db import get_app_state, set_app_state, to_iso
from mailer.scheduling import IST, in_app_window, sent_last_24h, thin_window_leads, todays_cap
from mailer.ui import setup_page

conn, settings = setup_page("Overview")


@st.cache_data(ttl=600, show_spinner=False)
def load_thin_windows(_conn, _settings: Settings, cache_key: str) -> dict[str, int]:
    return thin_window_leads(_settings, _conn)


# Top controls: Start Worker, Pause All/Resume All, Run Detection Now
col_c1, col_c2, col_c3 = st.columns(3)

paused_all = get_app_state("paused_all", "0", conn) == "1"

with col_c1:
    if st.button("🚀 Start Worker Now", width="stretch"):
        try:
            worker_script = PROJECT_ROOT / "worker.py"
            # Detached process on Windows
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            python_exe = sys.executable
            subprocess.Popen(
                [python_exe, str(worker_script), "--run"],
                cwd=str(PROJECT_ROOT),
                creationflags=creationflags,
                close_fds=True,
            )
            if in_app_window(settings=settings):
                st.success("Worker process initiated in background.")
            else:
                st.warning("Worker started, but it stops at once outside the IST app window.")
        except Exception as e:
            st.error(f"Failed to start worker: {e}")

with col_c2:
    if paused_all:
        if st.button("▶️ Resume All Sending", type="primary", width="stretch"):
            set_app_state("paused_all", "0", conn)
            st.rerun()
    else:
        if st.button("⏸️ Pause All Sending", type="secondary", width="stretch"):
            set_app_state("paused_all", "1", conn)
            st.rerun()

with col_c3:
    if st.button("🔍 Run Detection Now", width="stretch"):
        st.info("Detection will run in Milestone 6.")

if paused_all:
    st.error("⏸️ **GLOBAL PAUSE IS ON** — Worker will not dispatch any emails while paused.")

windows = settings.worker
window_key = f"{windows.app_window_ist}{windows.recipient_window}"
thin_windows = load_thin_windows(conn, settings, window_key)
if thin_windows:
    details = ", ".join(f"{tz_name}: {count:,} leads" for tz_name, count in sorted(thin_windows.items()))
    st.warning(f"⏰ These queued leads will rarely or never send, because their working hours barely overlap the IST app window. {details}")

st.markdown("### 📊 Campaign Funnel")
# Aggregate lead status counts
status_counts = dict(conn.execute("SELECT status, count(*) FROM leads GROUP BY status").fetchall())
ist_midnight = datetime.datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
replies_today = conn.execute(
    "SELECT COUNT(*) FROM events WHERE type = 'reply' AND ts >= ?",
    (to_iso(ist_midnight),),
).fetchone()[0]

m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
m1.metric("Queued", status_counts.get("queued", 0))
m2.metric("In Sequence", status_counts.get("in_sequence", 0))
m3.metric("Replied", status_counts.get("replied", 0))
m4.metric("Unsubscribed", status_counts.get("unsubscribed", 0))
m5.metric("Bounced", status_counts.get("bounced", 0))
m6.metric("Finished", status_counts.get("finished", 0))
m7.metric("Replies Today", replies_today)

st.markdown("### 📬 Inboxes")
inboxes = conn.execute("SELECT * FROM inboxes ORDER BY id ASC").fetchall()
if not inboxes:
    st.info("No inboxes added yet. Head to the **Inboxes** page to connect an inbox.")
else:
    counted = "Dry Runs" if settings.mode.dry_run else "Sent"
    table_data = []
    for ib in inboxes:
        status_label = "Active" if ib["active"] and not ib["paused_reason"] else f"Paused: {ib['paused_reason']}"
        sent = sent_last_24h(ib["id"], conn, include_dry_run=settings.mode.dry_run)
        cap = todays_cap(ib, hard_daily_cap=settings.worker.hard_daily_cap)
        table_data.append({
            "Email": ib["email"],
            "Display Name": ib["display_name"],
            "Role": ib["role"],
            f"{counted} Last 24h / Cap": f"{sent} / {cap}",
            "Status": status_label,
            "Next Send After": ib["next_send_after"] or "Immediately / Ready",
        })
    st.dataframe(table_data, width="stretch")
