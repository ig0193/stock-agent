"""Shared data structures for the stock-analysis agent."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# Portfolio "kind" distinguishes the holdings used by scheduled vs manual runs.
KIND_SCHEDULED = "scheduled"
KIND_MANUAL = "manual"
VALID_KINDS = (KIND_SCHEDULED, KIND_MANUAL)

ACTIONS = ("BUY", "HOLD", "CUT", "SELL")


@dataclass
class Holding:
    ticker: str
    qty: float
    avg_buy_price: float
    sector: Optional[str] = None


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
    data_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Decision:
    action: str                              # primary action, one of ACTIONS
    confidence: int                          # 0..100
    rationale: List[str] = field(default_factory=list)   # bullet points
    key_risks: List[str] = field(default_factory=list)   # bullet points
    alternatives: List[Dict] = field(default_factory=list)  # [{action, confidence}]
