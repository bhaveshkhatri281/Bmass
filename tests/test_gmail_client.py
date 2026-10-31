"""Tests for Gmail client error mapping and FakeGmailClient."""

import pytest
from googleapiclient.errors import HttpError
import httplib2

from mailer.gmail_client import (
    AuthExpired,
    PermanentSendError,
    QuotaExceeded,
    RateLimited,
    TransientError,
    _map_http_error,
)
from tests.fakes import FakeGmailClient


def make_http_error(status: int, body_json_str: str) -> HttpError:
    resp = httplib2.Response({"status": status})
    return HttpError(resp, body_json_str.encode("utf-8"))


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

