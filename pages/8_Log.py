"""Log page: view send_log events, filter history, and export reports to Excel."""

import streamlit as st

from mailer.ui import setup_page

conn, settings = setup_page("Send Log & Exports")

st.markdown("### 📋 Dispatch Log")
logs = conn.execute("SELECT * FROM send_log ORDER BY id DESC LIMIT 100").fetchall()
if not logs:
    st.info("No send attempts logged yet.")
else:
    st.dataframe([dict(l) for l in logs], width="stretch")

st.info("Log filtering, date ranges, and Excel export buttons will be fully enabled in Milestone 7.")

