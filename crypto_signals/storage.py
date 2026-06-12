"""SQLite repository (thin persistence layer).

State that must survive restarts lives here: subscribers, per-chat watchlists,
signal snapshots (history) and per-chat/symbol alert state (so we only alert on
rating *transitions*, never spam). The Repository interface is deliberately
small — swapping SQLite for Postgres later means re-implementing this one class.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id   INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlists (
    chat_id INTEGER NOT NULL,
    symbol  TEXT    NOT NULL,
    PRIMARY KEY (chat_id, symbol)
);
CREATE TABLE IF NOT EXISTS snapshots (
    symbol     TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    composite  REAL    NOT NULL,
    rating     TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS active_signals (
    chat_id         INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    entry_ts        INTEGER NOT NULL,
    entry_price     REAL    NOT NULL,
    entry_composite REAL    NOT NULL,
    PRIMARY KEY (chat_id, symbol)
);
"""


class Repository:
    def __init__(self, db_path: str):
        # check_same_thread=False + a lock: the scheduler thread and the bot
        # thread share one connection safely.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # --- subscribers -------------------------------------------------------
    def add_subscriber(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO subscribers(chat_id, created_at) VALUES (?, ?)",
                (chat_id, int(time.time())),
            )
            self._conn.commit()

    def remove_subscriber(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    def list_subscribers(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute("SELECT chat_id FROM subscribers").fetchall()
        return [r["chat_id"] for r in rows]

    def is_subscribed(self, chat_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM subscribers WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row is not None

    # --- watchlists --------------------------------------------------------
    def add_symbol(self, chat_id: int, symbol: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO watchlists(chat_id, symbol) VALUES (?, ?)",
                (chat_id, symbol.upper()),
            )
            self._conn.commit()

    def remove_symbol(self, chat_id: int, symbol: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM watchlists WHERE chat_id = ? AND symbol = ?",
                (chat_id, symbol.upper()),
            )
            self._conn.commit()

    def get_watchlist(self, chat_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol FROM watchlists WHERE chat_id = ? ORDER BY symbol",
                (chat_id,),
            ).fetchall()
        return [r["symbol"] for r in rows]

    # --- snapshots (history) ----------------------------------------------
    def save_snapshot(self, symbol: str, composite: float, rating: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO snapshots(symbol, ts, composite, rating, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (symbol.upper(), int(time.time()), composite, rating, json.dumps(payload)),
            )
            self._conn.commit()

    def latest_snapshots(self, limit: int = 15, min_composite: float = 0.30) -> list[dict]:
        """Most recent snapshot per symbol with composite >= threshold,
        strongest first. Powers the /radar command from the last scan."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.symbol, s.composite, s.rating, s.ts
                FROM snapshots s
                JOIN (SELECT symbol, MAX(ts) AS ts FROM snapshots GROUP BY symbol) m
                  ON s.symbol = m.symbol AND s.ts = m.ts
                WHERE s.composite >= ?
                ORDER BY s.composite DESC
                LIMIT ?
                """,
                (min_composite, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- active signals (lifecycle: open on entry, close on breakdown) -----
    def is_active_signal(self, chat_id: int, symbol: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM active_signals WHERE chat_id = ? AND symbol = ?",
                (chat_id, symbol.upper()),
            ).fetchone()
        return row is not None

    def get_active_signal(self, chat_id: int, symbol: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT chat_id, symbol, entry_ts, entry_price, entry_composite "
                "FROM active_signals WHERE chat_id = ? AND symbol = ?",
                (chat_id, symbol.upper()),
            ).fetchone()
        return dict(row) if row else None

    def open_signal(self, chat_id: int, symbol: str, price: float, composite: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO active_signals"
                "(chat_id, symbol, entry_ts, entry_price, entry_composite) VALUES (?, ?, ?, ?, ?)",
                (chat_id, symbol.upper(), int(time.time()), price, composite),
            )
            self._conn.commit()

    def close_signal(self, chat_id: int, symbol: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM active_signals WHERE chat_id = ? AND symbol = ?",
                (chat_id, symbol.upper()),
            )
            self._conn.commit()

    def list_active_signals(self, chat_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, entry_ts, entry_price, entry_composite "
                "FROM active_signals WHERE chat_id = ? ORDER BY entry_ts DESC",
                (chat_id,),
            ).fetchall()
        return [dict(r) for r in rows]
