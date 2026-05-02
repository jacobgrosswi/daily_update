"""Thin wrapper around the Anthropic SDK for the daily briefing.

Two model tiers per scoping doc Section 5:
- Haiku 4.5  — daily email bucketing, newsletter curation, feedback parsing
- Sonnet 4.6 — weekly tune-up (deeper pattern reasoning over a week of archive)

Cost math + the per-run cap live in budget.py; this wrapper just records
each call's actual usage into the attached Budget (if any) and pre-fails
when the cap is already met.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Union

import anthropic

from .budget import HAIKU, PRICES, SONNET, Budget, cost_usd
from .utils import get_logger

# Re-exports — existing callers import HAIKU/SONNET from here.
__all__ = ["HAIKU", "SONNET", "PRICES", "CallResult", "ClaudeClient"]

log = get_logger(__name__)


@dataclass
class CallResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    model: str
    stop_reason: str

    @property
    def cost_usd(self) -> float:
        return cost_usd(
            model=self.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens,
        )


class ClaudeClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        max_retries: int = 3,
        budget: Optional[Budget] = None,
    ):
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            max_retries=max_retries,
        )
        # Optional Budget; main.py attaches one for production runs. Tests that
        # inject a MagicMock claude don't need this set — record-on-success
        # is no-op when budget is None.
        self.budget: Optional[Budget] = budget

    def call(
        self,
        *,
        messages: list,
        model: str = HAIKU,
        system: Optional[Union[str, list]] = None,
        max_tokens: int = 4096,
        cache_system: bool = False,
        thinking: Optional[dict] = None,
        label: Optional[str] = None,
    ) -> CallResult:
        """Make a single Messages API call and return text + usage.

        Args:
            messages: Anthropic messages list.
            model: HAIKU or SONNET.
            system: System prompt as str or list of content blocks.
            max_tokens: Output cap. 4096 is enough for our briefing sections;
                weekly tune-up may want more.
            cache_system: Mark the last system block as ephemeral-cached. Note
                the prefix must be ≥4096 tokens (Haiku) or ≥2048 (Sonnet) to
                actually cache; shorter prefixes are silently uncached.
            thinking: Pass {"type": "adaptive"} for Sonnet 4.6 reasoning.
                Haiku 4.5 does not support thinking.
            label: Short tag for budget accounting (e.g. "newsletters",
                "feedback.triage"). Defaults to the model name.
        """
        if self.budget is not None:
            self.budget.assert_not_exhausted(label=label or model)

        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }

        if system is not None:
            kwargs["system"] = _prepare_system(system, cache=cache_system)

        if thinking is not None:
            if model == HAIKU:
                raise ValueError("Haiku 4.5 does not support the thinking parameter.")
            kwargs["thinking"] = thinking

        log.info(
            "Claude call: model=%s max_tokens=%d messages=%d cache_system=%s label=%s",
            model, max_tokens, len(messages), cache_system, label or "-",
        )

        resp = self._client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if b.type == "text")
        usage = resp.usage

        result = CallResult(
            text=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            model=model,
            stop_reason=resp.stop_reason,
        )

        log.info(
            "Claude response: in=%d out=%d cache_read=%d cache_write=%d cost=$%.4f stop=%s",
            result.input_tokens, result.output_tokens,
            result.cache_read_tokens, result.cache_creation_tokens,
            result.cost_usd, result.stop_reason,
        )

        if self.budget is not None:
            self.budget.record(label=label or model, model=model, cost=result.cost_usd)

        return result


def _prepare_system(system: Union[str, list], *, cache: bool) -> Union[str, list]:
    if not cache:
        return system
    if isinstance(system, str):
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    blocks = list(system)
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    return blocks
