"""Leads page: browse, filter, search, and manage individual lead records."""

import math

import streamlit as st

from mailer.db import (
    list_leads,
    remove_test_leads,
    reset_lead,
    set_custom_line,
    set_lead_status,
    suppress_lead,
)
from mailer.ui import setup_page

PAGE_SIZE = 100
ANY = "Any"
STATUSES = ["queued", "in_sequence", "replied", "unsubscribed", "bounced", "finished", "skipped"]

conn, settings = setup_page("Leads Directory")


def choice(value: str) -> str | None:
    return None if value == ANY else value


st.markdown("### 👥 Leads")

countries = [row[0] for row in conn.execute("SELECT DISTINCT country FROM leads ORDER BY country")]
qualities = [row[0] for row in conn.execute("SELECT DISTINCT quality FROM leads ORDER BY quality")]
inboxes = {row["email"]: row["id"] for row in conn.execute("SELECT id, email FROM inboxes ORDER BY id")}
steps = [str(row[0]) for row in conn.execute("SELECT DISTINCT step FROM leads ORDER BY step")]

col_f1, col_f2, col_f3 = st.columns(3)
status = col_f1.selectbox("Status", [ANY, *STATUSES])
country = col_f2.selectbox("Country", [ANY, *countries])
quality = col_f3.selectbox("Quality", [ANY, *qualities])
col_f4, col_f5, col_f6 = st.columns(3)
inbox = col_f4.selectbox("Inbox", [ANY, *inboxes])
step = col_f5.selectbox("Step", [ANY, *steps])
search = col_f6.text_input("Search email or name")
only_test = st.checkbox("Test leads only")

filters = {
    "status": choice(status),
    "country": choice(country),
    "quality": choice(quality),
    "inbox_id": inboxes.get(inbox),
    "step": choice(step),
    "search": search.strip(),
    "is_test": only_test,
}

_, total = list_leads(filters, 1, 0, conn)
page_count = max(1, math.ceil(total / PAGE_SIZE))
page = st.number_input(f"Page (of {page_count})", min_value=1, max_value=page_count, value=1)
rows, total = list_leads(filters, PAGE_SIZE, (page - 1) * PAGE_SIZE, conn)
st.write(f"**{total:,}** leads match.")

if not rows:
    st.info("No leads to show.")
else:
    st.dataframe(
        [
            {
                "ID": row["id"],
                "Email": row["email"],
                "Business": row["business_name"],
                "Country": row["country"],
                "Quality": row["quality"],
                "Status": row["status"],
                "Step": row["step"],
                "Next Due": row["next_due_at"],
                "Test": "Yes" if row["is_test"] else "",
            }
            for row in rows
        ],
        width="stretch",
    )

    st.markdown("### ✏️ Lead Actions")
    labels = {f"{row['id']} · {row['email']}": row for row in rows}
    picked = labels[st.selectbox("Lead", list(labels))]

    col_a1, col_a2, col_a3 = st.columns(3)
    if col_a1.button("⏭️ Skip", width="stretch"):
        set_lead_status(picked["id"], "skipped", conn)
        st.rerun()
    if col_a2.button("🚫 Add to suppression", width="stretch"):
        suppress_lead(picked["id"], conn)
        st.rerun()
    if col_a3.button("↩️ Reset to queued", width="stretch"):
        error = reset_lead(picked["id"], conn)
        if error:
            st.error(error)
        else:
            st.rerun()

    with st.form("custom_line_form"):
        custom_line = st.text_input("Custom line", value=picked["custom_line"] or "")
        if st.form_submit_button("💾 Save custom line"):
            set_custom_line(picked["id"], custom_line, conn)
            st.success("Custom line saved.")

test_count = conn.execute("SELECT COUNT(*) FROM leads WHERE is_test = 1").fetchone()[0]
if test_count:
    st.markdown("### 🧪 Test Leads")
    st.write(f"{test_count} test leads. Removing them also clears their log rows and suppression entries, so you can test again. Real leads are not touched.")
    if st.button("🗑️ Remove all test leads"):
        remove_test_leads(conn)
        st.rerun()
