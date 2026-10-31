"""Replies page: review incoming replies and opt-outs, open Gmail threads, and mark handled."""

import streamlit as st

from mailer.ui import setup_page

conn, settings = setup_page("Replies & Opt-outs")

st.markdown("### 💬 Detected Replies and Unsubscribes")
events = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50").fetchall()
if not events:
    st.info("No reply or unsubscribe events detected yet.")
else:
    st.dataframe([dict(e) for e in events], use_container_width=True)

st.info("Automatic reply classification, one-click Gmail thread navigation, and handled status toggling will be active in Milestone 6.")

