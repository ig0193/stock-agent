"""Parse uploaded CSV/Excel-exported CSV into Holding objects."""
from __future__ import annotations

import csv
import io
from typing import List, Tuple

from .models import Holding

# Accepted header aliases (lower-cased, stripped).
_ALIASES = {
    "ticker": {"ticker", "symbol", "scrip", "stock"},
    "qty": {"qty", "quantity", "shares", "units"},
    "avg_buy_price": {"avg_buy_price", "avg price", "avg_price", "average price",
                      "buy price", "avg buy price", "cost", "avg cost"},
    "sector": {"sector", "industry"},
}


def _map_header(name: str) -> str:
    n = name.strip().lower()
    for canonical, aliases in _ALIASES.items():
        if n in aliases:
            return canonical
    return n


def parse_csv(raw: bytes) -> Tuple[List[Holding], List[str]]:
    """Returns (holdings, errors). Skips bad rows, collecting messages."""
    errors: List[str] = []
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV has no header row."]

    field_map = {f: _map_header(f) for f in reader.fieldnames}
    canon = set(field_map.values())
    # Only ticker is required. Blank qty/avg_buy_price => watchlist (not yet bought).
    if "ticker" not in canon:
        return [], ["Missing required column: ticker"]

    def _num(raw: str):
        raw = (raw or "").strip()
        return float(raw) if raw else None

    holdings: List[Holding] = []
    for i, row in enumerate(reader, start=2):  # row 1 is header
        norm = {field_map[k]: (v or "").strip() for k, v in row.items() if k}
        ticker = norm.get("ticker", "").upper()
        if not ticker:
            continue
        try:
            qty = _num(norm.get("qty"))
            avg = _num(norm.get("avg_buy_price"))
        except ValueError:
            errors.append(f"Row {i}: non-numeric qty/price for {ticker}, skipped.")
            continue
        holdings.append(Holding(
            ticker=ticker, qty=qty, avg_buy_price=avg,
            sector=norm.get("sector") or None,
        ))
    if not holdings and not errors:
        errors.append("No valid rows found.")
    return holdings, errors
