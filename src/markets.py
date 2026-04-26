"""Market Update section: previous-trading-day closes plus a pre-market callout.

Indices and the threshold come from config/tickers.yml. yfinance handles
weekends/holidays for free — period='5d' always returns the most recent N
trading days, so on Mondays we get Friday's close as the latest row. The
trading date is preserved on each quote so the renderer can label it.

For ^TNX (10-year Treasury yield) the values are in percent (e.g., 4.42),
not dollars; `change` is the absolute change in percent points and the
renderer should show it as basis points (1 pp = 100 bps).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import yaml
import yfinance as yf

from .utils import REPO_ROOT, get_logger

log = get_logger(__name__)

TICKERS_PATH = REPO_ROOT / "config" / "tickers.yml"

# Futures proxies for the pre-market check. yfinance's fast_info on futures
# gives a usable last_price even before the cash market opens.
# 10-year Treasury is intentionally omitted — yields don't have a clean
# pre-market analogue and a 1% move in the cash yield is not meaningful here.
PREMARKET_PROXIES: dict[str, tuple[str, str]] = {
    "^GSPC": ("ES=F", "S&P 500 futures"),
    "^IXIC": ("NQ=F", "Nasdaq futures"),
    "^DJI": ("YM=F", "Dow futures"),
}


@dataclass
class IndexQuote:
    symbol: str
    label: str
    close: float
    change: float       # close - prior_close, in same units as close
    change_pct: float   # percent move (0.23 means 0.23%)
    as_of: date         # the trading date of `close`
    is_yield: bool = False

    @property
    def change_bps(self) -> float:
        """For yields: change in basis points. 1 percentage point = 100 bps."""
        return self.change * 100


@dataclass
class PremarketAlert:
    index_symbol: str   # e.g. '^GSPC'
    label: str          # e.g. 'S&P 500 futures'
    proxy_symbol: str   # e.g. 'ES=F'
    move_pct: float     # signed; can be negative


@dataclass
class TickersConfig:
    tickers: list[dict]
    premarket_alert_pct: float


def load_tickers_config(path: Path = TICKERS_PATH) -> TickersConfig:
    data = yaml.safe_load(path.read_text())
    return TickersConfig(
        tickers=data["tickers"],
        premarket_alert_pct=float(data.get("premarket_alert_pct", 1.0)),
    )


def fetch_index_quotes(
    tickers: list[dict],
    *,
    yf_module=None,
) -> list[IndexQuote]:
    """Fetch the latest two trading-day closes for each ticker.

    Failures are logged and skipped per the partial-failure policy
    (Section 9 of the scoping doc) — the section renders what it has.
    """
    yfm = yf_module or yf
    out: list[IndexQuote] = []

    for cfg in tickers:
        symbol = cfg["symbol"]
        try:
            ticker = yfm.Ticker(symbol)
            hist = ticker.history(period="5d", auto_adjust=False)
            closes = hist["Close"].dropna()
            if len(closes) < 2:
                log.warning("Not enough history for %s; skipping.", symbol)
                continue
            close = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            change = close - prev
            change_pct = (change / prev) * 100 if prev else 0.0
            as_of = closes.index[-1].date()
            out.append(IndexQuote(
                symbol=symbol,
                label=cfg["label"],
                close=close,
                change=change,
                change_pct=change_pct,
                as_of=as_of,
                is_yield=bool(cfg.get("is_yield", False)),
            ))
        except Exception as e:
            log.warning("yfinance fetch failed for %s: %s", symbol, e)

    log.info("Fetched %d/%d index quotes", len(out), len(tickers))
    return out


def check_premarket(
    symbols: list[str],
    threshold_pct: float = 1.0,
    *,
    yf_module=None,
) -> list[PremarketAlert]:
    """Return alerts for any index whose futures proxy moved ≥ threshold_pct."""
    yfm = yf_module or yf
    alerts: list[PremarketAlert] = []

    for sym in symbols:
        if sym not in PREMARKET_PROXIES:
            continue
        proxy, label = PREMARKET_PROXIES[sym]
        try:
            t = yfm.Ticker(proxy)
            info = t.fast_info
            # yfinance's FastInfo exposes snake_case attributes and camelCase
            # dict keys; attribute access is the documented form.
            prev_raw = getattr(info, "previous_close", None)
            last_raw = getattr(info, "last_price", None)
            if prev_raw is None or last_raw is None:
                # Markets closed (no overnight session active) — skip.
                continue
            prev = float(prev_raw)
            last = float(last_raw)
            if prev == 0:
                continue
            move_pct = (last - prev) / prev * 100
            if abs(move_pct) >= threshold_pct:
                alerts.append(PremarketAlert(
                    index_symbol=sym, label=label,
                    proxy_symbol=proxy, move_pct=move_pct,
                ))
        except Exception as e:
            log.warning("Pre-market check failed for %s (%s): %s", sym, proxy, e)

    return alerts


def render_markdown(
    quotes: list[IndexQuote],
    alerts: Optional[list[PremarketAlert]] = None,
) -> str:
    """Render the market section as Markdown matching scoping doc Section 4.3.

    Falls back to 'Section unavailable' if no quotes — the orchestrator can
    still ship the rest of the briefing.
    """
    if not quotes:
        return "## Market Update\n\nSection unavailable: no index data.\n"

    # Pick the most recent date across quotes for the header.
    as_of = max(q.as_of for q in quotes)
    header = f"## Market Update — {as_of.strftime('%A, %B %-d, %Y')}\n"

    lines = [header, "```"]
    for q in quotes:
        lines.append(_format_row(q))
    lines.append("```")

    if alerts:
        lines.append("")
        for a in alerts:
            sign = "up" if a.move_pct > 0 else "down"
            lines.append(f"_Pre-market note: {a.label} {sign} {abs(a.move_pct):.1f}%._")

    return "\n".join(lines) + "\n"


def _format_row(q: IndexQuote) -> str:
    """Fixed-width row matching the scoping doc example."""
    if q.is_yield:
        return f"{q.label:<14}{q.close:>6.2f}%   {q.change_bps:+.0f} bps"
    return (
        f"{q.label:<14}{q.close:>10,.2f}   "
        f"{q.change:+,.2f}   "
        f"({q.change_pct:+.2f}%)"
    )
