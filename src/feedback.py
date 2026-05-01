"""Continuous feedback loop — apply replies to the briefing as preference edits.

Section 4.5 of the scoping doc. Each morning before composing the briefing,
fetch any new replies to prior briefings (subject "Re: Daily Briefing - ...")
and ask Claude (Haiku 4.5) to translate them into a narrow, validated ops
vocabulary. Anything outside the vocabulary is collected as 'needs tune-up'
and surfaced in the briefing's "Applied your feedback" block so the user
can act on it manually or wait for the weekly Sonnet tune-up.

Why a narrow vocabulary, not arbitrary YAML diffs:
  Claude editing the YAML directly is one prompt-injection or hallucination
  away from breaking the daily run. The ops below cover the realistic cases
  ("show fewer items", "stop showing this sender", "categorize Etsy as
  shipping") and are each easy to validate before write.

Replay protection: state/processed_replies.json stores message IDs we've
already applied. Reprocessing a reply would either double-apply or no-op,
but we'd still pay Claude for the triage — so we skip them upstream.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from .claude_client import HAIKU, ClaudeClient
from .email_client import Email, EmailClient
from .utils import REPO_ROOT, UTC, get_logger, utc_now

log = get_logger(__name__)

PREFERENCES_PATH = REPO_ROOT / "config" / "preferences.yml"
PROCESSED_REPLIES_PATH = REPO_ROOT / "state" / "processed_replies.json"

# How far back to look for replies on the first run (no processed_replies state).
DEFAULT_REPLY_LOOKBACK = timedelta(days=14)

# Hard ceiling on reply bodies handed to Claude — replies typically quote the
# whole prior briefing, which can run thousands of lines. Keep cost bounded.
MAX_REPLY_CHARS = 4_000

# Allowed ops vocabulary — see module docstring for rationale.
OPS = {
    "set_top_n",
    "add_drop_sender",
    "add_bucket_keyword",
    "add_curation_rule",
    "set_paused",
    "noop",
}

ALLOWED_BUCKETS = {"orders_shipping", "appointments"}
ALLOWED_BUCKET_FIELDS = {"sender_keywords", "subject_keywords"}


# ---------- Models ----------

@dataclass
class AppliedOp:
    """A single op that successfully validated and was applied to prefs."""
    op: str
    args: dict
    summary: str  # one-line human-readable description for the briefing
    reply_id: str


@dataclass
class FeedbackResult:
    applied: list[AppliedOp] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (reply_id, reason)
    needs_tuneup: list[str] = field(default_factory=list)
    prefs_changed: bool = False


# ---------- Processed-replies state ----------

def _load_processed(path: Path = PROCESSED_REPLIES_PATH) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("ids", []))
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.warning("processed_replies.json unreadable (%s); treating as empty.", e)
        return set()


def _save_processed(ids: set[str], path: Path = PROCESSED_REPLIES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sorted so the file diffs cleanly in commits.
    payload = {"ids": sorted(ids)}
    path.write_text(json.dumps(payload, indent=2) + "\n")


# ---------- Op validation + application ----------

_RULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def _ensure_dict(obj: dict, key: str) -> dict:
    """Ensure obj[key] is a dict; create one if missing or wrong type."""
    cur = obj.get(key)
    if not isinstance(cur, dict):
        cur = {}
        obj[key] = cur
    return cur


def _ensure_list(obj: dict, key: str) -> list:
    cur = obj.get(key)
    if not isinstance(cur, list):
        cur = []
        obj[key] = cur
    return cur


def validate_op(op: dict) -> Optional[str]:
    """Return None if op is valid, else a human-readable reason it was rejected."""
    name = op.get("op")
    if name not in OPS:
        return f"unknown op {name!r}"
    if name == "noop":
        return None
    if name == "set_top_n":
        v = op.get("value")
        if not isinstance(v, int) or v < 1 or v > 20:
            return "set_top_n.value must be int in [1, 20]"
        return None
    if name == "add_drop_sender":
        v = op.get("pattern")
        if not isinstance(v, str) or not v.strip() or len(v) > 200:
            return "add_drop_sender.pattern must be a non-empty string ≤200 chars"
        return None
    if name == "add_bucket_keyword":
        b = op.get("bucket")
        f = op.get("field")
        v = op.get("value")
        if b not in ALLOWED_BUCKETS:
            return f"add_bucket_keyword.bucket must be one of {sorted(ALLOWED_BUCKETS)}"
        if f not in ALLOWED_BUCKET_FIELDS:
            return f"add_bucket_keyword.field must be one of {sorted(ALLOWED_BUCKET_FIELDS)}"
        if not isinstance(v, str) or not v.strip() or len(v) > 100:
            return "add_bucket_keyword.value must be a non-empty string ≤100 chars"
        return None
    if name == "add_curation_rule":
        rid = op.get("id")
        desc = op.get("description")
        weight = op.get("weight")
        if not isinstance(rid, str) or not _RULE_ID_RE.match(rid):
            return "add_curation_rule.id must match [a-z][a-z0-9_]{1,40}"
        if not isinstance(desc, str) or not desc.strip() or len(desc) > 500:
            return "add_curation_rule.description must be a non-empty string ≤500 chars"
        if not isinstance(weight, (int, float)) or not (0.1 <= weight <= 3.0):
            return "add_curation_rule.weight must be in [0.1, 3.0]"
        return None
    if name == "set_paused":
        v = op.get("value")
        if not isinstance(v, bool):
            return "set_paused.value must be a bool"
        return None
    return f"validator missing for {name}"  # defensive


def apply_op(prefs: dict, op: dict) -> tuple[bool, str]:
    """Apply one validated op to prefs in-place. Returns (changed, summary).

    `changed=False` indicates the op was a no-op (e.g., adding a keyword that
    already exists). The summary is a one-line description for the briefing.
    """
    name = op["op"]

    if name == "noop":
        return False, "no actionable change"

    if name == "set_top_n":
        nl = _ensure_dict(prefs, "newsletters")
        old = nl.get("top_n")
        new = op["value"]
        if old == new:
            return False, f"newsletters.top_n already {new}"
        nl["top_n"] = new
        return True, f"newsletters.top_n: {old} → {new}"

    if name == "add_drop_sender":
        em = _ensure_dict(prefs, "email")
        lst = _ensure_list(em, "drop_senders_matching")
        pattern = op["pattern"].strip()
        if pattern in lst:
            return False, f"drop sender pattern already present: {pattern!r}"
        lst.append(pattern)
        return True, f"email.drop_senders_matching += {pattern!r}"

    if name == "add_bucket_keyword":
        em = _ensure_dict(prefs, "email")
        buckets = _ensure_dict(em, "buckets")
        bucket = _ensure_dict(buckets, op["bucket"])
        field_lst = _ensure_list(bucket, op["field"])
        value = op["value"].strip()
        if value in field_lst:
            return False, f"{op['bucket']}.{op['field']} already contains {value!r}"
        field_lst.append(value)
        return True, f"{op['bucket']}.{op['field']} += {value!r}"

    if name == "add_curation_rule":
        nl = _ensure_dict(prefs, "newsletters")
        rules = _ensure_list(nl, "rules")
        rid = op["id"]
        if any(isinstance(r, dict) and r.get("id") == rid for r in rules):
            return False, f"curation rule {rid!r} already exists"
        rules.append({
            "id": rid,
            "description": op["description"].strip(),
            "weight": float(op["weight"]),
        })
        return True, f"newsletters.rules += {rid!r} (w={op['weight']})"

    if name == "set_paused":
        old = prefs.get("paused")
        new = op["value"]
        if old == new:
            return False, f"paused already {new}"
        prefs["paused"] = new
        return True, f"paused: {old} → {new}"

    raise ValueError(f"apply_op: missing handler for {name}")  # defensive


# ---------- Claude triage ----------

_TRIAGE_SYSTEM = """You translate a single email reply into a structured list of \
preference-edit operations for an automated daily briefing.

