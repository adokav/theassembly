import os
import re
import sys
import json
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

# --- Tracked accounts (multi-account) ---------------------------------------
# RSS_TEMPLATE lets us build a feed URL from an X handle, e.g.
#   "https://nitter.example/{handle}/rss"  or  ".../twitter/user/{handle}"
# Adding a new account is then just one line in _ACCOUNT_DEFS below.
RSS_TEMPLATE = (os.getenv("RSS_TEMPLATE") or "").strip() or None

# (key, görünen ad, X handle, o hesaba özel env değişkeni, varsayılan feed URL)
# Env değişkeni varsa o öncelikli; yoksa varsayılan kullanılır. rss.app feed'leri
# gizli anahtar değildir (URL'yi bilen okur), bu yüzden burada tutulabilir.
_ACCOUNT_DEFS = [
    ("assembly", "The Assembly", "InTheAssembly", "RSS_URL", ""),
    ("bora", "Bora Özkent", "BoraOzkent", "BORA_RSS_URL",
     "https://rss.app/feeds/XVMR34JsDkbWXBYl.xml"),
]


def _build_feeds(handle, env_key, default=""):
    urls = []
    raw = os.getenv(env_key, "") if env_key else ""
    urls += [u.strip() for u in raw.split(",") if u.strip()]
    if RSS_TEMPLATE:
        urls.append(RSS_TEMPLATE.format(handle=handle))
    if default:
        urls.append(default)
    return list(dict.fromkeys(urls))  # de-dupe, keep order


ACCOUNTS = {}
for _key, _name, _handle, _env, _default in _ACCOUNT_DEFS:
    _feeds = _build_feeds(_handle, _env, _default)
    if _key == "assembly":  # honor existing RSS_URL + RSS_FALLBACK_URLS too
        _feeds = list(dict.fromkeys(RSS_URLS + _feeds))
    ACCOUNTS[_key] = {"name": _name, "handle": _handle, "feeds": _feeds}

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


def fetch_any_feed(feeds):
    """Try each feed URL (primary then fallbacks) until one yields a usable feed.
    Returns (parsed_feed, used_url) or (None, None)."""
    for url in feeds:
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


def get_recent_posts(days, feeds):
    """Return (posts, error). error is a user-facing string when fetch fails."""
    if not feeds:
        return [], "feed_unreachable"
    parsed, used_url = fetch_any_feed(feeds)
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
# Analysis: structured extraction -> live market enrichment -> graded report
# ---------------------------------------------------------------------------
def _llm_chat(messages, max_tokens=1500, temperature=0.4, json_mode=False):
    kwargs = dict(model=LLM_MODEL, messages=messages,
                  temperature=temperature, max_tokens=max_tokens)
    if json_mode:
        try:
            return client.chat.completions.create(
                response_format={"type": "json_object"}, **kwargs
            ).choices[0].message.content
        except Exception:  # model may not support response_format; retry plain
            pass
    return client.chat.completions.create(**kwargs).choices[0].message.content


def _parse_json(raw):
    """Parse LLM output as JSON, tolerating surrounding prose/code fences."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def extract_recommendations(posts):
    """Pass 1: pull concrete, grounded recommendations as structured JSON."""
    text = "\n\n".join(
        f"[{i+1}] Tarih: {p['date']}\nBaşlık: {p['title']}\nİçerik: {p['text']}"
        for i, p in enumerate(posts)
    )
    system = ("Sen bir finansal metin çıkarım motorusun. SADECE geçerli JSON döndür. "
              "Paylaşımlarda olmayan hiçbir varlık, fiyat veya tarih uydurma.")
    user = f"""Aşağıdaki paylaşımlardan SOMUT hisse/varlık tavsiyelerini çıkar.
Sadece ABD/global borsalarda işlem gören hisseler için Yahoo Finance sembolü ver
(ör. Apple -> AAPL, Nvidia -> NVDA). Net sembol çıkaramıyorsan o kaydı atla.

