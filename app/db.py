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
                qty           REAL NOT NULL,
                avg_buy_price REAL NOT NULL,
                sector        TEXT
            );

            CREATE TABLE IF NOT EXISTS runs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at     TEXT NOT NULL,
                trigger        TEXT NOT NULL,       -- 'scheduled' | 'manual'
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
            """
        )
        # Lightweight migration: add columns introduced after first release.
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(recommendations)").fetchall()}
        if "alternatives" not in cols:
            conn.execute("ALTER TABLE recommendations ADD COLUMN alternatives TEXT")

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

def create_run(trigger: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (created_at, trigger, status) VALUES (?, ?, 'running')",
            (_now(), trigger),
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
               action, confidence, rationale, key_risks, alternatives, evidence_packet)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(rec.get("rationale")),
                json.dumps(rec.get("key_risks")),
                json.dumps(rec.get("alternatives") or []),
                json.dumps(rec.get("evidence_packet", {})),
            ),
        )


def list_runs() -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.created_at, r.trigger, r.status, r.error,
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


# Action ordering for display (most urgent first).
_ACTION_ORDER = {"SELL": 0, "CUT": 1, "BUY": 2, "HOLD": 3}


def get_recommendations(run_id: int) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM recommendations WHERE run_id = ? ORDER BY confidence DESC",
            (run_id,),
        ).fetchall()
    recs = []
    for r in rows:
        d = dict(r)
        try:
            d["evidence_packet"] = json.loads(d.get("evidence_packet") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["evidence_packet"] = {}
        d["rationale"] = _as_list(d.get("rationale"))
        d["key_risks"] = _as_list(d.get("key_risks"))
        d["alternatives"] = _as_list(d.get("alternatives"))
        recs.append(d)
    recs.sort(key=lambda x: (_ACTION_ORDER.get(x.get("action"), 9),
                             -(x.get("confidence") or 0)))
    return recs
