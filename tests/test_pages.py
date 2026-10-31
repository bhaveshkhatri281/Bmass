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


def test_name_exceptions_save_to_settings_file(monkeypatch, tmp_path):
    import shutil

    import mailer.config as config

    settings_file = tmp_path / "settings.yaml"
    shutil.copy(config.DEFAULT_SETTINGS_PATH, settings_file)
    monkeypatch.setattr(config, "DEFAULT_SETTINGS_PATH", settings_file)

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