JSON şeması:
{{"recommendations": [
  {{"asset": "şirket adı", "ticker": "AAPL", "action": "al|sat|izle",
    "date": "GG.AA.YYYY", "entry_price": null, "target": null,
    "conviction": "yüksek|orta|düşük", "thesis": "kısa gerekçe",
    "source_quote": "ilgili alıntı"}}
]}}

Paylaşımlar:
{text}

Yalnızca JSON döndür."""
    raw = _llm_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1500, temperature=0.2, json_mode=True,
    )
    data = _parse_json(raw) or {}
    recs = data.get("recommendations", [])
    return recs if isinstance(recs, list) else []


def fetch_quote(ticker):
    """Live market data from Yahoo Finance chart JSON (no heavy deps).
    Returns dict with current price, 50d MA, 52w hi/lo and (ts, close) pairs."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        resp = requests.get(
            url, params={"range": "1y", "interval": "1d"},
            headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res.get("meta", {}) or {}
        ts = res.get("timestamp", []) or []
        closes = (res.get("indicators", {}).get("quote", [{}])[0].get("close", []) or [])
        pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
        if not pairs:
            return None
        closes_only = [c for _, c in pairs]
        current = meta.get("regularMarketPrice") or closes_only[-1]
        ma50 = sum(closes_only[-50:]) / min(len(closes_only), 50)
        return {
            "current": current, "ma50": ma50,
            "high52": max(closes_only), "low52": min(closes_only),
            "currency": meta.get("currency", ""), "pairs": pairs,
        }
    except Exception as e:  # noqa: BLE001
        log.warning("Fiyat alınamadı (%s): %s", ticker, str(e)[:120])
        return None


def _price_on_or_before(pairs, date_str):
    try:
        target = datetime.strptime(date_str, "%d.%m.%Y").timestamp() + 86400
    except Exception:
        return None
    chosen = None
    for t, c in pairs:
        if t <= target:
            chosen = c
        else:
            break
    return chosen


def enrich_recommendations(recs):
    """Attach live market context to each recommendation that has a ticker."""
    for r in recs[:10]:
        tk = (r.get("ticker") or "").strip().upper()
        if not tk:
            r["market"] = None
            continue
        q = fetch_quote(tk)
        if not q:
            r["market"] = None
            continue
        entry = _price_on_or_before(q["pairs"], r.get("date", "")) or r.get("entry_price")
        cur = q["current"]
        chg = ((cur - entry) / entry * 100) if entry else None
        r["market"] = {
            "current": round(cur, 2),
            "entry_then": round(entry, 2) if entry else None,
            "pct_change": round(chg, 1) if chg is not None else None,
            "ma50": round(q["ma50"], 2),
            "high52": round(q["high52"], 2),
            "low52": round(q["low52"], 2),
            "currency": q["currency"],
        }
    return recs


def build_report_with_market(recs, period_text, account_name):
    """Pass 2: turn enriched structured data into a graded, Turkish report."""
    payload = json.dumps({"period": period_text, "recommendations": recs}, ensure_ascii=False)
    system = ("Sen profesyonel bir trading stratejistisin. Türkçe, net yazarsın. "
              "Sana verilen yapılandırılmış veriyi ve GÜNCEL piyasa fiyatlarını kullan; "
              "fiyat veya tarih UYDURMA. market alanı null ise fiyat yorumu yapma.")
    user = f"""Aşağıda {account_name} hesabının {period_text} içindeki tavsiyeleri ve her biri
için güncel piyasa verisi (JSON) var:

{payload}

Her tavsiye için tam olarak şu formatta yaz:

**<asset> ({{ticker}})**
• 🗓 Tavsiye: <date> — <action>
• 🧭 Gerekçe: <thesis>
• 💰 O günden bugüne: giriş ~<entry_then> → güncel <current> (<pct_change>%); 50G ort: <ma50>; 52H: <low52>–<high52>
• 🚦 *BUGÜN ALINIR MI?*: 🟢 Hâlâ geçerli / 🟡 Kısmen / 🔴 Geç kalındı–Geçersiz — <gerekçe: güncel fiyat girişe ve 50G ortalamaya göre nerede, tez bozuldu mu, önerilen yeni giriş/stop>

Karar mantığı: fiyat girişe yakın/altında ve tez sağlamsa 🟢; bir miktar kaçmış ama makulse 🟡;
hedefi çoktan aşmış, 52H zirveye yapışmış ya da tez geçersizse 🔴.
market null ise: "Fiyat verisi alınamadı, güncel değerlendirme yapılamadı" yaz.

Sonda: **📌 Genel Görünüm** (2-3 cümle)."""
    return _llm_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1900, temperature=0.4,
    )


