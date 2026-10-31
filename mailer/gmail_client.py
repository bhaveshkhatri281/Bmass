"""Gmail API client: OAuth flow, message sending, retrieval, and labeling."""

from __future__ import annotations

import base64
import email.header
import email.utils
import http.client
import json
import logging
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from mailer.config import PROJECT_ROOT

logger = logging.getLogger("gmail_client")

SECRETS_DIR = PROJECT_ROOT / "secrets"
CREDENTIALS_PATH = SECRETS_DIR / "credentials.json"
TOKENS_DIR = SECRETS_DIR / "tokens"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

LABELS = {
    "replied": "Mobisa/Replied",
    "unsubscribed": "Mobisa/Unsubscribed",
    "bounced": "Mobisa/Bounced",
}

OAUTH_TIMEOUT_SECONDS = 300

# A 400 only counts as permanent when the address itself is at fault. Anything
# else (bad thread id, account problem) must not suppress a healthy lead.
RECIPIENT_ERROR_HINTS = ("invalid to header", "invalid recipient")

NETWORK_ERRORS = (OSError, http.client.HTTPException, httplib2.HttpLib2Error, TransportError)

MAX_RETRIES = 3

# Tolerance for the PC clock and Gmail's clock disagreeing when matching a sent copy.
SENT_LOOKUP_SKEW_SECONDS = 60


# ==============================================================================
# Custom Exceptions
# ==============================================================================

class GmailClientError(Exception):
    """Base exception for Gmail API client errors."""


class RateLimited(GmailClientError):
    """Raised when Gmail rate limits requests (HTTP 429 or rateLimitExceeded)."""


class QuotaExceeded(GmailClientError):
    """Raised when Gmail sending quota is reached (dailyLimitExceeded)."""


class AuthExpired(GmailClientError):
    """Raised when OAuth token is expired and cannot be refreshed."""


class PermanentSendError(GmailClientError):
    """Raised for non-retryable send errors (e.g., malformed recipient, invalid data)."""


class TransientError(GmailClientError):
    """Raised for retryable temporary network or 5xx server errors."""


# ==============================================================================
# Data Models
# ==============================================================================

@dataclass
class SentResult:
    gmail_message_id: str
    thread_id: str
    rfc_message_id: str


@dataclass
class MessageMeta:
    id: str
    thread_id: str
    snippet: str
    from_: str = ""
    to: str = ""
    subject: str = ""
    auto_submitted: Optional[str] = None
    x_autoreply: Optional[str] = None
    x_autorespond: Optional[str] = None
    precedence: Optional[str] = None
    x_failed_recipients: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)


# ==============================================================================
# Helper & Error Mapping
# ==============================================================================

def _token_path_for_email(email_addr: str) -> Path:
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    clean_email = email_addr.strip().lower()
    return TOKENS_DIR / f"{clean_email}.json"


def _map_http_error(err: HttpError) -> GmailClientError:
    status = err.resp.status
    error_msg = str(err)
    try:
        content = json.loads(err.content.decode("utf-8")) if hasattr(err, "content") and err.content else {}
        err_details = content.get("error", {})
        errors_list = err_details.get("errors", [])
        reasons = [e.get("reason", "") for e in errors_list]
        msg = err_details.get("message", error_msg)
    except Exception:
        reasons = []
        msg = error_msg

    msg_lower = msg.lower()

    # Quota comes first: Gmail can report the daily limit with a 429 or a rate-limit reason.
    if "dailyLimitExceeded" in reasons or "sending limit exceeded" in msg_lower:
        return QuotaExceeded(f"Gmail sending quota exceeded: {msg}")

    if status == 429 or "rateLimitExceeded" in reasons or "userRateLimitExceeded" in reasons:
        return RateLimited(f"Gmail rate limit hit: {msg}")

    if status == 401 or "invalid_grant" in msg_lower:
        return AuthExpired(f"Gmail authentication expired: {msg}")

    if status == 400 and any(hint in msg_lower for hint in RECIPIENT_ERROR_HINTS):
        return PermanentSendError(f"Permanent Gmail API error ({status}): {msg}")

    if 500 <= status <= 599:
        return TransientError(f"Transient Gmail server error ({status}): {msg}")

    return GmailClientError(f"Gmail API error ({status}): {msg}")


