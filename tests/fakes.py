"""Fake Gmail client for unit and integration testing without network calls."""

from __future__ import annotations

import base64
import email
import re
import time
import uuid
from email import policy
from typing import Any, Dict, List, Optional

from mailer.gmail_client import (
    AuthExpired,
    MessageMeta,
    PermanentSendError,
    QuotaExceeded,
    RateLimited,
    SentResult,
)


class FakeRequest:
    """Deferred API call, like a googleapiclient request object."""

    def __init__(self, func: Any) -> None:
        self.func = func

    def execute(self) -> Any:
        return self.func()


class FakeGmailService:
    """users().messages() chain over a FakeGmailClient, so real send_message can run offline."""

    def __init__(self, client: "FakeGmailClient") -> None:
        self.client = client

    def users(self) -> "FakeGmailService":
        return self

    def messages(self) -> "FakeGmailService":
        return self

    def send(self, userId: str, body: Dict[str, Any]) -> FakeRequest:
        return FakeRequest(lambda: self.client.api_send(body))

    def list(self, userId: str, q: str = "", **kwargs: Any) -> FakeRequest:
        return FakeRequest(lambda: self.client.api_list_sent(q))

    def get(self, userId: str, id: str, **kwargs: Any) -> FakeRequest:
        return FakeRequest(lambda: self.client.api_get(id))


class FakeGmailClient:
    """In-memory fake implementing the same interface as mailer.gmail_client."""

    def __init__(self) -> None:
        self.sent_messages: List[Dict[str, Any]] = []
        self.inbox_messages: Dict[str, List[MessageMeta]] = {}
        self.message_texts: Dict[str, str] = {}
        self.labels: Dict[str, Dict[str, str]] = {}  # email -> {label_name: label_id}
        self.applied_labels: List[Dict[str, str]] = []  # [{email, msg_id, label_id}]
        self.should_raise: Optional[Exception] = None
        self.send_attempts = 0
        self.send_failures: List[Dict[str, Any]] = []  # [{error, delivered}], used by api_send in order
        self.sent_queries: List[str] = []
        self.search_error: Optional[Exception] = None

    def connect_inbox(self, email_addr: str) -> str:
        if self.should_raise:
            raise self.should_raise
        clean = email_addr.strip().lower()
        self.labels[clean] = {}
        return clean

    def has_token(self, email_addr: str) -> bool:
        return email_addr.strip().lower() in self.labels

    def get_service(self, email_addr: str) -> Any:
        if self.should_raise:
            raise self.should_raise
        return FakeGmailService(self)

    def send_message(
        self,
        email_addr: str,
        to: str,
        subject: str,
        body: str,
        display_name: str,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        list_unsubscribe: Optional[str] = None,
    ) -> SentResult:
        if self.should_raise:
            raise self.should_raise

        gmail_msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        res_thread_id = thread_id or f"th_{uuid.uuid4().hex[:12]}"
        rfc_id = f"<{gmail_msg_id}@mail.fake>"

        record = {
            "from": email_addr,
            "to": to,
            "subject": subject,
            "body": body,
            "display_name": display_name,
            "gmail_message_id": gmail_msg_id,
            "thread_id": res_thread_id,
            "in_reply_to": in_reply_to,
            "list_unsubscribe": list_unsubscribe,
            "rfc_message_id": rfc_id,
        }
        self.sent_messages.append(record)
        return SentResult(
            gmail_message_id=gmail_msg_id,
            thread_id=res_thread_id,
            rfc_message_id=rfc_id,
        )

    def list_recent_messages(self, email_addr: str, query: str) -> List[MessageMeta]:
        if self.should_raise:
            raise self.should_raise
        clean = email_addr.strip().lower()
        return self.inbox_messages.get(clean, [])

    def get_message_text(self, email_addr: str, msg_id: str) -> str:
        if self.should_raise:
            raise self.should_raise
        return self.message_texts.get(msg_id, "")

    def ensure_label(self, email_addr: str, name: str) -> str:
        if self.should_raise:
            raise self.should_raise
        clean = email_addr.strip().lower()
        if clean not in self.labels:
            self.labels[clean] = {}
        if name not in self.labels[clean]:
            self.labels[clean][name] = f"lbl_{name.lower().replace('/', '_')}"
        return self.labels[clean][name]

    def add_label(self, email_addr: str, msg_id: str, label_id: str) -> None:
        if self.should_raise:
            raise self.should_raise
        self.applied_labels.append({"email": email_addr, "msg_id": msg_id, "label_id": label_id})

    # Service-level API, reached through FakeGmailService
    def api_send(self, body: Dict[str, Any]) -> Dict[str, str]:
        self.send_attempts += 1
        failure = self.send_failures.pop(0) if self.send_failures else None
        if failure and not failure["delivered"]:
            raise failure["error"]

        parsed = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=policy.default)
        gmail_msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        thread_id = body.get("threadId") or f"th_{uuid.uuid4().hex[:12]}"
        self.sent_messages.append({
            "to": parsed["To"],
            "subject": parsed["Subject"],
            "in_reply_to": parsed.get("In-Reply-To", ""),
            "gmail_message_id": gmail_msg_id,
            "thread_id": thread_id,
            "rfc_message_id": f"<{gmail_msg_id}@mail.fake>",
            "internal_date_ms": int(time.time() * 1000),
        })
        if failure:
            raise failure["error"]
        return {"id": gmail_msg_id, "threadId": thread_id}

    def api_list_sent(self, query: str) -> Dict[str, Any]:
        if self.search_error:
            raise self.search_error
        self.sent_queries.append(query)
        match = re.search(r'to:(\S+) subject:"(.*)"', query)
        to, subject = match.groups() if match else ("", "")
        hits = [m for m in self.sent_messages if m["to"] == to and subject in m["subject"]]
        return {"messages": [{"id": m["gmail_message_id"]} for m in reversed(hits)]}

    def api_get(self, msg_id: str) -> Dict[str, Any]:
        sent = next(m for m in self.sent_messages if m["gmail_message_id"] == msg_id)
        headers = {"To": sent["to"], "Subject": sent["subject"], "Message-ID": sent["rfc_message_id"]}
        if sent["in_reply_to"]:
            headers["In-Reply-To"] = sent["in_reply_to"]
        return {
            "id": msg_id,
            "threadId": sent["thread_id"],
            "internalDate": str(sent["internal_date_ms"]),
            "payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()]},
        }

    # Test inspection / helper methods
    def fail_next_send(self, error: Exception, delivered: bool) -> None:
        """Make the next API send raise. delivered=True means Gmail kept the message anyway."""
        self.send_failures.append({"error": error, "delivered": delivered})

    def inject_incoming_message(self, email_addr: str, meta: MessageMeta, text_body: str = "") -> None:
        clean = email_addr.strip().lower()
        if clean not in self.inbox_messages:
            self.inbox_messages[clean] = []
        self.inbox_messages[clean].append(meta)
        if text_body:
            self.message_texts[meta.id] = text_body

