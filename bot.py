import os
import sys
import time
import logging
import calendar
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
XAI_API_KEY = os.getenv("XAI_API_KEY")

# Primary RSS source + optional comma-separated fallback mirrors.
# Example: RSS_FALLBACK_URLS="https://mirror1/feed,https://mirror2/feed"
_primary_rss = os.getenv("RSS_URL", "").strip()
_fallback_rss = [u.strip() for u in os.getenv("RSS_FALLBACK_URLS", "").split(",") if u.strip()]
RSS_URLS = [u for u in ([_primary_rss] + _fallback_rss) if u]

# Maximum Telegram message length is 4096; leave headroom for headers/markup.
TELEGRAM_LIMIT = 3900
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
    ok &= _require("XAI_API_KEY", XAI_API_KEY)
    if not RSS_URLS:
        log.error("Eksik ortam değişkeni: RSS_URL (en az bir RSS adresi gerekli)")
        ok = False
    if not ok:
        log.error("Bot başlatılamadı. Lütfen .env yapılandırmasını tamamlayın.")
        sys.exit(1)


validate_config()

bot = telebot.TeleBot(TOKEN)
client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")


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
            posts.append({
                "title": entry.get("title", "").strip(),
                "text": _entry_text(entry),
                "link": entry.get("link", ""),
                "date": pub_date.strftime("%Y-%m-%d %H:%M UTC"),
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
        "varlıkları kullan. Paylaşımlarda olmayan bir varlık, fiyat ya da seviye UYDURMA. "
        "Bir paylaşımda net bir öneri yoksa bunu açıkça belirt."
    )

    user = f"""The Assembly hesabının **{period_text}** içindeki paylaşımlarını analiz et.

Aşağıdaki {len(posts)} paylaşım gerçek veridir:

{text}

Raporu şu formatta yaz:
• **Öne Çıkan Hisseler / Varlıklar** (yalnızca paylaşımlarda geçenler)
• **Her birinin neden gündeme geldiği** (ilgili paylaşıma atıfla)
• **Zamanlama ve Risk Seviyesi**
• **Genel Stratejik Görünüm**

Paylaşımlarda somut bir yatırım sinyali yoksa "Bu dönemde belirgin bir yatırım sinyali tespit edilmedi" yaz."""

    try:
        response = client.chat.completions.create(
            model="grok-4",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=1500,
        )
        return response.choices[0].message.content
    except Exception as e:  # noqa: BLE001
        log.exception("Grok API hatası")
        return f"❌ Grok API hatası: {str(e)[:200]}"


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


if __name__ == "__main__":
    log.info("🚀 The Assembly Grok AI Botu BAŞLATILDI (%d RSS kaynağı yapılandırıldı)", len(RSS_URLS))
    bot.infinity_polling()
