"""Tests for src/feedback.py — narrow ops vocabulary + Claude triage glue."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from src import feedback
from src.email_client import Email

UTC = timezone.utc
NOW = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)


# ---------- Helpers ----------

def _reply(id_: str = "r1", subject: str = "Re: Daily Briefing - 2026-04-29",
           body: str = "Show me 7 newsletter items instead of 5.") -> Email:
    return Email(
        id=id_, subject=subject,
        sender_name="Jacob", sender_address="me@example.com",
        received_at=datetime(2026, 4, 30, 7, 30, tzinfo=UTC),
        body_preview=body,
    )


def _claude_returning(payload: dict) -> MagicMock:
    """Build a ClaudeClient mock whose .call() returns the given JSON payload."""
    claude = MagicMock()
    claude.call.return_value = SimpleNamespace(text=json.dumps(payload))
    return claude


def _email_client_with(replies: list[Email], bodies: dict[str, str]) -> MagicMock:
    ec = MagicMock()
    ec.list_replies_to_briefing.return_value = replies
    ec.get_message_body.side_effect = lambda mid: ("", bodies.get(mid, ""))
    return ec


@pytest.fixture
def prefs_file(tmp_path):
    """Seed a preferences.yml that mirrors the production config shape."""
    p = tmp_path / "preferences.yml"
    p.write_text(yaml.safe_dump({
        "paused": False,
        "newsletters": {
            "top_n": 5,
            "rules": [{"id": "existing", "description": "x", "weight": 1.0}],
        },
        "email": {
            "drop_senders_matching": ["noreply@"],
            "buckets": {
                "orders_shipping": {"sender_keywords": ["amazon"], "subject_keywords": ["shipped"]},
                "appointments": {"sender_keywords": ["calendly"], "subject_keywords": ["reminder"]},
            },
        },
    }))
    return p


@pytest.fixture
def processed_file(tmp_path):
    return tmp_path / "processed_replies.json"


# ---------- validate_op ----------

class TestValidate:
    def test_unknown_op_rejected(self):
        assert "unknown op" in feedback.validate_op({"op": "rm_rf"})

    def test_noop_ok(self):
        assert feedback.validate_op({"op": "noop"}) is None

    @pytest.mark.parametrize("v", [0, 21, "5", 5.5, None])
    def test_set_top_n_bad_value(self, v):
        assert feedback.validate_op({"op": "set_top_n", "value": v}) is not None

    def test_set_top_n_ok(self):
        assert feedback.validate_op({"op": "set_top_n", "value": 7}) is None

    def test_add_drop_sender_blank(self):
        assert feedback.validate_op({"op": "add_drop_sender", "pattern": "  "}) is not None

    def test_add_drop_sender_ok(self):
        assert feedback.validate_op({"op": "add_drop_sender", "pattern": "marketing@"}) is None

    def test_add_bucket_keyword_unknown_bucket(self):
        op = {"op": "add_bucket_keyword", "bucket": "spam",
              "field": "sender_keywords", "value": "x"}
        assert "bucket must be" in feedback.validate_op(op)

    def test_add_bucket_keyword_unknown_field(self):
        op = {"op": "add_bucket_keyword", "bucket": "orders_shipping",
              "field": "sender_addresses", "value": "x"}
        assert "field must be" in feedback.validate_op(op)

    def test_add_bucket_keyword_ok(self):
        op = {"op": "add_bucket_keyword", "bucket": "orders_shipping",
              "field": "sender_keywords", "value": "etsy"}
        assert feedback.validate_op(op) is None

    @pytest.mark.parametrize("rid", ["", "Bad-ID", "1starts_digit", "x" * 50])
    def test_add_curation_rule_bad_id(self, rid):
        op = {"op": "add_curation_rule", "id": rid,
              "description": "d", "weight": 1.0}
        assert feedback.validate_op(op) is not None

    @pytest.mark.parametrize("w", [0.0, 5.0, "1", None])
    def test_add_curation_rule_bad_weight(self, w):
        op = {"op": "add_curation_rule", "id": "ok_id",
              "description": "d", "weight": w}
        assert feedback.validate_op(op) is not None

    def test_add_curation_rule_ok(self):
        op = {"op": "add_curation_rule", "id": "no_open_source",
              "description": "Penalize open source repos", "weight": 0.5}
        assert feedback.validate_op(op) is None

    def test_set_paused_non_bool(self):
        assert feedback.validate_op({"op": "set_paused", "value": "yes"}) is not None

    def test_set_paused_ok(self):
        assert feedback.validate_op({"op": "set_paused", "value": True}) is None


# ---------- apply_op ----------

class TestApply:
    def test_set_top_n_changes(self):
        prefs = {"newsletters": {"top_n": 5}}
        changed, summary = feedback.apply_op(prefs, {"op": "set_top_n", "value": 7})
        assert changed and prefs["newsletters"]["top_n"] == 7
        assert "5" in summary and "7" in summary

    def test_set_top_n_idempotent(self):
        prefs = {"newsletters": {"top_n": 7}}
        changed, _ = feedback.apply_op(prefs, {"op": "set_top_n", "value": 7})
        assert not changed

    def test_set_top_n_bootstraps_section(self):
        prefs = {}
        changed, _ = feedback.apply_op(prefs, {"op": "set_top_n", "value": 5})
        assert changed and prefs["newsletters"]["top_n"] == 5

    def test_add_drop_sender_appends(self):
        prefs = {"email": {"drop_senders_matching": ["noreply@"]}}
        changed, _ = feedback.apply_op(prefs, {"op": "add_drop_sender", "pattern": "spam@"})
        assert changed
        assert prefs["email"]["drop_senders_matching"] == ["noreply@", "spam@"]

    def test_add_drop_sender_idempotent(self):
        prefs = {"email": {"drop_senders_matching": ["noreply@"]}}
        changed, _ = feedback.apply_op(prefs, {"op": "add_drop_sender", "pattern": "noreply@"})
        assert not changed

    def test_add_bucket_keyword_appends(self):
        prefs = {"email": {"buckets": {"orders_shipping": {"sender_keywords": ["amazon"]}}}}
        op = {"op": "add_bucket_keyword", "bucket": "orders_shipping",
              "field": "sender_keywords", "value": "etsy"}
        changed, _ = feedback.apply_op(prefs, op)
        assert changed
        assert prefs["email"]["buckets"]["orders_shipping"]["sender_keywords"] == ["amazon", "etsy"]

    def test_add_bucket_keyword_idempotent(self):
        prefs = {"email": {"buckets": {"orders_shipping": {"sender_keywords": ["amazon"]}}}}
        op = {"op": "add_bucket_keyword", "bucket": "orders_shipping",
              "field": "sender_keywords", "value": "amazon"}
        changed, _ = feedback.apply_op(prefs, op)
        assert not changed

    def test_add_curation_rule_appends(self):
        prefs = {"newsletters": {"rules": []}}
        op = {"op": "add_curation_rule", "id": "no_oss",
              "description": "Skip OSS releases", "weight": 0.5}
        changed, _ = feedback.apply_op(prefs, op)
        assert changed
        assert prefs["newsletters"]["rules"][0]["id"] == "no_oss"
        assert prefs["newsletters"]["rules"][0]["weight"] == 0.5

    def test_add_curation_rule_idempotent_by_id(self):
        prefs = {"newsletters": {"rules": [{"id": "no_oss", "description": "old", "weight": 1.0}]}}
        op = {"op": "add_curation_rule", "id": "no_oss",
              "description": "new", "weight": 0.3}
        changed, _ = feedback.apply_op(prefs, op)
        assert not changed
        # Existing rule must NOT be overwritten — `add_*` ops never mutate existing entries.
        assert prefs["newsletters"]["rules"][0]["description"] == "old"

    def test_set_paused_toggles(self):
        prefs = {"paused": False}
        changed, _ = feedback.apply_op(prefs, {"op": "set_paused", "value": True})
        assert changed and prefs["paused"] is True

    def test_set_paused_idempotent(self):
        prefs = {"paused": True}
        changed, _ = feedback.apply_op(prefs, {"op": "set_paused", "value": True})
        assert not changed

    def test_noop_returns_unchanged(self):
        prefs = {"foo": "bar"}
        changed, _ = feedback.apply_op(prefs, {"op": "noop"})
        assert not changed
        assert prefs == {"foo": "bar"}


# ---------- _extract_json ----------

class TestExtractJSON:
    def test_plain_json(self):
        assert feedback._extract_json('{"ops": []}') == {"ops": []}

    def test_with_fluff_around_it(self):
        assert feedback._extract_json('Here you go: {"ops": [{"op": "noop"}]} done.') \
            == {"ops": [{"op": "noop"}]}

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            feedback._extract_json("nothing useful here")


# ---------- apply_pending_feedback (integration) ----------

class TestOrchestrator:
    def test_no_replies_returns_empty(self, prefs_file, processed_file):
        ec = _email_client_with([], {})
        claude = MagicMock()
        result = feedback.apply_pending_feedback(
            email_client=ec, claude=claude, now=NOW,
            prefs_path=prefs_file, processed_path=processed_file,
        )
        assert result.applied == [] and result.needs_tuneup == []
        claude.call.assert_not_called()

    def test_processed_replies_skipped_before_claude(self, prefs_file, processed_file):
        processed_file.write_text(json.dumps({"ids": ["r1"]}))
        ec = _email_client_with([_reply("r1")], {"r1": "ignored"})
        claude = MagicMock()
        feedback.apply_pending_feedback(
            email_client=ec, claude=claude, now=NOW,
            prefs_path=prefs_file, processed_path=processed_file,
        )
        # Already-processed reply must not consume Claude budget.
        claude.call.assert_not_called()

    def test_apply_set_top_n_writes_prefs(self, prefs_file, processed_file):
        ec = _email_client_with(
            [_reply("r1", body="Show me 7 items.")],
            {"r1": "Show me 7 items."},
        )
        claude = _claude_returning({"ops": [{"op": "set_top_n", "value": 7}], "needs_tuneup": []})

        result = feedback.apply_pending_feedback(
            email_client=ec, claude=claude, now=NOW,
            prefs_path=prefs_file, processed_path=processed_file,
        )

        assert result.prefs_changed is True
        assert len(result.applied) == 1
        assert result.applied[0].op == "set_top_n"

        new_prefs = yaml.safe_load(prefs_file.read_text())
        assert new_prefs["newsletters"]["top_n"] == 7

        processed = json.loads(processed_file.read_text())
        assert "r1" in processed["ids"]

    def test_invalid_op_skipped_not_applied(self, prefs_file, processed_file):
        ec = _email_client_with([_reply("r1")], {"r1": "blah"})
        claude = _claude_returning({
            "ops": [
                {"op": "set_top_n", "value": 999},   # invalid
                {"op": "set_top_n", "value": 6},     # valid
            ],
            "needs_tuneup": [],
        })

        result = feedback.apply_pending_feedback(
            email_client=ec, claude=claude, now=NOW,
            prefs_path=prefs_file, processed_path=processed_file,
        )

        assert len(result.applied) == 1
        assert result.applied[0].args == {"value": 6}
        assert any("[1, 20]" in reason for _, reason in result.skipped)

    def test_needs_tuneup_collected(self, prefs_file, processed_file):
        ec = _email_client_with([_reply("r1")], {"r1": "Add weather."})
        claude = _claude_returning({
            "ops": [{"op": "noop"}],
            "needs_tuneup": ["user wants weather added"],
        })

        result = feedback.apply_pending_feedback(
            email_client=ec, claude=claude, now=NOW,
            prefs_path=prefs_file, processed_path=processed_file,
        )

        assert result.needs_tuneup == ["user wants weather added"]
        assert result.prefs_changed is False

    def test_failed_triage_does_not_mark_processed(self, prefs_file, processed_file):
        ec = _email_client_with([_reply("r1")], {"r1": "blah"})
        claude = MagicMock()
        # Simulate Claude returning malformed JSON.
        claude.call.return_value = SimpleNamespace(text="not json at all")

        result = feedback.apply_pending_feedback(
            email_client=ec, claude=claude, now=NOW,
            prefs_path=prefs_file, processed_path=processed_file,
        )

        assert any(rid == "r1" for rid, _ in result.skipped)
        # Reply NOT recorded as processed — so we'll retry next run.
        if processed_file.exists():
            data = json.loads(processed_file.read_text())
            assert "r1" not in data.get("ids", [])

    def test_list_replies_failure_returns_empty(self, prefs_file, processed_file):
        ec = MagicMock()
        ec.list_replies_to_briefing.side_effect = RuntimeError("graph 503")
        result = feedback.apply_pending_feedback(
            email_client=ec, claude=MagicMock(), now=NOW,
            prefs_path=prefs_file, processed_path=processed_file,
        )
        assert result.applied == []
        assert any("reply fetch failed" in reason for _, reason in result.skipped)

    def test_commit_false_skips_disk_writes(self, prefs_file, processed_file):
        before_prefs = prefs_file.read_text()
        ec = _email_client_with([_reply("r1")], {"r1": "blah"})
        claude = _claude_returning({"ops": [{"op": "set_top_n", "value": 7}], "needs_tuneup": []})

        result = feedback.apply_pending_feedback(
            email_client=ec, claude=claude, now=NOW, commit=False,
            prefs_path=prefs_file, processed_path=processed_file,
        )

        # Result is computed in-memory exactly as if we'd committed,
        # but no files are mutated.
        assert result.prefs_changed is True
        assert len(result.applied) == 1
        assert prefs_file.read_text() == before_prefs
        assert not processed_file.exists()

    def test_multiple_replies_independent(self, prefs_file, processed_file):
        replies = [_reply("r1", body="7 items"), _reply("r2", body="Stop spam@")]

        def fake_call(**kwargs):
            user = kwargs["messages"][0]["content"]
            if "7 items" in user:
                return SimpleNamespace(text=json.dumps(
                    {"ops": [{"op": "set_top_n", "value": 7}], "needs_tuneup": []}))
            return SimpleNamespace(text=json.dumps(
                {"ops": [{"op": "add_drop_sender", "pattern": "spam@"}], "needs_tuneup": []}))

        claude = MagicMock()
        claude.call.side_effect = fake_call

        ec = _email_client_with(replies, {"r1": "7 items", "r2": "Stop spam@"})
        result = feedback.apply_pending_feedback(
            email_client=ec, claude=claude, now=NOW,
            prefs_path=prefs_file, processed_path=processed_file,
        )

        assert {a.op for a in result.applied} == {"set_top_n", "add_drop_sender"}
        new_prefs = yaml.safe_load(prefs_file.read_text())
        assert new_prefs["newsletters"]["top_n"] == 7
        assert "spam@" in new_prefs["email"]["drop_senders_matching"]


# ---------- render_markdown ----------

class TestRender:
    def test_empty_returns_empty_string(self):
        assert feedback.render_markdown(feedback.FeedbackResult()) == ""

    def test_applied_only(self):
        r = feedback.FeedbackResult(applied=[
            feedback.AppliedOp(op="set_top_n", args={"value": 7},
                               summary="newsletters.top_n: 5 → 7", reply_id="r1"),
        ])
        md = feedback.render_markdown(r)
        assert md.startswith("## Applied your feedback")
        assert "newsletters.top_n: 5 → 7" in md
        assert "Needs tune-up" not in md

    def test_needs_tuneup_only(self):
        r = feedback.FeedbackResult(needs_tuneup=["user wants weather"])
        md = feedback.render_markdown(r)
        assert "## Applied your feedback" in md
        assert "**Needs tune-up**" in md
        assert "user wants weather" in md

    def test_both_sections(self):
        r = feedback.FeedbackResult(
            applied=[feedback.AppliedOp(op="noop", args={}, summary="x", reply_id="r1")],
            needs_tuneup=["weather"],
        )
        md = feedback.render_markdown(r)
        assert md.index("- x") < md.index("**Needs tune-up**")
