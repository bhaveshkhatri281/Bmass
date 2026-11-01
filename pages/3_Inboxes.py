"""Inboxes management: OAuth authentication, ramp caps, pause/resume, and test sending."""

import datetime
import sqlite3

import streamlit as st

from mailer.db import utc_now_iso
from mailer.gmail_client import connect_inbox, has_token, send_message
from mailer.scheduling import get_ramp_schedule, sent_last_24h, todays_cap
from mailer.ui import setup_page

conn, settings = setup_page("Inboxes")

# Refresh inbox list
inboxes = conn.execute("SELECT * FROM inboxes ORDER BY id ASC").fetchall()
unsub_alias = settings.sender.list_unsubscribe_address
unsub_inbox = settings.sender.unsubscribe_inbox

st.markdown("### 📬 Configured Inboxes")

if not unsub_inbox:
    st.warning(f"`sender.unsubscribe_inbox` is not set, so no inbox is marked as receiving `{unsub_alias}`.")
elif unsub_inbox not in [ib["email"].lower() for ib in inboxes]:
    st.warning(f"`{unsub_alias}` is delivered to `{unsub_inbox}`, but that inbox has not been added yet.")

if not inboxes:
    st.info("No inboxes configured yet. Fill out the form below to add and connect your first Google Workspace inbox.")
