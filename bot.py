import os
import sys
import time
import logging
import calendar
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta

import requests
import feedparser
import telebot
from dotenv import load_dotenv
from openai import OpenAI
from telebot import types

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration & startup validation (fail fast)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("assembly-bot")

TOKEN = os.getenv("TELEGRAM_TOKEN")

# --- LLM provider (OpenAI-compatible) ---------------------------------------
# The bot talks to any OpenAI-compatible API. Choose a provider purely via env;
# no code change needed to switch between OpenAI / xAI / Groq / DeepSeek / etc.
#
#   Generic (any provider):  LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
#   Shortcuts (auto-detect): OPENAI_API_KEY  -> OpenAI
#                            XAI_API_KEY     -> xAI (Grok)
_env_llm_key = os.getenv("LLM_API_KEY")
_env_openai_key = os.getenv("OPENAI_API_KEY")
_env_xai_key = os.getenv("XAI_API_KEY")
_env_base_url = (os.getenv("LLM_BASE_URL") or "").strip() or None
_env_model = (os.getenv("LLM_MODEL") or "").strip() or None

if _env_llm_key:
    LLM_PROVIDER = "custom"
    LLM_API_KEY = _env_llm_key
    LLM_BASE_URL = _env_base_url
    LLM_MODEL = _env_model or "gpt-4o-mini"
elif _env_openai_key:
    LLM_PROVIDER = "openai"
    LLM_API_KEY = _env_openai_key
    LLM_BASE_URL = _env_base_url  # None -> OpenAI default endpoint
    LLM_MODEL = _env_model or "gpt-4o-mini"
elif _env_xai_key:
    LLM_PROVIDER = "xai"
    LLM_API_KEY = _env_xai_key
    LLM_BASE_URL = _env_base_url or "https://api.x.ai/v1"
    LLM_MODEL = _env_model or "grok-4"
else:
    LLM_PROVIDER = None
    LLM_API_KEY = None
    LLM_BASE_URL = None
    LLM_MODEL = None

# Primary RSS source + optional comma-separated fallback mirrors.
# Example: RSS_FALLBACK_URLS="https://mirror1/feed,https://mirror2/feed"
_primary_rss = os.getenv("RSS_URL", "").strip()
_fallback_rss = [u.strip() for u in os.getenv("RSS_FALLBACK_URLS", "").split(",") if u.strip()]
RSS_URLS = [u for u in ([_primary_rss] + _fallback_rss) if u]

# Maximum Telegram message length is 4096; leave headroom for headers/markup.
TELEGRAM_LIMIT = 3900
# Turkey is permanently UTC+3 (no DST since 2016); used to show post times in TSİ.
TR_OFFSET = timedelta(hours=3)
HTTP_TIMEOUT = 20
HTTP_RETRIES = 4
USER_AGENT = (
    "Mozilla/5.0 (compatible; AssemblyBot/1.0; +https://github.com/adokav/theassembly)"
)

DISCLAIMER = (
    "\n\n———\n"
    "⚠️ _Bu rapor yapay zeka tarafından, ilgili X hesabının paylaşımlarından "
    "otomatik üretilmiştir. Yatırım danışmanlığı veya alım-satım tavsiyesi değildir. "
    "Kararlarınızdan yalnızca siz sorumlusunuz._"
)


def _require(name, value):
    if not value:
        log.error("Eksik ortam değişkeni: %s", name)
        return False
    return True


def validate_config():
    ok = True
    ok &= _require("TELEGRAM_TOKEN", TOKEN)
    if not LLM_API_KEY:
        log.error(
            "Eksik LLM anahtarı: OPENAI_API_KEY, XAI_API_KEY veya LLM_API_KEY'den "
            "en az biri tanımlı olmalı."
        )
        ok = False
    if not RSS_URLS:
        log.error("Eksik ortam değişkeni: RSS_URL (en az bir RSS adresi gerekli)")
        ok = False
    if not ok:
        log.error("Bot başlatılamadı. Lütfen .env yapılandırmasını tamamlayın.")
        sys.exit(1)


