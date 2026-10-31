"""Templates page: edit sequence steps, templates per language, category phrases, and preview."""

import streamlit as st

from mailer.ui import setup_page

conn, settings = setup_page("Templates & Copy")

st.markdown("### 📝 Sequence Steps & Languages")
steps = conn.execute("SELECT * FROM steps ORDER BY step_no ASC").fetchall()
langs = conn.execute("SELECT * FROM languages ORDER BY code ASC").fetchall()

col1, col2 = st.columns(2)
with col1:
    st.markdown("**Sequence Steps**")
    st.dataframe([dict(s) for s in steps], use_container_width=True)
with col2:
    st.markdown("**Languages & Footers**")
    st.dataframe([dict(l) for l in langs], use_container_width=True)

st.markdown("### ✉️ Email Templates")
templates = conn.execute("SELECT * FROM templates ORDER BY step_no ASC, language ASC").fetchall()
st.dataframe([dict(t) for t in templates], use_container_width=True)

st.info("Template editing, variable merge check, spintax preview, and test sending will be enabled in Milestone 3.")

