"""Newsletter section: filter to known senders, curate top stories via Claude.

Section 4.4 of the scoping doc:
- List which configured newsletters were received in the window (sender + subject).
- Claude (Haiku 4.5) ingests all bodies, dedupes overlapping stories, returns
  the top N items with sources, applying curation rules from preferences.yml.
- Each item is 2-7 sentences with a finance/FP&A angle when one exists.

Body fetch is a second Graph call per matched email — list_inbox returns
metadata only — so we keep the active set tight (5 newsletters/day in v1).
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .claude_client import HAIKU, ClaudeClient
from .email_client import Email, EmailClient
from .utils import REPO_ROOT, get_logger

log = get_logger(__name__)

NEWSLETTERS_PATH = REPO_ROOT / "config" / "newsletters.yml"
PREFERENCES_PATH = REPO_ROOT / "config" / "preferences.yml"

# Per-run cap on total characters of newsletter body sent to Claude. Keeps
# the daily curation cost predictable so the budget guardrail (Step 12) has
# a stable baseline. ~80k chars ≈ ~20k input tokens on Haiku.
DEFAULT_BODY_CHAR_BUDGET = 80_000


# ---------- Models ----------

@dataclass(frozen=True)
class NewsletterConfig:
    name: str
    sender: str  # lowercased


@dataclass
class NewsletterEmail:
    """One newsletter email with its body fetched and decoded."""
    newsletter: NewsletterConfig
    subject: str
    received_at: datetime
    body_text: str


@dataclass
class NewsItem:
    headline: str
    summary: str
    sources: list[str]


@dataclass
class NewslettersResult:
    received: list[NewsletterEmail]
    items: list[NewsItem]
    warnings: list[str] = field(default_factory=list)


# ---------- Config ----------

def load_newsletters_config(path: Path = NEWSLETTERS_PATH) -> list[NewsletterConfig]:
    data = yaml.safe_load(path.read_text())
    return [
        NewsletterConfig(name=n["name"], sender=n["sender"].lower())
        for n in data.get("newsletters", [])
        if n.get("active", True)
    ]


def load_preferences(path: Path = PREFERENCES_PATH) -> dict:
    return yaml.safe_load(path.read_text())


# ---------- Filter ----------

def filter_newsletters(
    emails: list[Email], configs: list[NewsletterConfig],
) -> list[tuple[Email, NewsletterConfig]]:
    """Return (email, config) pairs for messages whose sender matches a config."""
    by_sender = {c.sender: c for c in configs}
    out: list[tuple[Email, NewsletterConfig]] = []
    for e in emails:
        cfg = by_sender.get((e.sender_address or "").lower())
        if cfg is not None:
            out.append((e, cfg))
    return out


# ---------- Body fetch + HTML stripping ----------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_STYLE_SCRIPT_RE = re.compile(
    r"<(style|script)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL,
)


def _html_to_text(content: str) -> str:
    """Crude HTML → text. Newsletter HTML is heavy on layout markup; what we
    need is the prose, which survives a tag strip + entity unescape.
    """
    if not content:
        return ""
    content = _STYLE_SCRIPT_RE.sub(" ", content)
    text = _HTML_TAG_RE.sub(" ", content)
    text = html.unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def fetch_newsletter_bodies(
    pairs: list[tuple[Email, NewsletterConfig]],
    email_client: EmailClient,
) -> list[NewsletterEmail]:
    """Fetch full bodies for each filtered email and decode to plain text."""
    out: list[NewsletterEmail] = []
    for email, cfg in pairs:
        try:
            html_body, text_body = email_client.get_message_body(email.id)
        except Exception as e:
            log.warning("Body fetch failed for %r: %s", email.subject, e)
            continue
        body = text_body or _html_to_text(html_body)
        if not body:
            log.warning("Empty body for newsletter %r — skipping.", email.subject)
            continue
        out.append(NewsletterEmail(
            newsletter=cfg,
            subject=email.subject,
            received_at=email.received_at,
            body_text=body,
        ))
    return out


# ---------- Curation ----------

_SYSTEM_PROMPT = """You are a newsletter curator for a daily personal briefing. The reader is a senior finance/FP&A professional who follows AI news closely.

Your job: read several AI-focused newsletters from a single day, deduplicate overlapping stories, and select the top items.

