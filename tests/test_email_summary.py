"""Tests for src/email_summary.py — fakes ClaudeClient.call."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.email_client import Email
from src.email_summary import (
    EMAIL_BUCKETS,
    EmailItem,
    EmailSummaryResult,
    MAX_TRIAGE_ITEMS,
    _parse_items,
    fetch_email_summary_section,
    filter_emails,
    pre_bucket,
    render_markdown,
)

UTC = timezone.utc


def _email(
    *,
    eid: str = "0",
    subject: str = "Hello",
    sender_name: str = "Alice",
    sender_address: str = "alice@example.com",
    body_preview: str = "Hi there, just checking in.",
    headers: dict | None = None,
) -> Email:
    return Email(
        id=eid,
        subject=subject,
        sender_name=sender_name,
        sender_address=sender_address,
        received_at=datetime(2026, 4, 25, 12, 0, tzinfo=UTC),
        body_preview=body_preview,
        headers=headers or {},
    )


PREFS = {
    "email": {
        "buckets": {
            "orders_shipping": {
                "sender_keywords": ["amazon", "ups", "fedex"],
                "subject_keywords": ["shipped", "tracking"],
            },
            "appointments": {
                "sender_keywords": ["calendly"],
                "subject_keywords": ["appointment", "reminder"],
            },
        }
    }
}


def _claude_returning(text: str) -> MagicMock:
    c = MagicMock()
    c.call.return_value = SimpleNamespace(text=text)
    return c


# ---------- pre_bucket ----------

def test_pre_bucket_orders_by_sender():
    e = _email(sender_address="orders@amazon.com", subject="Your order")
    assert pre_bucket(e, PREFS) == "Orders & Shipping"


def test_pre_bucket_orders_by_subject():
    e = _email(sender_address="alice@example.com", subject="Your package was shipped")
    assert pre_bucket(e, PREFS) == "Orders & Shipping"


def test_pre_bucket_appointments_by_sender():
    e = _email(sender_address="notifications@calendly.com", subject="New event")
    assert pre_bucket(e, PREFS) == "Appointments"


def test_pre_bucket_appointments_by_subject():
    e = _email(sender_address="bob@gmail.com", subject="Friendly reminder for tomorrow")
    assert pre_bucket(e, PREFS) == "Appointments"


def test_pre_bucket_defaults_to_people():
    e = _email(sender_address="dad@example.com", subject="Quick question")
    assert pre_bucket(e, PREFS) == "People"


def test_pre_bucket_handles_missing_prefs():
    e = _email()
    assert pre_bucket(e, {}) == "People"


def test_pre_bucket_case_insensitive():
    e = _email(sender_address="ORDERS@AMAZON.COM", subject="Your order")
    assert pre_bucket(e, PREFS) == "Orders & Shipping"


# ---------- filter_emails ----------

def test_filter_drops_automated_and_newsletter_senders():
    auto = _email(eid="1", sender_address="noreply@updates.com")
    nl = _email(eid="2", sender_address="dan@tldrnewsletter.com")
    keep = _email(eid="3", sender_address="alice@example.com")
    out = filter_emails([auto, nl, keep], newsletter_senders={"dan@tldrnewsletter.com"})
    assert [e.id for e in out] == ["3"]


def test_filter_drops_list_unsubscribe_via_is_automated():
    e = _email(headers={"list-unsubscribe": "<mailto:x>"})
    assert filter_emails([e], newsletter_senders=set()) == []


def test_filter_keeps_when_no_filters_apply():
    e = _email()
    assert filter_emails([e], newsletter_senders=set()) == [e]


# ---------- _parse_items ----------

def test_parse_items_plain_json():
    raw = '{"items": [{"id": 0, "bucket": "People", "summary": "Hi"}]}'
    assert _parse_items(raw) == {0: {"id": 0, "bucket": "People", "summary": "Hi"}}


def test_parse_items_strips_code_fences():
    raw = '```json\n{"items": [{"id": 1, "bucket": "People", "summary": "x"}]}\n```'
    out = _parse_items(raw)
    assert 1 in out and out[1]["bucket"] == "People"


def test_parse_items_invalid_json_returns_empty():
    assert _parse_items("not json") == {}


def test_parse_items_skips_entries_without_id():
    raw = '{"items": [{"bucket": "People", "summary": "no id"}, {"id": 2, "bucket": "People", "summary": "ok"}]}'
    out = _parse_items(raw)
    assert list(out.keys()) == [2]


def test_parse_items_handles_string_id():
    raw = '{"items": [{"id": "5", "bucket": "People", "summary": "ok"}]}'
    assert 5 in _parse_items(raw)


# ---------- fetch_email_summary_section ----------

def test_fetch_section_groups_by_bucket():
    emails = [
        _email(eid="0", sender_address="alice@example.com", subject="Lunch?"),
        _email(eid="1", sender_address="orders@amazon.com", subject="Your shipment"),
        _email(eid="2", sender_address="cal@calendly.com", subject="New invite"),
    ]
    claude = _claude_returning(json.dumps({"items": [
        {"id": 0, "bucket": "People", "summary": "Alice asks about lunch."},
        {"id": 1, "bucket": "Orders & Shipping", "summary": "Amazon shipment update."},
        {"id": 2, "bucket": "Appointments", "summary": "Calendly invite from Cal."},
    ]}))
    res = fetch_email_summary_section(
        emails, claude=claude, prefs=PREFS, newsletter_senders=set(),
    )
    assert res.total_input == 3
    assert res.total_kept == 3
    assert [it.bucket for it in res.items_by_bucket["People"]] == ["People"]
    assert [it.subject for it in res.items_by_bucket["Orders & Shipping"]] == ["Your shipment"]
    assert [it.subject for it in res.items_by_bucket["Appointments"]] == ["New invite"]


def test_fetch_section_falls_back_when_bucket_invalid():
    emails = [_email(eid="0", sender_address="alice@example.com")]
    claude = _claude_returning(json.dumps({"items": [
        {"id": 0, "bucket": "Garbage Bucket", "summary": "x"},
    ]}))
    res = fetch_email_summary_section(
        emails, claude=claude, prefs=PREFS, newsletter_senders=set(),
    )
    # Falls back to pre_bucket suggestion (People for a generic sender).
    assert res.items_by_bucket["People"][0].bucket == "People"


def test_fetch_section_degrades_when_claude_raises():
    emails = [_email(eid="0", sender_address="alice@example.com", body_preview="hi")]
    claude = MagicMock()
    claude.call.side_effect = RuntimeError("anthropic 500")
    res = fetch_email_summary_section(
        emails, claude=claude, prefs=PREFS, newsletter_senders=set(),
    )
    assert res.warnings and "Claude" in res.warnings[0]
    # Item is preserved with pre-bucket and preview as summary.
    assert res.items_by_bucket["People"][0].summary == "hi"


def test_fetch_section_returns_zero_kept_when_all_filtered():
    emails = [_email(sender_address="noreply@x.com")]
    claude = MagicMock()
    res = fetch_email_summary_section(
        emails, claude=claude, prefs=PREFS, newsletter_senders=set(),
    )
    claude.call.assert_not_called()
    assert res.total_kept == 0
    assert res.total_input == 1


def test_fetch_section_truncates_to_max():
    emails = [_email(eid=str(i), subject=f"Subject {i}") for i in range(MAX_TRIAGE_ITEMS + 5)]
    claude = _claude_returning(json.dumps({"items": [
        {"id": i, "bucket": "People", "summary": "x"} for i in range(MAX_TRIAGE_ITEMS)
    ]}))
    res = fetch_email_summary_section(
        emails, claude=claude, prefs=PREFS, newsletter_senders=set(),
    )
    assert res.total_kept == MAX_TRIAGE_ITEMS
    assert res.truncated == 5
    assert any("truncated" in w.lower() for w in res.warnings)


def test_fetch_section_uses_preview_when_summary_missing():
    emails = [_email(eid="0", body_preview="The actual preview text here.")]
    claude = _claude_returning(json.dumps({"items": [
        {"id": 0, "bucket": "People", "summary": ""},
    ]}))
    res = fetch_email_summary_section(
        emails, claude=claude, prefs=PREFS, newsletter_senders=set(),
    )
    assert "actual preview" in res.items_by_bucket["People"][0].summary


# ---------- render_markdown ----------

def test_render_empty_window():
    res = EmailSummaryResult(items_by_bucket={b: [] for b in EMAIL_BUCKETS},
                             total_input=0, total_kept=0)
    out = render_markdown(res)
    assert out.startswith("## Email Summary")
    assert "No new personal mail" in out


def test_render_empty_window_notes_skipped_count():
    res = EmailSummaryResult(items_by_bucket={b: [] for b in EMAIL_BUCKETS},
                             total_input=12, total_kept=0)
    out = render_markdown(res)
    assert "12 automated/newsletter messages skipped" in out


def test_render_groups_by_bucket_in_canonical_order():
    res = EmailSummaryResult(
        items_by_bucket={
            "People": [EmailItem("Alice", "Lunch?", "People", "Alice asks about lunch.")],
            "Orders & Shipping": [EmailItem("Amazon", "Shipped", "Orders & Shipping", "On the way.")],
            "Appointments": [EmailItem("Calendly", "Invite", "Appointments", "New event.")],
        },
        total_input=3, total_kept=3,
    )
    out = render_markdown(res)
    # Order: People → Orders & Shipping → Appointments.
    assert out.index("### People") < out.index("### Orders & Shipping") < out.index("### Appointments")
    assert "Alice — Lunch?" in out
    assert "  Alice asks about lunch." in out


def test_render_omits_empty_buckets():
    res = EmailSummaryResult(
        items_by_bucket={
            "People": [EmailItem("Alice", "Hi", "People", "Just hi.")],
            "Orders & Shipping": [],
            "Appointments": [],
        },
        total_input=1, total_kept=1,
    )
    out = render_markdown(res)
    assert "### People" in out
    assert "### Orders & Shipping" not in out
    assert "### Appointments" not in out


def test_render_shows_warnings_in_italics():
    res = EmailSummaryResult(
        items_by_bucket={
            "People": [EmailItem("Alice", "Hi", "People", "Just hi.")],
            "Orders & Shipping": [],
            "Appointments": [],
        },
        total_input=1, total_kept=1,
        warnings=["Triage degraded."],
    )
    out = render_markdown(res)
    assert "_Triage degraded._" in out
