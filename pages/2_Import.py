"""Import page: upload Excel/CSV, column mapping, and import history."""

import streamlit as st

from mailer.ui import setup_page

conn, settings = setup_page("Import Leads")

st.markdown("### 📥 Import Leads from Excel or CSV")
st.info("Lead file import and column mapping will be implemented in Milestone 3.")

st.markdown("### 📜 Import History")
imports = conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 20").fetchall()
if not imports:
    st.write("No imports recorded yet.")
else:
    st.dataframe([dict(row) for row in imports], use_container_width=True)

