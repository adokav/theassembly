"""Render SignalReports into Telegram-friendly Markdown."""
from __future__ import annotations

from .signals import SignalReport

DISCLAIMER = (
    "\n_⚠️ Bu rapor yatırım tavsiyesi değildir. Sinyaller olasılık gösterir, "
    "garanti vermez. Kendi araştırmanı yap (DYOR)._"
)


def _signal_emoji(score: float) -> str:
    if score >= 0.3:
        return "🟢"
    if score <= -0.3:
        return "🔴"
    return "⚪️"


def _fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.3f}"
    return f"{p:.6f}"


def render_report(rep: SignalReport, *, with_disclaimer: bool = True) -> str:
    lines = [
        f"{rep.emoji} *{rep.symbol}* — {rep.rating}  (boğa olasılığı %{rep.bullish_pct:.0f})",
        f"💵 Fiyat: `{_fmt_price(rep.price)}`",
        "",
        "*Sinyaller:*",
    ]
    for s in rep.signals:
        detail = f"  `{s.detail}`" if s.detail else ""
        lines.append(f"{_signal_emoji(s.score)} {s.name}: {s.verdict}{detail}")

    if rep.stop_suggestion is not None and rep.rating != "ZAYIF":
        lines.append("")
        lines.append(f"🛡️ Veri-bazlı stop önerisi (2×ATR): `{_fmt_price(rep.stop_suggestion)}`")

    lines.append("")
    lines.append(f"📊 Kompozit skor: `{rep.composite:+.2f}` (-1 ayı … +1 boğa)")
    if with_disclaimer:
        lines.append(DISCLAIMER)
    return "\n".join(lines)


def render_new_signal(rep: SignalReport) -> str:
    """Auto-report sent when a coin first generates a signal."""
    head = f"🟢🔔 *YENİ SİNYAL* — *{rep.symbol}* sinyal üretti!\n\n"
    return head + render_report(rep)


def render_broken(rep: SignalReport, entry: dict, reasons: list[str]) -> str:
    """Auto-report sent when a previously-signaled coin's formation breaks."""
    entry_price = entry.get("entry_price") or 0.0
    pct = ((rep.price - entry_price) / entry_price * 100) if entry_price else 0.0
    lines = [
        f"🔻⚠️ *FORMASYON BOZULDU* — *{rep.symbol}*",
        "",
        f"Sinyal sonrası: `{_fmt_price(entry_price)}` → `{_fmt_price(rep.price)}` "
        f"({'+' if pct >= 0 else ''}{pct:.1f}%)",
    ]
    if reasons:
        lines.append("")
        lines.append("*Bozulan formasyon:*")
        for r in reasons:
            lines.append(f"🔴 {r}")
    lines.append("")
    lines.append(f"📊 Güncel kompozit skor: `{rep.composite:+.2f}` → sinyal kapatıldı.")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def render_watchlist_summary(reports: list[SignalReport]) -> str:
    if not reports:
        return "Takip listen boş. `/ekle BTC` ile sembol ekleyebilirsin."
    lines = ["*📋 Watchlist özeti:*", ""]
    for rep in sorted(reports, key=lambda r: r.composite, reverse=True):
        lines.append(
            f"{rep.emoji} *{rep.symbol}* — {rep.rating} "
            f"(%{rep.bullish_pct:.0f}) · `{_fmt_price(rep.price)}`"
        )
    lines.append(DISCLAIMER)
    return "\n".join(lines)
