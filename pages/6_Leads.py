"""Leads page: browse, filter, search, and manage individual lead records."""

import streamlit as st

from mailer.ui import setup_page

conn, settings = setup_page("Leads Directory")

st.markdown("### 👥 Leads")
count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
st.write(f"Total leads in database: **{count:,}**")

if count == 0:
    st.info("No leads in the database. Go to the **Import** page to import leads.")
else:
    leads = conn.execute("SELECT id, email, business_name, country, status, step, next_due_at FROM leads LIMIT 100").fetchall()
    st.dataframe([dict(l) for l in leads], width="stretch")

st.info("Lead filtering, pagination (100 rows/page), suppression actions, and custom line editing will be complete in Milestone 7.")

