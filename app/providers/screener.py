"""Fundamentals from screener.in — more accurate for Indian stocks than yfinance.

Scrapes the public 'top-ratios' block (Market Cap, P/E, Book Value, ROE, ROCE,
Dividend Yield, Face Value). Stdlib only, best-effort: any failure returns {} so
callers fall back to yfinance. One request per stock per run (daily) is polite.
"""
from __future__ import annotations

import re
import urllib.request
from typing import Dict, List, Optional

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _base_symbol(ticker: str) -> str:
    return ticker.split(".")[0].upper().strip()


def _to_float(s: str) -> Optional[float]:
    s = re.sub(r"[,%₹]", "", s).strip()
    try:
        return float(s)
    except ValueError:
        return None


def _fetch(url: str) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                return None
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def _parse_top_ratios(html: str) -> Dict[str, List[float]]:
    m = re.search(r'id="top-ratios".*?</ul>', html, re.S)
    if not m:
        return {}
    block = m.group(0)
    out: Dict[str, List[float]] = {}
    for li in re.findall(r"<li[^>]*>(.*?)</li>", block, re.S):
        nm = re.search(r'class="name">(.*?)</span>', li, re.S)
        if not nm:
            continue
        name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", nm.group(1))).strip()
        nums = re.findall(r'class="number">(.*?)</span>', li, re.S)
        vals = [_to_float(re.sub(r"<[^>]+>", "", n)) for n in nums]
        out[name] = [v for v in vals if v is not None]
    return out


def _parse_shareholding(html: str) -> Dict:
    """Extract promoter / FII / DII holding and the promoter trend over quarters."""
    m = re.search(r'id="shareholding".*?</section>', html, re.S)
    if not m:
        return {}
    block = m.group(0)
    # Quarter headers (skip the first empty corner cell).
    heads = [re.sub(r"<[^>]+>", "", h).strip()
             for h in re.findall(r"<th[^>]*>(.*?)</th>", block, re.S)]
    quarters = [h for h in heads if h]

    def row_vals(label: str) -> List[float]:
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).replace("\xa0", " ").strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if cells and cells[0].lower().startswith(label.lower()):
                return [v for v in (_to_float(c) for c in cells[1:]) if v is not None]
        return []

    promoters = row_vals("Promoter")
    fiis = row_vals("FII")
    diis = row_vals("DII")
    if not promoters:
        return {}

    latest = promoters[-1]
    out = {
        "promoter_pct": latest,
        "promoter_prev_q_pct": promoters[-2] if len(promoters) >= 2 else None,
        "promoter_change_q_pp": (round(latest - promoters[-2], 2)
                                 if len(promoters) >= 2 else None),
        "promoter_change_window_pp": round(latest - promoters[0], 2),
        "fii_pct": fiis[-1] if fiis else None,
        "dii_pct": diis[-1] if diis else None,
        "as_of": quarters[-1] if quarters else None,
        "window_start": quarters[0] if quarters else None,
    }
    return out


def fetch_company_data(ticker: str) -> Dict:
    """Single fetch returning both fundamentals ratios and shareholding."""
    sym = _base_symbol(ticker)
    for path in ("consolidated/", ""):
        url = f"https://www.screener.in/company/{sym}/{path}"
        html = _fetch(url)
        if not html:
            continue
        ratios = _ratios_from_html(html, path)
        if not ratios:
            continue
        ratios["shareholding"] = _parse_shareholding(html)
        return ratios
    return {}


def _ratios_from_html(html: str, path: str) -> Dict:
    """Normalize the top-ratios block into our fundamentals fields."""
    raw = _parse_top_ratios(html)
    if not raw:
        return {}

    def first(name: str) -> Optional[float]:
        vals = raw.get(name) or []
        return vals[0] if vals else None

    price = first("Current Price")
    book_value = first("Book Value")
    pe = first("Stock P/E")
    market_cap_cr = first("Market Cap")

    out = {
        "pe": pe,
        "book_value": book_value,
        "roe_pct": first("ROE"),
        "roce_pct": first("ROCE"),
        "dividend_yield_pct": first("Dividend Yield"),
        "face_value": first("Face Value"),
        # screener market cap is in ₹ crore; convert to absolute to match yfinance.
        "market_cap": int(market_cap_cr * 1e7) if market_cap_cr else None,
        # derived
        "pb": (round(price / book_value, 2) if price and book_value else None),
        "ttm_eps": (round(price / pe, 2) if price and pe else None),
        "source": f"screener.in ({'consolidated' if 'consolidated' in path else 'standalone'})",
    }
    # Only treat as success if we got the core ratios.
    return out if (out["pe"] is not None or out["book_value"] is not None) else {}


def fetch_fundamentals(ticker: str) -> Dict:
    """Return normalized fundamentals from screener.in, or {} on any failure."""
    data = fetch_company_data(ticker)
    if data:
        data.pop("shareholding", None)
    return data
