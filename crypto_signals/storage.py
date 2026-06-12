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
CREATE TABLE IF NOT EXISTS alert_state (
    chat_id     INTEGER NOT NULL,
    symbol      TEXT    NOT NULL,
    last_rating TEXT    NOT NULL,
    last_ts     INTEGER NOT NULL,
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

    # --- alert state -------------------------------------------------------
    def get_last_rating(self, chat_id: int, symbol: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_rating FROM alert_state WHERE chat_id = ? AND symbol = ?",
                (chat_id, symbol.upper()),
            ).fetchone()
        return row["last_rating"] if row else None

    def set_last_rating(self, chat_id: int, symbol: str, rating: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO alert_state(chat_id, symbol, last_rating, last_ts) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, symbol.upper(), rating, int(time.time())),
            )
            self._conn.commit()