validate_config()

log.info("LLM sağlayıcı: %s | model: %s%s", LLM_PROVIDER, LLM_MODEL,
         f" | base_url: {LLM_BASE_URL}" if LLM_BASE_URL else "")

bot = telebot.TeleBot(TOKEN)
client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


# ---------------------------------------------------------------------------
# RSS fetching: robust HTTP with retry/backoff + mirror fallback
# ---------------------------------------------------------------------------
def _fetch_feed(url):
    """Fetch & parse a single feed URL with retry/backoff. Returns a feedparser
    result or None on failure."""
    backoff = 2
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"},
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            # bozo=1 means malformed XML; tolerate only if we still got entries.
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"Bozulmuş/boş akış: {getattr(parsed, 'bozo_exception', '')}")
            return parsed
        except Exception as e:  # noqa: BLE001 - we want to retry on anything
            log.warning("RSS denemesi %d/%d başarısız (%s): %s", attempt, HTTP_RETRIES, url, str(e)[:160])
            if attempt < HTTP_RETRIES:
                time.sleep(backoff)
                backoff *= 2
    return None


def fetch_any_feed():
    """Try each configured RSS source (primary then fallbacks) until one yields
    a usable feed. Returns (parsed_feed, used_url) or (None, None)."""
    for url in RSS_URLS:
        log.info("RSS çekiliyor: %s", url)
        parsed = _fetch_feed(url)
        if parsed and parsed.entries:
            log.info("RSS başarılı (%d kayıt): %s", len(parsed.entries), url)
            return parsed, url
        log.warning("RSS kaynağından kullanılabilir veri alınamadı: %s", url)
    return None, None


def _entry_text(entry):
    """Prefer full content; fall back to summary. Keep generous length."""
    if entry.get("content"):
        body = entry["content"][0].get("value", "")
    else:
        body = entry.get("summary", "")
    return body.strip()[:1500]


def get_recent_posts(days):
    """Return (posts, error). error is a user-facing string when fetch fails."""
    parsed, used_url = fetch_any_feed()
    if parsed is None:
        return [], "feed_unreachable"

    # published_parsed is UTC; compare against UTC to avoid timezone drift.
    cutoff = datetime.utcnow() - timedelta(days=days)
    posts = []
    for entry in parsed.entries:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published:
            continue
        pub_date = datetime.utcfromtimestamp(calendar.timegm(published))
        if pub_date > cutoff:
            tr_date = pub_date + TR_OFFSET
            posts.append({
                "title": entry.get("title", "").strip(),
                "text": _entry_text(entry),
                "link": entry.get("link", ""),
                "date": tr_date.strftime("%d.%m.%Y %H:%M TSİ"),
            })
    return posts[:40], None


