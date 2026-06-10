"""Shared 'market weather' note + structured market regime for a run.

Derived deterministically from the Nifty 50 index (^NSEI) movement plus recent
top market headlines. One note per run, applied to all stocks (not per-ticker).
The structured regime lets even the rule-based fallback factor macro conditions
in (e.g. avoid selling into broad weakness).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .market_data import get_provider
from .news import fetch_headlines
from ..technicals import compute_technicals

NIFTY = "^NSEI"


def classify_regime(nifty_tech: Dict) -> Dict:
    """Turn Nifty technicals into a risk-on / risk-off label + the inputs."""
    above50 = nifty_tech.get("above_50dma")
    above200 = nifty_tech.get("above_200dma")
    rsi = nifty_tech.get("rsi14")
    r1m = nifty_tech.get("return_1mo_pct")
    r3m = nifty_tech.get("return_3mo_pct")

    r1d = nifty_tech.get("return_1d_pct")
    r1w = nifty_tech.get("return_1w_pct")

    label: Optional[str] = None
    if above50 is None and above200 is None:
        label = None  # unknown
    elif above200 is False and (above50 is False or (r3m is not None and r3m <= -5)):
        label = "risk-off"           # broad, sustained downtrend
    elif above50 is False and above200 is True:
        label = "cautious"           # pulling back within an uptrend
    elif above50 and above200 and (rsi is None or rsi < 78):
        label = "risk-on"            # healthy uptrend
    else:
        label = "neutral"

    # Note a short-term bounce inside a downtrend so it isn't read as pure risk-off.
    short_term = None
    if label == "risk-off" and r1w is not None and r1d is not None and r1w > 0 and r1d > 0:
        short_term = "short-term bounce (1W and 1D positive)"

    return {
        "label": label,
        "short_term": short_term,
        "nifty_above_50dma": above50,
        "nifty_above_200dma": above200,
        "nifty_rsi": rsi,
        "nifty_1d_pct": r1d,
        "nifty_1w_pct": r1w,
        "nifty_1mo_pct": r1m,
        "nifty_3mo_pct": r3m,
        "nifty_6mo_pct": nifty_tech.get("return_6mo_pct"),
        "nifty_1y_pct": nifty_tech.get("return_1y_pct"),
    }


def build_market_weather(nifty_tech: Optional[Dict] = None,
                         regime: Optional[Dict] = None) -> str:
    """Human-readable macro note. Reuses already-fetched Nifty data if given."""
    provider = get_provider()
    parts: List[str] = []

    if nifty_tech is None:
        nifty_tech = compute_technicals(provider.history(NIFTY, period="1y"))
    if regime is None:
        regime = classify_regime(nifty_tech)

    if nifty_tech:
        cur = nifty_tech.get("current_price")
        trend_bits = []
        if nifty_tech.get("above_50dma") is not None:
            trend_bits.append("above 50DMA" if nifty_tech["above_50dma"] else "below 50DMA")
        if nifty_tech.get("above_200dma") is not None:
            trend_bits.append("above 200DMA" if nifty_tech["above_200dma"] else "below 200DMA")
        label = regime.get("label")
        prefix = f"Market regime: {label.upper()}"
        if regime.get("short_term"):
            prefix += f" ({regime['short_term']})"
        prefix += ". "
        rets = (f"1D {nifty_tech.get('return_1d_pct')}%, 1W {nifty_tech.get('return_1w_pct')}%, "
                f"1M {nifty_tech.get('return_1mo_pct')}%, 3M {nifty_tech.get('return_3mo_pct')}%, "
                f"1Y {nifty_tech.get('return_1y_pct')}%")
        parts.append(
            f"{prefix}Nifty 50 at {cur} ({rets}; RSI {nifty_tech.get('rsi14')}; "
            f"{', '.join(trend_bits) if trend_bits else 'trend n/a'})."
        )

    headlines = fetch_headlines("Indian stock market Nifty Sensex")
    if headlines:
        parts.append("Top market headlines: " + " | ".join(headlines[:5]))

    if not parts:
        return "Market weather unavailable (data fetch failed)."
    return " ".join(parts)
