"""Unit tests for the pure indicator + scoring logic (no network)."""
from __future__ import annotations

import math

from crypto_signals import indicators as ind
from crypto_signals.signals import (
    Signal,
    SignalEngine,
    _eval_ma_trend,
    _eval_rsi,
    _rate,
)


def test_sma_basic():
    assert ind.sma([1, 2, 3, 4], 2) == 3.5
    assert ind.sma([1, 2], 5) is None


def test_ema_matches_known_value():
    # EMA of a constant series equals the constant.
    assert math.isclose(ind.ema([5, 5, 5, 5, 5], 3), 5.0)


def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 30)]  # strictly increasing
    assert ind.rsi(closes, 14) == 100.0


def test_rsi_none_when_short():
    assert ind.rsi([1, 2, 3], 14) is None


def test_macd_returns_triplet():
    closes = [float(i % 7) + i * 0.1 for i in range(60)]
    macd_val, signal_val, hist = ind.macd(closes)
    assert macd_val is not None and signal_val is not None
    assert math.isclose(hist, macd_val - signal_val, rel_tol=1e-9)


def test_atr_positive():
    highs = [10 + i for i in range(20)]
    lows = [8 + i for i in range(20)]
    closes = [9 + i for i in range(20)]
    assert ind.atr(highs, lows, closes, 14) > 0


def test_ma_trend_bullish_when_price_above():
    closes = [float(i) for i in range(1, 260)]  # uptrend, price above both SMAs
    sig = _eval_ma_trend(closes)
    assert sig.score > 0


def test_rsi_oversold_is_bullish_tilt():
    # Build a falling series so RSI is low.
    closes = [float(100 - i) for i in range(40)]
    sig = _eval_rsi(closes)
    assert isinstance(sig, Signal)
    assert sig.score > 0  # oversold -> bullish bounce potential


def test_rate_thresholds():
    assert _rate(0.5) == ("GÜÇLÜ", "🟢")
    assert _rate(0.0) == ("NÖTR", "🟡")
    assert _rate(-0.5) == ("ZAYIF", "🔴")


def test_composite_weighted_average():
    signals = [
        Signal("a", 1.0, 2.0, ""),
        Signal("b", -1.0, 1.0, ""),
    ]
    # (1*2 + -1*1) / 3 = 0.333...
    assert math.isclose(SignalEngine._composite(signals), 1 / 3, rel_tol=1e-9)
