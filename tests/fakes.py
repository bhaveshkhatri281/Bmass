"""Fake Gmail client for unit and integration testing without network calls."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from mailer.gmail_client import (
    AuthExpired,
    MessageMeta,
    PermanentSendError,
    QuotaExceeded,
    RateLimited,
    SentResult,
)


class FakeGmailClient:
    """In-memory fake implementing the same interface as mailer.gmail_client."""

    def __init__(self) -> None:
        self.sent_messages: List[Dict[str, Any]] = []
        self.inbox_messages: Dict[str, List[MessageMeta]] = {}
        self.message_texts: Dict[str, str] = {}
        self.labels: Dict[str, Dict[str, str]] = {}  # email -> {label_name: label_id}
        self.applied_labels: List[Dict[str, str]] = []  # [{email, msg_id, label_id}]
        self.should_raise: Optional[Exception] = None

    def connect_inbox(self, email_addr: str) -> str:
        if self.should_raise:
            raise self.should_raise
        clean = email_addr.strip().lower()
        self.labels[clean] = {}
        return clean

    def has_token(self, email_addr: str) -> bool:
        return email_addr.strip().lower() in self.labels or True

    def get_service(self, email_addr: str) -> Any:
        if self.should_raise:
            raise self.should_raise
        return self

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

    # Test inspection / helper methods
    def inject_incoming_message(self, email_addr: str, meta: MessageMeta, text_body: str = "") -> None:
        clean = email_addr.strip().lower()
        if clean not in self.inbox_messages:
            self.inbox_messages[clean] = []
        self.inbox_messages[clean].append(meta)
        if text_body:
            self.message_texts[meta.id] = text_body

