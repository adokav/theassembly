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
from telebot.apihelper import ApiTelegramException
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

# Bump when shipping notable changes so /diag confirms which build is live.
BUILD_TAG = "2026-06-03 webhook"

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
# Use a realistic browser UA: services like rss.app block generic "bot" agents
# (they return 403), which surfaced as "RSS'e ulaşılamıyor".
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
                    "Accept-Language": "tr,en;q=0.8",
                    "Cache-Control": "no-cache",
                },
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


def _sma(values, n):
    if not values:
        return None
    window = values[-n:]
    return sum(window) / len(window)


def _rsi(closes, period=14):
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


def _atr(highs, lows, closes, period=14):
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


def fetch_quote(ticker):
    """Live market data + technicals from Yahoo Finance chart JSON (no heavy deps).
    Returns current price, 50/200d MA, 52w hi/lo, RSI, ATR, volume trend and the
    (ts, close) pairs used for the since-the-call comparison."""
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
        q = (res.get("indicators", {}).get("quote", [{}]) or [{}])[0]
        closes_raw = q.get("close", []) or []
        highs_raw = q.get("high", []) or []
        lows_raw = q.get("low", []) or []
        vols_raw = q.get("volume", []) or []
        # Keep only rows with a valid close, aligning the other series.
        rows = [
            (t, c, h, l, v)
            for t, c, h, l, v in zip(ts, closes_raw, highs_raw, lows_raw, vols_raw)
            if c is not None
        ]
        if not rows:
            return None
        pairs = [(t, c) for t, c, *_ in rows]
        closes = [c for _, c, *_ in rows]
        highs = [h if h is not None else c for _, c, h, l, v in rows]
        lows = [l if l is not None else c for _, c, h, l, v in rows]
        vols = [v for *_, v in rows if v is not None]

        current = meta.get("regularMarketPrice") or closes[-1]
        vol_trend = None
        if len(vols) >= 30:
            recent = sum(vols[-10:]) / 10
            base = sum(vols[-40:-10]) / 30
            if base > 0:
                vol_trend = "artıyor" if recent > base * 1.15 else (
                    "azalıyor" if recent < base * 0.85 else "yatay")
        return {
            "current": current,
            "ma50": _sma(closes, 50),
            "ma200": _sma(closes, 200),
            "high52": max(closes), "low52": min(closes),
            "rsi": _rsi(closes), "atr": _atr(highs, lows, closes),
            "vol_trend": vol_trend,
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


def _pct_since(pairs, current, date_str):
    entry = _price_on_or_before(pairs, date_str)
    if entry and entry > 0:
        return ((current - entry) / entry) * 100
    return None


def enrich_recommendations(recs):
    """Attach live market context + technicals to each recommendation, plus an
    alpha (excess return vs the S&P 500) since the call date."""
    bench = fetch_quote("SPY")  # benchmark fetched once and reused
    for r in recs[:10]:
        tk = (r.get("ticker") or "").strip().upper()
        if not tk:
            r["market"] = None
            continue
        q = fetch_quote(tk)
        if not q:
            r["market"] = None
            continue
        date = r.get("date", "")
        entry = _price_on_or_before(q["pairs"], date) or r.get("entry_price")
        cur = q["current"]
        chg = ((cur - entry) / entry * 100) if entry else None
        # Alpha: stock return minus the index return over the same window.
        alpha = None
        bench_pct = None
        if bench and chg is not None:
            bench_pct = _pct_since(bench["pairs"], bench["current"], date)
            if bench_pct is not None:
                alpha = chg - bench_pct

        def _r(x, d=2):
            return round(x, d) if x is not None else None

        r["market"] = {
            "current": _r(cur),
            "entry_then": _r(entry) if entry else None,
            "pct_change": _r(chg, 1),
            "ma50": _r(q.get("ma50")),
            "ma200": _r(q.get("ma200")),
            "high52": _r(q.get("high52")),
            "low52": _r(q.get("low52")),
            "rsi": _r(q.get("rsi"), 0),
            "atr": _r(q.get("atr")),
            "vol_trend": q.get("vol_trend"),
            "sp500_pct": _r(bench_pct, 1),
            "alpha_vs_sp500": _r(alpha, 1),
            "currency": q.get("currency"),
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

Telegram'da okunacak, MOBİL DOSTU, taranabilir bir rapor yaz. Kısa satırlar, net,
abartısız. TAM olarak şu yapıda:

⚡ *ÖZET*
<2 cümle: dönemin genel tonu + bugün en cazip fırsat hangisi>

📋 *Tablo*
Her tavsiye için tek satır (alınabilirliğe göre sırala: önce 🟢, sonra 🟡, en sonda 🔴):
🟢/🟡/🔴 <ticker> — <pct_change>% (S&P'ye karşı <alpha_vs_sp500> puan)

———
Sonra her tavsiye için bir KART (yine 🟢→🟡→🔴 sırasıyla):

*<emoji> <ticker> · <asset>*
💬 _Tez:_ <thesis tek cümle>
📅 _Tavsiye:_ <date> — <action>
📈 _Fiyat:_ ~<entry_then> → *<current>* (<pct_change>%); S&P'ye karşı <alpha_vs_sp500> puan
📊 _Teknik:_ RSI <rsi> · 50G <ma50> · 200G <ma200> · 52H <low52>–<high52> · hacim <vol_trend>
🎯 _Plan:_ Giriş <bölge> · Stop <seviye> · Hedef <seviye> → ~<R>R   (ya da net "bekle")
🚦 _Karar:_ <🟢 Hâlâ alınır / 🟡 Geri çekilmede / 🔴 Geç kalındı> — <tek cümle gerekçe>

Kurallar:
- Stop'u VERİYE dayandır: stop ≈ giriş − (1.5 × atr). Hedef ile giriş/stop'tan R-katsayısını (ödül/risk) hesapla.
- RSI > 70 aşırı alım (🟢 verme, geri çekilme bekle); RSI < 35 + tez sağlam = fırsat.
- Fiyat 200G ortalamanın altındaysa trend zayıf, dikkat et.
- alpha_vs_sp500 negatifse "endeksin gerisinde" diye belirt; pozitifse güçlü.
- Karar mantığı: fiyat girişe yakın/altında, RSI aşırı değil, tez sağlam ve trend yukarı ise 🟢;
  bir miktar kaçmış ama makulse 🟡; hedefi aşmış, 52H zirveye yapışmış, RSI>75 ya da tez bozuksa 🔴.
- Bir değer null ise o metriği yazma; market alanı null ise kartta sadece "ℹ️ Fiyat verisi alınamadı" yaz.
- UYDURMA: yalnızca verilen sayıları kullan; R ve stop dışında yeni sayı türetme.

———
✅ *BUGÜN NE YAPMALI*
<sadece aksiyon: 1-3 madde, ör. "• NVDA: 950 altı topla, stop 900" / "• TSLA: bekle">"""
    return _llm_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2200, temperature=0.4,
    )


def verify_report(report, recs):
    """Self-critique pass: cross-check every number in the report against the
    structured market data, fix mismatches, drop invented figures. Returns the
    corrected report, or the original if the check fails."""
    facts = json.dumps(
        [{"ticker": r.get("ticker"), "asset": r.get("asset"),
          "date": r.get("date"), "market": r.get("market")} for r in recs],
        ensure_ascii=False,
    )
    system = ("Sen titiz bir finansal düzeltmensin. Görevin: rapordaki HER sayı ve "
              "tarihi verilen GERÇEK verilerle karşılaştırmak. Veride olmayan ya da "
              "uyuşmayan her rakamı düzelt veya çıkar. Biçimi ve dili aynen koru. "
              "Sadece düzeltilmiş raporu döndür, açıklama ekleme.")
    user = f"""GERÇEK VERİ (doğru kabul et):
{facts}

DENETLENECEK RAPOR:
{report}

Kurallar:
- Fiyat/yüzde/RSI/ortalama/52H/alpha gibi değerler GERÇEK VERİ ile birebir uyuşmalı.
- Stop/Hedef/R hesapları mantıklı kalsın (giriş/atr'den türetilmiş); uydurma fiyat ekleme.
- Veride olmayan bir varlık/sayı varsa çıkar.
- Düzeltme gerekmiyorsa raporu aynen geri ver.
Düzeltilmiş raporu döndür:"""
    try:
        out = _llm_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=2200, temperature=0.1,
        )
        return out.strip() if out and out.strip() else report
    except Exception as e:  # noqa: BLE001
        log.warning("Öz-denetim turu atlandı: %s", str(e)[:160])
        return report


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
            report = build_report_with_market(recs, period_text, account_name)
            return verify_report(report, recs)  # self-critique / anti-hallucination
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
    lines.append("")
    lines.append(f"👥 *Takip edilen hesaplar ({len(ACCOUNTS)})* — build: {BUILD_TAG}")
    for key, a in ACCOUNTS.items():
        lines.append(f"• {a['name']} — {len(a['feeds'])} feed")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=["feedtest"])
def send_feedtest(message):
    """Live-fetch every account's feed and report the raw HTTP result so we can
    see *why* a feed fails (403 block, empty, parse error, ...) from Telegram."""
    bot.send_message(message.chat.id, "🔬 Feed'ler test ediliyor...")
    lines = ["🔬 *Feed Testi*", ""]
    for key, a in ACCOUNTS.items():
        lines.append(f"*{a['name']}*")
        if not a["feeds"]:
            lines.append("• ❌ Tanımlı feed yok")
            lines.append("")
            continue
        for url in a["feeds"]:
            short = url if len(url) < 48 else url[:45] + "…"
            try:
                resp = requests.get(
                    url, timeout=HTTP_TIMEOUT,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
                        "Accept-Language": "tr,en;q=0.8",
                    },
                )
                ctype = resp.headers.get("Content-Type", "?").split(";")[0]
                parsed = feedparser.parse(resp.content)
                n = len(parsed.entries)
                mark = "✅" if (resp.status_code == 200 and n > 0) else "⚠️"
                lines.append(f"• {mark} HTTP {resp.status_code} · {ctype} · {len(resp.content)}B · {n} kayıt")
                if resp.status_code != 200 or n == 0:
                    snippet = resp.text[:120].replace("\n", " ").strip()
                    lines.append(f"  ↳ `{snippet}`")
            except Exception as e:  # noqa: BLE001
                lines.append(f"• ❌ Hata: {str(e)[:100]}")
            lines.append(f"  _{short}_")
        lines.append("")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")
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


WEBHOOK_PATH = f"/webhook/{TOKEN}" if TOKEN else "/webhook"


class _BotHTTPHandler(BaseHTTPRequestHandler):
    """Serves the platform health check (GET) and, in webhook mode, receives
    Telegram updates (POST). Each inbound POST also wakes a sleeping free-tier
    Render service — which is exactly why webhooks beat polling here."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        if self.path != WEBHOOK_PATH:
            self.send_response(403)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
        except Exception:  # noqa: BLE001
            self.send_response(400)
            self.end_headers()
            return
        # Acknowledge Telegram immediately, then dispatch to handlers (threaded).
        self.send_response(200)
        self.end_headers()
        try:
            update = telebot.types.Update.de_json(body)
            bot.process_new_updates([update])
        except Exception as e:  # noqa: BLE001
            log.warning("Webhook update işlenemedi: %s", str(e)[:160])

    def log_message(self, *args):  # silence per-request logging
        pass


def run_webhook(external_url, port):
    """Webhook mode: Telegram POSTs each update to our public URL. Best fit for a
    free Render Web Service, which sleeps without inbound HTTP — every message
    now wakes it. No user configuration needed (RENDER_EXTERNAL_URL is built-in)."""
    url = external_url.rstrip("/") + WEBHOOK_PATH
    server = HTTPServer(("0.0.0.0", int(port)), _BotHTTPHandler)
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=url, drop_pending_updates=True)
        log.info("🚀 Bot WEBHOOK modunda BAŞLADI → %s (%d hesap)", url, len(ACCOUNTS))
    except Exception as e:  # noqa: BLE001
        log.error("Webhook ayarlanamadı: %s", str(e)[:200])
        raise
    server.serve_forever()


def run_polling():
    """Polling mode for local/dev (no public URL). Resilient to 409 Conflict."""
    try:
        bot.remove_webhook()
        log.info("Webhook temizlendi; polling moduna geçiliyor.")
    except Exception as e:  # noqa: BLE001
        log.warning("Webhook temizlenemedi: %s", str(e)[:160])
    log.info("🚀 Bot POLLING modunda BAŞLADI (%d hesap)", len(ACCOUNTS))
    backoff = 5
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
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


if __name__ == "__main__":
    telebot.logger.setLevel(logging.INFO)

    # 1) Verify the token & network reach Telegram. Fail fast with a clear message.
    try:
        me = bot.get_me()
        log.info("Telegram bağlantısı OK → @%s (id=%s)", me.username, me.id)
    except Exception as e:  # noqa: BLE001
        log.error(
            "Telegram'a bağlanılamadı. TELEGRAM_TOKEN yanlış olabilir ya da ağ "
            "api.telegram.org'a çıkamıyor. Hata: %s", str(e)[:200],
        )
        sys.exit(1)

    # 2) Register slash commands so they show in Telegram's command menu.
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Menü ve butonları göster"),
            types.BotCommand("rapor", "Menü ve butonları göster"),
            types.BotCommand("diag", "Tanılama (ortam değişkenleri)"),
            types.BotCommand("feedtest", "RSS feed'lerini canlı test et"),
        ])
    except Exception as e:  # noqa: BLE001
        log.warning("Komut menüsü ayarlanamadı: %s", str(e)[:160])

    # 3) Webhook when a public URL is available (Render Web Service), else polling.
    _external = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL") or "").strip()
    _port = os.getenv("PORT")
    if _external and _port:
        run_webhook(_external, _port)
    else:
        run_polling()
