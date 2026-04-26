"""Briefing delivery: Markdown → HTML → Graph send + archive write.

Contract with main.py: hand `deliver()` a fully-composed Briefing (the
concatenated section markdown plus a date) and an EmailClient. It writes
the raw markdown to archive/YYYY-MM-DD.md and sends the HTML email.
The archive write happens before the send so we keep a local copy even
if Graph rejects the message.

The X-Briefing-ID header on the outbound email is what the feedback loop
keys on the next day to find replies to a specific briefing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import markdown as md_lib

from .email_client import EmailClient
from .utils import REPO_ROOT, get_logger

log = get_logger(__name__)

ARCHIVE_DIR = REPO_ROOT / "archive"
DEFAULT_RECIPIENT = "jacobgrosswi@outlook.com"

# nl2br preserves the indented continuation lines we use for sports notes and
# numbered newsletter items — without it, every \n inside a paragraph collapses
# to a space and the briefing reads as one wall of text.
_MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

# Inline-friendly CSS. Recipient is Outlook (web + desktop), which honours
# <head><style> blocks for basic selectors. Kept tight; no media queries.
_HTML_STYLES = """
  body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         font-size: 14px; line-height: 1.5; color: #222;
         max-width: 720px; margin: 0; padding: 0 4px; }
  h1, h2, h3 { color: #111; }
  h1 { font-size: 22px; margin: 0 0 16px; }
  h2 { font-size: 18px; border-bottom: 1px solid #e0e0e0;
       padding-bottom: 4px; margin-top: 28px; }
  h3 { font-size: 15px; margin-top: 18px; }
  pre { font-family: ui-monospace, "SF Mono", Consolas, "Roboto Mono", monospace;
        font-size: 13px; background: #f6f8fa; padding: 8px 12px;
        border-radius: 6px; white-space: pre; overflow-x: auto; }
  code { font-family: ui-monospace, "SF Mono", Consolas, "Roboto Mono", monospace; }
  ul, ol { padding-left: 24px; }
  li { margin: 2px 0; }
  a { color: #0366d6; }
  em { color: #555; }
"""


@dataclass
class Briefing:
    briefing_date: date
    markdown: str


# ---------- Naming ----------

def briefing_id(briefing_date: date) -> str:
    """Identifier used for the X-Briefing-ID header and archive filename."""
    return briefing_date.isoformat()


def briefing_subject(briefing_date: date) -> str:
    """Subject line that the feedback loop matches via 'Re: Daily Briefing'."""
    return f"Daily Briefing - {briefing_date.isoformat()}"


# ---------- HTML rendering ----------

def markdown_to_html(markdown_text: str, briefing_date: date) -> str:
    """Render briefing markdown to a self-contained HTML document."""
    body = md_lib.markdown(markdown_text, extensions=_MD_EXTENSIONS)
    title = f"Daily Briefing — {briefing_date.isoformat()}"
    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        f'<title>{title}</title>\n'
        f'<style>{_HTML_STYLES}</style>\n'
        '</head>\n'
        '<body>\n'
        f'{body}\n'
        '</body>\n'
        '</html>\n'
    )


# ---------- Archive ----------

def write_archive(briefing: Briefing, archive_dir: Path = ARCHIVE_DIR) -> Path:
    """Write the briefing's raw markdown to archive/YYYY-MM-DD.md."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{briefing_id(briefing.briefing_date)}.md"
    text = briefing.markdown if briefing.markdown.endswith("\n") else briefing.markdown + "\n"
    path.write_text(text)
    log.info("Archive written: %s (%d chars)", path, len(text))
    return path


# ---------- Send ----------

def send_briefing(
    briefing: Briefing,
    *,
    email_client: EmailClient,
    recipient: Optional[str] = None,
) -> str:
    """Render and send the briefing. Returns the briefing id."""
    to = recipient or os.environ.get("BRIEFING_RECIPIENT", DEFAULT_RECIPIENT)
    bid = briefing_id(briefing.briefing_date)
    html = markdown_to_html(briefing.markdown, briefing.briefing_date)
    email_client.send_mail(
        to=to,
        subject=briefing_subject(briefing.briefing_date),
        html_body=html,
        extra_headers={"X-Briefing-ID": bid},
    )
    log.info("Briefing sent: id=%s recipient=%s", bid, to)
    return bid


# ---------- Orchestrator ----------

def deliver(
    briefing: Briefing,
    *,
    email_client: EmailClient,
    recipient: Optional[str] = None,
    archive_dir: Path = ARCHIVE_DIR,
) -> tuple[str, Path]:
    """Write archive, then send. Returns (briefing_id, archive_path).

    Archive-before-send means a Graph-side send failure still leaves a
    debuggable copy on disk that the workflow can decide whether to keep.
    """
    path = write_archive(briefing, archive_dir=archive_dir)
    bid = send_briefing(briefing, email_client=email_client, recipient=recipient)
    return bid, path