# ---------------------------------------------------------------------------
# Analysis (Grok) — grounded to the actual posts
# ---------------------------------------------------------------------------
def analyze_posts(posts, period_text):
    if not posts:
        return "Bu dönemde analiz edilecek paylaşım bulunamadı."

    text = "\n\n".join(
        f"[{i+1}] Tarih: {p['date']}\nBaşlık: {p['title']}\nİçerik: {p['text']}\nLink: {p['link']}"
        for i, p in enumerate(posts)
    )

    system = (
        "Sen profesyonel bir makro analist ve trading stratejistisin. "
        "Türkçe, net ve aksiyon odaklı yazarsın. "
        "ÇOK ÖNEMLİ: Yalnızca sana verilen paylaşımlarda AÇIKÇA geçen hisse, kripto ve "
        "varlıkları kullan. Paylaşımlarda olmayan bir varlık, fiyat, seviye ya da TARİH "
        "UYDURMA. Her öneri için, o önerinin geçtiği paylaşımın sana verilen TARİH ve "
        "SAATİNİ ('Tarih: ...' satırından) aynen kullan. Bir paylaşımda net bir öneri "
        "yoksa bunu açıkça belirt."
    )

    user = f"""The Assembly hesabının **{period_text}** içindeki paylaşımlarını analiz et.

Aşağıdaki {len(posts)} paylaşım gerçek veridir. Her paylaşımın başında o paylaşımın
gerçek tarih ve saati (TSİ) yer alır:

{text}

Raporu şu formatta yaz. Tespit ettiğin HER tavsiye/varlık için ayrı bir madde aç:

**1. <Varlık / Hisse adı>**
   • 🗓 *Tarih/Saat:* <önerinin geçtiği paylaşımın tarih ve saati (TSİ)>
   • 💡 *Tavsiye:* <al / sat / izle / kısa-uzun pozisyon vb. — paylaşımda ne dendiyse>
   • 🧭 *Gerekçe:* <bu varlığın neden öne çıktığının paylaşıma dayalı açıklaması>
   • ⚠️ *Zamanlama & Risk:* <kısa risk/zamanlama notu>

Tüm maddelerden sonra:
**📌 Genel Stratejik Görünüm:** <2-3 cümlelik özet>

Paylaşımlarda somut bir yatırım sinyali yoksa "Bu dönemde belirgin bir yatırım sinyali tespit edilmedi" yaz."""

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=1500,
        )
        return response.choices[0].message.content
    except Exception as e:  # noqa: BLE001
        log.exception("LLM analiz hatası (%s/%s)", LLM_PROVIDER, LLM_MODEL)
        msg = str(e)
        low = msg.lower()
        if "403" in msg or "credit" in low or "permission" in low or "quota" in low or "insufficient" in low:
            return (
                f"❌ Yapay zeka sağlayıcısı ({LLM_PROVIDER}) isteği reddetti: kredi/limit "
                f"veya yetki sorunu görünüyor. Hesabınızda bakiye olduğundan ve API "
                f"anahtarının doğru olduğundan emin olun.\n\nDetay: {msg[:200]}"
            )
        return f"❌ Yapay zeka analiz hatası ({LLM_PROVIDER}/{LLM_MODEL}): {msg[:200]}"


# ---------------------------------------------------------------------------
# Safe Telegram delivery: chunking + Markdown→plain fallback
# ---------------------------------------------------------------------------
def _chunk(text, size=TELEGRAM_LIMIT):
    """Split text on paragraph/line boundaries when possible."""
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


def _send_chunk(chat_id, text, message_id=None):
    """Send/edit a chunk; retry without Markdown if Telegram rejects the markup."""
    try:
        if message_id is not None:
            bot.edit_message_text(text, chat_id, message_id, parse_mode="Markdown")
        else:
            bot.send_message(chat_id, text, parse_mode="Markdown")
    except Exception as e:  # noqa: BLE001 - typically a Markdown parse 400
        log.warning("Markdown gönderimi başarısız, düz metne düşülüyor: %s", str(e)[:160])
        if message_id is not None:
            bot.edit_message_text(text, chat_id, message_id)
        else:
            bot.send_message(chat_id, text)


def safe_send(chat_id, text, edit_message_id=None):
    """Deliver arbitrarily long text safely. First chunk edits the loading
    message (if given); the rest are sent as follow-up messages."""
    chunks = _chunk(text)
    for i, chunk in enumerate(chunks):
        _send_chunk(chat_id, chunk, message_id=edit_message_id if i == 0 else None)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start", "rapor"])
