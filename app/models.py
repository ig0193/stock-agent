"""Shared data structures for the stock-analysis agent."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# Portfolio "kind" distinguishes the holdings used by scheduled vs manual runs.
KIND_SCHEDULED = "scheduled"
KIND_MANUAL = "manual"
VALID_KINDS = (KIND_SCHEDULED, KIND_MANUAL)

# Owned positions use the first four; un-owned "watchlist" candidates use the last two.
ACTIONS = ("BUY", "HOLD", "CUT", "SELL", "WATCH", "AVOID")
OWNED_ACTIONS = ("BUY", "HOLD", "CUT", "SELL")
WATCHLIST_ACTIONS = ("BUY", "WATCH", "AVOID")


@dataclass
class Holding:
    ticker: str
    qty: Optional[float] = None          # None => watchlist (not yet purchased)
    avg_buy_price: Optional[float] = None
    sector: Optional[str] = None

    @property
    def is_watchlist(self) -> bool:
        return self.avg_buy_price is None or self.qty is None


@dataclass
class EvidencePacket:
    """Everything we gathered for one stock, stored verbatim as the audit trail."""
    ticker: str
    qty: float
    avg_buy_price: float
    current_price: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None
    technicals: Dict = field(default_factory=dict)
    fundamentals: Dict = field(default_factory=dict)
    company_profile: Dict = field(default_factory=dict)
    shareholding: Dict = field(default_factory=dict)
    sector: Dict = field(default_factory=dict)
    news_digest: str = ""
    market_weather: str = ""
    market_regime: Dict = field(default_factory=dict)
    as_of_date: str = ""
    is_watchlist: bool = False
    data_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Decision:
    action: str                              # primary action, one of ACTIONS
    confidence: int                          # 0..100 (primary's share of the distribution)
    stance: str = ""                         # one-line plain-English nuanced recommendation
    rationale: List[str] = field(default_factory=list)   # bullet points
    key_risks: List[str] = field(default_factory=list)   # bullet points
    alternatives: List[Dict] = field(default_factory=list)  # non-primary distribution entries [{action, confidence}]
    triggers: List[Dict] = field(default_factory=list)   # [{action, condition}] — what would change the call
