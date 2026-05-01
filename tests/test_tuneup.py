"""Tests for src/tuneup.py — Sonnet-driven weekly tune-up."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from src import tuneup


# ---------- Helpers ----------

def _claude_returning(text: str, cost: float = 0.05) -> MagicMock:
    claude = MagicMock()
    claude.call.return_value = SimpleNamespace(text=text, cost_usd=cost)
    return claude


def _seed_archive(archive_dir, days_data: list[tuple[date, str]]) -> None:
    for d, body in days_data:
        (archive_dir / f"{d.isoformat()}.md").write_text(body)


def _proposed_response(yaml_text: str, rationale: str = "- changed top_n.") -> str:
    return (
        "=== PROPOSED PREFERENCES YAML ===\n"
        f"{yaml_text}\n"
        "=== RATIONALE ===\n"
        f"{rationale}"
    )


# ---------- gather_archive ----------

class TestGatherArchive:
    def test_gathers_within_window_most_recent_first(self, tmp_path):
        d = tmp_path / "archive"
        d.mkdir()
        _seed_archive(d, [
            (date(2026, 4, 25), "old"),
            (date(2026, 4, 28), "newer"),
            (date(2026, 4, 30), "newest"),
        ])
        out = tuneup.gather_archive(end_date=date(2026, 4, 30), days=7, archive_dir=d)
        assert [item[0] for item in out] == [date(2026, 4, 30), date(2026, 4, 28), date(2026, 4, 25)]

    def test_skips_missing_days(self, tmp_path):
        d = tmp_path / "archive"
        d.mkdir()
        _seed_archive(d, [(date(2026, 4, 30), "x")])
        out = tuneup.gather_archive(end_date=date(2026, 4, 30), days=7, archive_dir=d)
        assert len(out) == 1

    def test_truncates_oversized_archives(self, tmp_path):
        d = tmp_path / "archive"
        d.mkdir()
        big = "x" * (tuneup.MAX_ARCHIVE_CHARS_PER_DAY + 5_000)
        _seed_archive(d, [(date(2026, 4, 30), big)])
        out = tuneup.gather_archive(end_date=date(2026, 4, 30), days=1, archive_dir=d)
        assert "[...truncated]" in out[0][1]
        assert len(out[0][1]) <= tuneup.MAX_ARCHIVE_CHARS_PER_DAY + 30


# ---------- parse_tuneup_output ----------

class TestParseOutput:
    def test_parses_clean_response(self):
        yaml_body = "paused: false\nnewsletters:\n  top_n: 6\n"
        out = tuneup.parse_tuneup_output(_proposed_response(yaml_body))
        assert "top_n: 6" in out.proposed_yaml
        assert out.rationale.startswith("- changed top_n")

    def test_strips_yaml_code_fence(self):
        yaml_body = "```yaml\npaused: true\n```"
        out = tuneup.parse_tuneup_output(_proposed_response(yaml_body))
        assert out.proposed_yaml == "paused: true"

    def test_invalid_yaml_raises(self):
        bad = _proposed_response("not: valid:\n  - [unclosed")
        with pytest.raises(ValueError, match="Proposed YAML failed to parse"):
            tuneup.parse_tuneup_output(bad)

    def test_missing_marker_raises(self):
        with pytest.raises(ValueError, match="did not match expected format"):
            tuneup.parse_tuneup_output("Sonnet went off-script today.")


# ---------- run_tuneup ----------

class TestRunTuneup:
    def test_calls_sonnet_with_adaptive_thinking(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        _seed_archive(archive_dir, [(date(2026, 4, 30), "# Daily Briefing — 2026-04-30")])

        prefs = tmp_path / "preferences.yml"
        prefs.write_text("paused: false\nnewsletters:\n  top_n: 5\n")

        claude = _claude_returning(_proposed_response(
            "paused: false\nnewsletters:\n  top_n: 4\n",
            "- lowered top_n",
        ))

        out = tuneup.run_tuneup(
            claude=claude, end_date=date(2026, 4, 30),
            archive_dir=archive_dir, prefs_path=prefs,
        )

        kwargs = claude.call.call_args.kwargs
        assert kwargs["model"] == tuneup.SONNET
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert "top_n: 4" in out.proposed_yaml

    def test_user_message_includes_current_prefs_and_archive(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        _seed_archive(archive_dir, [(date(2026, 4, 30), "BRIEFING_BODY_MARKER")])

        prefs = tmp_path / "preferences.yml"
        prefs.write_text("CURRENT_PREFS_MARKER: 1\n")

        claude = _claude_returning(_proposed_response("CURRENT_PREFS_MARKER: 1"))

        tuneup.run_tuneup(
            claude=claude, end_date=date(2026, 4, 30),
            archive_dir=archive_dir, prefs_path=prefs,
        )

        msg = claude.call.call_args.kwargs["messages"][0]["content"]
        assert "CURRENT_PREFS_MARKER" in msg
        assert "BRIEFING_BODY_MARKER" in msg


# ---------- CLI ----------

class TestCLI:
    def test_writes_outputs_to_files(self, tmp_path, monkeypatch):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        _seed_archive(archive_dir, [(date(2026, 4, 30), "x")])

        prefs = tmp_path / "preferences.yml"
        prefs.write_text("paused: false\n")

        out_yml = tmp_path / "proposed.yml"
        out_md = tmp_path / "rationale.md"

        proposed_yaml = "paused: false\nnewsletters:\n  top_n: 6\n"
        rationale = "- raised top_n based on observed weak ceiling"

        # Patch ClaudeClient so we don't hit the real API.
        fake_claude = _claude_returning(_proposed_response(proposed_yaml, rationale))
        monkeypatch.setattr(tuneup, "ClaudeClient", lambda: fake_claude)

        rc = tuneup.main([
            "--output-prefs", str(out_yml),
            "--output-rationale", str(out_md),
            "--archive-dir", str(archive_dir),
            "--prefs-path", str(prefs),
            "--end-date", "2026-04-30",
        ])
        assert rc == 0
        assert "top_n: 6" in out_yml.read_text()
        assert "raised top_n" in out_md.read_text()
        # Output should be a complete, valid YAML document.
        assert yaml.safe_load(out_yml.read_text())["newsletters"]["top_n"] == 6
