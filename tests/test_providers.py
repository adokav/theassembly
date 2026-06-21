"""Tests for the dynamic top-N universe selection (no network)."""
from __future__ import annotations

from crypto_signals.config import Config
from crypto_signals.providers import BinanceProvider, Ticker24h, is_scannable_base


def _cfg() -> Config:
    return Config(telegram_token="123:abc")


def test_is_scannable_excludes_stables_and_fiat():
    assert is_scannable_base("BTC")
    assert is_scannable_base("SOL")
    assert not is_scannable_base("USDC")
    assert not is_scannable_base("FDUSD")
    assert not is_scannable_base("EUR")


def test_is_scannable_extra_exclude():
    assert not is_scannable_base("DOGE", extra_exclude={"DOGE"})


def test_top_symbols_by_volume_sorts_and_filters(monkeypatch):
    provider = BinanceProvider(_cfg())
    fake = [
        Ticker24h("BTC", 60000, 1.0, 5_000_000_000),
        Ticker24h("ETH", 3000, 2.0, 3_000_000_000),
        Ticker24h("USDC", 1.0, 0.0, 9_000_000_000),   # highest volume but stable -> excluded
        Ticker24h("SOL", 150, 4.0, 1_000_000_000),
        Ticker24h("DOGE", 0.1, 5.0, 500_000_000),
    ]
    monkeypatch.setattr(provider, "fetch_all_tickers", lambda: fake)

    top3 = provider.top_symbols_by_volume(3)
    assert top3 == ["BTC", "ETH", "SOL"]          # USDC filtered out despite top volume
    assert "USDC" not in provider.top_symbols_by_volume(10)


def test_top_symbols_respects_extra_exclude(monkeypatch):
    provider = BinanceProvider(_cfg())
    fake = [
        Ticker24h("BTC", 60000, 1.0, 5_000_000_000),
        Ticker24h("ETH", 3000, 2.0, 3_000_000_000),
    ]
    monkeypatch.setattr(provider, "fetch_all_tickers", lambda: fake)
    assert provider.top_symbols_by_volume(5, extra_exclude={"BTC"}) == ["ETH"]
