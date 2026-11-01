"""Campaign page: dry run toggle, sender details, schedule windows, and country rules."""

import pandas as pd
import streamlit as st

from mailer.config import ConfigError, live_blockers, save_settings
from mailer.engine import LIVE_SENDING_READY
from mailer.scheduling import save_country_settings
from mailer.ui import setup_page

conn, settings = setup_page("Campaign Settings")


def save_and_report(message: str) -> None:
    try:
        save_settings(settings)
    except ConfigError as e:
        st.error(str(e))
    else:
        st.toast(message)
        st.rerun()


@st.dialog("Switch to LIVE?")
def confirm_live() -> None:
    st.write("The worker will send **real emails** to real leads. This cannot be undone once they are sent.")
    col_yes, col_no = st.columns(2)
    if col_yes.button("Yes, go LIVE", type="primary", width="stretch"):
        settings.mode.dry_run = False
        save_settings(settings)
        st.rerun()
    if col_no.button("Cancel", width="stretch"):
        st.rerun()


st.markdown("### 🚦 Mode")
if settings.mode.dry_run:
    st.write("**DRY RUN** is on. The worker only logs what it would send.")
    if st.button("Switch to LIVE"):
        blockers = live_blockers(settings)
        for blocker in blockers:
            st.error(f"Cannot go LIVE: {blocker}")
        if not blockers:
            confirm_live()
else:
    st.write("**LIVE** is on.")
    if st.button("Back to Dry Run", type="primary"):
        settings.mode.dry_run = True
        save_and_report("Back in dry run.")

if not LIVE_SENDING_READY:
    st.info("Live sending is not built yet (Milestone 5). The worker sends nothing in LIVE mode until then.")

st.markdown("### ⚙️ Sender")
with st.form("sender_form"):
    sender = settings.sender
    col_s1, col_s2 = st.columns(2)
    display_name = col_s1.text_input("Default display name", value=sender.default_display_name)
    sender_title = col_s2.text_input("Sender title", value=sender.sender_title)
    website = col_s1.text_input("Website", value=sender.website)
    booking_link = col_s2.text_input("Booking link", value=sender.booking_link)
    portfolio_link = col_s1.text_input("Portfolio link", value=sender.portfolio_link)
    business_address = col_s2.text_input("Business address", value=sender.business_address)
    unsubscribe_address = col_s1.text_input("Unsubscribe address", value=sender.list_unsubscribe_address)
    unsubscribe_inbox = col_s2.text_input("Inbox receiving the unsubscribe address", value=sender.unsubscribe_inbox)
    if st.form_submit_button("💾 Save sender"):
        if not settings.mode.dry_run and not business_address.strip():
            st.error("Business address cannot be empty in LIVE mode.")
        else:
            sender.default_display_name = display_name.strip()
            sender.sender_title = sender_title.strip()
            sender.website = website.strip()
            sender.booking_link = booking_link.strip()
            sender.portfolio_link = portfolio_link.strip()
            sender.business_address = business_address.strip()
            sender.list_unsubscribe_address = unsubscribe_address.strip()
            sender.unsubscribe_inbox = unsubscribe_inbox.strip().lower()
            save_and_report("Sender saved.")

st.markdown("### ⏱️ Worker Windows, Delays & Caps")
with st.form("worker_form"):
    worker = settings.worker
    col_w1, col_w2 = st.columns(2)
    app_start = col_w1.text_input("App window start (IST, HH:MM)", value=worker.app_window_ist.start)
    app_end = col_w2.text_input("App window end (IST, HH:MM)", value=worker.app_window_ist.end)
    recipient_start = col_w1.text_input("Recipient window start (local, HH:MM)", value=worker.recipient_window.start)
    recipient_end = col_w2.text_input("Recipient window end (local, HH:MM)", value=worker.recipient_window.end)
    weekdays_only = st.checkbox("Weekdays only", value=worker.recipient_window.weekdays_only)
    delay_min = col_w1.number_input("Min seconds between sends", min_value=0, value=worker.delay_between_sends_seconds.min)
    delay_max = col_w2.number_input("Max seconds between sends", min_value=0, value=worker.delay_between_sends_seconds.max)
    hard_cap = col_w1.number_input("Hard daily cap per inbox", min_value=1, value=worker.hard_daily_cap)
    if st.form_submit_button("💾 Save worker settings"):
        worker.app_window_ist.start = app_start.strip()
        worker.app_window_ist.end = app_end.strip()
        worker.recipient_window.start = recipient_start.strip()
        worker.recipient_window.end = recipient_end.strip()
        worker.recipient_window.weekdays_only = weekdays_only
        worker.delay_between_sends_seconds.min = int(delay_min)
        worker.delay_between_sends_seconds.max = int(delay_max)
        worker.hard_daily_cap = int(hard_cap)
        save_and_report("Worker settings saved.")

st.markdown("### 🌍 Country Timezone & Language Rules")
st.write("Language applies to future imports. Leads already imported keep theirs.")
countries = conn.execute("SELECT country, language, timezone, skip, sort_order FROM country_settings ORDER BY sort_order ASC").fetchall()
edited_countries = st.data_editor(
    pd.DataFrame([{**dict(c), "skip": bool(c["skip"])} for c in countries]),
    num_rows="dynamic",
    hide_index=True,
    width="stretch",
    key="countries_editor",
)
if st.button("💾 Save countries"):
    errors = save_country_settings(edited_countries.to_dict("records"), conn)
    for error in errors:
        st.error(error)
    if not errors:
        st.success("Countries saved.")
