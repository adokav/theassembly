"""Configuration & environment validation (fail-fast).

All tunables live here so the rest of the package stays free of os.getenv calls.
Only TELEGRAM_TOKEN is strictly required; market data uses keyless public APIs,
so the bot runs out of the box.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or invalid."""


def _get_list(name: str, default: str = "") -> list[str]:
    raw = (os.getenv(name) or default).strip()
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    # --- required ---
    telegram_token: str = field(default_factory=lambda: (os.getenv("TELEGRAM_TOKEN") or "").strip())

    # --- market data (keyless defaults) ---
    binance_base: str = field(default_factory=lambda: (os.getenv("BINANCE_BASE") or "https://api.binance.com").strip())
    quote_asset: str = field(default_factory=lambda: (os.getenv("QUOTE_ASSET") or "USDT").strip().upper())
    fear_greed_url: str = "https://api.alternative.me/fng/?limit=1"

    # --- default watchlist (used when a chat has none) ---
    default_symbols: list[str] = field(default_factory=lambda: _get_list("DEFAULT_SYMBOLS", "BTC,ETH,SOL"))

    # --- dynamic universe (top-N coins by 24h quote volume) ---
    # When > 0, subscribers without a custom watchlist are scanned against the
    # top-N most-traded coins (refreshed every scan). 0 disables dynamic mode.
    dynamic_top_n: int = field(default_factory=lambda: int(os.getenv("DYNAMIC_TOP_N") or "150"))
    extra_exclude_bases: list[str] = field(default_factory=lambda: _get_list("EXCLUDE_BASES", ""))

    # --- scheduler ---
    scan_interval_min: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_MIN") or "30"))
    alert_score_threshold: float = field(default_factory=lambda: float(os.getenv("ALERT_SCORE_THRESHOLD") or "0.35"))

    # --- HTTP ---
    http_timeout: int = 20
    http_retries: int = 4
    user_agent: str = (
        "Mozilla/5.0 (compatible; CryptoSignalsBot/1.0; +https://github.com/adokav/theassembly)"
    )

    # --- telegram delivery ---
    telegram_limit: int = 3900

    # --- storage ---
    db_path: str = field(default_factory=lambda: (os.getenv("DB_PATH") or "crypto_signals.db").strip())

    def validate(self) -> None:
        if not self.telegram_token:
            raise ConfigError(
                "TELEGRAM_TOKEN tanımlı değil. BotFather'dan bir token alıp .env'e ekleyin."
            )
        if self.scan_interval_min < 1:
            raise ConfigError("SCAN_INTERVAL_MIN en az 1 olmalı.")
        if self.dynamic_top_n < 0:
            raise ConfigError("DYNAMIC_TOP_N negatif olamaz (0 = devre dışı).")
        if not (0.0 < self.alert_score_threshold <= 1.0):
            raise ConfigError("ALERT_SCORE_THRESHOLD 0 ile 1 arasında olmalı.")


def load_config() -> Config:
    cfg = Config()
    cfg.validate()
    return cfg
