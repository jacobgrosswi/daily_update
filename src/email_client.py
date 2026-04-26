"""Microsoft Graph wrapper for reading and sending Outlook mail.

Auth model: a long-lived refresh token (90-day inactivity window for personal
Microsoft accounts) minted once via scripts/get_refresh_token.py and stored in
GitHub Secrets. Each run exchanges it for a short-lived access token via MSAL.

This module does not interpret message bodies — it just fetches metadata + raw
HTML/text. Bucketing and summarization live downstream in main.py + Claude.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx
import msal
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .utils import UTC, get_logger

log = get_logger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.Read", "Mail.Send"]  # offline_access is implied for refresh-token flow

# Headers we care about for filtering. Lower-cased for case-insensitive lookup.
_HEADERS_OF_INTEREST = {"list-unsubscribe", "x-briefing-id", "in-reply-to", "references"}


@dataclass
class Email:
    id: str
    subject: str
    sender_name: str
    sender_address: str
    received_at: datetime  # UTC-aware
    body_preview: str
    conversation_id: Optional[str] = None
    body_html: Optional[str] = None
    body_text: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)
    has_attachments: bool = False

    @property
    def is_automated(self) -> bool:
        """True if the message looks like a list/notification, not a person."""
        if "list-unsubscribe" in self.headers:
            return True
        addr = self.sender_address.lower()
        return any(addr.startswith(p) for p in ("noreply@", "no-reply@", "donotreply@",
                                                 "notifications@", "mailer-daemon@"))


def _retry_http():
    """Retry transient Graph errors per Section 9 of the scoping doc."""
    return retry(
        retry=retry_if_exception_type((httpx.HTTPError,)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )


class EmailClient:
    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
        msal_app: Optional[msal.ConfidentialClientApplication] = None,
    ):
        self._client_id = client_id or os.environ["MS_CLIENT_ID"]
        self._client_secret = client_secret or os.environ["MS_CLIENT_SECRET"]
        self._refresh_token = refresh_token or os.environ["MS_REFRESH_TOKEN"]
        self._http = http_client or httpx.Client(timeout=30.0)
        self._msal = msal_app or msal.ConfidentialClientApplication(
            client_id=self._client_id,
            client_credential=self._client_secret,
            authority=AUTHORITY,
        )
        self._access_token: Optional[str] = None

    # ---------- Auth ----------

    def _acquire_access_token(self) -> str:
        """Exchange the refresh token for a fresh access token. Cached per instance."""
        if self._access_token:
            return self._access_token
        result = self._msal.acquire_token_by_refresh_token(
            refresh_token=self._refresh_token,
            scopes=SCOPES,
        )
        if "access_token" not in result:
            raise RuntimeError(
                f"Token refresh failed: {result.get('error')} - {result.get('error_description')}"
            )
        # MSAL may return a rotated refresh token; surface it so callers can persist.
        new_rt = result.get("refresh_token")
        if new_rt and new_rt != self._refresh_token:
            log.info("Refresh token was rotated by Microsoft; update GitHub Secret to extend lifetime.")
            self._refresh_token = new_rt
        self._access_token = result["access_token"]
        return self._access_token

    @property
    def current_refresh_token(self) -> str:
        """The (possibly rotated) refresh token after the last auth call."""
        return self._refresh_token

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._acquire_access_token()}"}

    # ---------- Read ----------

    def list_inbox(self, start: datetime, end: datetime, page_size: int = 50) -> list[Email]:
        """List inbox messages received in [start, end), oldest first.

        Both bounds must be timezone-aware. Pagination is followed via @odata.nextLink.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware datetimes")

        params = {
            "$filter": (
                f"receivedDateTime ge {_iso(start)} and receivedDateTime lt {_iso(end)}"
            ),
            "$select": "id,subject,from,receivedDateTime,bodyPreview,"
                       "conversationId,internetMessageHeaders,hasAttachments",
            "$orderby": "receivedDateTime asc",
            "$top": str(page_size),
        }
        url = f"{GRAPH_BASE}/me/mailFolders/inbox/messages"
        out: list[Email] = []

        while url:
            data = self._get(url, params=params)
            for raw in data.get("value", []):
                out.append(_parse_message(raw))
            url = data.get("@odata.nextLink")
            params = None  # nextLink already encodes the params

        log.info("Graph list_inbox: %d messages between %s and %s", len(out), _iso(start), _iso(end))
        return out

    def get_message_body(self, message_id: str) -> tuple[str, str]:
        """Fetch the full HTML and text bodies of a single message."""
        params = {"$select": "body,bodyPreview"}
        data = self._get(f"{GRAPH_BASE}/me/messages/{message_id}", params=params)
        body = data.get("body", {}) or {}
        html = body.get("content", "") if body.get("contentType") == "html" else ""
        text = body.get("content", "") if body.get("contentType") == "text" else ""
        return html, text

    def list_replies_to_briefing(self, since: datetime) -> list[Email]:
        """Find replies to prior briefings (subject starts with 'Re: Daily Briefing').

        Used by the feedback loop. We look in the inbox for self-sent replies
        because Outlook delivers user replies into the inbox folder when the
        sender and recipient are the same address.
        """
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        params = {
            "$filter": (
                f"receivedDateTime ge {_iso(since)} and "
                "startswith(subject, 'Re: Daily Briefing')"
            ),
            "$select": "id,subject,from,receivedDateTime,bodyPreview,"
                       "conversationId,internetMessageHeaders",
            "$orderby": "receivedDateTime asc",
            "$top": "25",
        }
        data = self._get(f"{GRAPH_BASE}/me/mailFolders/inbox/messages", params=params)
        return [_parse_message(m) for m in data.get("value", [])]

    # ---------- Send ----------

    def send_mail(
        self,
        *,
        to: str,
        subject: str,
        html_body: str,
        extra_headers: Optional[dict[str, str]] = None,
        save_to_sent: bool = True,
    ) -> None:
        """Send an HTML email via Graph /me/sendMail.

        extra_headers must use names starting with 'X-' (Graph rejects others).
        """
        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
        if extra_headers:
            for name in extra_headers:
                if not name.lower().startswith("x-"):
                    raise ValueError(f"Graph only accepts custom headers prefixed with X-; got {name!r}")
            message["internetMessageHeaders"] = [
                {"name": k, "value": v} for k, v in extra_headers.items()
            ]
        payload = {"message": message, "saveToSentItems": save_to_sent}
        self._post(f"{GRAPH_BASE}/me/sendMail", json=payload)
        log.info("Graph send_mail: %r -> %s", subject, to)

    # ---------- HTTP plumbing ----------

    @_retry_http()
    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        r = self._http.get(url, headers=self._auth_headers(), params=params)
        _raise_for_graph(r)
        return r.json()

    @_retry_http()
    def _post(self, url: str, json: dict) -> Optional[dict]:
        r = self._http.post(url, headers=self._auth_headers(), json=json)
        _raise_for_graph(r)
        if r.status_code == 202 or not r.content:
            return None
        return r.json()