You MUST output ONLY valid JSON in this exact shape:
{
  "ops": [<op>, ...],
  "needs_tuneup": [<str>, ...]
}

`ops` is the list of edits to apply now. Each op uses one of the following \
shapes (and ONLY these — no other op names exist):

  {"op": "set_top_n", "value": <int 1-20>}
      // Change how many newsletter items appear in the briefing.
  {"op": "add_drop_sender", "pattern": "<substring>"}
      // Drop emails whose sender address contains this substring (case-insensitive).
      // Example: "noreply@" or "marketing@bigcorp.com".
  {"op": "add_bucket_keyword",
   "bucket": "orders_shipping" | "appointments",
   "field": "sender_keywords" | "subject_keywords",
   "value": "<keyword>"}
      // Route emails matching this keyword into the named bucket.
  {"op": "add_curation_rule", "id": "<lowercase_id>", "description": "<text>", \
"weight": <float 0.1-3.0>}
      // Add a newsletter curation rule (boost/penalty signal).
  {"op": "set_paused", "value": true | false}
      // Pause the daily briefing entirely (vacation), or unpause.
  {"op": "noop"}
      // The reply doesn't request any change (e.g., "thanks!").

`needs_tuneup` is a list of one-line summaries of any feedback that DOES NOT \
fit the ops vocabulary. Examples: "user wants the markets section moved to the \
top", "user wants weather added", "user wants summaries of personal emails to \
be shorter". Keep each item under 120 characters.

