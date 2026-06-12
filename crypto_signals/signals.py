"""Signal engine: turn raw data + indicators into weighted verdicts.

Each evaluator returns a Signal with a normalized score in [-1, +1] (bearish ->
bullish) and a weight. The engine combines them into a single confluence score,
so no single indicator dominates — multiple signals must agree to move the
needle. This is the heart of the "is it likely to go up?" question.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import indicators as ind
from .config import Config
from .providers import BinanceProvider, FearGreedProvider, OHLCV, Ticker24h


@dataclass
class Signal:
    name: str
    score: float          # [-1, +1]
    weight: float
    verdict: str          # short human-readable verdict
    detail: str = ""      # optional numeric detail


@dataclass
class SignalReport:
    symbol: str
    price: float
    signals: list[Signal]
    composite: float       # [-1, +1]
    bullish_pct: float     # 0..100
    rating: str            # "GÜÇLÜ" | "NÖTR" | "ZAYIF"
    emoji: str             # 🟢 🟡 🔴
    fear_greed: tuple[int, str] | None = None
    atr: float | None = None

    @property
    def stop_suggestion(self) -> float | None:
        """A data-driven (2*ATR) stop level below price, if ATR is available."""
        if self.atr is None:
            return None
        return self.price - 2 * self.atr


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# --- individual evaluators --------------------------------------------------

def _eval_ma_trend(closes: list[float]) -> Signal:
    price = closes[-1]
    sma50 = ind.sma(closes, 50)
    sma200 = ind.sma(closes, 200)
    if sma50 is None:
        return Signal("Trend (HO)", 0.0, 1.5, "yetersiz veri")
    score = 0.0
    parts = []
    score += 0.5 if price > sma50 else -0.5
    parts.append(f"fiyat {'>' if price > sma50 else '<'} SMA50")
    if sma200 is not None:
        score += 0.5 if price > sma200 else -0.5
        parts.append(f"{'>' if price > sma200 else '<'} SMA200")
    verdict = "yükselen trend" if score > 0 else ("düşen trend" if score < 0 else "yatay")
    return Signal("Trend (HO)", _clamp(score), 1.5, verdict, ", ".join(parts))


def _eval_cross(closes: list[float]) -> Signal:
    """Golden/Death cross on SMA50 vs SMA200, with a recent-cross bonus."""
    sma50 = ind.sma(closes, 50)
    sma200 = ind.sma(closes, 200)
    if sma50 is None or sma200 is None:
        return Signal("Golden/Death Cross", 0.0, 1.5, "yetersiz veri")
    base = 0.6 if sma50 > sma200 else -0.6
    # Recent-cross detection: compare with the values 5 candles ago.
    prev50 = ind.sma(closes[:-5], 50) if len(closes) > 55 else None
    prev200 = ind.sma(closes[:-5], 200) if len(closes) > 205 else None
    bonus = 0.0
    verdict = "SMA50 > SMA200 (boğa dizilimi)" if base > 0 else "SMA50 < SMA200 (ayı dizilimi)"
    if prev50 is not None and prev200 is not None:
        was_below = prev50 <= prev200
        if sma50 > sma200 and was_below:
            bonus = 0.4
            verdict = "🌟 GOLDEN CROSS (yeni)"
        elif sma50 < sma200 and not was_below:
            bonus = -0.4
            verdict = "💀 Death Cross (yeni)"
    return Signal("Golden/Death Cross", _clamp(base + bonus), 1.5, verdict)


def _eval_rsi(closes: list[float]) -> Signal:
    r = ind.rsi(closes, 14)
    if r is None:
        return Signal("RSI", 0.0, 1.0, "yetersiz veri")
    # Oversold -> bullish bounce potential; overbought -> caution.
    if r < 30:
        score, verdict = 0.7, "aşırı satım (tepki ihtimali)"
    elif r > 70:
        score, verdict = -0.6, "aşırı alım (dikkat)"
    else:
        # Linear tilt: 50 is neutral, drifts mildly with momentum.
        score = _clamp((50 - r) / 50 * 0.5 + (r - 50) / 50 * 0.3)
        verdict = "nötr bölge"
    return Signal("RSI", score, 1.0, verdict, f"RSI={r:.1f}")


def _eval_macd(closes: list[float]) -> Signal:
    macd_val, signal_val, hist = ind.macd(closes)
    if hist is None:
        return Signal("MACD", 0.0, 1.2, "yetersiz veri")
    if hist > 0 and macd_val > 0:
        score, verdict = 0.8, "boğa momentumu (hist+, MACD>0)"
    elif hist > 0:
        score, verdict = 0.4, "toparlanma (hist+)"
    elif hist < 0 and macd_val < 0:
        score, verdict = -0.8, "ayı momentumu (hist-, MACD<0)"
    else:
        score, verdict = -0.4, "zayıflama (hist-)"
    return Signal("MACD", score, 1.2, verdict, f"hist={hist:.4f}")


def _eval_volume(volumes: list[float]) -> Signal:
    recent = ind.sma(volumes, 5)
    base = ind.sma(volumes, 20)
    if recent is None or base is None or base == 0:
        return Signal("Hacim Trendi", 0.0, 0.8, "yetersiz veri")
    ratio = recent / base
    if ratio > 1.5:
        score, verdict = 0.6, "hacim güçlü artıyor"
    elif ratio > 1.1:
        score, verdict = 0.3, "hacim artıyor"
    elif ratio < 0.7:
        score, verdict = -0.3, "hacim zayıf"
    else:
        score, verdict = 0.0, "hacim normal"
    return Signal("Hacim Trendi", score, 0.8, verdict, f"5g/20g={ratio:.2f}x")


def _eval_breakout(closes: list[float], highs: list[float], lows: list[float]) -> Signal:
    if len(closes) < 30:
        return Signal("Kırılım", 0.0, 1.0, "yetersiz veri")
    price = closes[-1]
    recent_high = max(highs[-30:-1])
    recent_low = min(lows[-30:-1])
    if price >= recent_high:
        score, verdict = 0.8, "30g direnç kırılımı ⬆️"
    elif price <= recent_low:
        score, verdict = -0.8, "30g destek kırılımı ⬇️"
    else:
        # Position within the 30d range, centered at 0.
        rng = recent_high - recent_low
        pos = (price - recent_low) / rng if rng > 0 else 0.5
        score = _clamp((pos - 0.5) * 0.8)
        verdict = f"30g aralığın %{pos * 100:.0f} seviyesinde"
    return Signal("Kırılım", score, 1.0, verdict)


def _eval_momentum(ticker: Ticker24h | None) -> Signal:
    if ticker is None:
        return Signal("24s Momentum", 0.0, 0.6, "veri yok")
    pct = ticker.price_change_pct
    score = _clamp(pct / 10.0)  # ±10% maps to ±1
    verdict = f"24s {'+' if pct >= 0 else ''}{pct:.1f}%"
    return Signal("24s Momentum", score, 0.6, verdict)


def _eval_fear_greed(fg: tuple[int, str] | None) -> Signal:
    if fg is None:
        return Signal("Fear & Greed", 0.0, 0.5, "veri yok")
    value, label = fg
    # Mildly contrarian: extreme fear is bullish, extreme greed is a caution.
    if value <= 25:
        score, verdict = 0.5, f"aşırı korku ({value}) — fırsat olabilir"
    elif value >= 75:
        score, verdict = -0.5, f"aşırı açgözlülük ({value}) — temkinli ol"
    else:
        score = _clamp((50 - value) / 50 * 0.4)
        verdict = f"{label} ({value})"
    return Signal("Fear & Greed", score, 0.5, verdict)


# --- engine -----------------------------------------------------------------

def _rate(composite: float) -> tuple[str, str]:
    if composite >= 0.30:
        return "GÜÇLÜ", "🟢"
    if composite <= -0.20:
        return "ZAYIF", "🔴"
    return "NÖTR", "🟡"


class SignalEngine:
    """Orchestrates providers + evaluators into a SignalReport."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.market = BinanceProvider(cfg)
        self.sentiment = FearGreedProvider(cfg)

    def evaluate(self, symbol: str, fear_greed: tuple[int, str] | None = None) -> SignalReport:
        ohlcv: OHLCV = self.market.fetch_ohlcv(symbol)
        try:
            ticker = self.market.fetch_ticker24h(symbol)
        except Exception:  # noqa: BLE001 - 24h ticker is a nice-to-have
            ticker = None
        if fear_greed is None:
            fear_greed = self.sentiment.fetch()

        signals = [
            _eval_ma_trend(ohlcv.closes),
            _eval_cross(ohlcv.closes),
            _eval_rsi(ohlcv.closes),
            _eval_macd(ohlcv.closes),
            _eval_volume(ohlcv.volumes),
            _eval_breakout(ohlcv.closes, ohlcv.highs, ohlcv.lows),
            _eval_momentum(ticker),
            _eval_fear_greed(fear_greed),
        ]
        composite = self._composite(signals)
        rating, emoji = _rate(composite)
        price = ticker.last_price if ticker else (ohlcv.last_close or 0.0)
        return SignalReport(
            symbol=symbol.upper(),
            price=price,
            signals=signals,
            composite=composite,
            bullish_pct=(composite + 1) / 2 * 100,
            rating=rating,
            emoji=emoji,
            fear_greed=fear_greed,
            atr=ind.atr(ohlcv.highs, ohlcv.lows, ohlcv.closes),
        )

    @staticmethod
    def _composite(signals: list[Signal]) -> float:
        total_w = sum(s.weight for s in signals)
        if total_w == 0:
            return 0.0
        return _clamp(sum(s.score * s.weight for s in signals) / total_w)
