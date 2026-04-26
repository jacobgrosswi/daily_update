"""Tests for src/markets.py — fakes the yfinance module."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src import markets
from src.markets import (
    IndexQuote,
    PremarketAlert,
    check_premarket,
    fetch_index_quotes,
    render_markdown,
)


def _df(closes: list[float], dates: list[str]) -> pd.DataFrame:
    """Build a yfinance-shaped history frame indexed by trading date."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.DataFrame({"Close": closes}, index=idx)


def _fake_yf(history_map: dict[str, pd.DataFrame],
             fast_info_map: dict[str, dict] | None = None,
             raises: dict[str, Exception] | None = None):
    """Return a SimpleNamespace that quacks like yfinance."""
    raises = raises or {}
    fast_info_map = fast_info_map or {}

    def make_ticker(symbol):
        if symbol in raises:
            t = MagicMock()
            t.history.side_effect = raises[symbol]
            type(t).fast_info = property(lambda self: (_ for _ in ()).throw(raises[symbol]))
            return t
        t = MagicMock()
        t.history.return_value = history_map.get(symbol, pd.DataFrame())
        t.fast_info = fast_info_map.get(symbol, {})
        return t

    return SimpleNamespace(Ticker=make_ticker)


# ---------- fetch_index_quotes ----------

def test_fetch_index_quotes_computes_change(tmp_path):
    history = {
        "^GSPC": _df([5474.76, 5487.21], ["2026-04-22", "2026-04-23"]),
    }
    yfm = _fake_yf(history)
    out = fetch_index_quotes([{"symbol": "^GSPC", "label": "S&P 500"}], yf_module=yfm)
    assert len(out) == 1
    q = out[0]
    assert q.close == 5487.21
    assert q.change == pytest.approx(12.45)
    assert q.change_pct == pytest.approx(12.45 / 5474.76 * 100)
    assert q.as_of == date(2026, 4, 23)
    assert q.is_yield is False


def test_fetch_index_quotes_yield_flag_propagates():
    history = {"^TNX": _df([4.39, 4.42], ["2026-04-22", "2026-04-23"])}
    yfm = _fake_yf(history)
    out = fetch_index_quotes(
        [{"symbol": "^TNX", "label": "10-yr Tsy", "is_yield": True}], yf_module=yfm,
    )
    assert out[0].is_yield is True
    assert out[0].change_bps == pytest.approx(3.0)


def test_fetch_index_quotes_skips_when_history_too_short():
    history = {"^GSPC": _df([5487.21], ["2026-04-23"])}
    yfm = _fake_yf(history)
    out = fetch_index_quotes([{"symbol": "^GSPC", "label": "S&P 500"}], yf_module=yfm)
    assert out == []


def test_fetch_index_quotes_skips_on_yfinance_failure():
    yfm = _fake_yf({}, raises={"^GSPC": RuntimeError("rate limited")})
    out = fetch_index_quotes([{"symbol": "^GSPC", "label": "S&P 500"}], yf_module=yfm)
    assert out == []


def test_fetch_index_quotes_partial_failure_returns_what_succeeds():
    history = {"^DJI": _df([38800.0, 38912.44], ["2026-04-22", "2026-04-23"])}
    yfm = _fake_yf(history, raises={"^IXIC": RuntimeError("boom")})
    out = fetch_index_quotes(
        [{"symbol": "^IXIC", "label": "Nasdaq"},
         {"symbol": "^DJI", "label": "Dow"}],
        yf_module=yfm,
    )
    assert [q.symbol for q in out] == ["^DJI"]


def test_fetch_index_quotes_uses_most_recent_two_closes():
    """5-day history should still pick the latest pair (closes are sorted ascending)."""
    history = {"^GSPC": _df(
        [5400.0, 5410.0, 5420.0, 5474.76, 5487.21],
        ["2026-04-17", "2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23"],
    )}
    yfm = _fake_yf(history)
    out = fetch_index_quotes([{"symbol": "^GSPC", "label": "S&P 500"}], yf_module=yfm)
    assert out[0].close == 5487.21
    assert out[0].change == pytest.approx(12.45)


# ---------- check_premarket ----------

def test_premarket_alert_above_threshold():
    fast = {"ES=F": SimpleNamespace(previous_close=5500.0, last_price=5444.5)}  # -1.01%
    yfm = _fake_yf({}, fast_info_map=fast)
    alerts = check_premarket(["^GSPC"], threshold_pct=1.0, yf_module=yfm)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.index_symbol == "^GSPC"
    assert a.proxy_symbol == "ES=F"
    assert a.move_pct < -1.0


def test_premarket_no_alert_below_threshold():
    fast = {"ES=F": SimpleNamespace(previous_close=5500.0, last_price=5510.0)}  # 0.18%
    yfm = _fake_yf({}, fast_info_map=fast)
    assert check_premarket(["^GSPC"], threshold_pct=1.0, yf_module=yfm) == []


def test_premarket_skips_unsupported_symbol():
    """^TNX has no proxy mapping; it should be silently skipped."""
    yfm = _fake_yf({})
    assert check_premarket(["^TNX"], yf_module=yfm) == []


def test_premarket_skips_zero_prev_close():
    fast = {"ES=F": SimpleNamespace(previous_close=0.0, last_price=5444.0)}
    yfm = _fake_yf({}, fast_info_map=fast)
    assert check_premarket(["^GSPC"], yf_module=yfm) == []


def test_premarket_skips_when_market_closed():
    """yfinance returns None for futures values outside trading hours."""
    fast = {"ES=F": SimpleNamespace(previous_close=None, last_price=None)}
    yfm = _fake_yf({}, fast_info_map=fast)
    assert check_premarket(["^GSPC"], yf_module=yfm) == []


def test_premarket_swallows_proxy_failure():
    yfm = _fake_yf({}, raises={"ES=F": RuntimeError("network")})
    assert check_premarket(["^GSPC"], yf_module=yfm) == []


# ---------- render_markdown ----------

def test_render_markdown_with_quotes_and_alert():
    quotes = [
        IndexQuote("^GSPC", "S&P 500", 5487.21, 12.45, 0.2275, date(2026, 4, 23)),
        IndexQuote("^TNX", "10-yr Tsy", 4.42, 0.03, 0.68, date(2026, 4, 23), is_yield=True),
    ]
    alerts = [PremarketAlert("^GSPC", "S&P 500 futures", "ES=F", -1.2)]
    md = render_markdown(quotes, alerts)
    assert "Market Update — Thursday, April 23, 2026" in md
    assert "S&P 500" in md
    assert "5,487.21" in md
    assert "+12.45" in md
    assert "(+0.23%)" in md
    assert "10-yr Tsy" in md
    assert "+3 bps" in md
    assert "Pre-market note: S&P 500 futures down 1.2%." in md


def test_render_markdown_negative_changes():
    quotes = [IndexQuote("^IXIC", "Nasdaq", 17234.88, -45.12, -0.2614, date(2026, 4, 23))]
    md = render_markdown(quotes)
    assert "-45.12" in md
    assert "(-0.26%)" in md


def test_render_markdown_empty_section_unavailable():
    md = render_markdown([])
    assert "Section unavailable" in md


# ---------- load_tickers_config ----------

def test_load_tickers_config_round_trip():
    cfg = markets.load_tickers_config()
    assert cfg.premarket_alert_pct == 1.0
    symbols = [t["symbol"] for t in cfg.tickers]
    assert "^GSPC" in symbols and "^TNX" in symbols
    tnx = next(t for t in cfg.tickers if t["symbol"] == "^TNX")
    assert tnx.get("is_yield") is True