def execute_with_retry(callable_func: Any, max_retries: int = MAX_RETRIES) -> Any:
    """Execute a callable, retrying TransientError up to max_retries times with exponential backoff."""
    backoff = 1.0
    attempt = 0
    while True:
        try:
            return callable_func()
        except HttpError as e:
            error = _map_http_error(e)
        except NETWORK_ERRORS as e:
            error = TransientError(f"Network error reaching Gmail: {e}")

        if not isinstance(error, TransientError) or attempt >= max_retries:
            raise error

        attempt += 1
        logger.warning("Transient Gmail error, retry %d/%d in %.1fs: %s", attempt, max_retries, backoff, error)
        time.sleep(backoff)
        backoff *= 2


# ==============================================================================
# OAuth & Connection
# ==============================================================================

def connect_inbox(email_addr: str) -> str:
    """Run local OAuth flow and verify logged-in account matches target email."""
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Credentials file not found at {CREDENTIALS_PATH}. "
            "Please follow the setup steps in README.md and place credentials.json in the secrets/ folder."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    # Run local server flow with prompt='consent'
    creds = flow.run_local_server(
        port=0,
        login_hint=email_addr,
        prompt="consent",
        timeout_seconds=OAUTH_TIMEOUT_SECONDS,
    )

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = execute_with_retry(lambda: service.users().getProfile(userId="me").execute())
    verified_email = (profile.get("emailAddress") or "").strip().lower()
    expected_email = email_addr.strip().lower()

    if verified_email != expected_email:
        raise ValueError(
            f"Account mismatch: Expected to authenticate '{expected_email}', "
            f"but authenticated as '{verified_email}'. Connection rejected."
        )

    token_file = _token_path_for_email(expected_email)
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    logger.info("Successfully authenticated inbox: %s", expected_email)
    return verified_email


def has_token(email_addr: str) -> bool:
    """Check if token file exists for the given inbox email."""
    token_file = _token_path_for_email(email_addr)
    return token_file.exists()


