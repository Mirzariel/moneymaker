"""SQLite persistence. Everything the bot needs to survive a restart lives here."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import BotStatus, Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    take_profit REAL NOT NULL,
    expires_at REAL NOT NULL,
    reason TEXT NOT NULL,
    decision TEXT NOT NULL,          -- approved | rejected
    decision_detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,            -- open | closed
    amount REAL NOT NULL,
    entry_price REAL NOT NULL,
    cost REAL NOT NULL,              -- quote spent including quote-denominated entry fee
    stop_price REAL NOT NULL,
    take_profit REAL NOT NULL,
    stop_order_id TEXT,
    opened_at REAL NOT NULL,
    closed_at REAL,
    exit_price REAL,
    proceeds REAL,
    pnl REAL,
    fees REAL NOT NULL DEFAULT 0,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL UNIQUE,
    exchange_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    type TEXT NOT NULL,
    purpose TEXT NOT NULL,           -- entry | stop | exit | panic
    position_id INTEGER,
    amount REAL,
    cost REAL,
    price REAL,
    stop_price REAL,
    status TEXT NOT NULL,            -- pending | open | closed | canceled | rejected | expired | failed | unknown
    filled REAL NOT NULL DEFAULT 0,
    average REAL NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    fee_currency TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT,
    rule TEXT NOT NULL,
    detail TEXT NOT NULL,
    action TEXT NOT NULL             -- reject | resize | pause
);
CREATE TABLE IF NOT EXISTS equity (
    ts REAL PRIMARY KEY,
    equity REAL NOT NULL,
    quote_free REAL NOT NULL,
    exposure REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- key/value ---------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    @property
    def status(self) -> BotStatus:
        # A fresh install starts PAUSED: trading only begins after an explicit resume.
        return BotStatus(self.get("bot_status", BotStatus.PAUSED.value))

    def set_status(self, status: BotStatus, reason: str = "") -> None:
        self.set("bot_status", status.value)
        self.set("bot_status_reason", reason)
        self.set("bot_status_changed_at", time.time())

    # ---- audit / risk --------------------------------------------------------------
    def audit(self, actor: str, action: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit(ts, actor, action, detail) VALUES(?,?,?,?)", (time.time(), actor, action, detail)
        )

    def risk_event(self, rule: str, detail: str, action: str, symbol: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO risk_events(ts, symbol, rule, detail, action) VALUES(?,?,?,?,?)",
            (time.time(), symbol, rule, detail, action),
        )

    def record_signal(self, sig, decision: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO signals(ts, symbol, side, entry, stop, take_profit, expires_at, reason, decision, decision_detail)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sig.created_at, sig.symbol, sig.side, sig.entry, sig.stop, sig.take_profit, sig.expires_at,
             sig.reason, decision, detail),
        )

    # ---- orders ------------------------------------------------------------------
    def insert_order(self, *, client_id: str, symbol: str, side: str, type: str, purpose: str,
                     position_id: int | None = None, amount: float | None = None, cost: float | None = None,
                     price: float | None = None, stop_price: float | None = None) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO orders(client_id, symbol, side, type, purpose, position_id, amount, cost, price, stop_price,"
            " status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)",
            (client_id, symbol, side, type, purpose, position_id, amount, cost, price, stop_price, now, now),
        )
        return int(cur.lastrowid)

    def update_order(self, client_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE orders SET {cols} WHERE client_id=?", (*fields.values(), client_id))

    def update_order_by_exchange_id(self, exchange_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE orders SET {cols} WHERE exchange_id=?", (*fields.values(), exchange_id))

    def orders_with_status(self, *statuses: str) -> list[sqlite3.Row]:
        q = ",".join("?" for _ in statuses)
        return self.conn.execute(f"SELECT * FROM orders WHERE status IN ({q}) ORDER BY id", statuses).fetchall()

    # ---- positions ---------------------------------------------------------------
    def open_position(self, *, symbol: str, amount: float, entry_price: float, cost: float, stop_price: float,
                      take_profit: float, fees: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO positions(symbol, status, amount, entry_price, cost, stop_price, take_profit, opened_at, fees)"
            " VALUES(?, 'open', ?, ?, ?, ?, ?, ?, ?)",
            (symbol, amount, entry_price, cost, stop_price, take_profit, time.time(), fees),
        )
        return int(cur.lastrowid)

    def set_position_stop(self, position_id: int, stop_order_id: str | None, stop_price: float | None = None) -> None:
        if stop_price is None:
            self.conn.execute("UPDATE positions SET stop_order_id=? WHERE id=?", (stop_order_id, position_id))
        else:
            self.conn.execute("UPDATE positions SET stop_order_id=?, stop_price=? WHERE id=?",
                              (stop_order_id, stop_price, position_id))

    def close_position(self, position_id: int, *, exit_price: float, proceeds: float, extra_fees: float,
                       reason: str) -> float:
        row = self.conn.execute("SELECT cost, fees FROM positions WHERE id=?", (position_id,)).fetchone()
        pnl = proceeds - row["cost"]
        self.conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, exit_price=?, proceeds=?, pnl=?, fees=?, exit_reason=?,"
            " stop_order_id=NULL WHERE id=?",
            (time.time(), exit_price, proceeds, pnl, row["fees"] + extra_fees, reason, position_id),
        )
        return pnl

    def open_positions(self) -> list[Position]:
        rows = self.conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY id").fetchall()
        return [
            Position(id=r["id"], symbol=r["symbol"], amount=r["amount"], entry_price=r["entry_price"], cost=r["cost"],
                     stop_price=r["stop_price"], take_profit=r["take_profit"], stop_order_id=r["stop_order_id"],
                     opened_at=r["opened_at"], status=r["status"], fees=r["fees"])
            for r in rows
        ]

    def realized_pnl_since(self, ts: float) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS s FROM positions WHERE status='closed' AND closed_at>=?", (ts,)
        ).fetchone()
        return float(row["s"])

    def consecutive_losses(self, since: float = 0.0) -> int:
        rows = self.conn.execute(
            "SELECT pnl FROM positions WHERE status='closed' AND closed_at>=? ORDER BY closed_at DESC LIMIT 50",
            (since,),
        ).fetchall()
        n = 0
        for r in rows:
            if r["pnl"] is not None and r["pnl"] < 0:
                n += 1
            else:
                break
        return n

    def last_entry_time(self, symbol: str) -> float | None:
        row = self.conn.execute("SELECT MAX(opened_at) AS t FROM positions WHERE symbol=?", (symbol,)).fetchone()
        return row["t"] if row and row["t"] is not None else None

    # ---- equity --------------------------------------------------------------------
    def record_equity(self, equity: float, quote_free: float, exposure: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity(ts, equity, quote_free, exposure) VALUES(?,?,?,?)",
            (time.time(), equity, quote_free, exposure),
        )

    # ---- generic reads for API -----------------------------------------------------
    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
