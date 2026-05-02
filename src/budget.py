"""Per-run cost guardrail. Tracks Claude spend, enforces a $0.25 cap.

Why a cap when realistic days spend ~$0.04: the cap is a safety net for
runaway calls (loop bugs, accidental huge inputs, an API mistake that retries
forever). It is not a normal operating constraint. On a healthy day the
Budget object just observes; the kick-in path only matters when something
upstream is broken.

Design:
- ClaudeClient holds a `Budget` and records cost on every successful call.
- Pre-call, ClaudeClient fast-fails with `BudgetExceeded` if the budget is
  already exhausted — there's no point paying for a call we'd reject anyway.
- The newsletter section, which dominates input tokens, additionally consults
  `budget.affordable_input_chars(...)` and shrinks its body budget when tight.
  If even the minimum body would push us over, it raises `BudgetExceeded` and
  the section wrapper surfaces "Section unavailable" in the Issues footer.

Pricing lives here (not in claude_client.py) because the Budget is the
canonical owner of cost math; the API client just consumes it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .utils import get_logger

log = get_logger(__name__)

DEFAULT_CAP_USD = 0.25

# Model IDs. Mirrors the claude-api skill model table.
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-6"

# USD per 1M tokens.
PRICES: dict[str, dict[str, float]] = {
    HAIKU: {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    SONNET: {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
}

# Rough chars-per-token for the truncation guardrail. Anthropic's tokenizer
# averages ~3.5-4 chars/token on English; 3.5 is the conservative side (more
# tokens per char), which means we under-estimate affordable chars — safer
# than over-estimating and blowing past the cap.
CHARS_PER_TOKEN = 3.5


class BudgetExceeded(Exception):
    """Raised when a Claude call would push run spend over the cap."""


def cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Exact cost for one call's actual usage."""
    p = PRICES.get(model)
    if p is None:
        raise ValueError(f"unknown model {model!r}; cannot price")
    return (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_read_tokens * p["cache_read"]
        + cache_creation_tokens * p["cache_write"]
    ) / 1_000_000


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    max_output_tokens: int,
) -> float:
    """Worst-case projection for a planned call (no caching, output hits cap)."""
    return cost_usd(
        model=model,
        input_tokens=input_tokens,
        output_tokens=max_output_tokens,
    )


@dataclass(frozen=True)
class Record:
    label: str
    model: str
    cost_usd: float


@dataclass
class Budget:
    cap_usd: float = DEFAULT_CAP_USD
    spent_usd: float = 0.0
    records: list[Record] = field(default_factory=list)

    # ----- accounting -----

    def record(self, *, label: str, model: str, cost: float) -> None:
        self.spent_usd += cost
        self.records.append(Record(label=label, model=model, cost_usd=cost))
        log.info("Budget: +$%.4f for %s (%s); spent $%.4f / $%.4f",
                 cost, label, model, self.spent_usd, self.cap_usd)

    def remaining(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    def is_exhausted(self) -> bool:
        return self.spent_usd >= self.cap_usd

    def would_exceed(self, projected_cost: float) -> bool:
        return self.spent_usd + projected_cost > self.cap_usd

    # ----- pre-flight checks -----

    def assert_not_exhausted(self, *, label: str) -> None:
        """Raise BudgetExceeded if the cap is already met. Cheap pre-flight."""
        if self.is_exhausted():
            raise BudgetExceeded(
                f"budget exhausted before {label!r}: "
                f"spent=${self.spent_usd:.4f} / cap=${self.cap_usd:.4f}"
            )

    def assert_can_afford(
        self,
        *,
        label: str,
        model: str,
        input_tokens: int,
        max_output_tokens: int,
    ) -> None:
        """Raise BudgetExceeded if a planned call's worst case overshoots."""
        self.assert_not_exhausted(label=label)
        projected = estimate_cost_usd(
            model=model, input_tokens=input_tokens, max_output_tokens=max_output_tokens,
        )
        if self.would_exceed(projected):
            raise BudgetExceeded(
                f"{label!r} would exceed budget: "
                f"spent=${self.spent_usd:.4f} + projected=${projected:.4f} "
                f"> cap=${self.cap_usd:.4f}"
            )

    def affordable_input_chars(
        self,
        *,
        model: str,
        max_output_tokens: int,
        overhead_tokens: int = 500,
    ) -> int:
        """How many input characters fit in the remaining budget for `model`.

        Used by the newsletter section to size body_budget defensively. We
        subtract a fixed `overhead_tokens` for system prompt + user-message
        scaffolding before converting the remaining budget into chars.

        Returns 0 if the budget is already exhausted or if the per-call output
        cost alone exceeds remaining (no room for any input).
        """
        p = PRICES.get(model)
        if p is None:
            raise ValueError(f"unknown model {model!r}; cannot price")
        remaining = self.remaining()
        output_cost = max_output_tokens * p["output"] / 1_000_000
        overhead_cost = overhead_tokens * p["input"] / 1_000_000
        budget_for_body = remaining - output_cost - overhead_cost
        if budget_for_body <= 0:
            return 0
        # USD / ($/M tokens) → tokens, then × chars/token.
        body_tokens = budget_for_body * 1_000_000 / p["input"]
        return int(body_tokens * CHARS_PER_TOKEN)

    # ----- reporting -----

    def summary(self) -> str:
        n = len(self.records)
        return (
            f"${self.spent_usd:.4f} / ${self.cap_usd:.2f} "
            f"({n} call{'s' if n != 1 else ''})"
        )
