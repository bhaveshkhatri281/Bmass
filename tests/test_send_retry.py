"""Tests that an interrupted send is never resent if it already reached Gmail."""

import pytest
import httplib2
from googleapiclient.errors import HttpError

import mailer.gmail_client as gmail_client
from mailer.gmail_client import RateLimited, TransientError, _normalize_header
from tests.fakes import FakeGmailClient

INBOX = "beedu@mobisa.in"
LEAD = "lead@example.com"


def http_error(status: int, message: str = "server trouble") -> HttpError:
    body = f'{{"error": {{"message": "{message}"}}}}'.encode("utf-8")
    return HttpError(httplib2.Response({"status": status}), body)


@pytest.fixture
def fake(monkeypatch):
    client = FakeGmailClient()
    monkeypatch.setattr(gmail_client, "get_service", client.get_service)
    monkeypatch.setattr(gmail_client.time, "sleep", lambda seconds: None)
    return client


def send(subject="idea for Acme", **kwargs):
    return gmail_client.send_message(INBOX, LEAD, subject, "Hello", "Beedu", **kwargs)


@pytest.mark.parametrize("error", [TimeoutError("timed out"), http_error(503), ConnectionResetError(10054, "reset")])
def test_delivered_send_is_found_and_not_resent(fake, error):
    fake.fail_next_send(error, delivered=True)

    result = send()

    delivered = fake.sent_messages[0]
    assert fake.send_attempts == 1
    assert len(fake.sent_messages) == 1
    assert result.gmail_message_id == delivered["gmail_message_id"]
    assert result.thread_id == delivered["thread_id"]
    assert result.rfc_message_id == delivered["rfc_message_id"]
    assert fake.sent_queries == [f'in:sent to:{LEAD} subject:"idea for Acme" newer_than:1h']


def test_undelivered_send_is_retried_when_search_finds_nothing(fake):
    fake.fail_next_send(TimeoutError("timed out"), delivered=False)

    result = send()

    assert fake.send_attempts == 2
    assert len(fake.sent_messages) == 1
    assert result.gmail_message_id == fake.sent_messages[0]["gmail_message_id"]
    assert len(fake.sent_queries) == 1


def test_delivered_on_a_later_attempt_is_found(fake):
    fake.fail_next_send(TimeoutError("timed out"), delivered=False)
    fake.fail_next_send(TimeoutError("timed out"), delivered=True)

    send()

    assert fake.send_attempts == 2
    assert len(fake.sent_messages) == 1


def test_gives_up_after_three_retries(fake):
    for _ in range(4):
        fake.fail_next_send(TimeoutError("timed out"), delivered=False)

    with pytest.raises(TransientError):
        send()

    assert fake.send_attempts == 4
    assert fake.sent_messages == []
    assert len(fake.sent_queries) == 4


def test_failed_search_raises_and_never_resends(fake):
    fake.fail_next_send(TimeoutError("timed out"), delivered=False)
    fake.search_error = http_error(503)

    with pytest.raises(TransientError):
        send()

    assert fake.send_attempts == 1


def test_clean_send_runs_no_search(fake):
    send()

    assert fake.send_attempts == 1
    assert fake.sent_queries == []


def test_non_transient_error_runs_no_search(fake):
    fake.fail_next_send(http_error(429, "slow down"), delivered=False)

    with pytest.raises(RateLimited):
        send()

    assert fake.send_attempts == 1
    assert fake.sent_queries == []


def test_earlier_follow_up_with_same_subject_does_not_count(fake):
    first = send(subject="Re: idea for Acme", thread_id="th_1", in_reply_to="<step1@mail>")
    fake.fail_next_send(TimeoutError("timed out"), delivered=False)

    send(subject="Re: idea for Acme", thread_id="th_1", in_reply_to=first.rfc_message_id)

    assert fake.send_attempts == 3
    assert len(fake.sent_messages) == 2


def test_earlier_test_send_outside_the_attempt_does_not_count(fake):
    send()
    fake.sent_messages[0]["internal_date_ms"] -= 10 * 60 * 1000
    fake.fail_next_send(TimeoutError("timed out"), delivered=False)

    send()

    assert fake.send_attempts == 3
    assert len(fake.sent_messages) == 2


def test_copy_in_another_thread_does_not_count(fake):
    send(thread_id="th_other")
    fake.fail_next_send(TimeoutError("timed out"), delivered=False)

    send(thread_id="th_1")

    assert fake.send_attempts == 3


def test_subject_longer_than_ours_does_not_count(fake):
    send(subject="Re: idea for Acme")
    fake.fail_next_send(TimeoutError("timed out"), delivered=False)

    send(subject="idea for Acme")

    assert fake.send_attempts == 3


def test_double_quotes_in_subject_cannot_break_the_search(fake):
    fake.fail_next_send(TimeoutError("timed out"), delivered=False)

    send(subject='idea for "Acme" Ltd')

    assert fake.sent_queries == [f'in:sent to:{LEAD} subject:"idea for  Acme  Ltd" newer_than:1h']


def test_whole_sequence_through_the_real_client_stays_in_one_thread(fake, monkeypatch):
    import datetime

    from mailer import engine
    from mailer.config import Settings
    from mailer.db import open_conn

    monkeypatch.setattr(gmail_client, "has_token", lambda email_addr: True)
    settings = Settings()
    settings.mode.dry_run = False
    settings.sender.business_address = "Jaipur"
    settings.sender.website = "mobisa.in"
    settings.sender.portfolio_link = "https://mobisa.in"

    with open_conn() as conn:
        conn.execute("UPDATE steps SET delay_days = 0")
        conn.execute(
            "INSERT INTO inboxes (email, display_name, ramp_start_date, created_at) VALUES (?, 'Beedu', '2026-11-04', 'x')",
            (INBOX,),
        )
        conn.execute(
            """INSERT INTO leads (email, business_name, category, country, language, is_test, created_at, updated_at)
               VALUES (?, 'Acme', 'skincare', 'United Kingdom', 'en', 1, 'x', 'x')""",
            (LEAD,),
        )

    moment = datetime.datetime(2026, 11, 4, 15, 0, tzinfo=datetime.timezone.utc)
    for minute in range(3):
        with open_conn() as conn:
            conn.execute("UPDATE inboxes SET next_send_after = NULL")
            sent = engine.tick(settings, gmail_client, moment + datetime.timedelta(minutes=minute), conn)
        assert sent is True

    first, second, third = fake.sent_messages
    assert first["in_reply_to"] == ""
    assert second["in_reply_to"] == first["rfc_message_id"]
    assert third["in_reply_to"] == second["rfc_message_id"]
    assert first["thread_id"] == second["thread_id"] == third["thread_id"]
    assert second["subject"] == third["subject"] == f"Re: {first['subject']}"


def test_encoded_subject_header_matches_plain_subject():
    assert _normalize_header("=?utf-8?q?une_id=C3=A9e?=") == _normalize_header("une idée")
