"""Tests for src/budget.py — pricing math, Budget class, pre-flight checks."""
from __future__ import annotations

import pytest

from src.budget import (
    CHARS_PER_TOKEN,
    DEFAULT_CAP_USD,
    HAIKU,
    PRICES,
    SONNET,
    Budget,
    BudgetExceeded,
    Record,
    cost_usd,
    estimate_cost_usd,
)


# ---------- Pricing math ----------

def test_cost_usd_haiku_input_output():
    # 1M input + 1M output on Haiku = $1 + $5 = $6
    c = cost_usd(model=HAIKU, input_tokens=1_000_000, output_tokens=1_000_000)
    assert c == pytest.approx(6.00)


def test_cost_usd_sonnet_with_cache():
    # 1M cache_read + 1M cache_write on Sonnet = $0.30 + $3.75 = $4.05
    c = cost_usd(
        model=SONNET, input_tokens=0, output_tokens=0,
        cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000,
    )
    assert c == pytest.approx(4.05)


def test_cost_usd_unknown_model_raises():
    with pytest.raises(ValueError, match="unknown model"):
        cost_usd(model="claude-something-else", input_tokens=1, output_tokens=1)


def test_estimate_cost_usd_uses_max_output_tokens():
    # Worst case: assumes output = max_output_tokens, no caching.
    e = estimate_cost_usd(model=HAIKU, input_tokens=10_000, max_output_tokens=4_096)
    expected = (10_000 * 1.00 + 4_096 * 5.00) / 1_000_000
    assert e == pytest.approx(expected)


# ---------- Budget basics ----------

def test_budget_default_cap_is_25_cents():
    b = Budget()
    assert b.cap_usd == DEFAULT_CAP_USD
    assert b.spent_usd == 0.0
    assert b.records == []


def test_record_accumulates_spend_and_appends_record():
    b = Budget()
    b.record(label="email_summary", model=HAIKU, cost=0.01)
    b.record(label="newsletters", model=HAIKU, cost=0.02)
    assert b.spent_usd == pytest.approx(0.03)
    assert len(b.records) == 2
    assert b.records[0] == Record(label="email_summary", model=HAIKU, cost_usd=0.01)
    assert b.records[1].label == "newsletters"


def test_remaining_clamps_to_zero_when_overspent():
    b = Budget(cap_usd=0.10)
    b.record(label="x", model=HAIKU, cost=0.15)
    assert b.remaining() == 0.0


def test_is_exhausted_at_or_above_cap():
    b = Budget(cap_usd=0.10)
    assert not b.is_exhausted()
    b.record(label="x", model=HAIKU, cost=0.10)
    assert b.is_exhausted()


def test_would_exceed_compares_against_remaining():
    b = Budget(cap_usd=0.10)
    b.record(label="x", model=HAIKU, cost=0.06)
    # Remaining = 0.04. A 0.05 projected cost would exceed.
    assert b.would_exceed(0.05) is True
    assert b.would_exceed(0.04) is False


# ---------- Pre-flight assertions ----------

def test_assert_not_exhausted_passes_when_under_cap():
    b = Budget(cap_usd=0.10)
    b.assert_not_exhausted(label="anything")  # no raise


def test_assert_not_exhausted_raises_when_at_cap():
    b = Budget(cap_usd=0.10)
    b.record(label="x", model=HAIKU, cost=0.10)
    with pytest.raises(BudgetExceeded, match="exhausted before 'feedback'"):
        b.assert_not_exhausted(label="feedback")


def test_assert_can_afford_passes_when_room():
    b = Budget(cap_usd=DEFAULT_CAP_USD)
    b.assert_can_afford(
        label="newsletters", model=HAIKU,
        input_tokens=20_000, max_output_tokens=4_096,
    )  # ~$0.02 + $0.02 worst-case, under $0.25


def test_assert_can_afford_raises_when_projection_overflows():
    b = Budget(cap_usd=0.05)  # tight cap
    with pytest.raises(BudgetExceeded, match="would exceed budget"):
        b.assert_can_afford(
            label="newsletters", model=HAIKU,
            input_tokens=100_000, max_output_tokens=4_096,
        )


# ---------- affordable_input_chars ----------

def test_affordable_input_chars_full_budget_haiku():
    """At full $0.25 cap, Haiku call with 4096 max_output should leave plenty
    of room for input chars."""
    b = Budget()
    n = b.affordable_input_chars(model=HAIKU, max_output_tokens=4_096)
    # Sanity check: must be well above the 80k default body budget.
    assert n > 500_000


def test_affordable_input_chars_zero_when_exhausted():
    b = Budget(cap_usd=0.10)
    b.record(label="x", model=HAIKU, cost=0.10)
    assert b.affordable_input_chars(model=HAIKU, max_output_tokens=4_096) == 0


def test_affordable_input_chars_zero_when_output_alone_exceeds():
    """Output-only cost exceeds remaining → no room for any input."""
    b = Budget(cap_usd=0.01)
    # 4096 output tokens × $5/1M = $0.02048 > $0.01 cap.
    assert b.affordable_input_chars(model=HAIKU, max_output_tokens=4_096) == 0


def test_affordable_input_chars_uses_chars_per_token_ratio():
    """Spot-check: for $0.10 cap, Haiku, 0 output, 0 overhead, the affordable
    input cost is $0.10 → 100k tokens → 350k chars (at 3.5 chars/token)."""
    b = Budget(cap_usd=0.10)
    n = b.affordable_input_chars(model=HAIKU, max_output_tokens=0, overhead_tokens=0)
    # 100,000 tokens × 3.5 chars/token = 350,000 chars
    assert n == int(100_000 * CHARS_PER_TOKEN)


def test_affordable_input_chars_unknown_model_raises():
    b = Budget()
    with pytest.raises(ValueError, match="unknown model"):
        b.affordable_input_chars(model="bogus", max_output_tokens=4_096)


# ---------- Summary ----------

def test_summary_with_no_calls():
    b = Budget()
    s = b.summary()
    assert s == "$0.0000 / $0.25 (0 calls)"


def test_summary_pluralization():
    b = Budget()
    b.record(label="a", model=HAIKU, cost=0.01)
    assert "1 call)" in b.summary()  # singular
    b.record(label="b", model=HAIKU, cost=0.02)
    assert "2 calls)" in b.summary()


def test_summary_shows_spent_and_cap():
    b = Budget(cap_usd=0.50)
    b.record(label="x", model=HAIKU, cost=0.0345)
    assert b.summary() == "$0.0345 / $0.50 (1 call)"


# ---------- Constants integrity ----------

def test_prices_table_has_haiku_and_sonnet():
    assert HAIKU in PRICES
    assert SONNET in PRICES
    for model in (HAIKU, SONNET):
        for key in ("input", "output", "cache_read", "cache_write"):
            assert key in PRICES[model]
