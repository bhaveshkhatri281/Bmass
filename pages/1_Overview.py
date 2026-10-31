"""Overview page: inboxes summary, lead funnel metrics, and operational controls."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import streamlit as st

from mailer.config import PROJECT_ROOT
from mailer.db import get_app_state, set_app_state
from mailer.ui import setup_page

conn, settings = setup_page("Overview")

# Top controls: Start Worker, Pause All/Resume All, Run Detection Now
col_c1, col_c2, col_c3 = st.columns(3)

paused_all = get_app_state("paused_all", "0", conn) == "1"

with col_c1:
    if st.button("🚀 Start Worker Now", use_container_width=True):
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
            st.success("Worker process initiated in background.")
        except Exception as e:
            st.error(f"Failed to start worker: {e}")

with col_c2:
    if paused_all:
        if st.button("▶️ Resume All Sending", type="primary", use_container_width=True):
            set_app_state("paused_all", "0", conn)
            st.rerun()
    else:
        if st.button("⏸️ Pause All Sending", type="secondary", use_container_width=True):
            set_app_state("paused_all", "1", conn)
            st.rerun()

with col_c3:
    if st.button("🔍 Run Detection Now", use_container_width=True):
        st.info("Detection will run in Milestone 6.")

if paused_all:
    st.error("⏸️ **GLOBAL PAUSE IS ON** — Worker will not dispatch any emails while paused.")

st.markdown("### 📊 Campaign Funnel")
# Aggregate lead status counts
status_counts = dict(conn.execute("SELECT status, count(*) FROM leads GROUP BY status").fetchall())
total_queued = status_counts.get("queued", 0)
total_in_seq = status_counts.get("in_sequence", 0)
total_replied = status_counts.get("replied", 0)
total_unsub = status_counts.get("unsubscribed", 0)
total_bounced = status_counts.get("bounced", 0)
total_finished = status_counts.get("finished", 0)

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Queued", total_queued)
m2.metric("In Sequence", total_in_seq)
m3.metric("Replied", total_replied)
m4.metric("Unsubscribed", total_unsub)
m5.metric("Bounced", total_bounced)
m6.metric("Finished", total_finished)

st.markdown("### 📬 Inboxes")
inboxes = conn.execute("SELECT * FROM inboxes ORDER BY id ASC").fetchall()
if not inboxes:
    st.info("No inboxes added yet. Head to the **Inboxes** page to connect an inbox.")
else:
    table_data = []
    for ib in inboxes:
        status_label = "Active" if ib["active"] and not ib["paused_reason"] else f"Paused: {ib['paused_reason']}"
        table_data.append({
            "Email": ib["email"],
            "Display Name": ib["display_name"],
            "Role": ib["role"],
            "Status": status_label,
            "Next Send After": ib["next_send_after"] or "Immediately / Ready",
        })
    st.dataframe(table_data, use_container_width=True)

