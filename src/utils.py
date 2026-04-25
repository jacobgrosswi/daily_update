"""Cross-cutting helpers: logging, Central-time handling, last-run state I/O."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

CT = ZoneInfo(os.environ.get("BRIEFING_TZ", "America/Chicago"))
UTC = timezone.utc

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "state" / "last_run.json"

# Default lookback when no state file exists yet.
DEFAULT_LOOKBACK = timedelta(hours=24)


# ---------- Logging ----------

def configure_logging(level: str = "INFO") -> None:
    """Configure root logging once for the whole run."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ---------- Time ----------

def utc_now() -> datetime:
    return datetime.now(UTC)


def now_ct() -> datetime:
    return datetime.now(CT)


def to_ct(dt: datetime) -> datetime:
    """Convert any aware datetime to Central time. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(CT)


def yesterday_ct(reference: Optional[datetime] = None) -> datetime:
    """Return the start-of-day for 'yesterday' in Central time."""
    ref = reference or now_ct()
    y = (ref.astimezone(CT) - timedelta(days=1)).date()
    return datetime(y.year, y.month, y.day, tzinfo=CT)


# ---------- State ----------

@dataclass
class RunState:
    last_run_at_utc: datetime
    last_run_status: str
    last_briefing_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "last_run_at_utc": self.last_run_at_utc.astimezone(UTC).isoformat(),
            "last_run_status": self.last_run_status,
            "last_briefing_id": self.last_briefing_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunState":
        return cls(
            last_run_at_utc=datetime.fromisoformat(data["last_run_at_utc"]),
            last_run_status=data.get("last_run_status", "unknown"),
            last_briefing_id=data.get("last_briefing_id"),
        )


def read_last_run(path: Path = STATE_PATH) -> Optional[RunState]:
    """Return the last successful run state, or None if no state file exists."""
    if not path.exists():
        return None
    try:
        return RunState.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        get_logger(__name__).warning("State file unreadable (%s); treating as missing.", e)
        return None


def write_last_run(state: RunState, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2) + "\n")


def email_window(now: Optional[datetime] = None, path: Path = STATE_PATH) -> tuple[datetime, datetime]:
    """Return the (start, end) UTC window for the current briefing's email scan.

    Start = last successful run timestamp, or now - DEFAULT_LOOKBACK if none.
    End = now (UTC).
    """
    end = (now or utc_now()).astimezone(UTC)
    last = read_last_run(path)
    if last is None:
        start = end - DEFAULT_LOOKBACK
    else:
        start = last.last_run_at_utc.astimezone(UTC)
    return start, end
