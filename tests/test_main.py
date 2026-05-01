"""Tests for src/main.py — heavy mocking; the orchestrator is glue."""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import main
from src.delivery import Briefing
from src.email_client import Email
from src.feedback import AppliedOp, FeedbackResult
from src.newsletters import NewsletterConfig, NewslettersResult

UTC = timezone.utc

BRIEFING_DATE = date(2026, 4, 26)
TARGET_DATE = date(2026, 4, 25)
RUN_TS = datetime(2026, 4, 26, 11, 0, tzinfo=UTC)


def _email() -> Email:
    return Email(
        id="x", subject="Lunch?", sender_name="Alice",
        sender_address="alice@example.com",
        received_at=datetime(2026, 4, 26, 10, 0, tzinfo=UTC),
        body_preview="Hi, lunch?",
    )


def _stub_email_client(inbox=None):
    ec = MagicMock()
    ec.list_inbox.return_value = inbox or [_email()]
    return ec


@pytest.fixture
def patches(tmp_path, monkeypatch):
    """Patch all the section helpers + state I/O. Returns a SimpleNamespace
    of the mocks so tests can assert on calls."""
    monkeypatch.setattr(main, "email_window",
                        lambda now=None: (datetime(2026, 4, 25, 11, 0, tzinfo=UTC), RUN_TS))
    monkeypatch.setattr(main, "utc_now", lambda: RUN_TS)
    # Default: no rotation file path. Individual tests opt in.
    monkeypatch.delenv("REFRESH_TOKEN_OUT_PATH", raising=False)
    monkeypatch.delenv("MS_REFRESH_TOKEN", raising=False)

    write_state = MagicMock()
    monkeypatch.setattr(main, "write_last_run", write_state)

    p = SimpleNamespace(
        load_preferences=patch.object(main.newsletters, "load_preferences",
                                       return_value={"paused": False}),
        load_newsletters_config=patch.object(main.newsletters, "load_newsletters_config",
                                              return_value=[NewsletterConfig("TLDR", "dan@tldrnewsletter.com")]),
        fetch_email_summary=patch.object(main.email_summary, "fetch_email_summary_section",
                                          return_value=MagicMock(total_kept=1, items_by_bucket={}, warnings=[])),
        render_email_summary=patch.object(main.email_summary, "render_markdown",
                                           return_value="## Email Summary\n\nbody\n"),
        fetch_sports=patch.object(main.sports, "fetch_sports_section",
                                   return_value={"teams": [], "playoffs": {}}),
        render_sports=patch.object(main.sports, "render_markdown",
                                    return_value="## Sports\n\nbody\n"),
        load_tickers=patch.object(main.markets, "load_tickers_config",
                                   return_value=SimpleNamespace(tickers=[], premarket_alert_pct=1.0)),
        fetch_quotes=patch.object(main.markets, "fetch_index_quotes", return_value=[]),
        check_premarket=patch.object(main.markets, "check_premarket", return_value=[]),
        render_markets=patch.object(main.markets, "render_markdown",
                                     return_value="## Market Update\n\nbody\n"),
        fetch_newsletters=patch.object(main.newsletters, "fetch_newsletters_section",
                                        return_value=NewslettersResult(received=[], items=[])),
        render_newsletters=patch.object(main.newsletters, "render_markdown",
                                         return_value="## Newsletters\n\nbody\n"),
        deliver=patch.object(main, "deliver",
                              return_value=("2026-04-26", tmp_path / "2026-04-26.md")),
        apply_feedback=patch.object(main.feedback, "apply_pending_feedback",
                                     return_value=FeedbackResult()),
    )
    started = [getattr(p, k).start() for k in vars(p)]
    try:
        yield SimpleNamespace(**{k: m for k, m in zip(vars(p).keys(), started)},
                              write_state=write_state)
    finally:
        for k in vars(p):
            getattr(p, k).stop()


# ---------- Happy path ----------

