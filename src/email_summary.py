"""Section 4.1 of the scoping doc — personal inbox triage.

Filter the inbox window down to non-automated, non-newsletter messages, ask
Claude (Haiku) to bucket each one and write a 1-2 sentence summary, then
group by bucket for the briefing. The body preview returned by list_inbox is
enough material for triage — no second Graph fetch per message.

Buckets are fixed: People, Orders & Shipping, Appointments. Anything not
clearly orders/appointments lands in People by default; Claude can move
edge cases between the three.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from .claude_client import HAIKU, ClaudeClient
from .email_client import Email
from .utils import get_logger

log = get_logger(__name__)

# Display order in the rendered section.
EMAIL_BUCKETS = ["People", "Orders & Shipping", "Appointments"]

# Cap on how many messages we hand to Claude in one call. Personal inbox
# rarely exceeds this in a 24h window; if it does we keep the most recent
# and note the truncation.
MAX_TRIAGE_ITEMS = 50

# Hard ceiling on body_preview chars per email going to Claude.
MAX_PREVIEW_CHARS = 400


# ---------- Models ----------

@dataclass
class EmailItem:
    sender: str
    subject: str
    bucket: str
    summary: str


@dataclass
class EmailSummaryResult:
    items_by_bucket: dict[str, list[EmailItem]]
    total_input: int       # raw inbox count from the window
    total_kept: int        # post-filter count handed to Claude
    truncated: int = 0     # how many we dropped because of MAX_TRIAGE_ITEMS
    warnings: list[str] = field(default_factory=list)


# ---------- Pre-bucketing (rule-based hint to Claude) ----------

def pre_bucket(email: Email, prefs: dict) -> str:
    """Suggest a bucket from rules in preferences.yml. Returns a value in EMAIL_BUCKETS."""
    bucket_cfg = (prefs.get("email") or {}).get("buckets") or {}
    addr = (email.sender_address or "").lower()
    subj = (email.subject or "").lower()

    def _matches(rules: dict) -> bool:
        senders = [s.lower() for s in (rules.get("sender_keywords") or [])]
        subjects = [s.lower() for s in (rules.get("subject_keywords") or [])]
        if any(s in addr for s in senders):
            return True
        if any(s in subj for s in subjects):
            return True
        return False

    if _matches(bucket_cfg.get("orders_shipping") or {}):
        return "Orders & Shipping"
    if _matches(bucket_cfg.get("appointments") or {}):
        return "Appointments"
    return "People"


# ---------- Filter ----------

def filter_emails(
    emails: list[Email], newsletter_senders: set[str],
) -> list[Email]:
    """Drop automated mail (List-Unsubscribe / noreply) and known newsletter senders.

    Newsletter senders are handled by the newsletters section, so we never
    summarize them as personal mail.
    """
    out: list[Email] = []
    for e in emails:
        if e.is_automated:
            continue
        if (e.sender_address or "").lower() in newsletter_senders:
            continue
        out.append(e)
    return out


# ---------- Claude triage ----------

_SYSTEM_PROMPT = """You are an inbox triage assistant for a daily personal briefing.

You will receive a JSON list of emails. For each one:
- Confirm or correct its bucket. Valid buckets, exact spelling required:
  "People", "Orders & Shipping", "Appointments".
- Write a 1-2 sentence summary of what it's about and any action implied.
  Be concrete. Mention amounts, dates, names, or order numbers when present.
  Do not invent details that aren't in the input.

Output STRICT JSON only — no prose, no markdown fences. Schema:
{
  "items": [
    {"id": <int>, "bucket": "<bucket>", "summary": "<1-2 sentences>"}
  ]
}

