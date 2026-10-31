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