def get_service(email_addr: str) -> Resource:
    """Load valid Gmail API service resource, refreshing token if expired."""
    token_file = _token_path_for_email(email_addr)
    if not token_file.exists():
        raise AuthExpired(f"No credentials found for {email_addr}. Please connect this inbox via the Inboxes page.")

    try:
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    except Exception as e:
        raise AuthExpired(f"Could not load token file for {email_addr}: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_file, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
            except TransportError as e:
                # A network blip must not look like a revoked token.
                raise TransientError(f"Network error refreshing token for {email_addr}: {e}")
            except RefreshError as e:
                logger.error("Failed to refresh token for %s: %s", email_addr, e)
                raise AuthExpired(f"OAuth refresh failed for {email_addr}. Please reconnect inbox: {e}")
        else:
            raise AuthExpired(f"Invalid credentials for {email_addr}. Please reconnect inbox.")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ==============================================================================
# Sending Messages
# ==============================================================================

def build_message(
    email_addr: str,
    to: str,
    subject: str,
    body: str,
    display_name: str,
    in_reply_to: Optional[str] = None,
    list_unsubscribe: Optional[str] = None,
) -> EmailMessage:
    """Build the RFC 5322 plain text message for one send."""
    msg = EmailMessage()
    msg.set_content(body)

    # From header
    if display_name:
        msg["From"] = email.utils.formataddr((display_name, email_addr))
    else:
        msg["From"] = email_addr

    msg["To"] = to
    msg["Subject"] = subject

    # Threading headers for follow-ups
    if in_reply_to:
        clean_in_reply_to = in_reply_to if in_reply_to.startswith("<") and in_reply_to.endswith(">") else f"<{in_reply_to}>"
        msg["In-Reply-To"] = clean_in_reply_to
        msg["References"] = clean_in_reply_to

    # List-Unsubscribe header
    if list_unsubscribe:
        msg["List-Unsubscribe"] = f"<mailto:{list_unsubscribe}?subject=unsubscribe>"

    return msg


def _normalize_header(value: str) -> str:
    """Decode an RFC 2047 header value and collapse its whitespace."""
    return " ".join(str(email.header.make_header(email.header.decode_header(value))).split())


def _find_sent_copy(service: Resource, msg: EmailMessage, thread_id: Optional[str], not_before_ms: int) -> Optional[SentResult]:
    """Return the Sent copy of msg if an interrupted send reached Gmail, else None.

    Gmail search is loose, so each hit is checked against the message headers. Without
    that, step 3 would match step 2: same recipient, same "Re:" subject, same hour.
    """
    to = msg["To"]
    subject = msg["Subject"]
    in_reply_to = msg.get("In-Reply-To", "")
    query = f'in:sent to:{to} subject:"{subject.replace(chr(34), " ")}" newer_than:1h'

    def _call_list() -> Dict[str, Any]:
        return service.users().messages().list(userId="me", q=query, maxResults=10).execute()

    for item in execute_with_retry(_call_list).get("messages", []):
        def _call_get() -> Dict[str, Any]:
            return (
                service.users()
                .messages()
                .get(userId="me", id=item["id"], format="metadata",
                     metadataHeaders=["To", "Subject", "In-Reply-To", "Message-ID"])
                .execute()
            )

        data = execute_with_retry(_call_get)
        headers = {h.get("name", "").lower(): h.get("value", "") for h in data.get("payload", {}).get("headers", [])}

        if int(data.get("internalDate", 0)) < not_before_ms:
            continue
        if thread_id and data.get("threadId") != thread_id:
            continue
        if _normalize_header(headers.get("subject", "")) != _normalize_header(subject):
            continue
        if to.lower() not in headers.get("to", "").lower():
            continue
        if headers.get("in-reply-to", "").strip() != in_reply_to:
            continue

        return SentResult(
            gmail_message_id=data["id"],
            thread_id=data.get("threadId", thread_id or ""),
            rfc_message_id=headers.get("message-id", "").strip(),
        )

    return None


def send_message(
    email_addr: str,
    to: str,
    subject: str,
    body: str,
    display_name: str,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    list_unsubscribe: Optional[str] = None,
) -> SentResult:
    """Build RFC 5322 plain text email and send via Gmail API.

    A timeout can hide a send that worked, so before each retry the Sent folder is
    searched. If that search fails too, the error is raised and nothing is resent.
    """
    service = get_service(email_addr)

    msg = build_message(email_addr, to, subject, body, display_name, in_reply_to, list_unsubscribe)
    raw_bytes = msg.as_bytes()
    raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")

    send_payload: Dict[str, Any] = {"raw": raw_b64}
    if thread_id:
        send_payload["threadId"] = thread_id

    def _call_send() -> Dict[str, Any]:
        return service.users().messages().send(userId="me", body=send_payload).execute()

    not_before_ms = int((time.time() - SENT_LOOKUP_SKEW_SECONDS) * 1000)
    backoff = 1.0
    attempt = 0
    while True:
        try:
            sent_data = execute_with_retry(_call_send, max_retries=0)
            break
        except TransientError as e:
            logger.warning("Send from %s to %s interrupted: %s. Checking Sent before retrying.", email_addr, to, e)
            # Waiting first gives Gmail time to index a send that did go through.
            time.sleep(backoff)
            backoff *= 2
            existing = _find_sent_copy(service, msg, thread_id, not_before_ms)
            if existing:
                logger.info("Interrupted send from %s to %s had reached Gmail (Gmail ID: %s). Not resending.",
                            email_addr, to, existing.gmail_message_id)
                return existing
            if attempt >= MAX_RETRIES:
                raise
            attempt += 1

    gmail_msg_id = sent_data["id"]
    res_thread_id = sent_data.get("threadId", thread_id or "")

    # Retrieve real RFC 5322 Message-ID header
    def _call_get_meta() -> Dict[str, Any]:
        return (
            service.users()
            .messages()
            .get(userId="me", id=gmail_msg_id, format="metadata", metadataHeaders=["Message-ID"])
            .execute()
        )

    rfc_message_id = ""
    try:
        meta_data = execute_with_retry(_call_get_meta)
        headers = meta_data.get("payload", {}).get("headers", [])
        for h in headers:
            if h.get("name", "").lower() == "message-id":
                rfc_message_id = h.get("value", "").strip()
                break
    except Exception as e:
        logger.warning("Could not fetch Message-ID for sent message %s: %s", gmail_msg_id, e)

    logger.info("Sent email from %s to %s (Gmail ID: %s, Thread ID: %s)",
                email_addr, to, gmail_msg_id, res_thread_id)

    return SentResult(
        gmail_message_id=gmail_msg_id,
        thread_id=res_thread_id,
        rfc_message_id=rfc_message_id,
    )


# ==============================================================================
# Message Retrieval & Labels
# ==============================================================================

def list_recent_messages(email_addr: str, query: str) -> List[MessageMeta]:
    """List recent messages matching query with metadata headers, following every result page."""
    service = get_service(email_addr)
    results: List[MessageMeta] = []
    msg_summaries: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    def _call_list() -> Dict[str, Any]:
        return service.users().messages().list(userId="me", q=query, maxResults=100, pageToken=page_token).execute()

    while True:
        list_resp = execute_with_retry(_call_list)
        msg_summaries.extend(list_resp.get("messages", []))
        page_token = list_resp.get("nextPageToken")
        if not page_token:
            break

    headers_to_fetch = [
        "From",
        "To",
        "Subject",
        "Auto-Submitted",
        "X-Autoreply",
        "X-Autorespond",
        "Precedence",
        "X-Failed-Recipients",
    ]

    for item in msg_summaries:
        msg_id = item["id"]
        try:
            def _call_get() -> Dict[str, Any]:
                return (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="metadata", metadataHeaders=headers_to_fetch)
                    .execute()
                )

            data = execute_with_retry(_call_get)
            hdrs: Dict[str, str] = {}
            for h in data.get("payload", {}).get("headers", []):
                hdrs[h.get("name", "").lower()] = h.get("value", "")

            meta = MessageMeta(
                id=msg_id,
                thread_id=data.get("threadId", ""),
                snippet=data.get("snippet", ""),
                from_=hdrs.get("from", ""),
                to=hdrs.get("to", ""),
                subject=hdrs.get("subject", ""),
                auto_submitted=hdrs.get("auto-submitted"),
                x_autoreply=hdrs.get("x-autoreply"),
                x_autorespond=hdrs.get("x-autorespond"),
                precedence=hdrs.get("precedence"),
                x_failed_recipients=hdrs.get("x-failed-recipients"),
                headers=hdrs,
            )
            results.append(meta)
        except (AuthExpired, QuotaExceeded, RateLimited):
            raise
        except GmailClientError as e:
            logger.warning("Could not fetch metadata for message %s: %s", msg_id, e)

    return results