def _analyze_legacy(posts, period_text, account_name):
    """Single-pass, text-only report. Fallback when structured extraction fails."""
    text = "\n\n".join(
        f"[{i+1}] Tarih: {p['date']}\nBaşlık: {p['title']}\nİçerik: {p['text']}\nLink: {p['link']}"
        for i, p in enumerate(posts)
    )
    system = (
        "Sen profesyonel bir makro analist ve trading stratejistisin. "
        "Türkçe, net ve aksiyon odaklı yazarsın. "
        "ÇOK ÖNEMLİ: Yalnızca sana verilen paylaşımlarda AÇIKÇA geçen hisse, kripto ve "
        "varlıkları kullan. Paylaşımlarda olmayan bir varlık, fiyat, seviye ya da TARİH "
        "UYDURMA. Her öneri için, o önerinin geçtiği paylaşımın TARİH ve SAATİNİ aynen kullan."
    )
    user = f"""{account_name} hesabının **{period_text}** içindeki paylaşımlarını analiz et.

{text}

Her tavsiye için: **Varlık** · 🗓 Tarih/Saat · 💡 Tavsiye · 🧭 Gerekçe · ⚠️ Risk.
Sonda: **📌 Genel Stratejik Görünüm**.
Somut sinyal yoksa "Bu dönemde belirgin bir yatırım sinyali tespit edilmedi" yaz."""
    return _llm_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1500, temperature=0.4,
    )


def analyze_posts(posts, period_text, account_name="The Assembly"):
    if not posts:
        return "Bu dönemde analiz edilecek paylaşım bulunamadı."
    try:
        recs = extract_recommendations(posts)
        if recs:
            enrich_recommendations(recs)
            return build_report_with_market(recs, period_text, account_name)
        return _analyze_legacy(posts, period_text, account_name)
    except Exception as e:  # noqa: BLE001
        log.exception("LLM analiz hatası (%s/%s)", LLM_PROVIDER, LLM_MODEL)
        msg = str(e)
        low = msg.lower()
        if any(k in low for k in ("403", "credit", "permission", "quota", "insufficient")):
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
# Period buttons (label -> days). Used by the persistent reply keyboard.
PERIOD_BUTTONS = {
    "🔥 Son 1 Gün": 1,
    "🔥 Son 2 Gün": 2,
    "🔥 Son 3 Gün": 3,
    "📅 Son 1 Hafta": 7,
    "📅 Son 2 Hafta": 14,
    "📊 Geçtiğimiz Ay": 30,
}


# Persistent keyboard buttons -> account key. One button per tracked account.
ACCOUNT_BUTTONS = {f"📈 {a['name']}": key for key, a in ACCOUNTS.items()}


def _account_keyboard():
    """Persistent keyboard listing tracked accounts (pinned at the bottom)."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    for label in ACCOUNT_BUTTONS:
        kb.row(label)
    return kb


def _period_inline(account_key):
    """Inline period buttons; callback_data carries account + days."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[types.InlineKeyboardButton(lbl, callback_data=f"{account_key}:{days}")
             for lbl, days in PERIOD_BUTTONS.items()])
    return kb


def show_menu(chat_id, title=None):
    if title is None:
        title = "🎯 *Stratejik Rapor Botu*\n\nTakip edilen bir hesap seç:"
    title += f"\n\n🤖 _Aktif AI: {LLM_PROVIDER} · {LLM_MODEL}_"
    bot.send_message(chat_id, title, reply_markup=_account_keyboard(), parse_mode="Markdown")


