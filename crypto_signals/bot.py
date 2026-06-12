"""Telegram bot: handlers, keyboards and the application entrypoint.

The "UI" is a persistent keyboard plus slash commands. State (subscriptions,
watchlists) is persisted via the Repository; the SignalScheduler pushes alerts
in the background. Delivery uses safe_send (chunking + Markdown->plain fallback)
so long reports and markup edge-cases never crash a send.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

from .config import Config, load_config
from .formatting import render_report, render_watchlist_summary
from .scheduler import SignalScheduler
from .signals import SignalEngine
from .storage import Repository

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("crypto_signals.bot")


class CryptoSignalsBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bot = telebot.TeleBot(cfg.telegram_token, parse_mode=None)
        self.repo = Repository(cfg.db_path)
        self.engine = SignalEngine(cfg)
        self.scheduler = SignalScheduler(cfg, self.repo, self.engine, notify=self._notify)
        self._register_handlers()

    # --- delivery ----------------------------------------------------------
    def _chunk(self, text: str) -> list[str]:
        size = self.cfg.telegram_limit
        chunks = []
        while len(text) > size:
            split_at = text.rfind("\n", 0, size)
            if split_at <= 0:
                split_at = size
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        if text:
            chunks.append(text)
        return chunks

    def _send_chunk(self, chat_id: int, text: str) -> None:
        try:
            self.bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as e:  # noqa: BLE001 - usually a Markdown 400; fall back to plain
            log.warning("Markdown gönderimi başarısız, düz metne düşülüyor: %s", str(e)[:160])
            self.bot.send_message(chat_id, text)

    def safe_send(self, chat_id: int, text: str) -> None:
        for chunk in self._chunk(text):
            self._send_chunk(chat_id, chunk)

    def _notify(self, chat_id: int, text: str) -> None:
        """Notifier passed to the scheduler."""
        self.safe_send(chat_id, text)

    # --- keyboard ----------------------------------------------------------
    def _keyboard(self, chat_id: int) -> types.ReplyKeyboardMarkup:
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        symbols = self.repo.get_watchlist(chat_id) or self.cfg.default_symbols
        row: list[str] = []
        for sym in symbols[:6]:
            row.append(f"📈 {sym}")
            if len(row) == 3:
                kb.row(*row)
                row = []
        if row:
            kb.row(*row)
        kb.row("📡 Radar", "📋 Watchlist")
        kb.row("😱 Korku Endeksi", "➕ Ekle/Çıkar")
        kb.row("ℹ️ Yardım")
        return kb

    # --- handlers ----------------------------------------------------------
    def _register_handlers(self) -> None:
        bot = self.bot

        @bot.message_handler(commands=["start"])
        def _start(message):
            chat_id = message.chat.id
            self.repo.add_subscriber(chat_id)
            self.bot.send_message(
                chat_id,
                "👋 *Kripto Sinyal Botu*'na hoş geldin!\n\n"
                "Teknik + piyasa sinyallerini derler, bir coin'in yükselme "
                "olasılığını konfluens skoruyla özetlerim.\n\n"
                "• `/sinyal BTC` — anlık analiz\n"
                "• `/ekle SOL` · `/sil SOL` — takip listesi\n"
                "• `/liste` — watchlist özeti\n"
                "• Alttaki butonlarla tek dokunuşla analiz al.\n\n"
                "Periyodik tarama açık: güçlü sinyal oluşunca otomatik haber veririm.",
                parse_mode="Markdown",
                reply_markup=self._keyboard(chat_id),
            )

        @bot.message_handler(commands=["yardim", "help"])
        def _help(message):
            self.bot.send_message(
                message.chat.id,
                "*Komutlar*\n"
                "`/sinyal <SEMBOL>` — anlık sinyal raporu (örn. `/sinyal ETH`)\n"
                "`/radar` — son taramadaki en güçlü boğa sinyalleri\n"
                "`/ekle <SEMBOL>` — takip listesine ekle\n"
                "`/sil <SEMBOL>` — listeden çıkar\n"
                "`/liste` — watchlist özeti\n"
                "`/korku` — piyasa Korku & Açgözlülük endeksi\n"
                "`/abonelik_iptal` — otomatik alarmları kapat\n\n"
                "_Watchlist'in boşken otomatik tarama, hacme göre ilk "
                f"{self.cfg.dynamic_top_n} coin'i kapsar._\n\n"
                "_Sinyaller: Trend, Golden/Death Cross, RSI, MACD, Hacim, Kırılım, "
                "24s Momentum, Fear & Greed._",
                parse_mode="Markdown",
                reply_markup=self._keyboard(message.chat.id),
            )

        @bot.message_handler(commands=["sinyal", "signal"])
        def _signal(message):
            parts = message.text.split()
            if len(parts) < 2:
                self.bot.send_message(message.chat.id, "Kullanım: `/sinyal BTC`", parse_mode="Markdown")
                return
            self._report_symbol(message.chat.id, parts[1])

        @bot.message_handler(commands=["ekle", "add"])
        def _add(message):
            parts = message.text.split()
            if len(parts) < 2:
                self.bot.send_message(message.chat.id, "Kullanım: `/ekle SOL`", parse_mode="Markdown")
                return
            sym = parts[1].upper()
            self.repo.add_subscriber(message.chat.id)
            self.repo.add_symbol(message.chat.id, sym)
            self.bot.send_message(
                message.chat.id, f"✅ *{sym}* takip listene eklendi.",
                parse_mode="Markdown", reply_markup=self._keyboard(message.chat.id),
            )

        @bot.message_handler(commands=["sil", "remove"])
        def _remove(message):
            parts = message.text.split()
            if len(parts) < 2:
                self.bot.send_message(message.chat.id, "Kullanım: `/sil SOL`", parse_mode="Markdown")
                return
            sym = parts[1].upper()
            self.repo.remove_symbol(message.chat.id, sym)
            self.bot.send_message(
                message.chat.id, f"🗑️ *{sym}* listeden çıkarıldı.",
                parse_mode="Markdown", reply_markup=self._keyboard(message.chat.id),
            )

        @bot.message_handler(commands=["liste", "list"])
        def _list(message):
            self._report_watchlist(message.chat.id)

        @bot.message_handler(commands=["radar"])
        def _radar(message):
            self.repo.add_subscriber(message.chat.id)
            rows = self.repo.latest_snapshots(limit=15, min_composite=0.30)
            if not rows:
                self.bot.send_message(
                    message.chat.id,
                    "📡 Radar henüz boş. Arka plan taraması ilk turunu (≈birkaç dk) "
                    "tamamlayınca en güçlü boğa sinyalleri burada listelenir. "
                    "Bu arada `/sinyal BTC` deneyebilirsin.",
                    parse_mode="Markdown",
                )
                return
            lines = [f"📡 *Radar — en güçlü {len(rows)} boğa sinyali* (son tarama):", ""]
            for r in rows:
                emoji = "🟢" if r["composite"] >= 0.30 else "🟡"
                lines.append(f"{emoji} *{r['symbol']}* — {r['rating']} · skor `{r['composite']:+.2f}`")
            lines.append("\n_Detay için: `/sinyal <SEMBOL>`. Yatırım tavsiyesi değildir._")
            self.safe_send(message.chat.id, "\n".join(lines))

        @bot.message_handler(commands=["korku", "fng"])
        def _fng(message):
            fg = self.engine.sentiment.fetch()
            if fg is None:
                self.bot.send_message(message.chat.id, "Korku endeksi şu an alınamadı.")
                return
            value, label = fg
            self.bot.send_message(
                message.chat.id,
                f"😱 *Korku & Açgözlülük Endeksi:* {value}/100 — _{label}_",
                parse_mode="Markdown",
            )

        @bot.message_handler(commands=["abonelik_iptal", "unsubscribe"])
        def _unsub(message):
            self.repo.remove_subscriber(message.chat.id)
            self.bot.send_message(message.chat.id, "🔕 Otomatik alarmlar kapatıldı. `/start` ile tekrar açabilirsin.", parse_mode="Markdown")

        # Persistent keyboard buttons (text messages).
        @bot.message_handler(func=lambda m: bool(m.text))
        def _buttons(message):
            text = message.text.strip()
            if text.startswith("📈 "):
                self._report_symbol(message.chat.id, text[2:].strip())
            elif text == "📡 Radar":
                _radar(message)
            elif text == "📋 Watchlist":
                self._report_watchlist(message.chat.id)
            elif text == "😱 Korku Endeksi":
                _fng(message)
            elif text == "➕ Ekle/Çıkar":
                self.bot.send_message(
                    message.chat.id,
                    "Sembol eklemek için `/ekle SOL`, çıkarmak için `/sil SOL` yaz.",
                    parse_mode="Markdown",
                )
            elif text == "ℹ️ Yardım":
                _help(message)

    # --- report helpers ----------------------------------------------------
    def _report_symbol(self, chat_id: int, symbol: str) -> None:
        symbol = symbol.upper().strip()
        loading = self.bot.send_message(chat_id, f"⏳ *{symbol}* analiz ediliyor…", parse_mode="Markdown")
        try:
            rep = self.engine.evaluate(symbol)
        except Exception as e:  # noqa: BLE001
            self.bot.edit_message_text(
                f"⚠️ *{symbol}* için veri alınamadı. Sembolü kontrol et (örn. BTC, ETH, SOL).",
                chat_id, loading.message_id, parse_mode="Markdown",
            )
            log.warning("%s raporu başarısız: %s", symbol, str(e)[:160])
            return
        self.repo.save_snapshot(symbol, rep.composite, rep.rating, {"price": rep.price})
        try:
            self.bot.delete_message(chat_id, loading.message_id)
        except Exception:  # noqa: BLE001
            pass
        self.safe_send(chat_id, render_report(rep))

    def _report_watchlist(self, chat_id: int) -> None:
        symbols = self.repo.get_watchlist(chat_id) or self.cfg.default_symbols
        loading = self.bot.send_message(chat_id, "⏳ Watchlist taranıyor…")
        fg = self.engine.sentiment.fetch()
        reports = []
        for sym in symbols:
            try:
                reports.append(self.engine.evaluate(sym, fear_greed=fg))
            except Exception as e:  # noqa: BLE001
                log.warning("%s atlandı: %s", sym, str(e)[:160])
        try:
            self.bot.delete_message(chat_id, loading.message_id)
        except Exception:  # noqa: BLE001
            pass
        self.safe_send(chat_id, render_watchlist_summary(reports))

    # --- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        try:
            me = self.bot.get_me()
            log.info("Telegram bağlantısı OK → @%s (id=%s)", me.username, me.id)
        except Exception as e:  # noqa: BLE001
            log.error("Telegram'a bağlanılamadı. TELEGRAM_TOKEN yanlış olabilir. Hata: %s", str(e)[:200])
            sys.exit(1)

        try:
            self.bot.set_my_commands([
                types.BotCommand("start", "Başlat ve menüyü göster"),
                types.BotCommand("sinyal", "Bir coin için anlık sinyal"),
                types.BotCommand("radar", "En güçlü boğa sinyalleri (son tarama)"),
                types.BotCommand("ekle", "Takip listesine sembol ekle"),
                types.BotCommand("sil", "Takip listesinden çıkar"),
                types.BotCommand("liste", "Watchlist özeti"),
                types.BotCommand("korku", "Korku & Açgözlülük endeksi"),
            ])
        except Exception as e:  # noqa: BLE001
            log.warning("Komut menüsü ayarlanamadı: %s", str(e)[:160])

        self.scheduler.start()
        _maybe_start_health_server()
        self._run_polling()

    def _run_polling(self) -> None:
        try:
            self.bot.remove_webhook()
        except Exception as e:  # noqa: BLE001
            log.warning("Webhook temizlenemedi: %s", str(e)[:160])
        log.info("🚀 Kripto Sinyal Botu POLLING modunda başladı.")
        backoff = 5
        while True:
            try:
                self.bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
                backoff = 5
            except ApiTelegramException as e:
                if getattr(e, "error_code", None) == 409:
                    log.warning("409 Conflict: başka bir kopya dinliyor; %d sn sonra tekrar.", backoff)
                else:
                    log.warning("Telegram API hatası; %d sn sonra tekrar: %s", backoff, str(e)[:160])
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:  # noqa: BLE001 - keep the process alive
                log.warning("Beklenmeyen polling hatası; %d sn sonra tekrar: %s", backoff, str(e)[:160])
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):  # silence access logs
        pass


def _maybe_start_health_server() -> None:
    """Open a tiny health endpoint when PORT is set (e.g. Render Web Service)."""
    port = os.getenv("PORT")
    if not port:
        return
    server = HTTPServer(("0.0.0.0", int(port)), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health endpoint açıldı: :%s", port)


def main() -> None:
    cfg = load_config()
    CryptoSignalsBot(cfg).run()


if __name__ == "__main__":
    main()
