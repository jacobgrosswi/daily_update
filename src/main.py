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
import sys
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import httpx

from . import email_summary, markets, newsletters, sports
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

    email_client = email_client or EmailClient()
    claude = claude or ClaudeClient()

    issues: list[str] = []

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
            )
        ),
    )
    sections.append(md)
    if err:
        issues.append(err)

    # 4. Compose the full document.
    bid = briefing_id(briefing_date)
    header = f"# Daily Briefing — {bid}\n"
    body = "\n".join(sections)
    footer = ""
    if issues:
        footer = "\n## Issues\n\n" + "\n".join(f"- {i}" for i in issues) + "\n"
    markdown = header + "\n" + body + footer

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

    log.info("Briefing delivered: id=%s archive=%s issues=%d",
             delivered_id, archive_path, len(issues))

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