Rules:
- Output ONLY the JSON object — no preamble, no code fence, no commentary.
- Do not invent op names. If unsure, prefer `needs_tuneup` over a guessed op.
- Quoted material from the prior briefing is NOT new feedback — ignore it. \
Only act on what the user wrote in their reply.
- One reply may produce multiple ops. It may also produce zero ops (only \
`needs_tuneup`, or a single `noop`).
- For senders the user asks to "stop seeing", choose `add_drop_sender` with the \
most specific email substring you can extract.
"""


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    """Pull the first {...} block out of Claude's response and parse it."""
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        raise ValueError(f"no JSON object in Claude response: {text[:200]!r}")
    return json.loads(m.group(0))


def _truncate_reply(body: str, limit: int = MAX_REPLY_CHARS) -> str:
    if len(body) <= limit:
        return body
    return body[:limit] + "\n\n[...truncated]"


def parse_feedback(reply: Email, body_text: str, claude: ClaudeClient) -> dict:
    """Ask Claude to triage one reply. Returns the raw {'ops': [...], 'needs_tuneup': [...]}."""
    user_msg = (
        f"Reply subject: {reply.subject}\n"
        f"From: {reply.sender_address}\n"
        f"Received: {reply.received_at.isoformat()}\n\n"
        f"Reply body:\n{_truncate_reply(body_text)}\n"
    )
    result = claude.call(
        model=HAIKU,
        system=_TRIAGE_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        max_tokens=1024,
    )
    return _extract_json(result.text)


# ---------- Pref I/O ----------

