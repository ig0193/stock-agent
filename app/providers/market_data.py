"""Market-data provider interface + yfinance implementation.

The rest of the system depends only on this interface, so a broker API
(Kite/Upstox) can be slotted in later without touching analysis or UI code.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd
import yfinance as yf

from .screener import fetch_company_data as fetch_screener


def _trim(text: Optional[str], limit: int = 700) -> Optional[str]:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


class MarketDataProvider:
    """Abstract provider. Swap implementations without changing callers."""

    def history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        raise NotImplementedError

    def fundamentals(self, ticker: str) -> Dict:
        raise NotImplementedError

    def snapshot(self, ticker: str) -> Dict:
        """Return {'fundamentals', 'profile', 'shareholding'} in minimal fetches."""
        raise NotImplementedError


class YFinanceProvider(MarketDataProvider):
    def history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        try:
            df = yf.Ticker(ticker).history(period=period, auto_adjust=False)
            return df if df is not None else pd.DataFrame()
        except Exception:
            return pd.DataFrame()

    def fundamentals(self, ticker: str) -> Dict:
        """Best-effort fundamentals; missing fields come back as None."""
        return self.snapshot(ticker)["fundamentals"]

    def snapshot(self, ticker: str) -> Dict:
        info: Dict = {}
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception:
            info = {}

        def g(key):
            v = info.get(key)
            return v if isinstance(v, (int, float)) else None

        roe = g("returnOnEquity")
        rev_growth = g("revenueGrowth")
        margins = g("profitMargins")
        fundamentals = {
            "name": info.get("shortName") or info.get("longName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "pe": g("trailingPE"),
            "forward_pe": g("forwardPE"),
            "pb": g("priceToBook"),
            "book_value": g("bookValue"),
            "ttm_eps": g("trailingEps"),
            "roe_pct": round(roe * 100, 1) if roe is not None else None,
            "roce_pct": None,
            "debt_to_equity": g("debtToEquity"),
            "rev_growth_pct": round(rev_growth * 100, 1) if rev_growth is not None else None,
            "profit_margin_pct": round(margins * 100, 1) if margins is not None else None,
            "market_cap": g("marketCap"),
            "dividend_yield_pct": _dividend_yield_pct(g("dividendYield")),
            "face_value": None,
            "source": "Yahoo Finance",
        }

        # Qualitative profile — grounds the LLM in what the business actually does.
        profile = {
            "long_name": info.get("longName") or info.get("shortName"),
            "business_summary": _trim(info.get("longBusinessSummary")),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "employees": g("fullTimeEmployees"),
            "website": info.get("website"),
        }

        shareholding: Dict = {}

        # One screener.in fetch supplies ratios (more accurate for India) + holdings.
        scr = fetch_screener(ticker)
        if scr:
            for key in ("pe", "pb", "book_value", "ttm_eps", "roe_pct",
                        "roce_pct", "dividend_yield_pct", "market_cap", "face_value"):
                if scr.get(key) is not None:
                    fundamentals[key] = scr[key]
            # e.g. "screener.in (consolidated)" — make the basis explicit so any
            # difference vs apps that show standalone (e.g. Groww) is explainable.
            basis = scr.get("source", "screener.in")
            fundamentals["source"] = f"{basis} ratios + Yahoo Finance (sector, growth, margins)"
            shareholding = scr.get("shareholding") or {}

        return {"fundamentals": fundamentals, "profile": profile,
                "shareholding": shareholding}


def _dividend_yield_pct(raw: Optional[float]) -> Optional[float]:
    """Normalize yfinance's dividend yield to a percentage.

    Older yfinance returned a fraction (0.0176 = 1.76%); newer versions return
    the percentage directly (1.76). Detect which by magnitude.
    """
    if raw is None:
        return None
    pct = raw * 100 if raw < 1 else raw
    return round(pct, 2)


_DEFAULT_PROVIDER: Optional[MarketDataProvider] = None


def get_provider() -> MarketDataProvider:
    global _DEFAULT_PROVIDER
    if _DEFAULT_PROVIDER is None:
        _DEFAULT_PROVIDER = YFinanceProvider()
    return _DEFAULT_PROVIDER
