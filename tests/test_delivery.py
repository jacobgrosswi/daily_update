"""Tests for src/delivery.py — fakes EmailClient.send_mail."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from src.delivery import (
    DEFAULT_RECIPIENT,
    Briefing,
    briefing_id,
    briefing_subject,
    deliver,
    markdown_to_html,
    send_briefing,
    write_archive,
)

DATE = date(2026, 4, 25)
SAMPLE_MD = (
    "# Daily Briefing — 2026-04-25\n\n"
    "## Sports\n\n"
    "Brewers 5, Cubs 3 (final)\n  Winning pitcher: Skenes.\n\n"
    "## Market Update — Friday, April 24, 2026\n\n"
    "```\n"
    "S&P 500       5,487.21   +12.45   (+0.23%)\n"
    "Nasdaq       17,234.88   -45.12   (-0.26%)\n"
    "```\n\n"
    "## Newsletters\n\n"
    "### Top AI Stories\n\n"
    "1. Anthropic ships Opus 4.7\n"
    "   Notable for FP&A workloads.\n"
    "   Sources: TLDR AI\n"
)


# ---------- Naming ----------

def test_briefing_id_format():
    assert briefing_id(DATE) == "2026-04-25"


def test_briefing_subject_format():
    assert briefing_subject(DATE) == "Daily Briefing - 2026-04-25"


# ---------- markdown_to_html ----------

def test_markdown_to_html_renders_full_document():
    html = markdown_to_html(SAMPLE_MD, DATE)
    assert html.startswith("<!DOCTYPE html>")
    assert "<html" in html and "</html>" in html
    assert "<title>Daily Briefing — 2026-04-25</title>" in html
    assert "<style>" in html
    # Body contains rendered MD elements.
    assert "<h1>" in html  # the # heading
    assert "<h2>" in html  # the ## headings
    assert "<h3>" in html  # the ### heading


def test_markdown_to_html_preserves_fenced_code_block():
    html = markdown_to_html(SAMPLE_MD, DATE)
    assert "<pre>" in html
    assert "<code>" in html
    # The fixed-width market table content should land inside the pre.
    assert "S&amp;P 500" in html  # & is escaped by markdown lib
    assert "+12.45" in html


def test_markdown_to_html_renders_ordered_list():
    md = "1. First\n2. Second\n"
    html = markdown_to_html(md, DATE)
    assert "<ol>" in html
    assert "<li>First</li>" in html


def test_markdown_to_html_handles_empty_input():
    html = markdown_to_html("", DATE)
    assert "<body>" in html and "</body>" in html


def test_markdown_to_html_preserves_indented_continuation_lines():
    """Sports notes and numbered newsletter items rely on nl2br to keep their
    visual structure when rendered in Outlook."""
    md = (
        "Brewers 5, Cubs 3 (final)\n"
        "  Winning pitcher: Skenes.\n"
    )
    html = markdown_to_html(md, DATE)
    assert "<br" in html  # the \n between the two lines becomes a <br />
    assert "Winning pitcher: Skenes." in html


# ---------- write_archive ----------

def test_write_archive_creates_dir_and_writes_file(tmp_path):
    briefing = Briefing(briefing_date=DATE, markdown="# hello\n")
    archive_dir = tmp_path / "archive"
    path = write_archive(briefing, archive_dir=archive_dir)
    assert path == archive_dir / "2026-04-25.md"
    assert path.read_text() == "# hello\n"


def test_write_archive_appends_trailing_newline(tmp_path):
    briefing = Briefing(briefing_date=DATE, markdown="no newline")
    path = write_archive(briefing, archive_dir=tmp_path)
    assert path.read_text() == "no newline\n"


def test_write_archive_overwrites_existing_file(tmp_path):
    briefing = Briefing(briefing_date=DATE, markdown="first")
    write_archive(briefing, archive_dir=tmp_path)
    briefing.markdown = "second"
    path = write_archive(briefing, archive_dir=tmp_path)
    assert path.read_text() == "second\n"


# ---------- send_briefing ----------

def test_send_briefing_calls_send_mail_with_expected_fields():
    ec = MagicMock()
    briefing = Briefing(briefing_date=DATE, markdown=SAMPLE_MD)
    bid = send_briefing(briefing, email_client=ec, recipient="me@example.com")
    assert bid == "2026-04-25"
    ec.send_mail.assert_called_once()
    kwargs = ec.send_mail.call_args.kwargs
    assert kwargs["to"] == "me@example.com"
    assert kwargs["subject"] == "Daily Briefing - 2026-04-25"
    assert kwargs["extra_headers"] == {"X-Briefing-ID": "2026-04-25"}
    assert kwargs["html_body"].startswith("<!DOCTYPE html>")
    assert "Brewers" in kwargs["html_body"]


def test_send_briefing_uses_env_recipient_when_unspecified(monkeypatch):
    monkeypatch.setenv("BRIEFING_RECIPIENT", "env-target@example.com")
    ec = MagicMock()
    send_briefing(Briefing(DATE, "x"), email_client=ec)
    assert ec.send_mail.call_args.kwargs["to"] == "env-target@example.com"


def test_send_briefing_falls_back_to_default_recipient(monkeypatch):
    monkeypatch.delenv("BRIEFING_RECIPIENT", raising=False)
    ec = MagicMock()
    send_briefing(Briefing(DATE, "x"), email_client=ec)
    assert ec.send_mail.call_args.kwargs["to"] == DEFAULT_RECIPIENT


def test_send_briefing_propagates_send_failure():
    ec = MagicMock()
    ec.send_mail.side_effect = RuntimeError("graph 500")
    with pytest.raises(RuntimeError, match="graph 500"):
        send_briefing(Briefing(DATE, "x"), email_client=ec, recipient="x@x.com")


# ---------- deliver ----------

def test_deliver_writes_archive_then_sends(tmp_path):
    ec = MagicMock()
    briefing = Briefing(briefing_date=DATE, markdown=SAMPLE_MD)
    bid, path = deliver(
        briefing, email_client=ec,
        recipient="me@example.com", archive_dir=tmp_path,
    )
    assert bid == "2026-04-25"
    assert path == tmp_path / "2026-04-25.md"
    assert path.exists() and "Brewers" in path.read_text()
    ec.send_mail.assert_called_once()


def test_deliver_keeps_archive_even_when_send_fails(tmp_path):
    """Archive-before-send: a Graph failure must not lose the local copy."""
    ec = MagicMock()
    ec.send_mail.side_effect = RuntimeError("graph 500")
    briefing = Briefing(briefing_date=DATE, markdown="content")
    with pytest.raises(RuntimeError):
        deliver(briefing, email_client=ec,
                recipient="me@example.com", archive_dir=tmp_path)
    assert (tmp_path / "2026-04-25.md").exists()
