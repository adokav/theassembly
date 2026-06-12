"""Market-data providers (adapter pattern).

Each provider talks to one external API and returns normalized dataclasses so
the rest of the package never sees raw JSON. Adding a new exchange = adding a
new adapter that returns an OHLCV. All calls are keyless and use a small
retry-with-backoff helper for resilience.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from .config import Config

log = logging.getLogger("crypto_signals.providers")


@dataclass
class OHLCV:
    """Normalized candle series for one symbol."""
    symbol: str
    closes: list[float]
    highs: list[float]
    lows: list[float]
    volumes: list[float]

    @property
    def last_close(self) -> float | None:
        return self.closes[-1] if self.closes else None


@dataclass
class Ticker24h:
    symbol: str
    last_price: float
    price_change_pct: float  # 24h % change
    quote_volume: float      # 24h volume in quote asset


class ProviderError(RuntimeError):
    pass


def _request_json(cfg: Config, url: str, params: dict | None = None):
    """GET JSON with exponential backoff. Raises ProviderError on final failure."""
    backoff = 2
    last_exc: Exception | None = None
    for attempt in range(cfg.http_retries):
        try:
            resp = requests.get(
                url,
                params=params,
                headers={"User-Agent": cfg.user_agent},
                timeout=cfg.http_timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001 - network/JSON, retry then surface
            last_exc = e
            log.warning("İstek başarısız (deneme %d/%d): %s", attempt + 1, cfg.http_retries, str(e)[:160])
            if attempt < cfg.http_retries - 1:
                time.sleep(backoff)
                backoff = min(backoff * 2, 16)
    raise ProviderError(f"Veri çekilemedi: {url} — {last_exc}")


class BinanceProvider:
    """Keyless Binance public REST adapter (OHLCV + 24h ticker)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _pair(self, symbol: str) -> str:
        """BTC -> BTCUSDT. Already-paired symbols pass through."""
        symbol = symbol.upper()
        if symbol.endswith(self.cfg.quote_asset):
            return symbol
        return f"{symbol}{self.cfg.quote_asset}"

    def fetch_ohlcv(self, symbol: str, interval: str = "1d", limit: int = 250) -> OHLCV:
        data = _request_json(
            self.cfg,
            f"{self.cfg.binance_base}/api/v3/klines",
            params={"symbol": self._pair(symbol), "interval": interval, "limit": limit},
        )
        if not isinstance(data, list) or not data:
            raise ProviderError(f"{symbol}: boş mum verisi (sembol geçersiz olabilir).")
        # Kline columns: [openTime, open, high, low, close, volume, ...]
        highs = [float(c[2]) for c in data]
        lows = [float(c[3]) for c in data]
        closes = [float(c[4]) for c in data]
        volumes = [float(c[5]) for c in data]
        return OHLCV(symbol=symbol.upper(), closes=closes, highs=highs, lows=lows, volumes=volumes)

    def fetch_ticker24h(self, symbol: str) -> Ticker24h:
        data = _request_json(
            self.cfg,
            f"{self.cfg.binance_base}/api/v3/ticker/24hr",
            params={"symbol": self._pair(symbol)},
        )
        return Ticker24h(
            symbol=symbol.upper(),
            last_price=float(data["lastPrice"]),
            price_change_pct=float(data["priceChangePercent"]),
            quote_volume=float(data["quoteVolume"]),
        )


class FearGreedProvider:
    """alternative.me Crypto Fear & Greed Index (keyless, market-wide)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def fetch(self) -> tuple[int, str] | None:
        """Return (value 0-100, classification) or None if unavailable."""
        try:
            data = _request_json(self.cfg, self.cfg.fear_greed_url)
            item = (data.get("data") or [None])[0]
            if not item:
                return None
            return int(item["value"]), str(item.get("value_classification", ""))
        except Exception as e:  # noqa: BLE001 - sentiment is optional, degrade gracefully
            log.warning("Fear & Greed alınamadı, atlanıyor: %s", str(e)[:160])
            return None