Return one item per input id. Do not omit any.
"""


def _build_payload(emails: list[Email], prefs: dict) -> list[dict]:
    return [
        {
            "id": i,
            "from": (f"{e.sender_name} <{e.sender_address}>"
                     if e.sender_name else e.sender_address),
            "subject": e.subject,
            "preview": (e.body_preview or "")[:MAX_PREVIEW_CHARS],
            "suggested_bucket": pre_bucket(e, prefs),
        }
        for i, e in enumerate(emails)
    ]


def _classify_and_summarize(
    emails: list[Email], *, claude: ClaudeClient, prefs: dict,
) -> tuple[list[EmailItem], list[str]]:
    if not emails:
        return [], []
    payload = _build_payload(emails, prefs)
    user = (
        "Triage these emails. Return one item per input id, preserving the "
        "id field so I can match them back.\n\n"
        + json.dumps(payload, indent=2)
    )
    try:
        result = claude.call(
            messages=[{"role": "user", "content": user}],
            model=HAIKU,
            system=_SYSTEM_PROMPT,
            max_tokens=4096,
        )
    except Exception as e:
        log.warning("Email triage Claude call failed: %s", e)
        # Degrade: pre-bucketing only, preview as the summary text.
        items = [
            EmailItem(
                sender=_display_sender(e),
                subject=e.subject,
                bucket=pre_bucket(e, prefs),
                summary=(e.body_preview or "").strip()[:200] or "(no preview)",
            )
            for e in emails
        ]
        return items, [f"Email triage degraded (Claude call failed): {e}"]

    by_id = _parse_items(result.text)
    out: list[EmailItem] = []
    for i, e in enumerate(emails):
        meta = by_id.get(i) or {}
        bucket = meta.get("bucket")
        if bucket not in EMAIL_BUCKETS:
            bucket = pre_bucket(e, prefs)
        summary = (meta.get("summary") or "").strip()
        if not summary:
            summary = (e.body_preview or "").strip()[:200] or "(no preview)"
        out.append(EmailItem(
            sender=_display_sender(e),
            subject=e.subject,
            bucket=bucket,
            summary=summary,
        ))
    return out, []


def _display_sender(e: Email) -> str:
    return e.sender_name.strip() if e.sender_name else (e.sender_address or "(unknown)")


def _parse_items(raw: str) -> dict[int, dict]:
    """Extract Claude's items array, tolerating optional code fences."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("Email triage JSON parse failed: %s; first 200 chars: %r",
                    e, text[:200])
        return {}
    out: dict[int, dict] = {}
    for it in data.get("items", []):
        try:
            out[int(it["id"])] = it
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ---------- Orchestrator ----------

def fetch_email_summary_section(
    emails: list[Email],
    *,
    claude: ClaudeClient,
    prefs: dict,
    newsletter_senders: set[str],
) -> EmailSummaryResult:
    kept = filter_emails(emails, newsletter_senders)
    truncated = 0
    if len(kept) > MAX_TRIAGE_ITEMS:
        # Keep the most recent N — list_inbox returns oldest-first.
        truncated = len(kept) - MAX_TRIAGE_ITEMS
        kept = kept[-MAX_TRIAGE_ITEMS:]

    items, warnings = _classify_and_summarize(kept, claude=claude, prefs=prefs)
    if truncated:
        warnings.append(f"Triage truncated: showing the {MAX_TRIAGE_ITEMS} most recent of "
                        f"{MAX_TRIAGE_ITEMS + truncated} personal messages.")

    by_bucket: dict[str, list[EmailItem]] = {b: [] for b in EMAIL_BUCKETS}
    for it in items:
        by_bucket.setdefault(it.bucket, []).append(it)

    return EmailSummaryResult(
        items_by_bucket=by_bucket,
        total_input=len(emails),
        total_kept=len(kept),
        truncated=truncated,
        warnings=warnings,
    )


# ---------- Render ----------

def render_markdown(result: EmailSummaryResult) -> str:
    if result.total_kept == 0:
        skipped = result.total_input
        suffix = (f" ({skipped} automated/newsletter messages skipped)"
                  if skipped else "")
        return f"## Email Summary\n\nNo new personal mail in the window{suffix}.\n"

    lines = ["## Email Summary", ""]
    for bucket in EMAIL_BUCKETS:
        items = result.items_by_bucket.get(bucket) or []
        if not items:
            continue
        lines.append(f"### {bucket}")
        for it in items:
            lines.append(f"- {it.sender} — {it.subject}")
            lines.append(f"  {it.summary}")
        lines.append("")

    for w in result.warnings:
        lines.append(f"_{w}_")

    return "\n".join(lines).rstrip() + "\n"
