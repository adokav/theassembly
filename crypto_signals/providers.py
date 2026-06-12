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


# Stablecoins / fiat / wrapped quote-like bases that are tradable as <BASE>USDT
# but are not meaningful "will it go up?" candidates — excluded from the
# dynamic top-N universe. Extend at runtime via EXCLUDE_BASES.
STABLE_FIAT_BASES = {
    "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USD1", "AEUR",
    "EUR", "GBP", "TRY", "BRL", "ARS", "RON", "PLN", "ZAR", "JPY", "MXN",
    "COP", "CZK", "UAH", "NGN", "IDRT", "BIDR", "VAI", "PAXG", "WBTC",
}


def is_scannable_base(base: str, extra_exclude: set[str] | None = None) -> bool:
    """Whether a base asset should appear in the dynamic universe."""
    base = base.upper()
    if base in STABLE_FIAT_BASES:
        return False
    if extra_exclude and base in extra_exclude:
        return False
    return True


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

    def fetch_all_tickers(self) -> list[Ticker24h]:
        """One bulk call → 24h ticker for every <BASE>{quote_asset} pair.

        Returned objects use the *base* symbol (e.g. BTC, not BTCUSDT) so the
        rest of the package speaks one vocabulary. Reused to both pick the
        top-N universe and feed momentum without per-symbol ticker calls.
        """
        data = _request_json(self.cfg, f"{self.cfg.binance_base}/api/v3/ticker/24hr")
        if not isinstance(data, list):
            raise ProviderError("Beklenmeyen ticker yanıtı (liste değil).")
        quote = self.cfg.quote_asset
        out: list[Ticker24h] = []
        for item in data:
            sym = item.get("symbol", "")
            if not sym.endswith(quote) or len(sym) <= len(quote):
                continue
            try:
                out.append(Ticker24h(
                    symbol=sym[: -len(quote)],
                    last_price=float(item["lastPrice"]),
                    price_change_pct=float(item["priceChangePercent"]),
                    quote_volume=float(item["quoteVolume"]),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def top_symbols_by_volume(self, n: int, extra_exclude: set[str] | None = None) -> list[str]:
        """Top-N base symbols by 24h quote volume, stables/fiat excluded."""
        tickers = self.fetch_all_tickers()
        scannable = [t for t in tickers if is_scannable_base(t.symbol, extra_exclude)]
        scannable.sort(key=lambda t: t.quote_volume, reverse=True)
        return [t.symbol for t in scannable[:n]]


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
