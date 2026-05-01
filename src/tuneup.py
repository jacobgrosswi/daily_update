"""Weekly tune-up — Sonnet 4.6 reviews a week of briefings and proposes pref edits.

Section 4.6 of the scoping doc. Where the daily feedback loop (feedback.py)
applies *narrow*, individually-validated ops in response to direct replies,
the weekly tune-up takes a broader pattern view: which sections drift, which
buckets are over-/under-firing, which curation rules don't fit observed news,
which sender keywords are missing. Sonnet (with adaptive thinking) reads:

  - The last 7 archived briefings (archive/YYYY-MM-DD.md as sent).
  - The current preferences.yml.
  - Any unresolved 'needs_tuneup' notes from recent feedback (free-form notes
    the daily loop couldn't translate into ops).

…and emits a *proposed* updated preferences.yml plus a rationale. We do NOT
auto-apply — the GitHub Actions workflow opens a PR so the user reviews and
merges. Manual review is the safety net, since Sonnet has more freedom here
than the narrow daily ops.

CLI (invoked from .github/workflows/weekly-tuneup.yml):
  uv run python -m src.tuneup \\
      --output-prefs proposed_prefs.yml \\
      --output-rationale rationale.md \\
      [--archive-dir archive/] [--days 7]
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import yaml

from .claude_client import SONNET, ClaudeClient
from .utils import REPO_ROOT, configure_logging, get_logger, now_ct

log = get_logger(__name__)

ARCHIVE_DIR = REPO_ROOT / "archive"
PREFERENCES_PATH = REPO_ROOT / "config" / "preferences.yml"

DEFAULT_DAYS = 7

# Prevent runaway briefing length from blowing up Sonnet's input context.
# 7 briefings * ~25k chars each ≈ 175k chars (≈ 45k tokens on Sonnet) — well
# under the 1M context but cheap to keep tight.
MAX_ARCHIVE_CHARS_PER_DAY = 30_000


@dataclass
class TuneupOutput:
    proposed_yaml: str   # the full proposed preferences.yml content (string, not parsed)
    rationale: str       # human-readable rationale, markdown


# ---------- Archive gathering ----------

def gather_archive(
    *,
    end_date: date,
    days: int = DEFAULT_DAYS,
    archive_dir: Path = ARCHIVE_DIR,
) -> list[tuple[date, str]]:
    """Return [(date, markdown), ...] for up to `days` days ending at end_date.

    Missing days are silently skipped — we may have outages. Most-recent first.
    """
    out: list[tuple[date, str]] = []
    for offset in range(days):
        d = end_date - timedelta(days=offset)
        path = archive_dir / f"{d.isoformat()}.md"
        if not path.exists():
            continue
        text = path.read_text()
        if len(text) > MAX_ARCHIVE_CHARS_PER_DAY:
            text = text[:MAX_ARCHIVE_CHARS_PER_DAY] + "\n\n[...truncated]\n"
        out.append((d, text))
    log.info("Tune-up: gathered %d archived briefings (window %d days).", len(out), days)
    return out


# ---------- Prompt construction ----------

_TUNEUP_SYSTEM = """You are a careful preference-tuning assistant for an automated \
daily email briefing. The user has sent you:

  1. The current preferences.yml (controls bucketing, curation, drop rules, etc.).
  2. The last few days of briefings as actually delivered.

Your job: propose an UPDATED preferences.yml that better matches the patterns \
visible in the briefings. Examples of useful changes:

  - Lower `newsletters.top_n` if the curated section consistently has weaker \
items at the bottom.
  - Add senders or subject keywords to `email.buckets.*` if certain emails are \
landing in "People" but should be in Orders & Shipping or Appointments.
  - Add `email.drop_senders_matching` substrings for senders that show up daily \
as low-signal automated mail.
  - Add or refine `newsletters.rules` entries (each has id/description/weight in \
[0.1, 3.0]) when the curation pattern is missing a recurring theme.
  - DO NOT toggle `paused` — that is for vacations.
  - DO NOT change the structural shape of the file.

Output format (STRICT — no other text, no code fences):

```
=== PROPOSED PREFERENCES YAML ===
<full updated preferences.yml content>
=== RATIONALE ===
<markdown explanation: bullet list of changes, each with a one-sentence \
justification rooted in what you observed in the archive. If you propose NO \
changes, write a single line: "No changes proposed — current configuration \
matches observed patterns.">
```

Rules:
  - The proposed YAML must be valid YAML and a complete drop-in replacement \
