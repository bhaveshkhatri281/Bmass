"""Replies page: review incoming replies and opt-outs, open Gmail threads, and mark handled."""

import math

import pandas as pd
import streamlit as st

from mailer.db import set_events_handled
from mailer.ui import setup_page

PAGE_SIZE = 100

conn, settings = setup_page("Replies & Opt-outs")

st.markdown("### 💬 Detected Replies and Unsubscribes")

total = conn.execute("SELECT COUNT(*) FROM events WHERE type IN ('reply', 'unsubscribe')").fetchone()[0]
page_count = max(1, math.ceil(total / PAGE_SIZE))
page = st.number_input(f"Page (of {page_count})", min_value=1, max_value=page_count, value=1)

events = conn.execute(
    """SELECT e.id, e.ts, e.type, e.email, e.snippet, e.subject, e.handled, e.gmail_message_id,
              l.business_name, l.country, l.thread_id, i.email AS inbox_email
       FROM events e
       LEFT JOIN leads l ON l.id = e.lead_id
       LEFT JOIN inboxes i ON i.id = e.inbox_id
       WHERE e.type IN ('reply', 'unsubscribe')
       ORDER BY e.id DESC LIMIT ? OFFSET ?""",
    (PAGE_SIZE, (page - 1) * PAGE_SIZE),
).fetchall()

if not events:
    st.info("No reply or unsubscribe events detected yet.")
else:
    table = pd.DataFrame([
        {
            "id": event["id"],
            "Handled": bool(event["handled"]),
            "Type": event["type"],
            "Lead": event["business_name"],
            "Email": event["email"],
            "Country": event["country"],
            "Snippet": event["snippet"],
            "Time": event["ts"],
            "Open": f"https://mail.google.com/mail/u/{event['inbox_email']}/#all/{event['thread_id'] or event['gmail_message_id']}",
        }
        for event in events
    ])
    edited = st.data_editor(
        table,
        column_config={
            "id": None,
            "Open": st.column_config.LinkColumn("Open", display_text="Open in Gmail"),
        },
        disabled=[column for column in table.columns if column != "Handled"],
        hide_index=True,
        width="stretch",
        key="events_editor",
    )
    if st.button("💾 Save handled"):
        set_events_handled(dict(zip(edited["id"].tolist(), edited["Handled"].tolist())), conn)
        st.success("Saved.")

with st.expander("📭 Bounces and ignored auto-replies"):
    others = conn.execute(
        """SELECT ts, type, email, subject FROM events
           WHERE type IN ('bounce', 'auto_reply_ignored') ORDER BY id DESC LIMIT 100"""
    ).fetchall()
    if others:
        st.dataframe([dict(row) for row in others], width="stretch")
    else:
        st.write("None yet.")

with st.expander("🚫 Suppression list"):
    count = conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0]
    st.write(f"**{count:,}** addresses will never be emailed.")
    rows = conn.execute("SELECT email, reason, added_at FROM suppression ORDER BY added_at DESC LIMIT 100").fetchall()
    if rows:
        st.dataframe([dict(row) for row in rows], width="stretch")
