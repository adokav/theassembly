import os
import re
import sys
import json
import time
import logging
import calendar
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from urllib.parse import quote_plus

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
BUILD_TAG = "2026-06-10 sector-real"

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
    ("whale", "Whale Receipts", "WhaleReceipts", "WHALE_RSS_URL",
     "https://rss.app/feeds/y1A7Zf5WQbQL24xm.xml"),
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

# Colored badge per account, used to attribute each recommendation in the
# combined report (first account 🟦, second 🟧, ...).
_BADGE_POOL = ["🟦", "🟧", "🟩", "🟥", "🟪", "🟨"]
ACCOUNT_BADGES = {key: _BADGE_POOL[i % len(_BADGE_POOL)]
                  for i, key in enumerate(ACCOUNTS)}

# Maximum Telegram message length is 4096; leave headroom for headers/markup.
TELEGRAM_LIMIT = 3900
# Turkey is permanently UTC+3 (no DST since 2016); used to show post times in TSİ.
TR_OFFSET = timedelta(hours=3)
HTTP_TIMEOUT = 20
HTTP_RETRIES = 4
# LLM input budget (free tiers like Groq cap tokens-per-minute). Roughly
# 1 token ≈ 3-4 chars, so ~14k chars keeps the extraction request well under a
# 12k TPM ceiling while leaving room for the report + verify passes.
MAX_POST_CHARS = 700
EXTRACT_CHAR_BUDGET = 14000
# Per-stock news (best-effort, single quick attempt so it never blocks a report).
NEWS_TIMEOUT = 10
NEWS_MAX_ITEMS = 2
NEWS_LOOKBACK_DAYS = 14
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
# Bounded timeout + a single retry so a slow/hung LLM call fails fast and
# visibly instead of leaving the user on an endless "analiz ediliyor".
client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, timeout=40.0, max_retries=1)


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
    """Pass 1: pull concrete, grounded recommendations as structured JSON.

    Free LLM tiers (e.g. Groq) cap tokens-per-minute, so a week of long-form
    posts can exceed the per-request limit (HTTP 413 'Request too large'). We
    cap each post's length and the total input size, then shrink-and-retry if
    the provider still rejects the request as too large."""
    system = ("Sen bir finansal metin çıkarım motorusun. SADECE geçerli JSON döndür. "
              "Paylaşımlarda olmayan hiçbir varlık, fiyat veya tarih uydurma.")

    def build_user(items):
        text = "\n\n".join(
            f"[{i+1}] Tarih: {p['date']}\nBaşlık: {p['title']}\n"
            f"İçerik: {(p['text'] or '')[:MAX_POST_CHARS]}"
            for i, p in enumerate(items)
        )
        return f"""Aşağıdaki paylaşımlardan SOMUT hisse/varlık tavsiyelerini çıkar.
Sadece ABD/global borsalarda işlem gören hisseler için Yahoo Finance sembolü ver
(ör. Apple -> AAPL, Nvidia -> NVDA). Net sembol çıkaramıyorsan o kaydı atla.

JSON şeması:
{{"recommendations": [
  {{"asset": "şirket adı", "ticker": "AAPL", "action": "al|sat|izle",
    "date": "GG.AA.YYYY", "entry_price": null, "target": null,
    "conviction": "yüksek|orta|düşük", "sector": "şirketin SPESİFİK alt sektörü/endüstrisi (Türkçe). Genel 'Teknoloji/Finans/Sağlık' YAZMA; mümkün olan en dar niş: ör. NVDA->'Yarı iletken (GPU)', MSFT->'Bulut & kurumsal yazılım', JPM->'Yatırım bankacılığı', XOM->'Petrol & gaz (entegre)', LLY->'Biyofarma/ilaç', TSLA->'Elektrikli araç üreticisi', V->'Ödeme sistemleri'",
    "thesis": "kısa gerekçe", "source_quote": "ilgili alıntı"}}
]}}

Paylaşımlar:
{text}

Yalnızca JSON döndür."""

    # Trim to a character budget so the request stays under the TPM limit.
    items, total = [], 0
    for p in posts:
        approx = min(len(p.get("text") or ""), MAX_POST_CHARS) + 80
        if items and total + approx > EXTRACT_CHAR_BUDGET:
            break
        items.append(p)
        total += approx
    if len(items) < len(posts):
        log.info("Extraction bütçesi: %d/%d paylaşım gönderiliyor", len(items), len(posts))

    raw = None
    while items:
        try:
            raw = _llm_chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": build_user(items)}],
                max_tokens=1500, temperature=0.2, json_mode=True,
            )
            break
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            too_big = "413" in msg or "too large" in msg or "tpm" in msg or "tokens per minute" in msg
            if too_big and len(items) > 3:
                items = items[: max(3, len(items) // 2)]
                log.warning("LLM isteği çok büyük; paylaşım sayısı %d'e düşürülüp tekrar deneniyor", len(items))
                continue
            raise
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
    alpha (excess return vs the S&P 500) since the call date. All price fetches
    run in parallel so the report stays fast even with several tickers."""
    targets = recs[:12]
    tickers = {(r.get("ticker") or "").strip().upper() for r in targets}
    tickers.discard("")
    tickers.add("SPY")  # benchmark
    quotes = {}
    sectors = {}
    if tickers:
        stock_tickers = [tk for tk in tickers if tk != "SPY"]
        with ThreadPoolExecutor(max_workers=min(10, len(tickers) + len(stock_tickers))) as ex:
            qf = {ex.submit(fetch_quote, tk): tk for tk in tickers}
            sf = {ex.submit(fetch_sector, tk): tk for tk in stock_tickers}
            for fut in qf:
                try:
                    quotes[qf[fut]] = fut.result()
                except Exception:  # noqa: BLE001
                    quotes[qf[fut]] = None
            for fut in sf:
                try:
                    sectors[sf[fut]] = fut.result()
                except Exception:  # noqa: BLE001
                    sectors[sf[fut]] = None
    bench = quotes.get("SPY")
    for r in targets:
        tk = (r.get("ticker") or "").strip().upper()
        # Prefer the real sector from Yahoo; fall back to the LLM's guess.
        if tk and sectors.get(tk):
            r["sector"] = sectors[tk]
        if not tk:
            r["market"] = None
            continue
        q = quotes.get(tk)
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


def fetch_sector(ticker):
    """Real industry/sector from Yahoo's search endpoint (no crumb required).
    Returns the most specific label available (English) or None. Best-effort:
    a single quick attempt, used to OVERRIDE the LLM's guessed sector which can
    be wrong for small/less-known names (e.g. TMDX is medical devices, not
    semiconductors)."""
    url = (f"https://query1.finance.yahoo.com/v1/finance/search"
           f"?q={quote_plus(ticker)}&quotesCount=5&newsCount=0&listsCount=0")
    try:
        resp = requests.get(url, timeout=NEWS_TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Sektör alınamadı (%s): %s", ticker, str(e)[:120])
        return None
    quotes = data.get("quotes") or []
    tk = ticker.strip().upper()
    match = next((q for q in quotes if (q.get("symbol") or "").upper() == tk), None)
    if match is None:
        match = next((q for q in quotes if q.get("quoteType") == "EQUITY"), None)
    if not match:
        return None
    return (match.get("industryDisp") or match.get("industry")
            or match.get("sectorDisp") or match.get("sector") or None)


def fetch_news(asset, ticker):
    """Best-effort: most recent headlines for a stock via Google News RSS.
    Single quick attempt; returns a short list of {title, date} or [] on any
    failure (news must never block or slow down the report meaningfully)."""
    query = quote_plus(f"{asset} {ticker} stock")
    url = (f"https://news.google.com/rss/search?q={query}"
           "&hl=en-US&gl=US&ceid=US:en")
    try:
        resp = requests.get(url, timeout=NEWS_TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as e:  # noqa: BLE001
        log.warning("Haber alınamadı (%s): %s", ticker, str(e)[:120])
        return []
    cutoff = datetime.utcnow() - timedelta(days=NEWS_LOOKBACK_DAYS)
    out = []
    for entry in parsed.entries:
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        when = datetime.utcfromtimestamp(calendar.timegm(pub)) if pub else None
        if when and when < cutoff:
            continue
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        # Google News appends " - Source"; keep it, it's useful provenance.
        out.append({"title": title[:160],
                    "date": (when + TR_OFFSET).strftime("%d.%m.%Y") if when else ""})
        if len(out) >= NEWS_MAX_ITEMS:
            break
    return out


def enrich_news(items):
    """Attach recent headlines to each item in parallel (best-effort)."""
    targets = items[:12]
    if not targets:
        return items
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
        futs = {ex.submit(fetch_news, it.get("asset") or it["ticker"], it["ticker"]): it
                for it in targets}
        for fut in futs:
            try:
                futs[fut]["news"] = fut.result()
            except Exception:  # noqa: BLE001
                futs[fut]["news"] = []
    return items


def _earliest_date(dates):
    """Earliest 'GG.AA.YYYY[ HH:MM]' string in a list (for 'since the call' math)."""
    best, best_s = None, ""
    for s in dates:
        try:
            d = datetime.strptime((s or "").split()[0], "%d.%m.%Y")
        except Exception:  # noqa: BLE001
            continue
        if best is None or d < best:
            best, best_s = d, s
    return best_s or (dates[0] if dates else "")


def merge_by_ticker(per_account):
    """Collapse per-account recommendations into one item per ticker, tagging the
    source account(s) and classifying each as consensus / divergence / single.

    per_account: list of (account_key, account_name, [recs]).
    Returns a list of merged items sorted so the report leads with consensus.
    """
    groups = {}
    for key, name, recs in per_account:
        for r in recs:
            tk = (r.get("ticker") or "").strip().upper()
            if not tk:
                continue
            g = groups.setdefault(tk, {
                "ticker": tk, "asset": r.get("asset") or tk,
                "sector": None, "entry_price": None, "_dates": [], "sources": [],
            })
            if (g["asset"] == tk) and r.get("asset"):
                g["asset"] = r["asset"]
            if not g["sector"] and r.get("sector"):
                g["sector"] = r["sector"]
            if g["entry_price"] is None and r.get("entry_price"):
                g["entry_price"] = r["entry_price"]
            if r.get("date"):
                g["_dates"].append(r["date"])
            g["sources"].append({
                "account_key": key, "account": name,
                "badge": ACCOUNT_BADGES.get(key, "•"),
                "action": (r.get("action") or "izle").lower().strip(),
                "conviction": r.get("conviction"),
                "thesis": r.get("thesis"),
                "date": r.get("date"),
            })

    items = []
    for g in groups.values():
        g["date"] = _earliest_date(g.pop("_dates"))
        accounts = {s["account_key"] for s in g["sources"]}
        actions = {s["action"] for s in g["sources"]}
        if len(accounts) >= 2:
            g["kind"] = "consensus" if len(actions) == 1 else "divergence"
        else:
            g["kind"] = "single"
        items.append(g)

    kind_rank = {"consensus": 0, "divergence": 1, "single": 2}
    conv_rank = {"yüksek": 0, "orta": 1, "düşük": 2}

    def sort_key(g):
        best_conv = min((conv_rank.get((s.get("conviction") or "").lower(), 3)
                         for s in g["sources"]), default=3)
        return (kind_rank[g["kind"]], best_conv, g["ticker"])

    items.sort(key=sort_key)
    return items


def build_combined_report(items, period_text, counts):
    """Pass 2 (combined): a Wall-Street-analyst-style report across both accounts.
    Consensus picks lead, then disagreements, then single-source ideas."""
    accounts_meta = [
        {"name": a["name"], "badge": ACCOUNT_BADGES[k], "post_count": counts.get(k, 0)}
        for k, a in ACCOUNTS.items()
    ]
    payload = json.dumps({
        "period": period_text,
        "accounts": accounts_meta,
        "consensus": [i for i in items if i["kind"] == "consensus"],
        "divergence": [i for i in items if i["kind"] == "divergence"],
        "singles": [i for i in items if i["kind"] == "single"],
    }, ensure_ascii=False, default=str)

    system = (
        "Sen deneyimli bir Wall Street sell-side analistisin. Türkçe, net, ölçülü "
        "yazarsın. Takip edilen X hesaplarının önerilerini bağımsız bir analist "
        "gözüyle değerlendirirsin: kanaat, teknik kurulum, risk/ödül ve katalizör. "
        "SADECE sana verilen sayıları kullan; fiyat/teknik/tarih UYDURMA. market "
        "alanı null ise o varlık için fiyat/teknik yorumu yapma."
    )
    user = f"""Aşağıda takip edilen X hesaplarının {period_text} içindeki hisse önerileri,
kaynak etiketleriyle ve güncel piyasa verisiyle (JSON) birlikte verildi:

{payload}

Telegram'da okunacak, MOBİL DOSTU, taranabilir TEK bir rapor yaz. Rozetleri aynen
kullan (her hesabın badge'i payload'da). Her öneriyi bir analist gibi değerlendir ve
şu KARAR ölçeğinden birini ver: *GÜÇLÜ AL* / *AL* / *TUT* / *SAT*.

TAM olarak şu yapıda yaz:

📊 *YÖNETİCİ ÖZETİ*
<2-3 cümle: dönemin genel tonu, en yüksek kanaatli fikir, dikkat çeken risk>

━━━ 🤝 *ORTAK GÖRÜŞLER (KONSENSÜS)* ━━━
(consensus listesi; her biri için KART. Liste boşsa bu bölümü "• Bu dönemde hesapların ortak önerisi yok." yaz.)

━━━ ⚖️ *GÖRÜŞ AYRILIĞI* ━━━
(divergence listesi; aynı hisseye zıt görüş. Boşsa bu başlığı tamamen atla.)

━━━ 📌 *TEKİL ÖNERİLER* ━━━
(singles listesi; KART. Boşsa başlığı atla.)

━━━ 🧭 *ANALİST GÖRÜŞÜ* ━━━
<3-4 cümle sentez: konsensüs fikirler neden öne çıkıyor, hangi ayrışmada kim daha
ikna edici, dönemin net çıkarımı ve pratik aksiyon.>

Her KART formatı:
*<badge(ler)> <ticker> · <asset>*
🏷️ _Sektör:_ <sector>
👥 _Kaynak:_ <her kaynak için "badge Hesap Adı: AKSİYON"; ortak ise "Her iki hesap da: AKSİYON">
📈 _Fiyat:_ ~<entry_then> → *<current> <currency>* (<pct_change>%); S&P'ye karşı <alpha_vs_sp500> puan
📊 _Teknik:_ RSI <rsi> · 50G <ma50> · 200G <ma200> · 52H <low52>–<high52> · hacim <vol_trend>
📰 _Haber:_ <news listesindeki en önemli 1 başlığı Türkçe, kısa özetle + (tarih). news boşsa bu satırı YAZMA.>
💬 _Tez:_ <kaynakların tezini 1 cümlede sentezle>
🧠 _Analist görüşü:_ <1-2 cümle: kurulum + risk/ödül; konsensüste "iki bağımsız kaynağın da aynı yönde olması kanaati güçlendiriyor" vurgusu; ayrışmada hangisi daha sağlam. Varsa haberin karara etkisini de belirt.>
🎯 _Karar:_ *<GÜÇLÜ AL / AL / TUT / SAT>* — <tek cümle gerekçe>

Kurallar:
- Karar ölçeği: konsensüs + teknik destek + makul RSI → GÜÇLÜ AL eğilimi. Tek kaynak ama
  sağlam kurulum → AL. Fiyat çok kaçmış / RSI>75 / 52H zirvede → TUT. Tez bozulmuş, trend
  aşağı, endeks gerisinde belirgin → SAT.
- RSI>70 aşırı alım (GÜÇLÜ AL verme); fiyat 200G altındaysa trend zayıf; alpha negatifse
  "endeksin gerisinde", pozitifse "endeksi yendi" de.
- Sektör (sector) null ise 🏷️ satırını yazma. Sektör İngilizce geldiyse (ör.
  "Medical Devices", "Semiconductors") Türkçeye çevirerek yaz ("Medikal Cihazlar",
  "Yarı İletkenler"); anlamı KORU, kategoriyi değiştirme.
- 📰 Haber satırı: SADECE verilen news başlıklarını kullan, haber UYDURMA. Başlık İngilizce
  ise anlamını Türkçe ver. En güncel/önemli 1 başlık yeterli.
- Bir değer null ise o metriği yazma; market null ise kartta sadece
  "ℹ️ Fiyat verisi alınamadı" yaz ve kararı tez/kanaat üzerinden ver.
- UYDURMA: yalnızca verilen sayıları kullan, yeni fiyat türetme.
- Kısa satırlar, abartısız, profesyonel bir analist tonu."""
    return _llm_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2600, temperature=0.4,
    )


def verify_report(report, recs):
    """Self-critique pass: cross-check every number in the report against the
    structured market data, fix mismatches, drop invented figures. Returns the
    corrected report, or the original if the check fails."""
    facts = json.dumps(
        [{"ticker": r.get("ticker"), "asset": r.get("asset"),
          "date": r.get("date"), "sector": r.get("sector"),
          "news": r.get("news"), "market": r.get("market")} for r in recs],
        ensure_ascii=False,
    )
    system = ("Sen titiz bir finansal düzeltmensin. Görevin: rapordaki HER sayı ve "
              "tarihi verilen GERÇEK verilerle karşılaştırmak. Veride olmayan ya da "
              "uyuşmayan her rakamı düzelt veya çıkar. Sektör ve haber satırları veride "
              "varsa KORU. Biçimi ve dili aynen koru. "
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


def _llm_error_message(e):
    """Map a provider exception to a friendly Turkish message."""
    msg = str(e)
    low = msg.lower()
    if any(k in low for k in ("403", "credit", "permission", "quota", "insufficient")):
        return (
            f"❌ Yapay zeka sağlayıcısı ({LLM_PROVIDER}) isteği reddetti: kredi/limit "
            f"veya yetki sorunu görünüyor. API anahtarının doğru ve bakiyenin yeterli "
            f"olduğundan emin olun.\n\nDetay: {msg[:200]}"
        )
    if any(k in low for k in ("rate limit", "429", "tpm", "tokens per minute")):
        return (
            "❌ Yapay zeka sağlayıcısı dakikalık limiti aştı (rate limit). Lütfen kısa "
            "dönem seçin ya da bir dakika sonra tekrar deneyin."
        )
    return f"❌ Yapay zeka analiz hatası ({LLM_PROVIDER}/{LLM_MODEL}): {msg[:200]}"


def macro_strategic_analysis(posts, period_text, accounts_label):
    """Macro & strategic read across all tracked accounts' posts — beyond single
    stock picks: market regime, institutional positioning / insider flows, sector
    rotation, key risks/catalysts and a strategic takeaway. Grounded in the posts
    only (no fabricated macro data)."""
    text = "\n\n".join(
        f"[{i+1}] Tarih: {p['date']}\nBaşlık: {p['title']}\nİçerik: {p['text']}"
        for i, p in enumerate(posts)
    )
    system = (
        "Sen kıdemli bir makro stratejist ve akış/positioning (kurumsal konumlanma) "
        "analistisin. Türkçe, net ve abartısız yazarsın. ÇOK ÖNEMLİ: yalnızca verilen "
        "paylaşımlarda AÇIKÇA geçen bilgiye dayan; makro veri, rakam, isim ya da olay "
        "UYDURMA. Bir başlık için paylaşımlarda dayanak yoksa o başlığı atla."
    )
    user = f"""Takip edilen X hesaplarının ({accounts_label}) **{period_text}** paylaşımları
aşağıda. Tek tek hisse önerilerinin ÖTESİNDE bir MAKRO & STRATEJİK görünüm çıkar.

{text}

Telegram'da okunacak, mobil dostu, KISA ve taranabilir yaz. Şu yapıda:

━━━ 🌍 *MAKRO & STRATEJİK GÖRÜNÜM* ━━━
🌡️ _Piyasa Rejimi:_ <risk-on / risk-off / nötr — tek cümle gerekçe>
🏦 _Kurumsal Konumlanma & Akışlar:_ <insider/kurumsal alım-satım, büyük pozisyonlar; paylaşımlardaki somut akışlara dayan>
🔄 _Sektör / Tema Rotasyonu:_ <öne çıkan sektör/temalar, paraya giriş-çıkış>
⚠️ _Riskler & Katalizörler:_ <2-4 madde: yalnızca paylaşımlarda geçen makro veri/olaylar>
🧭 _Stratejik Çıkarım:_ <1-2 cümle: bu görünümde genel nasıl konumlanılmalı (tek hisse değil)>

Kurallar:
- Yalnızca paylaşımlarda geçen olgulara dayan; dışarıdan veri/rakam ekleme.
- Dayanak yoksa ilgili satırı "—" ile geç.
- Kesin tahmin yerine olasılık dilini kullan."""
    return _llm_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1200, temperature=0.4,
    )


def _safe_macro(posts, period_text, accounts_label):
    """Run the macro pass defensively; never let it break the main report."""
    if not posts:
        return ""
    try:
        section = (macro_strategic_analysis(posts, period_text, accounts_label) or "").strip()
        return f"\n\n{section}" if section else ""
    except Exception as e:  # noqa: BLE001 - macro is additive; degrade gracefully
        log.warning("Makro analiz turu atlandı: %s", str(e)[:160])
        return ""


def analyze_combined(days, period_text, notify=None):
    """Combined pipeline across all tracked accounts:
    fetch (parallel) -> extract per account (attributed) -> merge & classify
    (consensus/divergence/single) -> live market enrichment -> Wall-Street-style
    report -> self-critique. Returns a result dict (see run_report) or None when
    no posts could be fetched at all.
    """
    def step(msg):
        if notify:
            try:
                notify(msg)
            except Exception:  # noqa: BLE001
                pass

    # 1) Fetch every account's posts in parallel.
    step(f"📥 Paylaşımlar çekiliyor ({len(ACCOUNTS)} hesap)...")
    fetched = {}
    with ThreadPoolExecutor(max_workers=max(1, len(ACCOUNTS))) as ex:
        futs = {ex.submit(get_recent_posts, days, a["feeds"]): key
                for key, a in ACCOUNTS.items()}
        for fut in futs:
            key = futs[fut]
            try:
                fetched[key] = fut.result()
            except Exception as e:  # noqa: BLE001
                log.warning("Fetch hatası (%s): %s", key, str(e)[:160])
                fetched[key] = ([], "feed_unreachable")

    counts = {key: len(fetched[key][0]) for key in ACCOUNTS}
    if sum(counts.values()) == 0:
        return None  # nothing fetched -> caller shows a 'no posts' notice

    # 2) Extract recommendations per account (keeps attribution + smaller requests).
    per_account = []
    for key, a in ACCOUNTS.items():
        posts = fetched[key][0]
        if not posts:
            per_account.append((key, a["name"], []))
            continue
        step(f"🧠 {a['name']}: {len(posts)} paylaşım ayıklanıyor...")
        try:
            recs = extract_recommendations(posts)
        except Exception as e:  # noqa: BLE001 - one account failing shouldn't sink the report
            log.warning("Çıkarım hatası (%s): %s", key, str(e)[:160])
            recs = []
        per_account.append((key, a["name"], recs))

    # 3) Merge by ticker + classify.
    items = merge_by_ticker(per_account)
    if not items:
        return {"empty": True, "counts": counts}

    # 4) Live market enrichment (parallel quotes for unique tickers).
    step(f"📈 {len(items)} hisse için canlı fiyatlar alınıyor...")
    enrich_recommendations(items)

    # 4b) Recent per-stock headlines (best-effort, parallel).
    step("📰 Güncel haberler taranıyor...")
    enrich_news(items)

    # 5) Build the analyst report, then self-critique it.
    step("📝 Wall Street analisti raporu yazıyor...")
    report = build_combined_report(items, period_text, counts)
    step("🔍 Rapor doğrulanıyor (son kontrol)...")
    report = verify_report(report, items)

    # 6) Macro & strategic overlay across every account's posts (additive; the
    #    main report is never blocked if this pass fails).
    step("🌍 Makro & stratejik görünüm çıkarılıyor...")
    all_posts = [p for key in ACCOUNTS for p in fetched[key][0]]
    report += _safe_macro(all_posts, period_text, _account_names())
    return {"report": report, "counts": counts, "items": items}


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
# Period buttons (label -> days). The persistent keyboard now offers periods
# directly: one tap produces a single combined report across all accounts.
PERIOD_BUTTONS = {
    "📈 Son 1 Gün": 1,
    "📈 Son 2 Gün": 2,
    "📈 Son 3 Gün": 3,
    "📅 Son 1 Hafta": 7,
    "📅 Son 2 Hafta": 14,
    "📊 Geçtiğimiz Ay": 30,
}


def _period_keyboard():
    """Persistent keyboard of period buttons (two per row)."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    labels = list(PERIOD_BUTTONS)
    for i in range(0, len(labels), 2):
        kb.row(*labels[i:i + 2])
    return kb


def _account_names():
    return " + ".join(a["name"] for a in ACCOUNTS.values())


def show_menu(chat_id, title=None):
    if title is None:
        title = (f"🎯 *Stratejik Rapor Botu*\n\n{_account_names()} önerilerini tek raporda, "
                 "bir Wall Street analisti gözüyle birleştirir.\n\nHangi dönemi raporlayalım?")
    title += f"\n\n🤖 _Aktif AI: {LLM_PROVIDER} · {LLM_MODEL}_"
    bot.send_message(chat_id, title, reply_markup=_period_keyboard(), parse_mode="Markdown")


@bot.message_handler(commands=["start", "rapor"])
def send_menu(message):
    show_menu(message.chat.id)


@bot.message_handler(func=lambda m: m.text in PERIOD_BUTTONS)
def period_button_handler(message):
    run_report(message.chat.id, PERIOD_BUTTONS[message.text])


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


def _period_text(days):
    """Human-readable Turkish label for a look-back window (in days)."""
    if days <= 3:
        return f"Son {days} Gün"
    if days <= 14:
        return f"Son {days // 7} Hafta"
    return "Geçtiğimiz Ay"


def _post_counts_line(counts):
    return " · ".join(f"{ACCOUNTS[k]['name']}: {v} paylaşım" for k, v in counts.items())


def run_report(chat_id, days):
    """Fetch all accounts' posts, build the combined analyst report, deliver it."""
    period_text = _period_text(days)
    names = _account_names()
    log.info("Rapor isteği: %s · %s gün", names, days)
    status = bot.send_message(
        chat_id, f"🔄 *{names}* · {period_text}\nHazırlanıyor... (15-50 sn)",
        parse_mode="Markdown",
    )
    status_id = status.message_id

    def notify(msg):
        """Edit the status message so the user sees live progress / where it stalls."""
        bot.edit_message_text(
            f"🔄 *{names}* · {period_text}\n{msg}", chat_id, status_id,
            parse_mode="Markdown",
        )

    try:
        result = analyze_combined(days, period_text, notify=notify)

        if result is None:
            bot.edit_message_text(
                f"⚠️ *{period_text}* için paylaşım kaynaklarına ulaşılamadı ya da bu "
                "dönemde hiç paylaşım yok. Daha geniş bir dönem deneyin.",
                chat_id, status_id, parse_mode="Markdown",
            )
            return

        if result.get("empty"):
            bot.edit_message_text(
                f"📊 *{period_text}* — somut bir hisse önerisi tespit edilemedi.\n"
                f"_({_post_counts_line(result['counts'])} tarandı)_",
                chat_id, status_id, parse_mode="Markdown",
            )
            return

        header = (f"📊 *{period_text.upper()} STRATEJİ RAPORU*\n{names}\n"
                  f"_({_post_counts_line(result['counts'])})_\n\n")
        out = header + result["report"] + DISCLAIMER
        safe_send(chat_id, out, edit_message_id=status_id)
    except Exception as e:  # noqa: BLE001 - never fail silently
        log.exception("run_report hatası (%s gün)", days)
        friendly = _llm_error_message(e)
        try:
            bot.edit_message_text(friendly, chat_id, status_id)
        except Exception:  # noqa: BLE001
            bot.send_message(chat_id, friendly)


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    """Back-compat for any old inline period buttons still in chat history."""
    log.info("Callback alındı: %s", call.data)
    try:
        bot.answer_callback_query(call.id)
    except Exception:  # noqa: BLE001
        pass
    d = (call.data or "").split(":")[-1]
    days = int(d) if d.isdigit() else 7
    run_report(call.message.chat.id, days)


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
        bot.set_webhook(
            url=url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
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
