"""Daily briefing orchestrator.

Wires the four sections together (email summary, sports, markets, newsletters),
composes a single Markdown document, and hands it to delivery.deliver() for
archive write + Graph send. Each section runs inside _safe_section so one
failing API does not abort the briefing — failures land in an Issues footer.

CLI:
  --dry-run         compose and print to stdout, skip send + archive
  --recipient ADDR  override BRIEFING_RECIPIENT for one run
  --for-date DATE   override the briefing date (default: today CT). Sports/
                    markets use --for-date minus one day.
  --log-level LVL   DEBUG / INFO / WARNING / ERROR (default INFO)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import email_summary, feedback, markets, newsletters, sports
from .budget import Budget
from .claude_client import ClaudeClient
from .delivery import Briefing, briefing_id, deliver
from .email_client import Email, EmailClient
from .utils import (
    RunState,
    configure_logging,
    email_window,
    get_logger,
    now_ct,
    utc_now,
    write_last_run,
)

log = get_logger(__name__)


# ---------- Refresh token rotation ----------

def _persist_rotated_refresh_token(email_client: EmailClient) -> None:
    """If MSAL rotated the refresh token mid-run, write it to the path in
    REFRESH_TOKEN_OUT_PATH so the GitHub Actions runner can update the
    MS_REFRESH_TOKEN secret. No-op when the env var is unset (local dev) or
    when no rotation occurred (file is left absent so the workflow's existence
    check naturally signals "no update needed").
    """
    out_path = os.environ.get("REFRESH_TOKEN_OUT_PATH")
    if not out_path:
        return
    try:
        current = email_client.current_refresh_token
    except Exception:
        return
    original = os.environ.get("MS_REFRESH_TOKEN")
    if current and current != original:
        Path(out_path).write_text(current)
        log.info("Refresh token rotated; wrote new value to %s.", out_path)


# ---------- Section wrapper ----------

def _safe_section(name: str, fn: Callable[[], str]) -> tuple[str, Optional[str]]:
    """Call fn() and return (markdown, error). On failure, return a
    'Section unavailable' placeholder so the briefing still ships.
    """
    try:
        return fn(), None
    except Exception as e:  # noqa: BLE001 — partial-failure policy is intentional
        log.exception("Section %s failed", name)
        msg = f"## {name}\n\nSection unavailable: {e}\n"
        return msg, f"{name}: {e}"


# ---------- Orchestrator ----------

def run(
    *,
    briefing_date: date,
    target_date: date,
    dry_run: bool = False,
    recipient: Optional[str] = None,
    email_client: Optional[EmailClient] = None,
    claude: Optional[ClaudeClient] = None,
    http_client: Optional[httpx.Client] = None,
    now_utc: Optional[datetime] = None,
) -> int:
    """Compose and (optionally) deliver the briefing. Returns exit code.

    Args injected for tests; production calls pass None and we wire defaults.
    """
    run_started_utc = now_utc or utc_now()

    # 0. Vacation pause check.
    try:
        prefs = newsletters.load_preferences()
    except Exception as e:
        log.warning("Preferences load failed (%s); using empty defaults.", e)
        prefs = {}
    if prefs.get("paused"):
        log.info("Briefing paused via preferences.yml. Exiting without action.")
        return 0

    budget = Budget()
    email_client = email_client or EmailClient()
    claude = claude or ClaudeClient(budget=budget)
    # Attach budget even when claude was injected (test fixtures use MagicMock,
    # which silently accepts the attribute write).
    try:
        claude.budget = budget
    except AttributeError:
        pass

    try:
        return _run_inner(
            briefing_date=briefing_date,
            target_date=target_date,
            dry_run=dry_run,
            recipient=recipient,
            email_client=email_client,
            claude=claude,
            http_client=http_client,
            run_started_utc=run_started_utc,
            prefs=prefs,
            budget=budget,
        )
    finally:
        # Always check for refresh-token rotation, even on partial/full failure —
        # MSAL may have rotated the token before whatever failed downstream, and
        # if we don't persist it the next run will fail with an invalid grant.
        _persist_rotated_refresh_token(email_client)


def _run_inner(
    *,
    briefing_date: date,
    target_date: date,
    dry_run: bool,
    recipient: Optional[str],
    email_client: EmailClient,
    claude: ClaudeClient,
    http_client: Optional[httpx.Client],
    run_started_utc: datetime,
    prefs: dict,
    budget: Budget,
) -> int:
    issues: list[str] = []

    # 0.5. Apply pending feedback (replies to prior briefings) BEFORE composing.
    # Reload prefs after so today's run sees any fresh edits.
    feedback_result = feedback.FeedbackResult()
    try:
        feedback_result = feedback.apply_pending_feedback(
            email_client=email_client, claude=claude, now=run_started_utc,
            commit=not dry_run,
        )
        if feedback_result.prefs_changed:
            prefs = newsletters.load_preferences()
            log.info("Feedback: %d op(s) applied; prefs reloaded.", len(feedback_result.applied))
    except Exception as e:
        log.exception("Feedback loop failed; continuing without applying replies.")
        issues.append(f"Feedback: {e}")

    # 1. Inbox fetch — feeds both the email summary and the newsletters section.
    start, end = email_window(now=run_started_utc)
    log.info("Email window: %s → %s", start.isoformat(), end.isoformat())
    try:
        inbox = email_client.list_inbox(start, end)
        log.info("Fetched %d inbox messages.", len(inbox))
    except Exception as e:
        log.exception("Inbox fetch failed; cannot compose Email Summary or Newsletters.")
        inbox = []
        issues.append(f"Inbox fetch: {e}")

    # 2. Newsletter config (also used to filter newsletter senders out of the personal summary).
    try:
        nl_configs = newsletters.load_newsletters_config()
    except Exception as e:
        log.warning("Newsletter config load failed: %s", e)
        nl_configs = []
        issues.append(f"Newsletter config: {e}")
    nl_sender_set = {c.sender for c in nl_configs}

    # 3. Compose each section. Order matches scoping doc §4.
    sections: list[str] = []

    md, err = _safe_section(
        "Email Summary",
        lambda: email_summary.render_markdown(
            email_summary.fetch_email_summary_section(
                inbox,
                claude=claude,
                prefs=prefs,
                newsletter_senders=nl_sender_set,
            )
        ),
    )
    sections.append(md)
    if err:
        issues.append(err)

    md, err = _safe_section(
        "Sports",
        lambda: sports.render_markdown(
            sports.fetch_sports_section(target_date, client=http_client),
            target_date,
        ),
    )
    sections.append(md)
    if err:
        issues.append(err)

    md, err = _safe_section("Market Update", lambda: _markets_section())
    sections.append(md)
    if err:
        issues.append(err)

    md, err = _safe_section(
        "Newsletters",
        lambda: newsletters.render_markdown(
            newsletters.fetch_newsletters_section(
                inbox,
                email_client=email_client,
                claude=claude,
                configs=nl_configs,
                preferences=prefs,
                budget=budget,
            )
        ),
    )
    sections.append(md)
    if err:
        issues.append(err)

    # Surface budget exhaustion in the Issues footer so the user sees that the
    # cap kicked in. The Budget object itself logs every recorded call.
    if budget.is_exhausted():
        issues.append(f"Budget cap reached: {budget.summary()}")

    # 4. Compose the full document. The "Applied your feedback" block goes at
    # the top, right under the date header, so the user sees what changed before
    # reading the rest.
    bid = briefing_id(briefing_date)
    header = f"# Daily Briefing — {bid}\n"
    feedback_md = feedback.render_markdown(feedback_result)
    body = "\n".join(sections)
    footer = ""
    if issues:
        footer = "\n## Issues\n\n" + "\n".join(f"- {i}" for i in issues) + "\n"
    parts = [header]
    if feedback_md:
        parts.append("\n" + feedback_md)
    parts.append("\n" + body + footer)
    markdown = "".join(parts)

    briefing = Briefing(briefing_date=briefing_date, markdown=markdown)

    # 5. Deliver (or print).
    if dry_run:
        log.info("Dry run: skipping archive + send. Printing briefing to stdout.")
        sys.stdout.write(markdown)
        sys.stdout.flush()
        return 0

    try:
        delivered_id, archive_path = deliver(
            briefing, email_client=email_client, recipient=recipient,
        )
    except Exception:
        log.exception("Delivery failed; not updating last_run state.")
        return 1

    log.info("Briefing delivered: id=%s archive=%s issues=%d budget=%s",
             delivered_id, archive_path, len(issues), budget.summary())

    # 6. Persist run state so tomorrow's email window starts from this run.
    write_last_run(RunState(
        last_run_at_utc=run_started_utc,
        last_run_status="partial" if issues else "ok",
        last_briefing_id=delivered_id,
    ))
    return 0


def _markets_section() -> str:
    cfg = markets.load_tickers_config()
    quotes = markets.fetch_index_quotes(cfg.tickers)
    symbols = [q.symbol for q in quotes]
    alerts = markets.check_premarket(symbols, threshold_pct=cfg.premarket_alert_pct)
    return markets.render_markdown(quotes, alerts)


# ---------- CLI ----------

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compose and send the daily briefing.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print briefing to stdout; do not send or archive.")
    p.add_argument("--recipient", help="Override the briefing recipient address.")
    p.add_argument("--for-date", dest="for_date", type=date.fromisoformat,
                   help="Briefing date YYYY-MM-DD. Default: today CT.")
    p.add_argument("--log-level", default="INFO",
                   help="Log level: DEBUG, INFO, WARNING, ERROR.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)
    briefing_date = args.for_date or now_ct().date()
    target_date = briefing_date - timedelta(days=1)
    return run(
        briefing_date=briefing_date,
        target_date=target_date,
        dry_run=args.dry_run,
        recipient=args.recipient,
    )


if __name__ == "__main__":
    sys.exit(main())