def _load_prefs(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def _save_prefs(prefs: dict, path: Path) -> None:
    """Write prefs back to YAML. Comments in the source file are not preserved
    — feedback-loop edits are the canonical source of truth, and the file is
    machine-edited daily."""
    path.write_text(yaml.safe_dump(prefs, sort_keys=False, default_flow_style=False))


# ---------- Orchestrator ----------

def apply_pending_feedback(
    *,
    email_client: EmailClient,
    claude: ClaudeClient,
    now: Optional[datetime] = None,
    prefs_path: Path = PREFERENCES_PATH,
    processed_path: Path = PROCESSED_REPLIES_PATH,
    commit: bool = True,
) -> FeedbackResult:
    """Fetch new briefing replies, translate them into ops, apply them.

    Idempotent: replies already in processed_replies.json are skipped before
    we pay Claude for triage. Failures on one reply do not block the rest —
    the failed reply is NOT marked processed so it'll retry next run.

    `commit=False` (used by --dry-run) does everything in-memory and skips the
    writes to preferences.yml and processed_replies.json — so you can preview
    what the feedback loop would do without consuming replies or modifying
    config.
    """
    result = FeedbackResult()
    now = (now or utc_now()).astimezone(UTC)
    processed = _load_processed(processed_path)
    since = now - DEFAULT_REPLY_LOOKBACK
    log.info("Feedback: scanning replies since %s (%d already processed)",
             since.isoformat(), len(processed))

    try:
        replies = email_client.list_replies_to_briefing(since)
    except Exception as e:
        log.exception("Feedback: list_replies_to_briefing failed.")
        result.skipped.append(("<fetch>", f"reply fetch failed: {e}"))
        return result

    new_replies = [r for r in replies if r.id not in processed]
    log.info("Feedback: %d total replies, %d new.", len(replies), len(new_replies))
    if not new_replies:
        return result

    prefs = _load_prefs(prefs_path)

    for reply in new_replies:
        try:
            html, text = email_client.get_message_body(reply.id)
            body = text or _strip_html(html) or reply.body_preview or ""
            triage = parse_feedback(reply, body, claude)
        except Exception as e:
            log.exception("Feedback: triage failed for reply %s", reply.id)
            result.skipped.append((reply.id, f"triage failed: {e}"))
            continue

        ops = triage.get("ops") or []
        for raw_op in ops:
            if not isinstance(raw_op, dict):
                result.skipped.append((reply.id, f"non-dict op: {raw_op!r}"))
                continue
            err = validate_op(raw_op)
            if err:
                result.skipped.append((reply.id, err))
                continue
            try:
                changed, summary = apply_op(prefs, raw_op)
            except Exception as e:
                log.exception("Feedback: apply_op failed for %s", raw_op)
                result.skipped.append((reply.id, f"apply failed: {e}"))
                continue
            if changed:
                result.prefs_changed = True
            result.applied.append(AppliedOp(
                op=raw_op["op"],
                args={k: v for k, v in raw_op.items() if k != "op"},
                summary=summary,
                reply_id=reply.id,
            ))

        for note in triage.get("needs_tuneup") or []:
            if isinstance(note, str) and note.strip():
                result.needs_tuneup.append(note.strip())

        # Mark this reply processed only after we successfully triaged it.
        processed.add(reply.id)

    if not commit:
        log.info("Feedback: commit=False (dry-run); skipping writes to %s and %s.",
                 prefs_path, processed_path)
        return result

    if result.prefs_changed:
        _save_prefs(prefs, prefs_path)
        log.info("Feedback: wrote %d applied op(s) to %s", len(result.applied), prefs_path)

    _save_processed(processed, processed_path)
    return result


# ---------- Rendering ----------

def render_markdown(result: FeedbackResult) -> str:
    """Render the 'Applied your feedback' block. Empty string when nothing happened.

    Placed at the top of the briefing (right under the date header) so the user
    sees confirmation of what changed before reading the rest.
    """
    if not (result.applied or result.needs_tuneup):
        return ""
    lines = ["## Applied your feedback", ""]
    if result.applied:
        for op in result.applied:
            lines.append(f"- {op.summary}")
    if result.needs_tuneup:
        if result.applied:
            lines.append("")
        lines.append("**Needs tune-up** (couldn't auto-apply):")
        for note in result.needs_tuneup:
            lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


# ---------- HTML stripping (mirrors newsletters.py, kept local to avoid coupling) ----------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_SCRIPT_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    if not html:
        return ""
    no_blocks = _STYLE_SCRIPT_RE.sub(" ", html)
    no_tags = _HTML_TAG_RE.sub(" ", no_blocks)
    return _WHITESPACE_RE.sub(" ", no_tags).strip()
