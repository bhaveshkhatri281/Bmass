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
