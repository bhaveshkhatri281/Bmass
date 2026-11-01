"""Log page: view send_log events, filter history, and export reports to Excel."""

import datetime
import math

import streamlit as st

from mailer.db import list_send_log, to_iso
from mailer.exporter import export_leads, export_send_log
from mailer.scheduling import IST
from mailer.ui import setup_page

PAGE_SIZE = 100
ANY = "Any"
RESULTS = ["sent", "dry_run", "error", "test"]

conn, settings = setup_page("Send Log & Exports")


def ist_midnight(day: datetime.date) -> str:
    return to_iso(datetime.datetime.combine(day, datetime.time(0), tzinfo=IST))


st.markdown("### 📋 Dispatch Log")

inboxes = {row["email"]: row["id"] for row in conn.execute("SELECT id, email FROM inboxes ORDER BY id")}
today = datetime.datetime.now(IST).date()

col_f1, col_f2, col_f3, col_f4 = st.columns(4)
inbox = col_f1.selectbox("Inbox", [ANY, *inboxes])
result = col_f2.selectbox("Result", [ANY, *RESULTS])
date_from = col_f3.date_input("From (IST)", value=today - datetime.timedelta(days=30))
date_to = col_f4.date_input("To (IST)", value=today)

filters = {
    "inbox_id": inboxes.get(inbox),
    "result": None if result == ANY else result,
    "date_from": ist_midnight(date_from),
    "date_to": ist_midnight(date_to + datetime.timedelta(days=1)),
}

_, total = list_send_log(filters, 1, 0, conn)
page_count = max(1, math.ceil(total / PAGE_SIZE))
page = st.number_input(f"Page (of {page_count})", min_value=1, max_value=page_count, value=1)
rows, total = list_send_log(filters, PAGE_SIZE, (page - 1) * PAGE_SIZE, conn)
st.write(f"**{total:,}** rows match.")

if not rows:
    st.info("No send attempts match these filters.")
else:
    st.dataframe([dict(row) for row in rows], width="stretch")

st.markdown("### 📤 Export to Excel")
col_e1, col_e2 = st.columns(2)
if col_e1.button("📤 Export send log", width="stretch"):
    st.success(f"Saved to {export_send_log(conn, filters)}")
if col_e2.button("📤 Export leads", width="stretch"):
    st.success(f"Saved to {export_leads(conn)}")
