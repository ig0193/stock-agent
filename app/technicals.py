"""Deterministic technical indicators computed from OHLCV history.

Kept dependency-light (plain pandas) so it is reproducible and avoids the
install friction of heavier TA libraries.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd


def _rsi(close: pd.Series, period: int = 14) -> Optional[float]:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    last_loss = avg_loss.iloc[-1]
    last_gain = avg_gain.iloc[-1]
    if pd.isna(last_gain) or pd.isna(last_loss):
        return None
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return round(100 - (100 / (1 + rs)), 1)


def _ma(close: pd.Series, window: int) -> Optional[float]:
    if len(close) < window:
        return None
    val = close.rolling(window).mean().iloc[-1]
    return None if pd.isna(val) else round(float(val), 2)


def compute_technicals(history: pd.DataFrame) -> Dict:
    """history: DataFrame with at least a 'Close' column (yfinance 1y daily)."""
    out: Dict = {}
    if history is None or history.empty or "Close" not in history:
        return out

    close = history["Close"].dropna()
    if close.empty:
        return out

    current = round(float(close.iloc[-1]), 2)
    dma50 = _ma(close, 50)
    dma200 = _ma(close, 200)
    # 52-week range must use intraday High/Low, not Close — otherwise it
    # understates the true range (validated against broker data for HDFCBANK).
    high_src = history["High"].dropna() if "High" in history else close
    low_src = history["Low"].dropna() if "Low" in history else close
    high_52w = round(float(high_src.max()), 2)
    low_52w = round(float(low_src.min()), 2)

    out["current_price"] = current
    out["dma50"] = dma50
    out["dma200"] = dma200
    out["above_50dma"] = (current > dma50) if dma50 is not None else None
    out["above_200dma"] = (current > dma200) if dma200 is not None else None
    out["rsi14"] = _rsi(close)
    out["high_52w"] = high_52w
    out["low_52w"] = low_52w
    out["pct_from_52w_high"] = (
        round((current - high_52w) / high_52w * 100, 1) if high_52w else None
    )
    out["pct_from_52w_low"] = (
        round((current - low_52w) / low_52w * 100, 1) if low_52w else None
    )

    # Trailing returns across horizons (trading days): 1d, 1w, 1m, 3m, 6m, 1y.
    out["return_1d_pct"] = _trailing_return(close, 1)
    out["return_1w_pct"] = _trailing_return(close, 5)
    out["return_1mo_pct"] = _trailing_return(close, 21)
    out["return_3mo_pct"] = _trailing_return(close, 63)
    out["return_6mo_pct"] = _trailing_return(close, 126)
    out["return_1y_pct"] = _trailing_return(close, 252)
    return out


def _trailing_return(close: pd.Series, periods: int) -> Optional[float]:
    if len(close) <= periods:
        return None
    past = close.iloc[-(periods + 1)]
    now = close.iloc[-1]
    if past == 0 or pd.isna(past):
        return None
    return round((now - past) / past * 100, 1)