for preferences.yml.
  - Preserve any keys you don't have a reason to change.
  - Be conservative. A working configuration is better than a clever one.
"""


def build_user_message(
    current_prefs_yaml: str,
    archive: list[tuple[date, str]],
) -> str:
    parts = [
        "=== CURRENT preferences.yml ===",
        current_prefs_yaml.rstrip(),
        "",
        "=== RECENT BRIEFINGS (most recent first) ===",
    ]
    if not archive:
        parts.append("(none — archive directory was empty)")
    for d, text in archive:
        parts.append(f"\n--- {d.isoformat()} ---")
        parts.append(text.rstrip())
    return "\n".join(parts)


# ---------- Output parsing ----------

_PROPOSED_RE = re.compile(
    r"=== PROPOSED PREFERENCES YAML ===\s*\n(.*?)\n=== RATIONALE ===\s*\n(.*)",
    re.DOTALL,
)


def parse_tuneup_output(text: str) -> TuneupOutput:
    """Pull the proposed YAML and rationale out of Sonnet's response.

    Strips a leading ```yaml / ``` fence around the YAML if Sonnet adds one,
    even though the prompt says not to — defensive against minor format drift.
    """
    m = _PROPOSED_RE.search(text)
    if not m:
        raise ValueError(
            f"Tune-up output did not match expected format. First 300 chars:\n{text[:300]!r}"
        )
    yaml_text = m.group(1).strip()
    rationale = m.group(2).strip()

    # Strip optional fenced code block.
    yaml_text = re.sub(r"^```(?:yaml)?\s*\n", "", yaml_text)
    yaml_text = re.sub(r"\n```\s*$", "", yaml_text)

    # Validate that it parses — a malformed YAML proposal would silently break
    # the workflow's commit step. Better to fail loudly here.
    try:
        yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ValueError(f"Proposed YAML failed to parse: {e}\n{yaml_text[:300]!r}")

    return TuneupOutput(proposed_yaml=yaml_text, rationale=rationale)


# ---------- Orchestrator ----------

def run_tuneup(
    *,
    claude: Optional[ClaudeClient] = None,
    end_date: Optional[date] = None,
    days: int = DEFAULT_DAYS,
    archive_dir: Path = ARCHIVE_DIR,
    prefs_path: Path = PREFERENCES_PATH,
) -> TuneupOutput:
    """Compose, call Sonnet 4.6 with adaptive thinking, parse output."""
    claude = claude or ClaudeClient()
    end_date = end_date or now_ct().date()

    current = prefs_path.read_text()
    archive = gather_archive(end_date=end_date, days=days, archive_dir=archive_dir)
    user_msg = build_user_message(current, archive)

    log.info("Tune-up: calling Sonnet 4.6 with adaptive thinking.")
    result = claude.call(
        model=SONNET,
        system=_TUNEUP_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        max_tokens=8192,
        thinking={"type": "adaptive"},
    )
    log.info("Tune-up: Sonnet response %d chars, cost $%.4f.", len(result.text), result.cost_usd)
    return parse_tuneup_output(result.text)


# ---------- CLI ----------

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Weekly tune-up: propose preferences.yml updates.")
    p.add_argument("--output-prefs", required=True, type=Path,
                   help="Where to write the proposed preferences.yml content.")
    p.add_argument("--output-rationale", required=True, type=Path,
                   help="Where to write the human-readable rationale (markdown).")
    p.add_argument("--archive-dir", type=Path, default=ARCHIVE_DIR,
                   help="Archive directory (default: archive/).")
    p.add_argument("--prefs-path", type=Path, default=PREFERENCES_PATH,
                   help="Current preferences.yml (default: config/preferences.yml).")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"Lookback window in days (default: {DEFAULT_DAYS}).")
    p.add_argument("--end-date", type=date.fromisoformat, default=None,
                   help="End date YYYY-MM-DD (default: today CT).")
    p.add_argument("--log-level", default="INFO",
                   help="Log level: DEBUG, INFO, WARNING, ERROR.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)

    out = run_tuneup(
        end_date=args.end_date,
        days=args.days,
        archive_dir=args.archive_dir,
        prefs_path=args.prefs_path,
    )

    args.output_prefs.write_text(out.proposed_yaml + ("\n" if not out.proposed_yaml.endswith("\n") else ""))
    args.output_rationale.write_text(out.rationale + ("\n" if not out.rationale.endswith("\n") else ""))
    log.info("Tune-up: wrote proposal to %s and rationale to %s.",
             args.output_prefs, args.output_rationale)
    return 0


if __name__ == "__main__":
    sys.exit(main())
