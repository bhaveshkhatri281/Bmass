"""Campaign page: dry run toggle, sender details, schedule windows, and country rules."""

import streamlit as st

from mailer.ui import setup_page

conn, settings = setup_page("Campaign Settings")

st.markdown("### ⚙️ Sender & Operational Settings")
st.write(f"**Default Sender Name:** {settings.sender.default_display_name}")
st.write(f"**Business Address:** {settings.sender.business_address or '*(Not set - required before Live Mode)*'}")
st.write(f"**Unsubscribe Address:** {settings.sender.list_unsubscribe_address}")
st.write(f"**IST App Window:** {settings.worker.app_window_ist.start} — {settings.worker.app_window_ist.end}")
st.write(f"**Recipient Window:** {settings.worker.recipient_window.start} — {settings.worker.recipient_window.end} (Weekdays only: {settings.worker.recipient_window.weekdays_only})")

st.markdown("### 🌍 Country Timezone & Language Rules")
countries = conn.execute("SELECT * FROM country_settings ORDER BY sort_order ASC").fetchall()
st.dataframe([dict(c) for c in countries], use_container_width=True)

st.info("Interactive settings configuration and dry-run toggle will be active in Milestone 4.")

