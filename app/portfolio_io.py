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
    for required in ("ticker", "qty", "avg_buy_price"):
        if required not in canon:
            errors.append(f"Missing required column: {required}")
    if errors:
        return [], errors

    holdings: List[Holding] = []
    for i, row in enumerate(reader, start=2):  # row 1 is header
        norm = {field_map[k]: (v or "").strip() for k, v in row.items() if k}
        ticker = norm.get("ticker", "").upper()
        if not ticker:
            continue
        try:
            qty = float(norm.get("qty", "") or 0)
            avg = float(norm.get("avg_buy_price", "") or 0)
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
