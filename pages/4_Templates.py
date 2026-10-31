"""Templates page: edit templates per step and language, preview, test send, and copy tables."""

import pandas as pd
import streamlit as st

from mailer.db import get_template, utc_now_iso
from mailer.gmail_client import has_token, send_message
from mailer.templating import (
    ALLOWED_FIELDS,
    TemplateError,
    add_language,
    add_step,
    has_link,
    preview_random_lead,
    remove_last_step,
    render,
    save_category_phrases,
    save_footer,
    save_steps,
    save_template,
)
from mailer.ui import setup_page

conn, settings = setup_page("Templates & Copy")

steps = conn.execute("SELECT * FROM steps ORDER BY step_no ASC").fetchall()
langs = conn.execute("SELECT * FROM languages ORDER BY code ASC").fetchall()


def show_errors(errors: list[str]) -> None:
    for error in errors:
        st.error(error)


def template_editor(step_no: int, language: str) -> None:
    template = get_template(step_no, language, conn)
    with st.form(f"template_{step_no}_{language}"):
        subject = st.text_input(
            "Subject",
            value=template["subject"] if template else "",
            disabled=step_no > 1,
            help="Follow-ups use Re: and the step 1 subject." if step_no > 1 else None,
        )
        body = st.text_area("Body", value=template["body"] if template else "", height=220)
        saved = st.form_submit_button("💾 Save")

    if saved:
        errors = save_template(step_no, language, subject, body, conn)
        show_errors(errors)
        if not errors:
            st.success("Template saved.")
            if step_no == 1 and has_link(body):
                st.warning("This step 1 body contains a link, which can hurt deliverability on a first email.")


st.markdown("### ✉️ Email Templates")
st.caption(
    "Merge fields: " + ", ".join(f"`{{{{{name}}}}}`" for name in ALLOWED_FIELDS)
    + ". Spintax: `{option a|option b}`. The footer is added automatically."
)

for step_tab, step in zip(st.tabs([f"Step {s['step_no']}" for s in steps]), steps):
    with step_tab:
        for lang_tab, lang in zip(st.tabs([f"{l['name']} ({l['code']})" for l in langs]), langs):
            with lang_tab:
                template_editor(step["step_no"], lang["code"])

st.markdown("---")
st.markdown("### 👁️ Preview")

col_p1, col_p2 = st.columns(2)
preview_step = col_p1.selectbox("Step", [s["step_no"] for s in steps], key="preview_step")
preview_language = col_p2.selectbox("Language", [l["code"] for l in langs], key="preview_language")

send_inboxes = conn.execute("SELECT * FROM inboxes WHERE role = 'send' ORDER BY id ASC").fetchall()
connected_inboxes = [ib for ib in send_inboxes if has_token(ib["email"])]
sender = connected_inboxes[0] if connected_inboxes else None
if len(connected_inboxes) > 1:
    inbox_map = {f"{ib['display_name']} ({ib['email']})": ib for ib in connected_inboxes}
    sender = inbox_map[st.selectbox("Sending inbox", list(inbox_map.keys()))]

preview_key = (preview_step, preview_language)
if st.button("🎲 New random lead") or st.session_state.get("preview_key") != preview_key:
    st.session_state["preview_key"] = preview_key
    st.session_state["preview_lead_id"] = None
    try:
        picked = preview_random_lead(preview_step, preview_language, settings, sender, conn)
        if picked is not None:
            st.session_state["preview_lead_id"] = picked.lead["id"]
    except TemplateError as e:
        st.error(str(e))

lead_id = st.session_state.get("preview_lead_id")
lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone() if lead_id else None

if lead is None:
    st.info(f"No {preview_language} leads to preview. Import leads first.")