@bot.message_handler(commands=["start", "rapor"])
def send_menu(message):
    show_menu(message.chat.id)


@bot.message_handler(func=lambda m: m.text in ACCOUNT_BUTTONS)
def account_button_handler(message):
    key = ACCOUNT_BUTTONS[message.text]
    bot.send_message(
        message.chat.id,
        f"📊 *{ACCOUNTS[key]['name']}* — hangi dönemi raporlayalım?",
        reply_markup=_period_inline(key), parse_mode="Markdown",
    )


@bot.message_handler(commands=["diag"])
def send_diag(message):
    """Show what env the RUNNING process actually sees (no secret values)."""
    lines = ["🔧 *Tanılama — botun gördüğü ortam*", ""]
    for k in ["LLM_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"]:
        v = os.getenv(k)
        lines.append(f"`{k}`: {'✅ VAR (uzunluk ' + str(len(v)) + ')' if v else '❌ YOK'}")
    for k in ["LLM_BASE_URL", "LLM_MODEL"]:
        v = os.getenv(k)
        lines.append(f"`{k}`: {('✅ ' + v) if v else '❌ YOK'}")
    lines.append("")
    lines.append(f"🤖 Aktif sağlayıcı: *{LLM_PROVIDER}* · `{LLM_MODEL}`")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")


def _period_text(days):
    if days <= 3:
        return f"Son {days} Gün"
    if days <= 14:
        return f"Son {days // 7} Hafta"
    return "Geçtiğimiz Ay"


def run_report(chat_id, account_key, days):
    """Fetch a tracked account's posts, analyze, and deliver the graded report."""
    account = ACCOUNTS.get(account_key) or ACCOUNTS.get("assembly")
    name = account["name"]
    period_text = _period_text(days)
    status = bot.send_message(
        chat_id, f"🔄 *{name}* · {period_text} analiz ediliyor... (10-40 sn)",
        parse_mode="Markdown",
    )
    status_id = status.message_id

    posts, error = get_recent_posts(days, account["feeds"])

    if error == "feed_unreachable":
        bot.edit_message_text(
            f"⚠️ *{name}* için paylaşım kaynağına (RSS) ulaşılamıyor. "
            "Bu hesabın RSS adresi tanımlı olmayabilir ya da kaynak geçici kapalıdır.",
            chat_id, status_id, parse_mode="Markdown",
        )
        return

    analysis = analyze_posts(posts, period_text, name)
    header = f"📊 *{name} — {period_text} Stratejik Rapor*\n"
    if posts:
        header += f"_({len(posts)} paylaşım analiz edildi)_\n\n"
    else:
        header += "\n"

    result = header + analysis + DISCLAIMER
    safe_send(chat_id, result, edit_message_id=status_id)


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:  # noqa: BLE001
        pass
    data = call.data or ""
    if ":" in data:
        key, _, d = data.partition(":")
        days = int(d) if d.isdigit() else 1
    else:  # legacy inline buttons (bare day count) -> default account
        key, days = "assembly", (int(data) if data.isdigit() else 1)
    run_report(call.message.chat.id, key, days)


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

    # 3) Register slash commands so they show in Telegram's command menu.
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Menü ve butonları göster"),
            types.BotCommand("rapor", "Menü ve butonları göster"),
            types.BotCommand("diag", "Tanılama (ortam değişkenleri)"),
        ])
    except Exception as e:  # noqa: BLE001
        log.warning("Komut menüsü ayarlanamadı: %s", str(e)[:160])

    log.info("🚀 The Assembly Grok AI Botu BAŞLATILDI (%d RSS kaynağı yapılandırıldı)", len(RSS_URLS))
    # skip_pending: ignore the backlog accrued while the bot was offline.
    # If a second instance runs the same token, Telegram returns 409 — that is
    # now logged (above) instead of being invisible.
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
