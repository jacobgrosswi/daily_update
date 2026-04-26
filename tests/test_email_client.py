"""Tests for src/email_client.py — mocks httpx + MSAL."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from src.email_client import Email, EmailClient, _iso, _parse_message

UTC = timezone.utc


def _msal_app(token: str = "fake-access-token", rotated_rt: str | None = None):
    """Build a MagicMock that quacks like msal.ConfidentialClientApplication."""
    app = MagicMock()
    payload = {"access_token": token}
    if rotated_rt:
        payload["refresh_token"] = rotated_rt
    app.acquire_token_by_refresh_token.return_value = payload
    return app


def _http_response(status: int = 200, json_data: dict | None = None,
                   content: bytes = b""):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.is_success = 200 <= status < 300
    r.json.return_value = json_data or {}
    r.content = content or (b"{}" if json_data else b"")
    r.text = ""
    r.request = MagicMock()
    return r


@pytest.fixture
def client():
    http = MagicMock(spec=httpx.Client)
    msal_app = _msal_app()
    return EmailClient(
        client_id="cid", client_secret="csec", refresh_token="rt",
        http_client=http, msal_app=msal_app,
    )


# ---------- Auth ----------

def test_acquire_access_token_caches_per_instance(client):
    client._acquire_access_token()
    client._acquire_access_token()
    assert client._msal.acquire_token_by_refresh_token.call_count == 1


def test_acquire_access_token_surfaces_failure():
    msal_app = MagicMock()
    msal_app.acquire_token_by_refresh_token.return_value = {
        "error": "invalid_grant", "error_description": "expired"
    }
    c = EmailClient(client_id="x", client_secret="x", refresh_token="x",
                    http_client=MagicMock(), msal_app=msal_app)
    with pytest.raises(RuntimeError, match="invalid_grant"):
        c._acquire_access_token()


def test_rotated_refresh_token_is_captured():
    msal_app = _msal_app(rotated_rt="new-rt")
    c = EmailClient(client_id="x", client_secret="x", refresh_token="old-rt",
                    http_client=MagicMock(), msal_app=msal_app)
    c._acquire_access_token()
    assert c.current_refresh_token == "new-rt"


# ---------- list_inbox ----------

def test_list_inbox_parses_messages(client):
    msg = {
        "id": "abc",
        "subject": "Hello",
        "from": {"emailAddress": {"name": "Alice", "address": "alice@example.com"}},
        "receivedDateTime": "2026-04-25T11:00:00Z",
        "bodyPreview": "Hi there",
        "conversationId": "conv-1",
        "internetMessageHeaders": [
            {"name": "X-Briefing-ID", "value": "2026-04-24"},
            {"name": "Some-Other", "value": "ignored"},
        ],
        "hasAttachments": False,
    }
    client._http.get.return_value = _http_response(200, {"value": [msg]})
    start = datetime(2026, 4, 25, 0, 0, 0, tzinfo=UTC)
    end = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)

    out = client.list_inbox(start, end)
    assert len(out) == 1
    e = out[0]
    assert e.id == "abc"
    assert e.sender_address == "alice@example.com"
    assert e.received_at == datetime(2026, 4, 25, 11, 0, 0, tzinfo=UTC)
    assert e.headers == {"x-briefing-id": "2026-04-24"}  # filtered to known headers
    assert e.conversation_id == "conv-1"


def test_list_inbox_follows_pagination(client):
    page1 = {"value": [{"id": "1", "subject": "a", "from": {"emailAddress": {"address": "x@y.com"}},
                        "receivedDateTime": "2026-04-25T10:00:00Z", "bodyPreview": ""}],
             "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"}
    page2 = {"value": [{"id": "2", "subject": "b", "from": {"emailAddress": {"address": "x@y.com"}},
                        "receivedDateTime": "2026-04-25T10:30:00Z", "bodyPreview": ""}]}
    client._http.get.side_effect = [_http_response(200, page1), _http_response(200, page2)]

    out = client.list_inbox(
        datetime(2026, 4, 25, tzinfo=UTC), datetime(2026, 4, 26, tzinfo=UTC)
    )
    assert [e.id for e in out] == ["1", "2"]
    assert client._http.get.call_count == 2
    # Second call should hit the nextLink without params (params=None).
    second = client._http.get.call_args_list[1]
    assert second.args[0] == "https://graph.microsoft.com/v1.0/next"
    assert second.kwargs.get("params") is None


def test_list_inbox_rejects_naive_datetime(client):
    with pytest.raises(ValueError, match="timezone-aware"):
        client.list_inbox(datetime(2026, 4, 25), datetime(2026, 4, 26))


def test_list_inbox_filter_uses_iso_z(client):
    client._http.get.return_value = _http_response(200, {"value": []})
    start = datetime(2026, 4, 25, 11, 30, 0, tzinfo=UTC)
    end = datetime(2026, 4, 25, 12, 30, 0, tzinfo=UTC)
    client.list_inbox(start, end)
    params = client._http.get.call_args.kwargs["params"]
    assert "2026-04-25T11:30:00Z" in params["$filter"]
    assert "2026-04-25T12:30:00Z" in params["$filter"]


# ---------- get_message_body ----------

def test_get_message_body_html(client):
    client._http.get.return_value = _http_response(200, {
        "body": {"contentType": "html", "content": "<p>hi</p>"}
    })
    html, text = client.get_message_body("msg-1")
    assert html == "<p>hi</p>"
    assert text == ""


def test_get_message_body_text(client):
    client._http.get.return_value = _http_response(200, {
        "body": {"contentType": "text", "content": "plain"}
    })
    html, text = client.get_message_body("msg-2")
    assert text == "plain"
    assert html == ""


# ---------- send_mail ----------

def test_send_mail_posts_correct_payload(client):
    client._http.post.return_value = _http_response(202)
    client.send_mail(
        to="a@b.com",
        subject="Daily Briefing - 2026-04-25",
        html_body="<p>hello</p>",
        extra_headers={"X-Briefing-ID": "2026-04-25"},
    )
    payload = client._http.post.call_args.kwargs["json"]
    msg = payload["message"]
    assert msg["subject"] == "Daily Briefing - 2026-04-25"
    assert msg["body"]["content"] == "<p>hello</p>"
    assert msg["toRecipients"][0]["emailAddress"]["address"] == "a@b.com"
    assert msg["internetMessageHeaders"] == [{"name": "X-Briefing-ID", "value": "2026-04-25"}]
    assert payload["saveToSentItems"] is True


def test_send_mail_rejects_non_x_headers(client):
    with pytest.raises(ValueError, match="X-"):
        client.send_mail(to="a@b.com", subject="s", html_body="b",
                         extra_headers={"Reply-To": "x@y.com"})


# ---------- error mapping ----------

def test_graph_error_message_is_surfaced(client):
    bad = MagicMock(spec=httpx.Response)
    bad.is_success = False
    bad.status_code = 401
    bad.json.return_value = {"error": {"code": "InvalidAuthenticationToken",
                                        "message": "Token expired"}}
    bad.text = ""
    bad.request = MagicMock()
    client._http.get.return_value = bad
    with pytest.raises(httpx.HTTPStatusError, match="InvalidAuthenticationToken"):
        client._get("https://graph.microsoft.com/v1.0/me")


# ---------- Email model ----------

def test_email_is_automated_via_unsubscribe_header():
    e = Email(
        id="1", subject="x", sender_name="N", sender_address="human@example.com",
        received_at=datetime(2026, 4, 25, tzinfo=UTC), body_preview="",
        headers={"list-unsubscribe": "<mailto:u@x.com>"},
    )
    assert e.is_automated


def test_email_is_automated_via_sender_prefix():
    e = Email(
        id="1", subject="x", sender_name="", sender_address="noreply@example.com",
        received_at=datetime(2026, 4, 25, tzinfo=UTC), body_preview="",
    )
    assert e.is_automated


def test_email_human_is_not_automated():
    e = Email(
        id="1", subject="x", sender_name="Alice", sender_address="alice@example.com",
        received_at=datetime(2026, 4, 25, tzinfo=UTC), body_preview="",
    )
    assert not e.is_automated


# ---------- pure helpers ----------

def test_iso_format_z_suffix():
    dt = datetime(2026, 4, 25, 11, 30, 0, tzinfo=UTC)
    assert _iso(dt) == "2026-04-25T11:30:00Z"


def test_parse_message_minimal():
    raw = {
        "id": "x", "subject": None,
        "from": {"emailAddress": {"address": "a@b.com"}},
        "receivedDateTime": "2026-04-25T11:00:00Z",
    }
    e = _parse_message(raw)
    assert e.subject == "(no subject)"
    assert e.sender_name == ""
