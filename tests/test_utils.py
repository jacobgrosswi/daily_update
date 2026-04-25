"""Tests for src/utils.py: timezone handling and state I/O."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src import utils


CT = ZoneInfo("America/Chicago")
UTC = timezone.utc


def test_to_ct_naive_is_treated_as_utc():
    naive = datetime(2026, 4, 25, 12, 0, 0)
    result = utils.to_ct(naive)
    assert result.tzinfo == CT
    assert result.hour == 7  # 12:00 UTC -> 07:00 CT (CDT, UTC-5)


def test_to_ct_aware_passes_through():
    aware = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    assert utils.to_ct(aware).hour == 7


def test_yesterday_ct_returns_midnight():
    ref = datetime(2026, 4, 25, 6, 0, 0, tzinfo=CT)
    y = utils.yesterday_ct(ref)
    assert y.year == 2026 and y.month == 4 and y.day == 24
    assert y.hour == 0 and y.minute == 0
    assert y.tzinfo == CT


def test_state_round_trip(tmp_path: Path):
    path = tmp_path / "last_run.json"
    state = utils.RunState(
        last_run_at_utc=datetime(2026, 4, 24, 11, 0, 0, tzinfo=UTC),
        last_run_status="success",
        last_briefing_id="2026-04-24",
    )
    utils.write_last_run(state, path)
    loaded = utils.read_last_run(path)
    assert loaded is not None
    assert loaded.last_run_at_utc == state.last_run_at_utc
    assert loaded.last_run_status == "success"
    assert loaded.last_briefing_id == "2026-04-24"


def test_read_missing_state_returns_none(tmp_path: Path):
    assert utils.read_last_run(tmp_path / "nope.json") is None


def test_read_corrupt_state_returns_none(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    assert utils.read_last_run(path) is None


def test_email_window_no_state_uses_default_lookback(tmp_path: Path):
    now = datetime(2026, 4, 25, 11, 0, 0, tzinfo=UTC)
    start, end = utils.email_window(now=now, path=tmp_path / "missing.json")
    assert end == now
    assert end - start == timedelta(hours=24)


def test_email_window_with_state_uses_last_run(tmp_path: Path):
    path = tmp_path / "last_run.json"
    last_run = datetime(2026, 4, 22, 11, 0, 0, tzinfo=UTC)
    utils.write_last_run(
        utils.RunState(last_run_at_utc=last_run, last_run_status="success"),
        path,
    )
    now = datetime(2026, 4, 25, 11, 0, 0, tzinfo=UTC)
    start, end = utils.email_window(now=now, path=path)
    assert start == last_run
    assert end == now


def test_state_file_is_pretty_json(tmp_path: Path):
    path = tmp_path / "last_run.json"
    utils.write_last_run(
        utils.RunState(
            last_run_at_utc=datetime(2026, 4, 24, 11, 0, 0, tzinfo=UTC),
            last_run_status="success",
        ),
        path,
    )
    text = path.read_text()
    assert text.endswith("\n")
    json.loads(text)
