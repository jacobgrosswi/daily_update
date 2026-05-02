"""Tests for src/claude_client.py — mocks the Anthropic SDK."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import claude_client
from src.budget import Budget, BudgetExceeded
from src.claude_client import HAIKU, SONNET, CallResult, ClaudeClient, _prepare_system


def _mock_response(text: str = "ok", input_tokens: int = 100, output_tokens: int = 50,
                   cache_read: int = 0, cache_create: int = 0):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create,
        ),
        stop_reason="end_turn",
    )


@pytest.fixture
def client(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(claude_client.anthropic, "Anthropic", lambda **kw: fake)
    c = ClaudeClient(api_key="test-key")
    c._underlying = fake
    return c


def test_basic_call_returns_text_and_usage(client):
    client._underlying.messages.create.return_value = _mock_response(text="Hello!")
    result = client.call(messages=[{"role": "user", "content": "Hi"}])
    assert isinstance(result, CallResult)
    assert result.text == "Hello!"
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.model == HAIKU
    assert result.stop_reason == "end_turn"


def test_cost_calculation_haiku():
    r = CallResult(text="x", input_tokens=1_000_000, output_tokens=1_000_000,
                   cache_read_tokens=0, cache_creation_tokens=0,
                   model=HAIKU, stop_reason="end_turn")
    # Haiku: $1 in + $5 out = $6
    assert r.cost_usd == pytest.approx(6.00)


def test_cost_calculation_sonnet_with_cache():
    r = CallResult(text="x", input_tokens=0, output_tokens=0,
                   cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000,
                   model=SONNET, stop_reason="end_turn")
    # Sonnet: $0.30 cache_read + $3.75 cache_write = $4.05
    assert r.cost_usd == pytest.approx(4.05)


def test_haiku_rejects_thinking(client):
    with pytest.raises(ValueError, match="does not support"):
        client.call(messages=[{"role": "user", "content": "hi"}],
                    model=HAIKU, thinking={"type": "adaptive"})


def test_sonnet_accepts_thinking(client):
    client._underlying.messages.create.return_value = _mock_response()
    client.call(
        messages=[{"role": "user", "content": "hi"}],
        model=SONNET,
        thinking={"type": "adaptive"},
    )
    kwargs = client._underlying.messages.create.call_args.kwargs
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["model"] == SONNET


def test_system_string_passes_through(client):
    client._underlying.messages.create.return_value = _mock_response()
    client.call(messages=[{"role": "user", "content": "hi"}], system="You are X")
    kwargs = client._underlying.messages.create.call_args.kwargs
    assert kwargs["system"] == "You are X"


def test_cache_system_wraps_string():
    out = _prepare_system("a system prompt", cache=True)
    assert out == [{
        "type": "text",
        "text": "a system prompt",
        "cache_control": {"type": "ephemeral"},
    }]


def test_cache_system_marks_last_block_in_list():
    blocks = [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
    out = _prepare_system(blocks, cache=True)
    assert out[0] == {"type": "text", "text": "first"}  # unchanged
    assert out[1] == {"type": "text", "text": "second", "cache_control": {"type": "ephemeral"}}


def test_no_cache_when_disabled():
    assert _prepare_system("hello", cache=False) == "hello"


def test_default_model_is_haiku(client):
    client._underlying.messages.create.return_value = _mock_response()
    client.call(messages=[{"role": "user", "content": "hi"}])
    kwargs = client._underlying.messages.create.call_args.kwargs
    assert kwargs["model"] == HAIKU


def test_max_tokens_override(client):
    client._underlying.messages.create.return_value = _mock_response()
    client.call(messages=[{"role": "user", "content": "hi"}], max_tokens=8192)
    kwargs = client._underlying.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == 8192


# ---------- Budget integration ----------

def test_call_records_cost_to_budget_on_success(client):
    client._underlying.messages.create.return_value = _mock_response(
        input_tokens=1_000_000, output_tokens=1_000_000,
    )
    budget = Budget(cap_usd=10.00)
    client.budget = budget
    client.call(messages=[{"role": "user", "content": "hi"}], label="test_label")
    # Haiku 1M in + 1M out = $6.00
    assert budget.spent_usd == pytest.approx(6.00)
    assert len(budget.records) == 1
    assert budget.records[0].label == "test_label"
    assert budget.records[0].model == HAIKU


def test_label_defaults_to_model_name(client):
    client._underlying.messages.create.return_value = _mock_response()
    budget = Budget()
    client.budget = budget
    client.call(messages=[{"role": "user", "content": "hi"}])
    assert budget.records[0].label == HAIKU


def test_call_raises_when_budget_already_exhausted(client):
    budget = Budget(cap_usd=0.05)
    budget.record(label="prior", model=HAIKU, cost=0.10)
    client.budget = budget
    with pytest.raises(BudgetExceeded, match="exhausted before"):
        client.call(messages=[{"role": "user", "content": "hi"}], label="next")
    # Pre-flight refused — must NOT have hit the API.
    client._underlying.messages.create.assert_not_called()


def test_call_without_budget_does_not_record(client):
    client._underlying.messages.create.return_value = _mock_response()
    client.budget = None  # default
    # Should not raise; just makes the call normally.
    result = client.call(messages=[{"role": "user", "content": "hi"}])
    assert result.text == "ok"


def test_budget_passed_via_constructor(monkeypatch):
    fake = MagicMock()
    fake.messages.create.return_value = _mock_response()
    monkeypatch.setattr(claude_client.anthropic, "Anthropic", lambda **kw: fake)
    budget = Budget()
    c = ClaudeClient(api_key="test-key", budget=budget)
    assert c.budget is budget
    c.call(messages=[{"role": "user", "content": "hi"}])
    assert len(budget.records) == 1