def test_run_composes_all_sections_and_delivers(patches):
    ec = _stub_email_client()
    claude = MagicMock()
    rc = main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
                  email_client=ec, claude=claude, now_utc=RUN_TS)
    assert rc == 0

    patches.deliver.assert_called_once()
    briefing: Briefing = patches.deliver.call_args.args[0]
    assert briefing.briefing_date == BRIEFING_DATE
    md = briefing.markdown
    assert md.startswith("# Daily Briefing — 2026-04-26\n")
    # Section order matches scoping doc §4.
    assert md.index("## Email Summary") < md.index("## Sports") < md.index("## Market Update") < md.index("## Newsletters")
    assert "## Issues" not in md  # no failures, no footer

    patches.write_state.assert_called_once()
    state = patches.write_state.call_args.args[0]
    assert state.last_run_status == "ok"
    assert state.last_briefing_id == "2026-04-26"


def test_run_passes_target_date_to_sports(patches):
    main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
             email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    patches.fetch_sports.assert_called_once()
    assert patches.fetch_sports.call_args.args[0] == TARGET_DATE


def test_run_filters_newsletter_senders_out_of_email_summary(patches):
    main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
             email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    kwargs = patches.fetch_email_summary.call_args.kwargs
    assert kwargs["newsletter_senders"] == {"dan@tldrnewsletter.com"}


# ---------- Vacation pause ----------

def test_run_skips_when_paused(patches):
    patches.load_preferences.return_value = {"paused": True}
    rc = main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
                  email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    assert rc == 0
    patches.deliver.assert_not_called()
    patches.write_state.assert_not_called()


# ---------- Partial failure ----------

def test_run_section_failure_is_isolated_and_reported(patches):
    patches.fetch_sports.side_effect = RuntimeError("MLB API down")
    rc = main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
                  email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    assert rc == 0
    briefing: Briefing = patches.deliver.call_args.args[0]
    assert "Section unavailable: MLB API down" in briefing.markdown
    assert "## Issues" in briefing.markdown
    assert "Sports: MLB API down" in briefing.markdown
    state = patches.write_state.call_args.args[0]
    assert state.last_run_status == "partial"


def test_run_inbox_failure_is_recorded_but_does_not_abort(patches):
    ec = MagicMock()
    ec.list_inbox.side_effect = RuntimeError("graph 503")
    rc = main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
                  email_client=ec, claude=MagicMock(), now_utc=RUN_TS)
    assert rc == 0
    briefing: Briefing = patches.deliver.call_args.args[0]
    assert "Inbox fetch: graph 503" in briefing.markdown
    # Email summary + newsletter sections still ran with empty inbox (rendered placeholders).
    assert "## Email Summary" in briefing.markdown
    assert "## Newsletters" in briefing.markdown


# ---------- Delivery failure ----------

def test_run_returns_failure_when_deliver_raises(patches):
    patches.deliver.side_effect = RuntimeError("graph send 500")
    rc = main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
                  email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    assert rc == 1
    # State NOT updated — so tomorrow's window starts from the prior run, not this failed one.
    patches.write_state.assert_not_called()


# ---------- Dry run ----------

def test_dry_run_prints_to_stdout_and_skips_delivery(patches, capsys):
    rc = main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE, dry_run=True,
                  email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("# Daily Briefing — 2026-04-26\n")
    patches.deliver.assert_not_called()
    patches.write_state.assert_not_called()


# ---------- Refresh token rotation ----------

def test_persist_writes_when_token_rotated(patches, tmp_path, monkeypatch):
    out = tmp_path / "rt.txt"
    monkeypatch.setenv("REFRESH_TOKEN_OUT_PATH", str(out))
    monkeypatch.setenv("MS_REFRESH_TOKEN", "old-rt")

    ec = _stub_email_client()
    ec.current_refresh_token = "new-rt"
    main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
             email_client=ec, claude=MagicMock(), now_utc=RUN_TS)

    assert out.read_text() == "new-rt"


def test_persist_skips_when_token_unchanged(patches, tmp_path, monkeypatch):
    out = tmp_path / "rt.txt"
    monkeypatch.setenv("REFRESH_TOKEN_OUT_PATH", str(out))
    monkeypatch.setenv("MS_REFRESH_TOKEN", "same-rt")

    ec = _stub_email_client()
    ec.current_refresh_token = "same-rt"
    main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
             email_client=ec, claude=MagicMock(), now_utc=RUN_TS)

    assert not out.exists()


