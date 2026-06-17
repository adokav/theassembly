"""Pure technical-indicator functions (no third-party deps).

Every function takes plain lists of floats and returns a float or None when
there is not enough data. Keeping these pure makes them trivial to unit-test
and reuse outside the bot.
"""
from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last `period` values."""
    if len(values) < period or period <= 0:
        return None
    window = values[-period:]
    return sum(window) / len(window)


def ema(values: list[float], period: int) -> float | None:
    """Exponential moving average. Seeds with the SMA of the first window."""
    if len(values) < period or period <= 0:
        return None
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    e = seed
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values: list[float], period: int) -> list[float]:
    """Full EMA series (aligned to values[period-1:]). Empty if too short."""
    if len(values) < period or period <= 0:
        return []
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder-style RSI from a close series. None if not enough data."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Return (macd_line, signal_line, histogram) or (None, None, None).

    Computes EMA series, aligns them, then EMAs the MACD line for the signal.
    """
    if len(closes) < slow + signal:
        return None, None, None
    fast_series = ema_series(closes, fast)
    slow_series = ema_series(closes, slow)
    # Align tails (fast series is longer because period is smaller).
    n = min(len(fast_series), len(slow_series))
    macd_line = [fast_series[-n + i] - slow_series[-n + i] for i in range(n)]
    signal_series = ema_series(macd_line, signal)
    if not signal_series:
        return None, None, None
    macd_val = macd_line[-1]
    signal_val = signal_series[-1]
    return macd_val, signal_val, macd_val - signal_val


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """Average True Range (volatility) — used for data-driven stop suggestions."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period
