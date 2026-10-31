"""Mobisa Mailer Streamlit entry point: page navigation."""

import streamlit as st

st.set_page_config(
    page_title="Mobisa Mailer",
    page_icon="✉️",
    layout="wide",
)

try:
    st.switch_page("pages/1_Overview.py")
except Exception:
    st.title("✉️ Mobisa Mailer")
    st.info("Welcome to Mobisa Mailer. Select a page from the sidebar to continue.")

