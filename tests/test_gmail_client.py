"""Tests for Gmail client error mapping and FakeGmailClient."""

import base64
import email
from email import policy

import pytest
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError
import httplib2

import mailer.gmail_client as gmail_client
from mailer.gmail_client import (
    AuthExpired,
    GmailClientError,
    PermanentSendError,
    QuotaExceeded,
    RateLimited,
    TransientError,
    _map_http_error,
    build_message,
    execute_with_retry,
)
from tests.fakes import FakeGmailClient


def make_http_error(status: int, body_json_str: str) -> HttpError:
    resp = httplib2.Response({"status": status})
    return HttpError(resp, body_json_str.encode("utf-8"))


class FakeRequest:
    """Stands in for a googleapiclient request object."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class FakeService:
    """Minimal users().messages() chain, recording list calls."""

    def __init__(self, pages=None, list_error=None, get_result=None):
        self.pages = list(pages or [])
        self.list_error = list_error
        self.get_result = get_result
        self.page_tokens = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.page_tokens.append(kwargs.get("pageToken"))
        return FakeRequest(self.pages.pop(0) if self.pages else None, self.list_error)

    def get(self, **kwargs):
        return FakeRequest(self.get_result or {"id": kwargs["id"], "threadId": "t", "snippet": "", "payload": {"headers": []}})


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(gmail_client.time, "sleep", sleeps.append)
    return sleeps


def test_map_rate_limited():
    err = make_http_error(429, '{"error": {"message": "Rate limit exceeded"}}')
    mapped = _map_http_error(err)
    assert isinstance(mapped, RateLimited)


def test_map_quota_exceeded():
    err = make_http_error(403, '{"error": {"errors": [{"reason": "dailyLimitExceeded"}], "message": "Daily user sending limit exceeded"}}')
    mapped = _map_http_error(err)
    assert isinstance(mapped, QuotaExceeded)


def test_map_auth_expired():
    err = make_http_error(401, '{"error": {"message": "invalid_grant: Token has been expired or revoked."}}')
    mapped = _map_http_error(err)
    assert isinstance(mapped, AuthExpired)


def test_map_transient_error():
    err = make_http_error(503, '{"error": {"message": "Backend service unavailable"}}')
    mapped = _map_http_error(err)
    assert isinstance(mapped, TransientError)


def test_map_permanent_error():
    err = make_http_error(400, '{"error": {"message": "Invalid recipient format"}}')
    mapped = _map_http_error(err)
    assert isinstance(mapped, PermanentSendError)
    err = make_http_error(400, '{"error": {"errors": [{"reason": "invalidArgument"}], "message": "Invalid To header"}}')
    assert isinstance(_map_http_error(err), PermanentSendError)


def test_quota_message_beats_rate_limit_status():
    err = make_http_error(429, '{"error": {"message": "Daily user sending limit exceeded"}}')
    assert isinstance(_map_http_error(err), QuotaExceeded)
    err = make_http_error(403, '{"error": {"errors": [{"reason": "rateLimitExceeded"}], "message": "Daily user sending limit exceeded"}}')
    assert isinstance(_map_http_error(err), QuotaExceeded)


@pytest.mark.parametrize(
    "status, body",
    [
        (404, '{"error": {"message": "Requested entity was not found."}}'),
        (400, '{"error": {"errors": [{"reason": "failedPrecondition"}], "message": "Mail service not enabled"}}'),
        (403, '{"error": {"errors": [{"reason": "insufficientPermissions"}], "message": "Insufficient Permission"}}'),
    ],
)
def test_account_errors_are_not_permanent_lead_errors(status, body):
    mapped = _map_http_error(make_http_error(status, body))
    assert type(mapped) is GmailClientError


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"),
        TimeoutError("timed out"),
        httplib2.ServerNotFoundError("Unable to find the server"),
        TransportError("no route"),
    ],
)
def test_network_errors_retry_then_raise_transient(exc, no_sleep):
    calls = []

    def boom():
        calls.append(1)
        raise exc

    with pytest.raises(TransientError):
        execute_with_retry(boom)
    assert len(calls) == 4  # first try plus 3 retries
    assert no_sleep == [1.0, 2.0, 4.0]


def test_retry_recovers_and_returns(no_sleep):
    results = iter([make_http_error(503, '{"error": {"message": "down"}}'), "ok"])

    def flaky():
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    assert execute_with_retry(flaky) == "ok"
    assert len(no_sleep) == 1


def test_non_transient_error_is_not_retried(no_sleep):
    calls = []

    def boom():
        calls.append(1)
        raise make_http_error(429, '{"error": {"message": "slow down"}}')

    with pytest.raises(RateLimited):
        execute_with_retry(boom)
    assert len(calls) == 1
    assert no_sleep == []


def _write_expired_token(email_addr):
    token_file = gmail_client._token_path_for_email(email_addr)
    token_file.write_text(
        '{"token": "x", "refresh_token": "y", "client_id": "c", "client_secret": "s", '
        '"token_uri": "https://oauth2.googleapis.com/token", "expiry": "2020-01-01T00:00:00Z"}',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "refresh_error, expected",
    [(TransportError("network down"), TransientError), (RefreshError("invalid_grant"), AuthExpired)],
)
def test_token_refresh_failures(monkeypatch, refresh_error, expected):
    _write_expired_token("me@mobisa.in")

    def fail_refresh(self, request):
        raise refresh_error

    monkeypatch.setattr(gmail_client.Credentials, "refresh", fail_refresh)
    with pytest.raises(expected):
        gmail_client.get_service("me@mobisa.in")


def test_missing_token_is_auth_expired():
    with pytest.raises(AuthExpired):
        gmail_client.get_service("nobody@mobisa.in")


def test_list_recent_messages_follows_every_page(monkeypatch):
    service = FakeService(pages=[
        {"messages": [{"id": "1"}], "nextPageToken": "page2"},
        {"messages": [{"id": "2"}]},
    ])
    monkeypatch.setattr(gmail_client, "get_service", lambda email_addr: service)

    messages = gmail_client.list_recent_messages("me@mobisa.in", "in:inbox")
    assert [m.id for m in messages] == ["1", "2"]
    assert service.page_tokens == [None, "page2"]


def test_list_recent_messages_raises_instead_of_hiding_errors(monkeypatch):
    service = FakeService(list_error=make_http_error(429, '{"error": {"message": "slow down"}}'))
    monkeypatch.setattr(gmail_client, "get_service", lambda email_addr: service)

    with pytest.raises(RateLimited):
        gmail_client.list_recent_messages("me@mobisa.in", "in:inbox")


def test_get_message_text_handles_unpadded_base64(monkeypatch):
    raw = base64.urlsafe_b64encode(b"failed: bob@example.com").decode().rstrip("=")
    assert len(raw) % 4 != 0
    service = FakeService(get_result={"payload": {"mimeType": "text/plain", "body": {"data": raw}}})
    monkeypatch.setattr(gmail_client, "get_service", lambda email_addr: service)

    assert gmail_client.get_message_text("me@mobisa.in", "1") == "failed: bob@example.com"


def _parsed(msg):
    return email.message_from_bytes(msg.as_bytes(), policy=policy.default)


def test_build_message_first_email_headers():
    parsed = _parsed(build_message(
        "beedu@mobisa.in", "lead@example.com", "Une idée pour Café", "Bonjour é\n",
        "Beedu from Mobisa", list_unsubscribe="unsubscribe@mobisa.in",
    ))
    assert parsed["From"].addresses[0].display_name == "Beedu from Mobisa"
    assert parsed["From"].addresses[0].addr_spec == "beedu@mobisa.in"
    assert parsed["To"] == "lead@example.com"
    assert parsed["Subject"] == "Une idée pour Café"
    assert parsed["List-Unsubscribe"] == "<mailto:unsubscribe@mobisa.in?subject=unsubscribe>"
    assert parsed["In-Reply-To"] is None
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_content_charset() == "utf-8"
    assert parsed.get_content().strip() == "Bonjour é"


def test_build_message_follow_up_threading_headers():
    for message_id in ("<abc@mail.gmail.com>", "abc@mail.gmail.com"):
        parsed = _parsed(build_message(
            "beedu@mobisa.in", "lead@example.com", "Re: idea", "Bump", "Beedu", in_reply_to=message_id,
        ))
        assert parsed["In-Reply-To"] == "<abc@mail.gmail.com>"
        assert parsed["References"] == "<abc@mail.gmail.com>"


def test_fake_gmail_client_send_and_labels():
    fake = FakeGmailClient()
    email = "test@example.com"
    fake.connect_inbox(email)
    assert fake.has_token(email)

    res = fake.send_message(
        email_addr=email,
        to="client@example.org",
        subject="Hello",
        body="World",
        display_name="Sender",
    )
    assert res.gmail_message_id.startswith("msg_")
    assert res.thread_id.startswith("th_")
    assert len(fake.sent_messages) == 1

    lbl_id = fake.ensure_label(email, "Mobisa/Replied")
    assert lbl_id == "lbl_mobisa_replied"
    fake.add_label(email, res.gmail_message_id, lbl_id)
    assert len(fake.applied_labels) == 1

