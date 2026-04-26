"""Tests for src/newsletters.py — fakes EmailClient + ClaudeClient."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src import newsletters
from src.claude_client import HAIKU, CallResult
from src.email_client import Email
from src.newsletters import (
    NewsItem,
    NewsletterConfig,
    NewsletterEmail,
    NewslettersResult,
    _build_user_message,
    _format_bodies,
    _html_to_text,
    _parse_items,
    curate_top_stories,
    fetch_newsletter_bodies,
    fetch_newsletters_section,
    filter_newsletters,
    load_newsletters_config,
    render_markdown,
)

UTC = timezone.utc


# ---------- helpers ----------

def _email(addr: str, subject: str = "Daily AI", msg_id: str = "m1") -> Email:
    return Email(
        id=msg_id,
        subject=subject,
        sender_name="Newsletter",
        sender_address=addr,
        received_at=datetime(2026, 4, 25, 10, 0, tzinfo=UTC),
        body_preview="",
    )


def _cfg(name: str = "TLDR AI", sender: str = "dan@tldrnewsletter.com") -> NewsletterConfig:
    return NewsletterConfig(name=name, sender=sender.lower())


def _claude_returning(text: str) -> MagicMock:
    c = MagicMock()
    c.call.return_value = CallResult(
        text=text, input_tokens=100, output_tokens=50,
        cache_read_tokens=0, cache_creation_tokens=0,
        model=HAIKU, stop_reason="end_turn",
    )
    return c


# ---------- load_newsletters_config ----------

def test_load_newsletters_config_round_trip():
    cfgs = load_newsletters_config()
    senders = {c.sender for c in cfgs}
    assert "dan@tldrnewsletter.com" in senders
    assert "news@daily.therundown.ai" in senders
    assert all(c.sender == c.sender.lower() for c in cfgs)


def test_load_newsletters_config_skips_inactive(tmp_path):
    p = tmp_path / "newsletters.yml"
    p.write_text(
        "newsletters:\n"
        "  - name: Live\n    sender: a@x.com\n    active: true\n"
        "  - name: Off\n    sender: b@x.com\n    active: false\n"
    )
    cfgs = load_newsletters_config(p)
    assert [c.name for c in cfgs] == ["Live"]


# ---------- filter_newsletters ----------

def test_filter_newsletters_matches_case_insensitively():
    cfgs = [_cfg(sender="Dan@TLDRNewsletter.com")]
    emails = [
        _email("DAN@tldrnewsletter.COM", msg_id="hit"),
        _email("someone@else.com", msg_id="miss"),
    ]
    out = filter_newsletters(emails, cfgs)
    assert len(out) == 1
    assert out[0][0].id == "hit"
    assert out[0][1].name == "TLDR AI"


def test_filter_newsletters_drops_unconfigured_senders():
    cfgs = [_cfg()]
    emails = [_email("noreply@random.com")]
    assert filter_newsletters(emails, cfgs) == []


# ---------- _html_to_text ----------

def test_html_to_text_strips_tags_and_unescapes():
    html_in = "<p>Hello&nbsp;<b>world</b> &amp; friends</p>"
    # &nbsp; (\xa0) is unescaped then collapsed to a regular space by the
    # whitespace normalizer, which is what we want for clean prose output.
    assert _html_to_text(html_in) == "Hello world & friends"


def test_html_to_text_drops_style_and_script_blocks():
    html_in = (
        "<style>body{color:red}</style>"
        "<script>alert('x')</script>"
        "<p>visible</p>"
    )
    assert _html_to_text(html_in) == "visible"


def test_html_to_text_collapses_whitespace():
    assert _html_to_text("<p>a\n\n   b\t  c</p>") == "a b c"


def test_html_to_text_empty_input():
    assert _html_to_text("") == ""


# ---------- fetch_newsletter_bodies ----------

def test_fetch_newsletter_bodies_prefers_text_when_present():
    ec = MagicMock()
    ec.get_message_body.return_value = ("<p>html version</p>", "plain version")
    out = fetch_newsletter_bodies([(_email("dan@tldrnewsletter.com"), _cfg())], ec)
    assert len(out) == 1
    assert out[0].body_text == "plain version"


def test_fetch_newsletter_bodies_falls_back_to_html_strip():
    ec = MagicMock()
    ec.get_message_body.return_value = ("<p>HTML <b>only</b></p>", "")
    out = fetch_newsletter_bodies([(_email("dan@tldrnewsletter.com"), _cfg())], ec)
    assert out[0].body_text == "HTML only"


def test_fetch_newsletter_bodies_skips_empty():
    ec = MagicMock()
    ec.get_message_body.return_value = ("", "")
    out = fetch_newsletter_bodies([(_email("dan@tldrnewsletter.com"), _cfg())], ec)
    assert out == []


def test_fetch_newsletter_bodies_swallows_per_email_failure():
    ec = MagicMock()
    ec.get_message_body.side_effect = [
        RuntimeError("graph 500"),
        ("", "fine"),
    ]
    pairs = [
        (_email("a@x.com", msg_id="bad"), _cfg(name="A", sender="a@x.com")),
        (_email("b@x.com", msg_id="ok"), _cfg(name="B", sender="b@x.com")),
    ]
    out = fetch_newsletter_bodies(pairs, ec)
    assert [n.newsletter.name for n in out] == ["B"]


# ---------- _format_bodies / _build_user_message ----------

def _ne(name: str, body: str, subject: str = "Issue") -> NewsletterEmail:
    return NewsletterEmail(
        newsletter=_cfg(name=name, sender=f"{name}@x.com"),
        subject=subject,
        received_at=datetime(2026, 4, 25, tzinfo=UTC),
        body_text=body,
    )


def test_format_bodies_truncates_per_newsletter_evenly():
    bodies = _format_bodies([_ne("A", "x" * 5_000), _ne("B", "y" * 5_000)],
                            budget=4_000)
    # Per-newsletter floor is 2_000; both should be truncated.
    assert "…[truncated]" in bodies
    assert bodies.count("…[truncated]") == 2


def test_format_bodies_no_truncation_when_under_budget():
    bodies = _format_bodies([_ne("A", "short body")], budget=80_000)
    assert "…[truncated]" not in bodies
    assert "short body" in bodies


def test_format_bodies_attributes_each_newsletter():
    bodies = _format_bodies(
        [_ne("Alpha", "alpha body", subject="Sub-A"),
         _ne("Beta", "beta body", subject="Sub-B")],
        budget=10_000,
    )
    assert "### Alpha — Sub-A" in bodies
    assert "### Beta — Sub-B" in bodies


def test_format_bodies_handles_empty_list():
    assert _format_bodies([], budget=10_000) == "(no newsletters in the window)"


def test_build_user_message_includes_top_n_rules_buckets_and_names():
    prefs = {
        "newsletters": {
            "top_n": 3,
            "rules": [
                {"id": "finance_boost", "description": "Finance items boosted.", "weight": 2.0},
                {"id": "cross_sig", "description": "Repeated stories boosted.", "weight": 1.5},
            ],
            "default_buckets": ["model releases", "regulatory news"],
        }
    }
    msg = _build_user_message([_ne("TLDR AI", "body", subject="Issue 42")],
                              prefs, body_budget=10_000)
    assert "top 3 stories" in msg
    assert "Finance items boosted." in msg
    assert "model releases, regulatory news" in msg
    assert "### TLDR AI — Issue 42" in msg


def test_build_user_message_orders_rules_by_weight_desc():
    prefs = {
        "newsletters": {
            "rules": [
                {"description": "low", "weight": 1.0},
                {"description": "high", "weight": 3.0},
                {"description": "mid", "weight": 2.0},
            ],
        }
    }
    msg = _build_user_message([_ne("X", "y")], prefs, body_budget=10_000)
    high_idx = msg.index("high")
    mid_idx = msg.index("mid")
    low_idx = msg.index("low")
    assert high_idx < mid_idx < low_idx


# ---------- _parse_items ----------

def test_parse_items_plain_json():
    raw = json.dumps({
        "items": [
            {"headline": "H1", "summary": "S1.", "sources": ["TLDR AI"]},
            {"headline": "H2", "summary": "S2.", "sources": ["Ben's Bites"]},
        ]
    })
    out = _parse_items(raw)
    assert [i.headline for i in out] == ["H1", "H2"]
    assert out[0].sources == ["TLDR AI"]


def test_parse_items_strips_code_fence():
    raw = "```json\n" + json.dumps({"items": [{"headline": "H", "summary": "S"}]}) + "\n```"
    out = _parse_items(raw)
    assert len(out) == 1
    assert out[0].sources == []


def test_parse_items_drops_entries_missing_required_fields():
    raw = json.dumps({"items": [
        {"headline": "ok", "summary": "ok"},
        {"headline": "", "summary": "missing headline"},
        {"headline": "missing summary", "summary": ""},
    ]})
    out = _parse_items(raw)
    assert [i.headline for i in out] == ["ok"]


def test_parse_items_returns_empty_on_invalid_json():
    assert _parse_items("not even close to json") == []


# ---------- curate_top_stories ----------

def test_curate_returns_items_capped_to_top_n():
    items = [{"headline": f"H{i}", "summary": "s.", "sources": ["TLDR AI"]} for i in range(8)]
    claude = _claude_returning(json.dumps({"items": items}))
    prefs = {"newsletters": {"top_n": 3}}
    out, warnings = curate_top_stories([_ne("TLDR AI", "body")],
                                       claude=claude, preferences=prefs)
    assert len(out) == 3
    assert warnings == []
    # Verify the call routed through the Haiku tier with the system prompt set.
    kwargs = claude.call.call_args.kwargs
    assert kwargs["model"] == HAIKU
    assert "newsletter curator" in kwargs["system"].lower()


def test_curate_handles_empty_input_without_calling_claude():
    claude = _claude_returning("{}")
    out, warnings = curate_top_stories([], claude=claude, preferences={})
    assert out == []
    assert warnings and "No newsletters" in warnings[0]
    claude.call.assert_not_called()


def test_curate_warns_on_claude_failure():
    claude = MagicMock()
    claude.call.side_effect = RuntimeError("rate limit")
    out, warnings = curate_top_stories([_ne("X", "body")], claude=claude, preferences={})
    assert out == []
    assert any("Curation failed" in w for w in warnings)


def test_curate_warns_on_parse_failure():
    claude = _claude_returning("not json at all")
    out, warnings = curate_top_stories([_ne("X", "body")], claude=claude, preferences={})
    assert out == []
    assert any("parse failure" in w for w in warnings)


# ---------- fetch_newsletters_section ----------

def test_fetch_section_end_to_end():
    cfgs = [_cfg(name="TLDR AI", sender="dan@tldrnewsletter.com")]
    emails = [
        _email("dan@tldrnewsletter.com", subject="Today's AI", msg_id="n1"),
        _email("random@noise.com", msg_id="skip"),
    ]
    ec = MagicMock()
    ec.get_message_body.return_value = ("", "OpenAI shipped GPT-X today.")
    payload = {"items": [{"headline": "GPT-X ships",
                          "summary": "Big release.", "sources": ["TLDR AI"]}]}
    claude = _claude_returning(json.dumps(payload))

    result = fetch_newsletters_section(
        emails, email_client=ec, claude=claude,
        configs=cfgs, preferences={"newsletters": {"top_n": 5}},
    )
    assert isinstance(result, NewslettersResult)
    assert [n.newsletter.name for n in result.received] == ["TLDR AI"]
    assert [i.headline for i in result.items] == ["GPT-X ships"]
    assert result.warnings == []
    ec.get_message_body.assert_called_once_with("n1")


def test_fetch_section_no_newsletters_in_window():
    ec = MagicMock()
    claude = _claude_returning("{}")
    result = fetch_newsletters_section(
        [_email("random@noise.com")],
        email_client=ec, claude=claude,
        configs=[_cfg()], preferences={},
    )
    assert result.received == []
    assert result.items == []
    assert any("No newsletters" in w for w in result.warnings)
    ec.get_message_body.assert_not_called()


# ---------- render_markdown ----------

def test_render_markdown_full_section():
    result = NewslettersResult(
        received=[_ne("TLDR AI", "body", subject="Issue 42"),
                  _ne("Ben's Bites", "body", subject="Today")],
        items=[
            NewsItem("Anthropic ships Opus 4.7",
                     "Big model release. Notable for FP&A workloads.",
                     ["TLDR AI", "Ben's Bites"]),
            NewsItem("EU AI Act enforcement begins",
                     "Compliance window closes.", ["TLDR AI"]),
        ],
    )
    md = render_markdown(result)
    assert "## Newsletters" in md
    assert "### Newsletters Received" in md
    assert "- TLDR AI: Issue 42" in md
    assert "- Ben's Bites: Today" in md
    assert "### Top AI Stories" in md
    assert "1. Anthropic ships Opus 4.7" in md
    assert "Sources: TLDR AI, Ben's Bites" in md
    assert "2. EU AI Act enforcement begins" in md


def test_render_markdown_received_but_no_items_shows_fallback():
    result = NewslettersResult(
        received=[_ne("TLDR AI", "body")],
        items=[],
        warnings=["Curation returned no items."],
    )
    md = render_markdown(result)
    assert "Newsletters Received" in md
    assert "Curation unavailable" in md
    assert "Curation returned no items." in md


def test_render_markdown_nothing_to_show():
    md = render_markdown(NewslettersResult(received=[], items=[]))
    assert "Section unavailable" in md


def test_render_markdown_omits_sources_line_when_empty():
    result = NewslettersResult(
        received=[_ne("TLDR AI", "body")],
        items=[NewsItem("H", "S.", [])],
    )
    md = render_markdown(result)
    assert "1. H" in md
    assert "Sources:" not in md
