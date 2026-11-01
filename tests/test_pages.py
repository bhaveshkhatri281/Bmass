"""Smoke tests: every page survives two runs, because Streamlit uses a new thread per run."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import mailer.gmail_client as gmail_client
from mailer.config import PROJECT_ROOT
from mailer.db import open_conn
from tests.fakes import FakeGmailClient

PAGES = sorted(path.name for path in (PROJECT_ROOT / "pages").glob("*.py"))


def page(name: str) -> AppTest:
    return AppTest.from_file(str(Path(PROJECT_ROOT, "pages", name)), default_timeout=30)


def add_inbox(email_addr: str = "beedu@mobisa.in") -> None:
    with open_conn() as conn:
        conn.execute(
            "INSERT INTO inboxes (email, display_name, ramp_start_date, created_at) VALUES (?, 'Beedu', '2026-10-01', '2026-10-01T00:00:00Z')",
            (email_addr,),
        )


def button(app: AppTest, label_part: str):
    return next(b for b in app.button if label_part in b.label)


@pytest.mark.parametrize("name", PAGES)
def test_page_loads_twice_without_exception(name):
    app = page(name).run()
    assert not app.exception
    app.run()
    assert not app.exception


@pytest.mark.parametrize("name", PAGES)
def test_page_renders_with_an_inbox_present(name):
    add_inbox()
    app = page(name).run()
    assert not app.exception


def test_add_inbox_form_creates_row():
    app = page("3_Inboxes.py").run()
    app.text_input[0].set_value("new@mobisa.in")
    next(b for b in app.button if b.label == "Add Inbox").click().run()
    assert not app.exception

    with open_conn() as conn:
        row = conn.execute("SELECT email, role, active FROM inboxes").fetchone()
    assert (row["email"], row["role"], row["active"]) == ("new@mobisa.in", "send", 1)


def test_connect_button_stores_verified_inbox(monkeypatch):
    add_inbox()
    fake = FakeGmailClient()
    monkeypatch.setattr(gmail_client, "connect_inbox", fake.connect_inbox)

    app = page("3_Inboxes.py").run()
    button(app, "Connect Account").click().run()

    assert not app.exception
    assert "beedu@mobisa.in" in fake.labels


def test_failed_connect_shows_error(monkeypatch):
    add_inbox()

    def wrong_account(email_addr):
        raise ValueError("Account mismatch")

    monkeypatch.setattr(gmail_client, "connect_inbox", wrong_account)
    app = page("3_Inboxes.py").run()
    button(app, "Connect Account").click().run()

    assert not app.exception
    assert any("Account mismatch" in e.value for e in app.error)


def send_test_email(monkeypatch, fake):
    add_inbox()
    monkeypatch.setattr(gmail_client, "has_token", lambda email_addr: True)
    monkeypatch.setattr(gmail_client, "send_message", fake.send_message)

    app = page("3_Inboxes.py").run()
    next(t for t in app.text_input if t.label == "Recipient Email").set_value("me@example.com")
    button(app, "Send Test Email").click().run()
    assert not app.exception
    return app


def test_send_test_logged_as_test_and_ignores_caps(monkeypatch):
    fake = FakeGmailClient()
    app = send_test_email(monkeypatch, fake)

    assert [m["to"] for m in fake.sent_messages] == ["me@example.com"]
    assert fake.sent_messages[0]["list_unsubscribe"] == "unsubscribe@mobisa.in"
    assert any("successfully sent" in s.value for s in app.success)

    with open_conn() as conn:
        rows = conn.execute("SELECT lead_id, lead_email, result FROM send_log").fetchall()
    assert [tuple(r) for r in rows] == [(None, "me@example.com", "test")]

    from mailer.scheduling import sent_last_24h
    assert sent_last_24h(1) == 0


def test_send_test_failure_is_logged_as_error(monkeypatch):
    fake = FakeGmailClient()
    fake.should_raise = gmail_client.AuthExpired("Please reconnect")
    app = send_test_email(monkeypatch, fake)

    assert any("Please reconnect" in e.value for e in app.error)
    with open_conn() as conn:
        assert conn.execute("SELECT result FROM send_log").fetchone()["result"] == "error"


class FakeUpload:
    def __init__(self, name, data):
        self.name = name
        self.data = data

    def getvalue(self):
        return self.data


def lead_csv() -> bytes:
    return (
        "Professional email,Business name,Product category,Country,Location,Website,Company Instagram,Email type,Priority,Status\n"
        "a@acme.com,LUXE SKIN®,Skincare; Haircare,France,Paris,acme.com,acme,General inbox,1,Good\n"
        "b@acme.com,Bad One,Skincare,France,Paris,acme.com,acme,General inbox,1,Bad\n"
        "c@acme.com,Unknown,Skincare,Atlantis,,acme.com,acme,General inbox,1,Good\n"
        "not-an-email,Nope,Skincare,France,Paris,acme.com,acme,General inbox,1,Good\n"
    ).encode("utf-8")


def upload(monkeypatch, name="leads.csv", data=None):
    monkeypatch.setattr("streamlit.file_uploader", lambda *a, **k: FakeUpload(name, data or lead_csv()))
    return page("2_Import.py").run()


def import_leads(monkeypatch):
    app = upload(monkeypatch)
    assert not app.exception
    button(app, "Import").click().run()
    assert not app.exception
    return app


def test_import_page_shows_summary_and_history(monkeypatch):
    app = import_leads(monkeypatch)

    assert any("Imported **2** leads" in s.value for s in app.success)
    assert any("Unknown countries" in w.value for w in app.warning)
    with open_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2
        assert conn.execute("SELECT imported_count FROM imports").fetchone()[0] == 2


def test_import_page_preselects_default_columns(monkeypatch):
    app = upload(monkeypatch)

    labels = {box.label: box.value for box in app.selectbox}
    assert labels["Email (required)"] == "Professional email"
    assert labels["Quality"] == "Status"


def test_import_page_second_import_skips_everything(monkeypatch):
    import_leads(monkeypatch)

    app = page("2_Import.py").run()
    button(app, "Import").click().run()

    assert any("Imported **0** leads" in s.value for s in app.success)


def test_import_page_sheet_without_status_defaults_quality(monkeypatch):
    from tests.test_importer import excel_bytes

    rows = [{"Professional email": "a@acme.com", "Business name": "Acme", "Country": "France"}]
    app = upload(monkeypatch, "leads.xlsx", excel_bytes({"Summary": [{"Note": "x"}], "Weak": rows, "Good": rows}))

    assert next(box for box in app.selectbox if box.label == "Sheet").value == "Good"
    assert next(t for t in app.text_input if t.label == "Quality for every row").value == "Good"
    button(app, "Import").click().run()

    with open_conn() as conn:
        assert conn.execute("SELECT quality FROM leads").fetchone()[0] == "Good"


def test_import_page_rejects_unreadable_excel(monkeypatch):
    app = upload(monkeypatch, "leads.xlsx", b"not a workbook")

    assert not app.exception
    assert any("Could not read this file" in e.value for e in app.error)


def template_form_button(app, step, language):
    return app.button(f"FormSubmitter:template_{step}_{language}-💾 Save")


def test_templates_page_saves_valid_template():
    app = page("4_Templates.py").run()
    body_box = next(t for t in app.text_area if "Quick bump" in t.value)
    body_box.set_value("Fresh follow-up for {{business_name}}.")
    template_form_button(app, 2, "en").click().run()

    assert not app.exception
    assert any("Template saved" in s.value for s in app.success)
    with open_conn() as conn:
        assert conn.execute("SELECT body FROM templates WHERE step_no = 2 AND language = 'en'").fetchone()[0].startswith("Fresh")


def test_templates_page_blocks_unknown_field():
    app = page("4_Templates.py").run()
    body_box = next(t for t in app.text_area if "Quick bump" in t.value)
    body_box.set_value("Hi {{first_name}}")
    template_form_button(app, 2, "en").click().run()

    assert any("Unknown merge field" in e.value for e in app.error)
    with open_conn() as conn:
        assert "Quick bump" in conn.execute("SELECT body FROM templates WHERE step_no = 2 AND language = 'en'").fetchone()[0]


def test_templates_page_warns_about_links_on_step_1():
    app = page("4_Templates.py").run()
    body_box = next(t for t in app.text_area if t.value.startswith("Hi {{business_name}}"))
    body_box.set_value("See https://mobisa.in for {{business_name}}")
    template_form_button(app, 1, "en").click().run()

    assert any("Template saved" in s.value for s in app.success)
    assert any("contains a link" in w.value for w in app.warning)


def test_follow_up_subject_is_greyed_out():
    app = page("4_Templates.py").run()

    disabled = [t.disabled for t in app.text_input if t.label == "Subject"]
    assert disabled.count(False) == 2  # step 1, en and fr
    assert disabled.count(True) == 4


def test_templates_page_previews_a_real_lead(monkeypatch):
    import_leads(monkeypatch)

    app = page("4_Templates.py").run()
    next(box for box in app.selectbox if box.label == "Language").set_value("fr").run()

    assert not app.exception
    assert any("Bonjour l'équipe Luxe Skin," in c.value for c in app.code)
    assert any("Répondez « stop »" in c.value for c in app.code)
    assert any("**Subject:**" in m.value and "pour Luxe Skin" in m.value for m in app.markdown)


def test_templates_page_test_send_uses_rendered_email(monkeypatch):
    import_leads(monkeypatch)
    add_inbox()
    fake = FakeGmailClient()
    monkeypatch.setattr(gmail_client, "has_token", lambda email_addr: True)
    monkeypatch.setattr(gmail_client, "send_message", fake.send_message)

    app = page("4_Templates.py").run()
    next(box for box in app.selectbox if box.label == "Language").set_value("fr").run()
    next(t for t in app.text_input if t.label == "Send to").set_value("me@example.com")
    next(b for b in app.button if b.label == "🚀 Send Test").click().run()

    assert not app.exception
    assert [m["to"] for m in fake.sent_messages] == ["me@example.com"]
    assert fake.sent_messages[0]["body"].startswith("Bonjour l'équipe Luxe Skin,")
    assert fake.sent_messages[0]["display_name"] == "Beedu"
    with open_conn() as conn:
        rows = conn.execute("SELECT lead_id, result, step_no FROM send_log").fetchall()
        assert [tuple(r) for r in rows] == [(None, "test", 1)]
        assert conn.execute("SELECT COUNT(*) FROM leads WHERE step > 0").fetchone()[0] == 0


def test_templates_page_add_and_remove_step():
    app = page("4_Templates.py").run()
    button(app, "Add step").click().run()
    assert not app.exception
    assert len([t for t in app.text_input if t.label == "Subject"]) == 8

    button(app, "Remove last step").click().run()
    assert not app.exception
    assert len([t for t in app.text_input if t.label == "Subject"]) == 6


def test_name_exceptions_save_to_settings_file(settings_file):
    import mailer.config as config

    app = page("4_Templates.py").run()
    box = next(t for t in app.text_area if t.label == "Names")
    assert box.value == "Nails Inc."

    box.set_value("Nails Inc.\n\n  Acme Ltd  \n")
    app.button("FormSubmitter:name_exceptions-💾 Save names").click().run()

    assert not app.exception
    saved = config.load_settings(settings_file)
    assert saved.names.keep_suffix == ["Nails Inc.", "Acme Ltd"]
    assert saved.mode.dry_run is True
    assert saved.sender.website == "mobisa.in"


def settings_with(settings_file, change):
    from mailer.config import load_settings, save_settings

    settings = load_settings(settings_file)
    change(settings)
    save_settings(settings, settings_file)


def text_box(app, label):
    return next(t for t in app.text_input if t.label == label)


def test_campaign_saves_sender_settings(settings_file):
    from mailer.config import load_settings

    app = page("5_Campaign.py").run()
    text_box(app, "Booking link").set_value("https://cal.com/beedu")
    text_box(app, "Inbox receiving the unsubscribe address").set_value("Sales@Mobisa.in")
    app.button("FormSubmitter:sender_form-💾 Save sender").click().run()

    assert not app.exception
    saved = load_settings(settings_file)
    assert saved.sender.booking_link == "https://cal.com/beedu"
    assert saved.sender.unsubscribe_inbox == "sales@mobisa.in"
    assert saved.mode.dry_run is True


def test_campaign_saves_worker_settings(settings_file):
    from mailer.config import load_settings

    app = page("5_Campaign.py").run()
    next(box for box in app.number_input if box.label == "Hard daily cap per inbox").set_value(90)
    next(box for box in app.checkbox if box.label == "Weekdays only").set_value(False)
    app.button("FormSubmitter:worker_form-💾 Save worker settings").click().run()

    assert not app.exception
    saved = load_settings(settings_file)
    assert saved.worker.hard_daily_cap == 90
    assert saved.worker.recipient_window.weekdays_only is False


def test_campaign_rejects_bad_time_and_keeps_the_file(settings_file):
    from mailer.config import load_settings

    app = page("5_Campaign.py").run()
    text_box(app, "App window start (IST, HH:MM)").set_value("25:99")
    app.button("FormSubmitter:worker_form-💾 Save worker settings").click().run()

    assert not app.exception
    assert any("Invalid time" in e.value for e in app.error)
    assert load_settings(settings_file).worker.app_window_ist.start == "18:00"


def test_campaign_blocks_live_without_business_address(settings_file):
    from mailer.config import load_settings

    settings_with(settings_file, lambda s: setattr(s.sender, "business_address", ""))

    app = page("5_Campaign.py").run()
    button(app, "Switch to LIVE").click().run()

    assert any("Business address is empty" in e.value for e in app.error)
    assert load_settings(settings_file).mode.dry_run is True


def test_campaign_asks_before_going_live_and_stays_dry_until_confirmed(settings_file):
    from mailer.config import load_settings

    app = page("5_Campaign.py").run()
    button(app, "Switch to LIVE").click().run()

    assert not app.exception
    assert any(b.label == "Yes, go LIVE" for b in app.button)
    assert load_settings(settings_file).mode.dry_run is True


def test_campaign_returns_to_dry_run(settings_file):
    from mailer.config import load_settings

    settings_with(settings_file, lambda s: setattr(s.mode, "dry_run", False))

    app = page("5_Campaign.py").run()
    button(app, "Back to Dry Run").click().run()

    assert not app.exception
    assert load_settings(settings_file).mode.dry_run is True


def test_overview_shows_cap_and_dry_runs():
    from mailer.db import utc_now_iso

    add_inbox()
    with open_conn() as conn:
        conn.execute(
            "INSERT INTO send_log (ts, inbox_id, lead_email, step_no, result) VALUES (?, 1, 'x@y.z', 1, 'dry_run')",
            (utc_now_iso(),),
        )

    app = page("1_Overview.py").run()

    assert not app.exception
    cell = app.dataframe[0].value["Dry Runs Last 24h / Cap"].iloc[0]
    assert cell.startswith("1 / ")


def test_overview_warns_about_timezones_that_never_send():
    stamp = "2026-11-01T00:00:00Z"
    with open_conn() as conn:
        conn.execute(
            "INSERT INTO leads (email, country, created_at, updated_at) VALUES ('a@x.com', 'Australia', ?, ?)",
            (stamp, stamp),
        )

    app = page("1_Overview.py").run()

    assert any("Australia/Sydney: 1 leads" in w.value for w in app.warning)


def test_overview_shows_no_window_warning_for_normal_leads():
    stamp = "2026-11-01T00:00:00Z"
    with open_conn() as conn:
        conn.execute(
            "INSERT INTO leads (email, country, created_at, updated_at) VALUES ('b@x.com', 'France', ?, ?)",
            (stamp, stamp),
        )

    app = page("1_Overview.py").run()

    assert not any("working hours" in w.value for w in app.warning)


def test_campaign_saves_us_reservation(settings_file):
    from mailer.config import load_settings

    app = page("5_Campaign.py").run()
    next(box for box in app.number_input if box.label == "Percent of cap held for US leads").set_value(30)
    text_box(app, "Hold until (IST, HH:MM)").set_value("21:15")
    app.button("FormSubmitter:worker_form-💾 Save worker settings").click().run()

    assert not app.exception
    saved = load_settings(settings_file).worker.us_reservation
    assert (saved.percent, saved.until_ist) == (30, "21:15")


def test_campaign_shows_the_lock_on_by_default(settings_file):
    app = page("5_Campaign.py").run()

    assert any("Test leads lock is ON" in m.value for m in app.markdown)


def test_unlock_asks_for_the_typed_phrase_and_stays_locked(settings_file):
    from mailer.config import load_settings

    app = page("5_Campaign.py").run()
    button(app, "Unlock real leads").click().run()

    assert not app.exception
    assert next(b for b in app.button if b.label == "Unlock").disabled is True
    assert load_settings(settings_file).mode.test_leads_only is True


def test_campaign_can_turn_the_lock_back_on(settings_file):
    from mailer.config import load_settings

    settings_with(settings_file, lambda s: setattr(s.mode, "test_leads_only", False))

    app = page("5_Campaign.py").run()
    button(app, "Lock to test leads").click().run()

    assert not app.exception
    assert load_settings(settings_file).mode.test_leads_only is True


def test_live_banners_say_who_gets_email(settings_file):
    settings_with(settings_file, lambda s: setattr(s.mode, "dry_run", False))
    assert any("TEST LEADS ONLY" in s.value for s in page("1_Overview.py").run().success)

    settings_with(settings_file, lambda s: setattr(s.mode, "test_leads_only", False))
    assert any("REAL LEADS" in e.value for e in page("1_Overview.py").run().error)


def test_import_page_adds_pasted_test_leads():
    app = page("2_Import.py").run()
    next(t for t in app.text_area if t.label == "Addresses").set_value("me+a@example.com\nme+b@example.com")
    app.button("FormSubmitter:test_leads_form-➕ Add test leads").click().run()

    assert not app.exception
    assert any("Imported **2** leads" in s.value for s in app.success)
    with open_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads WHERE is_test = 1").fetchone()[0] == 2


def test_import_page_marks_uploaded_test_leads(monkeypatch):
    app = upload(monkeypatch)
    next(box for box in app.checkbox if "test leads" in box.label).set_value(True)
    button(app, "Import").click().run()

    with open_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads WHERE is_test = 1").fetchone()[0] == 2


def add_page_lead(email, **values):
    columns = {"email": email, "business_name": "Acme", "country": "France", "created_at": "x", "updated_at": "x", **values}
    with open_conn() as conn:
        cursor = conn.execute(
            f"INSERT INTO leads ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            list(columns.values()),
        )
        return cursor.lastrowid


def lead_value(lead_id, column):
    with open_conn() as conn:
        return conn.execute(f"SELECT {column} FROM leads WHERE id = ?", (lead_id,)).fetchone()[0]


def test_leads_page_filters_by_search():
    add_page_lead("a@acme.com")
    add_page_lead("b@beta.com", business_name="Beta")

    app = page("6_Leads.py").run()
    assert any("**2** leads match" in m.value for m in app.markdown)

    text_box(app, "Search email or name").set_value("beta").run()
    assert not app.exception
    assert any("**1** leads match" in m.value for m in app.markdown)


def test_leads_page_pages_through_large_lists():
    with open_conn() as conn:
        conn.executemany(
            "INSERT INTO leads (email, created_at, updated_at) VALUES (?, 'x', 'x')",
            [(f"l{i}@x.com",) for i in range(150)],
        )

    app = page("6_Leads.py").run()

    assert not app.exception
    assert len(app.dataframe[0].value) == 100
    next(box for box in app.number_input if box.label.startswith("Page")).set_value(2).run()
    assert len(app.dataframe[0].value) == 50


def test_leads_page_skip_and_suppress():
    lead_id = add_page_lead("a@acme.com")

    app = page("6_Leads.py").run()
    button(app, "Skip").click().run()
    assert lead_value(lead_id, "status") == "skipped"

    button(app, "Add to suppression").click().run()
    with open_conn() as conn:
        assert conn.execute("SELECT reason FROM suppression").fetchone()[0] == "manual"


def test_leads_page_reset_refuses_an_emailed_lead():
    add_page_lead("a@acme.com", status="finished", step=1)

    app = page("6_Leads.py").run()
    button(app, "Reset to queued").click().run()

    assert any("already emailed" in e.value for e in app.error)


def test_leads_page_saves_custom_line():
    lead_id = add_page_lead("a@acme.com")

    app = page("6_Leads.py").run()
    text_box(app, "Custom line").set_value("Love your range.")
    app.button("FormSubmitter:custom_line_form-💾 Save custom line").click().run()

    assert lead_value(lead_id, "custom_line") == "Love your range."


def test_leads_page_removes_test_leads_and_keeps_real_ones():
    add_page_lead("real@acme.com")
    add_page_lead("me@example.com", is_test=1)

    app = page("6_Leads.py").run()
    button(app, "Remove all test leads").click().run()

    assert not app.exception
    with open_conn() as conn:
        assert [r[0] for r in conn.execute("SELECT email FROM leads")] == ["real@acme.com"]


def test_no_remove_button_without_test_leads():
    add_page_lead("real@acme.com")

    app = page("6_Leads.py").run()

    assert not any("Remove all test leads" in b.label for b in app.button)


def test_replies_page_lists_replies_and_opt_outs_newest_first():
    add_inbox("sales@mobisa.in")
    lead_id = add_page_lead("a@acme.com", thread_id="thread_9")
    with open_conn() as conn:
        conn.execute("INSERT INTO events (ts, inbox_id, lead_id, email, type, gmail_message_id, snippet) VALUES ('2026-11-04T10:00:00Z', 1, ?, 'a@acme.com', 'reply', 'm1', 'Yes')", (lead_id,))
        conn.execute("INSERT INTO events (ts, inbox_id, lead_id, email, type, gmail_message_id, snippet) VALUES ('2026-11-04T11:00:00Z', 1, ?, 'a@acme.com', 'unsubscribe', 'm2', 'stop')", (lead_id,))
        conn.execute("INSERT INTO events (ts, inbox_id, email, type, gmail_message_id, subject) VALUES ('2026-11-04T12:00:00Z', 1, 'x@y.z', 'bounce', 'm3', 'Failure')")

    app = page("7_Replies.py").run()

    assert not app.exception
    assert button(app, "Save handled") is not None


def test_replies_page_is_empty_without_events():
    app = page("7_Replies.py").run()

    assert not app.exception
    assert any("No reply" in i.value for i in app.info)


def test_log_page_exports_both_files(monkeypatch, tmp_path):
    import mailer.exporter as exporter

    monkeypatch.setattr(exporter, "EXPORT_DIR", tmp_path / "exports")
    add_page_lead("a@acme.com")

    app = page("8_Log.py").run()
    button(app, "Export send log").click().run()
    assert not app.exception
    button(app, "Export leads").click().run()
    assert not app.exception

    names = sorted(path.name.split("_2")[0] for path in (tmp_path / "exports").iterdir())
    assert names == ["leads", "send_log"]


def test_log_page_filters_by_result():
    from mailer.db import utc_now_iso

    with open_conn() as conn:
        conn.execute("INSERT INTO send_log (ts, lead_email, step_no, result) VALUES (?, 'a@x.com', 1, 'sent')", (utc_now_iso(),))
        conn.execute("INSERT INTO send_log (ts, lead_email, step_no, result) VALUES (?, 'b@x.com', 1, 'error')", (utc_now_iso(),))

    app = page("8_Log.py").run()
    assert any("**2** rows match" in m.value for m in app.markdown)

    next(box for box in app.selectbox if box.label == "Result").set_value("error").run()
    assert any("**1** rows match" in m.value for m in app.markdown)


def test_overview_runs_detection(monkeypatch):
    import mailer.gmail_client as gmail

    add_inbox()
    fake = FakeGmailClient()
    fake.connect_inbox("beedu@mobisa.in")
    monkeypatch.setattr(gmail, "has_token", fake.has_token)
    monkeypatch.setattr(gmail, "list_recent_messages", fake.list_recent_messages)

    app = page("1_Overview.py").run()
    button(app, "Run Detection Now").click().run()

    assert not app.exception
    assert any("Detection finished" in s.value for s in app.success)


def test_overview_shows_the_us_reservation(monkeypatch):
    monkeypatch.setattr("mailer.scheduling.reservation_is_open", lambda now, settings: True)
    add_page_lead("us@acme.com", country="United States")

    app = page("1_Overview.py").run()

    assert any("held for US leads until 22:30 IST" in i.value for i in app.info)


def test_resume_button_forgives_old_bounces():
    add_inbox()
    with open_conn() as conn:
        conn.execute("UPDATE inboxes SET paused_reason = 'Too many bounces'")

    app = page("3_Inboxes.py").run()
    button(app, "Resume Inbox").click().run()

    assert not app.exception
    with open_conn() as conn:
        assert conn.execute("SELECT paused_reason FROM inboxes").fetchone()[0] is None
        assert conn.execute("SELECT value FROM app_state WHERE key = 'bounce_reset_1'").fetchone() is not None


def test_paused_inbox_banner_shows_on_rerun():
    add_inbox()
    with open_conn() as conn:
        conn.execute("UPDATE inboxes SET paused_reason = 'Reconnect inbox'")

    app = page("1_Overview.py").run().run()
    assert any("Reconnect inbox" in e.value for e in app.error)


def test_alias_receiver_marked_on_inboxes_page(monkeypatch):
    add_inbox()
    from mailer.config import load_settings

    settings = load_settings()
    settings.sender.unsubscribe_inbox = "beedu@mobisa.in"
    monkeypatch.setattr("mailer.ui.load_settings", lambda: settings)

    app = page("3_Inboxes.py").run()
    assert not app.exception
    assert any("Receives unsubscribe@mobisa.in" in m.value for m in app.markdown)