Output STRICT JSON only — no prose, no markdown fences. Schema:
{
  "items": [
    {
      "headline": "string — short and concrete",
      "summary": "string — 2 to 7 sentences. Call out a finance, FP&A, or accounting angle if one exists.",
      "sources": ["Newsletter name as given in input", "..."]
    }
  ]
}
"""


def _build_user_message(
    newsletters: list[NewsletterEmail], prefs: dict, body_budget: int,
) -> str:
    nl = (prefs.get("newsletters") or {})
    top_n = int(nl.get("top_n", 5))
    rules = nl.get("rules") or []
    buckets = nl.get("default_buckets") or []

    rule_lines = [
        f"- {r.get('description', '').strip()} (weight {r.get('weight')})"
        for r in sorted(rules, key=lambda r: -float(r.get("weight", 1.0)))
    ]
    bucket_line = ", ".join(buckets) if buckets else "broad AI news"
    bodies = _format_bodies(newsletters, body_budget)

    return (
        f"Select the top {top_n} stories across these newsletters.\n\n"
        "Curation rules (in priority order):\n"
        + "\n".join(rule_lines) + "\n\n"
        f"Default coverage buckets when nothing else stands out: {bucket_line}.\n\n"
        "Each summary must be 2-7 sentences. List every newsletter that ran the "
        "story in `sources`, using the exact names below.\n\n"
        f"Newsletters:\n\n{bodies}"
    )


def _format_bodies(newsletters: list[NewsletterEmail], budget: int) -> str:
    """Concatenate bodies with attribution, distributing `budget` chars evenly
    so no single newsletter crowds out the others.
    """
    if not newsletters:
        return "(no newsletters in the window)"
    per = max(2_000, budget // len(newsletters))
    parts: list[str] = []
    for n in newsletters:
        body = n.body_text
        if len(body) > per:
            body = body[:per] + " …[truncated]"
        parts.append(f"### {n.newsletter.name} — {n.subject}\n{body}")
    return "\n\n".join(parts)


def curate_top_stories(
    newsletters: list[NewsletterEmail],
    *,
    claude: ClaudeClient,
    preferences: dict,
    body_budget: int = DEFAULT_BODY_CHAR_BUDGET,
) -> tuple[list[NewsItem], list[str]]:
    """Run Claude over the newsletter bodies; return (items, warnings)."""
    if not newsletters:
        return [], ["No newsletters received in the window."]

    user = _build_user_message(newsletters, preferences, body_budget)
    try:
        result = claude.call(
            messages=[{"role": "user", "content": user}],
            model=HAIKU,
            system=_SYSTEM_PROMPT,
            max_tokens=4096,
        )
    except Exception as e:
        log.warning("Claude curation call failed: %s", e)
        return [], [f"Curation failed: {e}"]

    items = _parse_items(result.text)
    if not items:
        return [], ["Curation returned no items (parse failure)."]

    top_n = int((preferences.get("newsletters") or {}).get("top_n", 5))
    return items[:top_n], []


def _parse_items(raw: str) -> list[NewsItem]:
    """Extract the items array from Claude's response, tolerating optional fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("Curation JSON parse failed: %s; first 200 chars: %r",
                    e, text[:200])
        return []
    items: list[NewsItem] = []
    for it in data.get("items", []):
        headline = str(it.get("headline", "")).strip()
        summary = str(it.get("summary", "")).strip()
        sources = [str(s).strip() for s in (it.get("sources") or []) if str(s).strip()]
        if headline and summary:
            items.append(NewsItem(headline=headline, summary=summary, sources=sources))
    return items


# ---------- Orchestrator ----------

def fetch_newsletters_section(
    emails: list[Email],
    *,
    email_client: EmailClient,
    claude: ClaudeClient,
    configs: Optional[list[NewsletterConfig]] = None,
    preferences: Optional[dict] = None,
    body_budget: int = DEFAULT_BODY_CHAR_BUDGET,
) -> NewslettersResult:
    """End-to-end: filter → fetch bodies → curate."""
    cfgs = configs if configs is not None else load_newsletters_config()
    prefs = preferences if preferences is not None else load_preferences()

    pairs = filter_newsletters(emails, cfgs)
    bodies = fetch_newsletter_bodies(pairs, email_client)
    items, warnings = curate_top_stories(
        bodies, claude=claude, preferences=prefs, body_budget=body_budget,
    )
    return NewslettersResult(received=bodies, items=items, warnings=warnings)


# ---------- Render ----------

def render_markdown(result: NewslettersResult) -> str:
    if not result.received and not result.items:
        return "## Newsletters\n\nSection unavailable: no newsletters in the window.\n"

    lines = ["## Newsletters", ""]

    if result.received:
        lines.append("### Newsletters Received")
        for n in result.received:
            lines.append(f"- {n.newsletter.name}: {n.subject}")
        lines.append("")

    if result.items:
        lines.append("### Top AI Stories")
        lines.append("")
        for i, item in enumerate(result.items, 1):
            lines.append(f"{i}. {item.headline}")
            lines.append(f"   {item.summary}")
            if item.sources:
                lines.append(f"   Sources: {', '.join(item.sources)}")
            lines.append("")
    elif result.received:
        lines.append("_Curation unavailable._")
        lines.append("")

    for w in result.warnings:
        lines.append(f"_{w}_")

    return "\n".join(lines).rstrip() + "\n"
