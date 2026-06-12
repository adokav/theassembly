"""Background scanner: periodic signal scan + transition-based alerts.

Runs in its own daemon thread. On each tick it evaluates every symbol that any
subscriber watches (deduplicated, so each symbol hits the API once), stores a
snapshot, and notifies a subscriber only when that symbol's rating *changes*
into a meaningful state — so users get a ping on the transition, not every tick.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .config import Config
from .formatting import render_alert
from .signals import SignalEngine, SignalReport
from .storage import Repository

log = logging.getLogger("crypto_signals.scheduler")

# Notifier: (chat_id, markdown_text) -> None
Notifier = Callable[[int, str], None]


class SignalScheduler:
    def __init__(self, cfg: Config, repo: Repository, engine: SignalEngine, notify: Notifier):
        self.cfg = cfg
        self.repo = repo
        self.engine = engine
        self.notify = notify
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="signal-scheduler", daemon=True)
        self._thread.start()
        log.info("🕒 Scheduler başladı (her %d dk).", self.cfg.scan_interval_min)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # Small initial delay so the bot finishes booting first.
        self._stop.wait(10)
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as e:  # noqa: BLE001 - keep the thread alive
                log.warning("Tarama hatası: %s", str(e)[:200])
            self._stop.wait(self.cfg.scan_interval_min * 60)

    def _scan_once(self) -> None:
        subscribers = self.repo.list_subscribers()
        if not subscribers:
            return
        # Map symbol -> set of interested chat_ids (dedupe API calls).
        interest: dict[str, list[int]] = {}
        for chat_id in subscribers:
            symbols = self.repo.get_watchlist(chat_id) or self.cfg.default_symbols
            for sym in symbols:
                interest.setdefault(sym, []).append(chat_id)

        fear_greed = self.engine.sentiment.fetch()  # one market-wide fetch per tick
        for symbol, chat_ids in interest.items():
            try:
                rep = self.engine.evaluate(symbol, fear_greed=fear_greed)
            except Exception as e:  # noqa: BLE001 - one bad symbol shouldn't stop the scan
                log.warning("%s değerlendirilemedi: %s", symbol, str(e)[:160])
                continue
            self.repo.save_snapshot(symbol, rep.composite, rep.rating, _payload(rep))
            self._maybe_alert(symbol, rep, chat_ids)

    def _maybe_alert(self, symbol: str, rep: SignalReport, chat_ids: list[int]) -> None:
        for chat_id in chat_ids:
            previous = self.repo.get_last_rating(chat_id, symbol)
            self.repo.set_last_rating(chat_id, symbol, rep.rating)
            if previous == rep.rating:
                continue  # no transition -> stay quiet
            # Alert when entering a strong signal, or leaving one (state change).
            meaningful = rep.rating == "GÜÇLÜ" or previous == "GÜÇLÜ" or rep.rating == "ZAYIF"
            if previous is not None and meaningful:
                try:
                    self.notify(chat_id, render_alert(rep, previous))
                except Exception as e:  # noqa: BLE001
                    log.warning("Alarm gönderilemedi (chat %s): %s", chat_id, str(e)[:160])


def _payload(rep: SignalReport) -> dict:
    return {
        "price": rep.price,
        "bullish_pct": rep.bullish_pct,
        "signals": [{"name": s.name, "score": s.score, "verdict": s.verdict} for s in rep.signals],
    }