else:
    for ib in inboxes:
        ib_id = ib["id"]
        ib_email = ib["email"]
        token_connected = has_token(ib_email)
        today_cap = todays_cap(ib, hard_daily_cap=settings.worker.hard_daily_cap)
        sent_24 = sent_last_24h(ib_id, conn=conn, include_dry_run=settings.mode.dry_run)

        is_unsub_alias = ib_email.lower() == unsub_inbox

        # Card container for each inbox
        with st.container(border=True):
            col_hdr1, col_hdr2 = st.columns([3, 1])
            with col_hdr1:
                title_badge = f"**{ib['display_name']}** (`{ib_email}`)"
                if is_unsub_alias:
                    title_badge += f"  🏷️ *Receives {unsub_alias}*"
                st.markdown(title_badge)
                if ib["sender_title"]:
                    st.caption(f"Title: {ib['sender_title']} | Role: `{ib['role']}`")
                else:
                    st.caption(f"Role: `{ib['role']}`")

            with col_hdr2:
                if token_connected:
                    st.success("🟢 Connected")
                else:
                    st.error("🔴 Auth Required")

            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            col_m1.metric("Today's Cap", f"{today_cap} emails")
            col_m2.metric("Sent Last 24h", f"{sent_24} emails")
            col_m3.metric("Max Cap", f"{ib['max_cap']} emails")
            status_text = "Active" if ib["active"] and not ib["paused_reason"] else f"Paused: {ib['paused_reason']}"
            col_m4.metric("Status", status_text)

            # Action buttons
            col_btn1, col_btn2, col_btn3 = st.columns(3)

            with col_btn1:
                btn_label = "🔗 Connect Account" if not token_connected else "🔄 Reconnect Account"
                if st.button(btn_label, key=f"auth_{ib_id}", width="stretch"):
                    with st.spinner(f"Opening Google OAuth flow for {ib_email}..."):
                        try:
                            verified = connect_inbox(ib_email)
                            st.success(f"Successfully authenticated as {verified}!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"OAuth connection failed: {e}")

            with col_btn2:
                if ib["active"] and not ib["paused_reason"]:
                    if st.button("⏸️ Pause Inbox", key=f"pause_{ib_id}", width="stretch"):
                        conn.execute("UPDATE inboxes SET paused_reason = 'Manually paused' WHERE id = ?", (ib_id,))
                        conn.commit()
                        st.rerun()
                else:
                    if st.button("▶️ Resume Inbox", key=f"resume_{ib_id}", width="stretch"):
                        conn.execute("UPDATE inboxes SET paused_reason = NULL, paused_until = NULL WHERE id = ?", (ib_id,))
                        conn.commit()
                        st.rerun()

            with col_btn3:
                with st.expander("⚙️ Ramp & Settings", expanded=False):
                    with st.form(key=f"edit_ramp_{ib_id}"):
                        edit_disp = st.text_input("Display Name", value=ib["display_name"])
                        edit_title = st.text_input("Sender Title", value=ib["sender_title"] or "")
                        edit_start_cap = st.number_input("Ramp Start Cap", min_value=1, max_value=500, value=int(ib["ramp_start_cap"]))
                        edit_inc = st.number_input("Ramp Increment", min_value=0, max_value=100, value=int(ib["ramp_increment"]))
                        edit_every = st.number_input("Ramp Every (Days)", min_value=1, max_value=30, value=int(ib["ramp_every_days"]))
                        edit_max = st.number_input(
                            "Max Cap",
                            min_value=1,
                            max_value=settings.worker.hard_daily_cap,
                            value=min(int(ib["max_cap"]), settings.worker.hard_daily_cap),
                        )
                        save_ramp = st.form_submit_button("Update Settings")
                        if save_ramp:
                            conn.execute(
                                """UPDATE inboxes SET display_name = ?, sender_title = ?, ramp_start_cap = ?,
                                   ramp_increment = ?, ramp_every_days = ?, max_cap = ? WHERE id = ?""",
                                (edit_disp, edit_title, edit_start_cap, edit_inc, edit_every, edit_max, ib_id),
                            )
                            conn.commit()
                            st.success("Inbox settings updated.")
                            st.rerun()

                    st.markdown("**30-Day Ramp Schedule Projection:**")
                    sched = get_ramp_schedule(ib, days=30, hard_daily_cap=settings.worker.hard_daily_cap)
                    st.dataframe(sched, width="stretch", height=200)

st.markdown("---")

# ==============================================================================
# Send Test to Myself
# ==============================================================================
st.markdown("### 🧪 Send Test Email to Myself")
st.write("Send a live verification email through any authenticated inbox (logged as `result='test'`, bypasses dry-run, does not increment daily caps).")

connected_inboxes = [ib for ib in inboxes if has_token(ib["email"])]

if not connected_inboxes:
    st.warning("No authenticated inboxes available. Please add and connect an inbox first.")
else:
    inbox_map = {f"{ib['display_name']} ({ib['email']})": ib for ib in connected_inboxes}
    chosen_label = st.selectbox("Select Sending Inbox", list(inbox_map.keys()))
    chosen_inbox = inbox_map[chosen_label]

    col_t1, col_t2 = st.columns([1, 1])
    with col_t1:
        test_to = st.text_input("Recipient Email", placeholder="your-email@example.com")
        test_subj = st.text_input("Subject", value="Test from Mobisa Mailer")
    with col_t2:
        test_body = st.text_area(
            "Plain Text Body",
            value="Hi,\n\nThis is a verification test email from Mobisa Mailer to confirm Google Workspace Gmail API authentication and delivery.\n\nBest,\nMobisa Team",
            height=120,
        )

    if st.button("🚀 Send Test Email", type="primary"):
        if not test_to or "@" not in test_to:
            st.error("Please enter a valid recipient email address.")
        else:
            with st.spinner("Dispatching test email via Gmail API..."):
                try:
                    res = send_message(
                        email_addr=chosen_inbox["email"],
                        to=test_to.strip(),
                        subject=test_subj,
                        body=test_body,
                        display_name=chosen_inbox["display_name"],
                        list_unsubscribe=settings.sender.list_unsubscribe_address,
                    )
                    now_iso = utc_now_iso()
                    # Log to send_log
                    conn.execute(
                        """INSERT INTO send_log (ts, inbox_id, lead_id, lead_email, step_no, subject, gmail_message_id, thread_id, result, error_text)
                           VALUES (?, ?, NULL, ?, 1, ?, ?, ?, 'test', NULL)""",
                        (now_iso, chosen_inbox["id"], test_to.strip(), test_subj, res.gmail_message_id, res.thread_id),
                    )
                    conn.commit()
                    st.success(f"✅ Test email successfully sent! (Gmail ID: `{res.gmail_message_id}`, Thread ID: `{res.thread_id}`)")
                except Exception as e:
                    err_msg = str(e)
                    conn.execute(
                        """INSERT INTO send_log (ts, inbox_id, lead_id, lead_email, step_no, subject, gmail_message_id, thread_id, result, error_text)
                           VALUES (?, ?, NULL, ?, 1, ?, NULL, NULL, 'error', ?)""",
                        (utc_now_iso(), chosen_inbox["id"], test_to.strip(), test_subj, f"Test Send Error: {err_msg}"),
                    )
                    conn.commit()
                    st.error(f"Failed to send test email: {err_msg}")

st.markdown("---")

# ==============================================================================
# Add New Inbox Form
# ==============================================================================
st.markdown("### ➕ Add New Inbox")

with st.form("add_inbox_form", clear_on_submit=True):
    col_a1, col_a2 = st.columns(2)
    with col_a1:
        new_email = st.text_input("Google Workspace Email Address", placeholder="name@yourdomain.com")
        new_disp = st.text_input("Display Name (shown in From header)", value=settings.sender.default_display_name)
        new_title = st.text_input("Sender Title (optional)", placeholder="e.g. Founder & Creative Director")
        new_role = st.selectbox("Inbox Role", ["send", "monitor"], help="'send' dispatches sequences; 'monitor' only reads incoming emails (e.g. unsubscribe alias)")

    with col_a2:
        new_ramp_start = st.date_input("Ramp Start Date", value=datetime.date.today())
        new_start_cap = st.number_input("Ramp Starting Cap (emails/day)", min_value=1, max_value=100, value=10)
        new_inc = st.number_input("Ramp Increment (emails added)", min_value=0, max_value=50, value=5)
        new_every = st.number_input("Ramp Interval (every N days)", min_value=1, max_value=14, value=3)
        new_max_cap = st.number_input("Maximum Daily Cap", min_value=1, max_value=settings.worker.hard_daily_cap, value=80)

    submitted = st.form_submit_button("Add Inbox", type="primary")
    if submitted:
        if not new_email or "@" not in new_email:
            st.error("Please provide a valid email address.")
        else:
            clean_email = new_email.strip().lower()
            existing = conn.execute("SELECT id FROM inboxes WHERE lower(email) = lower(?)", (clean_email,)).fetchone()
            if existing:
                st.error(f"An inbox with email '{clean_email}' already exists.")
            else:
                now_iso = utc_now_iso()
                conn.execute(
                    """INSERT INTO inboxes (email, display_name, sender_title, role, active, ramp_start_date,
                                           ramp_start_cap, ramp_increment, ramp_every_days, max_cap, created_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                    (
                        clean_email,
                        new_disp.strip(),
                        new_title.strip(),
                        new_role,
                        new_ramp_start.isoformat(),
                        new_start_cap,
                        new_inc,
                        new_every,
                        new_max_cap,
                        now_iso,
                    ),
                )
                conn.commit()
                st.success(f"Inbox '{clean_email}' added successfully! You can now click 'Connect Account' to authenticate via OAuth.")
                st.rerun()
