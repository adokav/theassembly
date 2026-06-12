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
from .formatting import render_broken, render_new_signal
from .providers import is_scannable_base
from .signals import (
    SignalEngine,
    SignalReport,
    broken_reasons,
    is_bullish_signal,
    is_formation_broken,
)
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

        # Subscribers without a custom watchlist follow the dynamic universe.
        watchlists = {c: self.repo.get_watchlist(c) for c in subscribers}
        need_dynamic = any(not wl for wl in watchlists.values())

        dynamic: list[str] = []
        ticker_map: dict[str, "object"] = {}
        if need_dynamic and self.cfg.dynamic_top_n > 0:
            try:
                all_tickers = self.engine.market.fetch_all_tickers()  # one bulk call
                ticker_map = {t.symbol: t for t in all_tickers}
                extra = set(self.cfg.extra_exclude_bases)
                scannable = [t for t in all_tickers if is_scannable_base(t.symbol, extra)]
                scannable.sort(key=lambda t: t.quote_volume, reverse=True)
                dynamic = [t.symbol for t in scannable[: self.cfg.dynamic_top_n]]
                log.info("Dinamik evren: ilk %d coin (hacme göre).", len(dynamic))
            except Exception as e:  # noqa: BLE001 - fall back to a small static set
                log.warning("Dinamik evren alınamadı, varsayılana düşülüyor: %s", str(e)[:160])
                dynamic = list(self.cfg.default_symbols)

        # Map symbol -> interested chat_ids (dedupe API calls across users).
        interest: dict[str, list[int]] = {}
        for chat_id, wl in watchlists.items():
            symbols = wl or dynamic or self.cfg.default_symbols
            for sym in symbols:
                interest.setdefault(sym, []).append(chat_id)

        fear_greed = self.engine.sentiment.fetch()  # one market-wide fetch per tick
        for symbol, chat_ids in interest.items():
            if self._stop.is_set():
                return
            try:
                rep = self.engine.evaluate(
                    symbol, fear_greed=fear_greed, ticker=ticker_map.get(symbol)
                )
            except Exception as e:  # noqa: BLE001 - one bad symbol shouldn't stop the scan
                log.warning("%s değerlendirilemedi: %s", symbol, str(e)[:160])
                continue
            self.repo.save_snapshot(symbol, rep.composite, rep.rating, _payload(rep))
            self._process_signal(symbol, rep, chat_ids)
            # Gentle throttle so a 150-coin scan stays well under rate limits.
            self._stop.wait(0.15)

    def _process_signal(self, symbol: str, rep: SignalReport, chat_ids: list[int]) -> None:
        """Signal lifecycle per subscriber:
        - not active + bullish  -> auto-report 'YENİ SİNYAL', open the signal
        - active + broken        -> auto-report 'FORMASYON BOZULDU', close it
        """
        bullish = is_bullish_signal(rep, self.cfg.alert_score_threshold)
        broken = is_formation_broken(rep, self.cfg.signal_exit_threshold)
        for chat_id in chat_ids:
            active = self.repo.is_active_signal(chat_id, symbol)
            if not active and bullish:
                self.repo.open_signal(chat_id, symbol, rep.price, rep.composite)
                self._safe_notify(chat_id, render_new_signal(rep))
            elif active and broken:
                entry = self.repo.get_active_signal(chat_id, symbol) or {}
                self.repo.close_signal(chat_id, symbol)
                self._safe_notify(chat_id, render_broken(rep, entry, broken_reasons(rep)))

    def _safe_notify(self, chat_id: int, text: str) -> None:
        try:
            self.notify(chat_id, text)
        except Exception as e:  # noqa: BLE001
            log.warning("Otomatik rapor gönderilemedi (chat %s): %s", chat_id, str(e)[:160])


def _payload(rep: SignalReport) -> dict:
    return {
        "price": rep.price,
        "bullish_pct": rep.bullish_pct,
        "signals": [{"name": s.name, "score": s.score, "verdict": s.verdict} for s in rep.signals],
    }