# ---------- Helpers ----------

def _iso(dt: datetime) -> str:
    """Graph wants ISO8601 in UTC with 'Z' suffix."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_message(raw: dict) -> Email:
    sender = (raw.get("from") or {}).get("emailAddress") or {}
    headers_raw = raw.get("internetMessageHeaders") or []
    headers = {
        h["name"].lower(): h["value"]
        for h in headers_raw
        if h.get("name", "").lower() in _HEADERS_OF_INTEREST
    }
    return Email(
        id=raw["id"],
        subject=raw.get("subject") or "(no subject)",
        sender_name=sender.get("name", ""),
        sender_address=sender.get("address", ""),
        received_at=datetime.fromisoformat(raw["receivedDateTime"].replace("Z", "+00:00")),
        body_preview=raw.get("bodyPreview", ""),
        conversation_id=raw.get("conversationId"),
        headers=headers,
        has_attachments=bool(raw.get("hasAttachments", False)),
    )


def _raise_for_graph(r: httpx.Response) -> None:
    """Raise a useful error including the Graph error code/message when present."""
    if r.is_success:
        return
    try:
        err = r.json().get("error", {})
        detail = f"{err.get('code')}: {err.get('message')}"
    except Exception:
        detail = r.text[:200]
    raise httpx.HTTPStatusError(
        f"Graph {r.status_code} {detail}", request=r.request, response=r
    )