def get_message_text(email_addr: str, msg_id: str) -> str:
    """Fetch full message content and decode text/plain parts for bounce diagnosis."""
    service = get_service(email_addr)

    def _call_get_full() -> Dict[str, Any]:
        return service.users().messages().get(userId="me", id=msg_id, format="full").execute()

    data = execute_with_retry(_call_get_full)

    parts_text: List[str] = []

    def _extract_parts(payload: Dict[str, Any]) -> None:
        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data", "")
        if mime_type.startswith("text/plain") and body_data:
            # Gmail may omit base64 padding, and the decoder rejects that.
            padded = body_data + "=" * (-len(body_data) % 4)
            parts_text.append(base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace"))
        for child in payload.get("parts", []):
            _extract_parts(child)

    payload = data.get("payload", {})
    _extract_parts(payload)
    return "\n".join(parts_text)


def ensure_label(email_addr: str, name: str) -> str:
    """Ensure a user label exists in Gmail, creating it if needed, and return labelId."""
    service = get_service(email_addr)

    def _call_list_labels() -> Dict[str, Any]:
        return service.users().labels().list(userId="me").execute()

    labels_data = execute_with_retry(_call_list_labels)
    for lbl in labels_data.get("labels", []):
        if lbl.get("name", "").lower() == name.lower():
            return lbl["id"]

    # Label doesn't exist; create it
    create_body = {
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }

    def _call_create_label() -> Dict[str, Any]:
        return service.users().labels().create(userId="me", body=create_body).execute()

    new_lbl = execute_with_retry(_call_create_label)
    logger.info("Created label '%s' (id: %s) in inbox %s", name, new_lbl["id"], email_addr)
    return new_lbl["id"]


def add_label(email_addr: str, msg_id: str, label_id: str) -> None:
    """Attach a label to a specific Gmail message."""
    service = get_service(email_addr)

    def _call_modify() -> Dict[str, Any]:
        return service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    try:
        execute_with_retry(_call_modify)
    except Exception as e:
        logger.warning("Failed to apply label %s to message %s: %s", label_id, msg_id, e)