def send_menu(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🔥 Son 1 Gün", callback_data="1"))
    markup.add(types.InlineKeyboardButton("🔥 Son 2 Gün", callback_data="2"))
    markup.add(types.InlineKeyboardButton("🔥 Son 3 Gün", callback_data="3"))
    markup.add(types.InlineKeyboardButton("📅 Son 1 Hafta", callback_data="7"))
    markup.add(types.InlineKeyboardButton("📅 Son 2 Hafta", callback_data="14"))
    markup.add(types.InlineKeyboardButton("📊 Geçtiğimiz Ay", callback_data="30"))

    bot.send_message(
        message.chat.id,
        "🎯 *The Assembly Stratejik Rapor Botu*\n\nHangi dönemi analiz etmek istersin?",
        reply_markup=markup,
        parse_mode="Markdown",
    )


def _period_text(days):
    if days <= 3:
        return f"Son {days} Gün"
    if days <= 14:
        return f"Son {days // 7} Hafta"
    return "Geçtiğimiz Ay"


@bot.callback_query_handler(func=lambda call: call.data.isdigit())
def callback_handler(call):
    # Stop the Telegram loading spinner immediately.
    try:
        bot.answer_callback_query(call.id)
    except Exception:  # noqa: BLE001
        pass

    days = int(call.data)
    period_text = _period_text(days)
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    bot.edit_message_text("🔄 Grok AI analiz yapıyor... (10-30 sn)", chat_id, msg_id)

    posts, error = get_recent_posts(days)

    if error == "feed_unreachable":
        bot.edit_message_text(
            "⚠️ Paylaşım kaynağına (RSS) şu anda ulaşılamıyor. "
            "Kaynak geçici olarak kapalı olabilir; lütfen birazdan tekrar deneyin.",
            chat_id, msg_id,
        )
        return

    analysis = analyze_posts(posts, period_text)
    header = f"📊 *The Assembly — {period_text} Stratejik Rapor*\n"
    if posts:
        header += f"_({len(posts)} paylaşım analiz edildi)_\n\n"
    else:
        header += "\n"

    result = header + analysis + DISCLAIMER
    safe_send(chat_id, result, edit_message_id=msg_id)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence per-request logging
        pass


def start_health_server():
    """When PORT is set (e.g. Render Web Service), serve a tiny health endpoint
    in a background thread so the platform's port/health check passes while the
    bot polls. No-op for Background Workers (no PORT)."""
    port = os.getenv("PORT")
    if not port:
        return
    try:
        server = HTTPServer(("0.0.0.0", int(port)), _HealthHandler)
    except Exception as e:  # noqa: BLE001
        log.warning("Health sunucusu başlatılamadı (PORT=%s): %s", port, str(e)[:160])
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health sunucusu %s portunda dinliyor.", port)


if __name__ == "__main__":
    # Surface polling/Telegram errors instead of silently swallowing them.
    telebot.logger.setLevel(logging.INFO)

    # Keep the platform health check happy if deployed as a Web Service.
    start_health_server()

    # 1) Verify the token & network reach Telegram. Fails fast with a clear
    #    message instead of a silent "no response" bot.
    try:
        me = bot.get_me()
        log.info("Telegram bağlantısı OK → @%s (id=%s)", me.username, me.id)
    except Exception as e:  # noqa: BLE001
        log.error(
            "Telegram'a bağlanılamadı. TELEGRAM_TOKEN yanlış olabilir ya da ağ "
            "api.telegram.org'a çıkamıyor. Hata: %s", str(e)[:200],
        )
        sys.exit(1)

    # 2) Clear any leftover webhook — a set webhook makes getUpdates (polling)
    #    return 409 and the bot silently receives no messages.
    try:
        bot.remove_webhook()
        log.info("Webhook temizlendi; polling moduna geçiliyor.")
    except Exception as e:  # noqa: BLE001
        log.warning("Webhook temizlenemedi: %s", str(e)[:160])

    log.info("🚀 The Assembly Grok AI Botu BAŞLATILDI (%d RSS kaynağı yapılandırıldı)", len(RSS_URLS))
    # skip_pending: ignore the backlog accrued while the bot was offline.
    # If a second instance runs the same token, Telegram returns 409 — that is
    # now logged (above) instead of being invisible.
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
