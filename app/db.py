"""SQLite storage: portfolios/holdings (scheduled + manual) and runs/recommendations."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

from .models import KIND_MANUAL, KIND_SCHEDULED, VALID_KINDS, Holding

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "stock_agent.db")


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS portfolios (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT UNIQUE NOT NULL,   -- 'scheduled' | 'manual'
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS holdings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
                ticker        TEXT NOT NULL,
                qty           REAL,            -- NULL => watchlist (not yet purchased)
                avg_buy_price REAL,            -- NULL => watchlist
                sector        TEXT
            );

            CREATE TABLE IF NOT EXISTS runs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at     TEXT NOT NULL,
                trigger        TEXT NOT NULL,       -- 'scheduled' | 'manual' | 'individual'
                title          TEXT,                -- user-friendly label for the run
                status         TEXT NOT NULL,       -- 'running' | 'done' | 'failed'
                market_weather TEXT,
                error          TEXT
            );

            CREATE TABLE IF NOT EXISTS recommendations (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id             INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                ticker             TEXT NOT NULL,
                qty                REAL,
                avg_buy_price      REAL,
                current_price      REAL,
                unrealized_pnl_pct REAL,
                action             TEXT,
                confidence         INTEGER,
                rationale          TEXT,
                key_risks          TEXT,
                evidence_packet    TEXT               -- JSON blob (audit trail)
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                rec_id     INTEGER NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
                role       TEXT NOT NULL,       -- 'user' | 'assistant'
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # Lightweight migration: add columns introduced after first release.
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(recommendations)").fetchall()}
        if "alternatives" not in cols:
            conn.execute("ALTER TABLE recommendations ADD COLUMN alternatives TEXT")
        if "triggers" not in cols:
            conn.execute("ALTER TABLE recommendations ADD COLUMN triggers TEXT")
        if "stance" not in cols:
            conn.execute("ALTER TABLE recommendations ADD COLUMN stance TEXT")
        if "distribution" not in cols:
            conn.execute("ALTER TABLE recommendations ADD COLUMN distribution TEXT")
        run_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(runs)").fetchall()}
        if "title" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN title TEXT")

        # Migration: make holdings.qty / avg_buy_price nullable (watchlist support).
        # SQLite can't drop a NOT NULL constraint in place, so rebuild the table.
        h_info = conn.execute("PRAGMA table_info(holdings)").fetchall()
        if any(r["name"] == "qty" and r["notnull"] == 1 for r in h_info):
            conn.executescript(
                """
                CREATE TABLE holdings_new (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
                    ticker        TEXT NOT NULL,
                    qty           REAL,
                    avg_buy_price REAL,
                    sector        TEXT
                );
                INSERT INTO holdings_new (id, portfolio_id, ticker, qty, avg_buy_price, sector)
                    SELECT id, portfolio_id, ticker, qty, avg_buy_price, sector FROM holdings;
                DROP TABLE holdings;
                ALTER TABLE holdings_new RENAME TO holdings;
                """
            )

        # Ensure both portfolios always exist.
        for kind in VALID_KINDS:
            conn.execute(
                "INSERT OR IGNORE INTO portfolios (kind, updated_at) VALUES (?, ?)",
                (kind, _now()),
            )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- Portfolios / holdings ----------

def get_portfolio_id(conn: sqlite3.Connection, kind: str) -> int:
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown portfolio kind: {kind}")
    row = conn.execute("SELECT id FROM portfolios WHERE kind = ?", (kind,)).fetchone()
    return int(row["id"])


def get_holdings(kind: str) -> List[Holding]:
    with _connect() as conn:
        pid = get_portfolio_id(conn, kind)
        rows = conn.execute(
            "SELECT ticker, qty, avg_buy_price, sector FROM holdings "
            "WHERE portfolio_id = ? ORDER BY ticker",
            (pid,),
        ).fetchall()
    return [
        Holding(r["ticker"], r["qty"], r["avg_buy_price"], r["sector"]) for r in rows
    ]


def replace_holdings(kind: str, holdings: List[Holding]) -> None:
    """Replace the entire holdings set for a portfolio (used by CSV upload)."""
    with _connect() as conn:
        pid = get_portfolio_id(conn, kind)
        conn.execute("DELETE FROM holdings WHERE portfolio_id = ?", (pid,))
        conn.executemany(
            "INSERT INTO holdings (portfolio_id, ticker, qty, avg_buy_price, sector) "
            "VALUES (?, ?, ?, ?, ?)",
            [(pid, h.ticker, h.qty, h.avg_buy_price, h.sector) for h in holdings],
        )
        conn.execute(
            "UPDATE portfolios SET updated_at = ? WHERE id = ?", (_now(), pid)
        )


def add_holding(kind: str, holding: Holding) -> None:
    with _connect() as conn:
        pid = get_portfolio_id(conn, kind)
        conn.execute(
            "INSERT INTO holdings (portfolio_id, ticker, qty, avg_buy_price, sector) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, holding.ticker, holding.qty, holding.avg_buy_price, holding.sector),
        )
        conn.execute(
            "UPDATE portfolios SET updated_at = ? WHERE id = ?", (_now(), pid)
        )


def delete_holding(kind: str, holding_id: int) -> None:
    with _connect() as conn:
        pid = get_portfolio_id(conn, kind)
        conn.execute(
            "DELETE FROM holdings WHERE id = ? AND portfolio_id = ?",
            (holding_id, pid),
        )
        conn.execute(
            "UPDATE portfolios SET updated_at = ? WHERE id = ?", (_now(), pid)
        )


def get_holdings_with_ids(kind: str) -> List[Dict]:
    """Holdings including their row id, for the editable UI table."""
    with _connect() as conn:
        pid = get_portfolio_id(conn, kind)
        rows = conn.execute(
            "SELECT id, ticker, qty, avg_buy_price, sector FROM holdings "
            "WHERE portfolio_id = ? ORDER BY ticker",
            (pid,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Runs / recommendations ----------

def create_run(trigger: str, title: Optional[str] = None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (created_at, trigger, title, status) "
            "VALUES (?, ?, ?, 'running')",
            (_now(), trigger, title),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, market_weather: str, status: str = "done",
               error: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, market_weather = ?, error = ? WHERE id = ?",
            (status, market_weather, error, run_id),
        )


def add_recommendation(run_id: int, rec: Dict) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO recommendations
              (run_id, ticker, qty, avg_buy_price, current_price, unrealized_pnl_pct,
               action, confidence, stance, rationale, key_risks, alternatives, triggers,
               distribution, evidence_packet)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                rec["ticker"],
                rec.get("qty"),
                rec.get("avg_buy_price"),
                rec.get("current_price"),
                rec.get("unrealized_pnl_pct"),
                rec.get("action"),
                rec.get("confidence"),
                rec.get("stance"),
                json.dumps(rec.get("rationale")),
                json.dumps(rec.get("key_risks")),
                json.dumps(rec.get("alternatives") or []),
                json.dumps(rec.get("triggers") or []),
                json.dumps(rec.get("distribution") or []),
                json.dumps(rec.get("evidence_packet", {})),
            ),
        )


def list_runs() -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.created_at, r.trigger, r.title, r.status, r.error,
                   COUNT(rec.id) AS n_recs
            FROM runs r
            LEFT JOIN recommendations rec ON rec.run_id = r.id
            GROUP BY r.id
            ORDER BY r.created_at DESC, r.id DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(run_id: int) -> Optional[Dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def _as_list(value):
    """Parse a JSON-list TEXT column; tolerate legacy plain-text rows."""
    if value is None or value == "":
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return [value]  # legacy single-string rationale/risks
    if isinstance(parsed, list):
        return parsed
    return [parsed] if parsed else []


_PASSIVE_ACTIONS = {"HOLD", "WATCH"}


def _attention_score(rec) -> float:
    """How much a position warrants attention, for cross-portfolio ranking.

    Active calls (BUY/CUT/SELL/AVOID) always rank on top. Among passive calls
    (HOLD/WATCH), those with the most pull toward an action — i.e. the highest
    weight on any active action in the distribution — rank above settled ones.
    This surfaces borderline holds without changing any call.
    """
    primary = rec.get("action")
    conf = rec.get("confidence") or 0
    if primary not in _PASSIVE_ACTIONS:
        return 1000 + conf
    active = [d.get("confidence") or 0 for d in rec.get("distribution", [])
              if d.get("action") not in _PASSIVE_ACTIONS]
    return max(active) if active else 0


def _hydrate_rec(row) -> Dict:
    d = dict(row)
    try:
        d["evidence_packet"] = json.loads(d.get("evidence_packet") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["evidence_packet"] = {}
    d["rationale"] = _as_list(d.get("rationale"))
    d["key_risks"] = _as_list(d.get("key_risks"))
    d["alternatives"] = _as_list(d.get("alternatives"))
    d["triggers"] = _as_list(d.get("triggers"))
    d["stance"] = d.get("stance") or ""
    # Prefer the stored full distribution (with per-action reasons). Fall back to
    # reconstructing from primary + alternatives for rows written before that column.
    stored_dist = _as_list(d.get("distribution"))
    if stored_dist:
        d["distribution"] = stored_dist
    else:
        d["distribution"] = ([{"action": d.get("action"), "confidence": d.get("confidence")}]
                             + [a for a in d["alternatives"] if isinstance(a, dict)])
    # When the primary is a passive HOLD/WATCH, surface the strongest pull toward an
    # action so every hold shows which way it leans (if at all). The magnitude — not
    # presence/absence — signals how settled the hold is; only a hold with no real
    # tilt (top active weight < 10%) stays clean.
    d["lean"] = None
    if d.get("action") in ("HOLD", "WATCH"):
        active = [a for a in d["alternatives"]
                  if isinstance(a, dict) and a.get("action") not in ("HOLD", "WATCH")]
        active.sort(key=lambda a: -(a.get("confidence") or 0))
        if active and (active[0].get("confidence") or 0) >= 10:
            d["lean"] = active[0]
    return d


def get_recommendations(run_id: int) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM recommendations WHERE run_id = ? ORDER BY confidence DESC",
            (run_id,),
        ).fetchall()
    recs = [_hydrate_rec(r) for r in rows]
    # Rank by attention: action-worthy / borderline positions surface to the top.
    recs.sort(key=_attention_score, reverse=True)
    return recs


def get_recommendation(rec_id: int) -> Optional[Dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM recommendations WHERE id = ?", (rec_id,)).fetchone()
    return _hydrate_rec(row) if row else None


# ---------- Chat ----------

def add_chat_message(rec_id: int, role: str, content: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages (rec_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (rec_id, role, content, _now()),
        )


def get_chat_messages(rec_id: int) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM chat_messages "
            "WHERE rec_id = ? ORDER BY id",
            (rec_id,),
        ).fetchall()
    return [dict(r) for r in rows]