else:
    try:
        rendered = render(lead, preview_step, sender, settings, conn)
    except TemplateError as e:
        rendered = None
        st.error(str(e))

    if rendered is not None:
        st.caption(f"{lead['business_name']} · {lead['email']} · {lead['country']} · {lead['category']}")
        st.markdown(f"**Subject:** {rendered.subject}")
        st.code(rendered.body, language=None, wrap_lines=True)
        for warning in rendered.warnings:
            st.warning(warning)

        st.markdown("#### 🧪 Send test to myself")
        st.write("Sends this rendered email for real, even in dry run. It is logged as `result='test'` and does not count against caps.")
        if sender is None:
            st.warning("No connected inbox. Connect one on the Inboxes page.")
        else:
            test_to = st.text_input("Send to", placeholder="your-email@example.com", key="template_test_to")
            if st.button("🚀 Send Test", type="primary"):
                if "@" not in test_to:
                    st.error("Please enter a valid email address.")
                else:
                    try:
                        res = send_message(
                            email_addr=sender["email"],
                            to=test_to.strip(),
                            subject=rendered.subject,
                            body=rendered.body,
                            display_name=sender["display_name"],
                            list_unsubscribe=settings.sender.list_unsubscribe_address,
                        )
                        conn.execute(
                            """INSERT INTO send_log (ts, inbox_id, lead_id, lead_email, step_no, subject, gmail_message_id, thread_id, result, error_text)
                               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'test', NULL)""",
                            (utc_now_iso(), sender["id"], test_to.strip(), preview_step, rendered.subject, res.gmail_message_id, res.thread_id),
                        )
                        conn.commit()
                        st.success(f"✅ Test email sent to {test_to.strip()}.")
                    except Exception as e:
                        conn.execute(
                            """INSERT INTO send_log (ts, inbox_id, lead_id, lead_email, step_no, subject, gmail_message_id, thread_id, result, error_text)
                               VALUES (?, ?, NULL, ?, ?, ?, NULL, NULL, 'error', ?)""",
                            (utc_now_iso(), sender["id"], test_to.strip(), preview_step, rendered.subject, f"Test Send Error: {e}"),
                        )
                        conn.commit()
                        st.error(f"Failed to send test email: {e}")

st.markdown("---")

with st.expander("🔢 Sequence steps"):
    st.write("`delay_days` counts from the previous step, so 0, 3, 4 means day 0, day 3, day 7.")
    edited_steps = st.data_editor(
        pd.DataFrame(
            [{"step_no": s["step_no"], "delay_days": s["delay_days"], "active": bool(s["active"])} for s in steps]
        ),
        disabled=["step_no"],
        hide_index=True,
        key="steps_editor",
    )
    col_s1, col_s2, col_s3 = st.columns(3)
    if col_s1.button("💾 Save steps", width="stretch"):
        errors = save_steps(edited_steps.to_dict("records"), conn)
        show_errors(errors)
        if not errors:
            st.success("Steps saved.")
    if col_s2.button("➕ Add step", width="stretch"):
        add_step(conn)
        st.rerun()
    if col_s3.button("➖ Remove last step", width="stretch"):
        error = remove_last_step(conn)
        if error:
            st.error(error)
        else:
            st.rerun()

with st.expander("🌐 Languages & footers"):
    for lang in langs:
        with st.form(f"footer_{lang['code']}"):
            footer = st.text_area(f"{lang['name']} ({lang['code']}) footer", value=lang["footer_text"], height=80)
            if st.form_submit_button("💾 Save footer"):
                errors = save_footer(lang["code"], footer, conn)
                show_errors(errors)
                if not errors:
                    st.success("Footer saved.")

    with st.form("add_language", clear_on_submit=True):
        st.markdown("**Add language**")
        col_l1, col_l2 = st.columns(2)
        new_code = col_l1.text_input("Code", placeholder="es")
        new_name = col_l2.text_input("Name", placeholder="Spanish")
        new_footer = st.text_area("Footer", height=80)
        if st.form_submit_button("➕ Add language"):
            errors = add_language(new_code, new_name, new_footer, conn)
            show_errors(errors)
            if not errors:
                st.rerun()

with st.expander("🏷️ Category phrases"):
    st.write("Matched on the lowercased raw category, then on the part before the first `;`.")
    phrases = conn.execute("SELECT raw, phrase_en, phrase_fr FROM category_phrases ORDER BY raw ASC").fetchall()
    edited_phrases = st.data_editor(
        pd.DataFrame([dict(p) for p in phrases], columns=["raw", "phrase_en", "phrase_fr"]),
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key="phrases_editor",
    )
    if st.button("💾 Save phrases"):
        errors = save_category_phrases(edited_phrases.to_dict("records"), conn)
        show_errors(errors)
        if not errors:
            st.success("Category phrases saved.")