def test_persist_runs_even_when_delivery_fails(patches, tmp_path, monkeypatch):
    """Auth happens before delivery, so rotation must be persisted even on send failure."""
    out = tmp_path / "rt.txt"
    monkeypatch.setenv("REFRESH_TOKEN_OUT_PATH", str(out))
    monkeypatch.setenv("MS_REFRESH_TOKEN", "old-rt")
    patches.deliver.side_effect = RuntimeError("graph send 500")

    ec = _stub_email_client()
    ec.current_refresh_token = "rotated-rt"
    rc = main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
                  email_client=ec, claude=MagicMock(), now_utc=RUN_TS)

    assert rc == 1
    assert out.read_text() == "rotated-rt"


def test_persist_noop_without_env_var(patches, tmp_path, monkeypatch):
    """Local dev path: no env var, no file — even if email_client reports rotation."""
    monkeypatch.setenv("MS_REFRESH_TOKEN", "old-rt")
    ec = _stub_email_client()
    ec.current_refresh_token = "new-rt"
    main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
             email_client=ec, claude=MagicMock(), now_utc=RUN_TS)
    # No assertion of file absence — just that the run didn't raise.


# ---------- Feedback wiring ----------

def test_feedback_block_renders_at_top_of_briefing(patches):
    patches.apply_feedback.return_value = FeedbackResult(
        applied=[AppliedOp(op="set_top_n", args={"value": 7},
                           summary="newsletters.top_n: 5 → 7", reply_id="r1")],
    )
    main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
             email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    md = patches.deliver.call_args.args[0].markdown
    # Feedback block must appear before the first section.
    assert md.index("## Applied your feedback") < md.index("## Email Summary")
    assert "newsletters.top_n: 5 → 7" in md


def test_feedback_reloads_prefs_after_change(patches):
    patches.apply_feedback.return_value = FeedbackResult(
        applied=[AppliedOp(op="noop", args={}, summary="x", reply_id="r1")],
        prefs_changed=True,
    )
    main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
             email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    # When prefs change, load_preferences is called twice — once at startup,
    # once after feedback applies.
    assert patches.load_preferences.call_count == 2


def test_feedback_failure_isolated_to_issues_footer(patches):
    patches.apply_feedback.side_effect = RuntimeError("triage exploded")
    rc = main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
                  email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    assert rc == 0
    md = patches.deliver.call_args.args[0].markdown
    assert "Feedback: triage exploded" in md


def test_feedback_block_omitted_when_no_replies(patches):
    main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
             email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    md = patches.deliver.call_args.args[0].markdown
    assert "## Applied your feedback" not in md


# ---------- Recipient override ----------

def test_run_passes_recipient_override_to_deliver(patches):
    main.run(briefing_date=BRIEFING_DATE, target_date=TARGET_DATE,
             recipient="other@example.com",
             email_client=_stub_email_client(), claude=MagicMock(), now_utc=RUN_TS)
    assert patches.deliver.call_args.kwargs["recipient"] == "other@example.com"


# ---------- CLI ----------

def test_cli_for_date_overrides_briefing_date(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(main, "run", fake_run)
    main.main(["--for-date", "2026-05-01", "--dry-run"])
    assert captured["briefing_date"] == date(2026, 5, 1)
    assert captured["target_date"] == date(2026, 4, 30)
    assert captured["dry_run"] is True


def test_cli_default_dates_use_today_ct(monkeypatch):
    captured = {}

    monkeypatch.setattr(main, "run", lambda **kw: captured.update(kw) or 0)
    monkeypatch.setattr(main, "now_ct",
                        lambda: datetime(2026, 4, 26, 6, 0, tzinfo=UTC))
    main.main([])
    assert captured["briefing_date"] == date(2026, 4, 26)
    assert captured["target_date"] == date(2026, 4, 25)
    assert captured["dry_run"] is False


def test_cli_recipient_passes_through(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "run", lambda **kw: captured.update(kw) or 0)
    monkeypatch.setattr(main, "now_ct",
                        lambda: datetime(2026, 4, 26, 6, 0, tzinfo=UTC))
    main.main(["--recipient", "x@y.com"])
    assert captured["recipient"] == "x@y.com"
