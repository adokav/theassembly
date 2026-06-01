import os
import time
import json
import feedparser
import telegram
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ====================== CONFIG ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RSS_URL = os.getenv("RSS_URL")
XAI_API_KEY = os.getenv("XAI_API_KEY")

CHECK_INTERVAL = 300          # Yeni post kontrolü (5 dakika)
PERIODIC_INTERVAL = 3600 * 6  # Periyodik rapor (6 saatte bir)

STATE_FILE = "state.json"

bot = telegram.Bot(token=TELEGRAM_TOKEN)

client = OpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1"
)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"last_guid": None, "last_periodic": 0}
    return {"last_guid": None, "last_periodic": 0}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def analyze_with_grok(text, is_periodic=False):
    try:
        system_prompt = """Sen deneyimli bir makro analist, piyasa yapısı uzmanı ve trading stratejistisin.
Türkçe, net, profesyonel ve aksiyon odaklı rapor yaz."""

        if is_periodic:
            user_prompt = f"""
Aşağıdaki The Assembly paylaşımlarını analiz et ve **stratejik rapor** hazırla:

{text}

Rapor Formatı:
• **Genel Değerlendirme**
• **Ana Trend / Tema**
• **Fırsatlar**
• **Riskler**
• **Stratejik Tavsiyeler** (pozisyon, zamanlama, risk yönetimi)
• **İzlenmesi Gerekenler**
"""
        else:
            user_prompt = f"""
Aşağıdaki tek tweet'i detaylı analiz et:

{text}

Rapor Formatı:
• **Ana Konu ve Önem Seviyesi**
• **Piyasa Etkisi**
• **Fırsat / Risk**
• **Trading / Yatırım Önerisi**
• **Stratejik Tavsiye**
"""

        response = client.chat.completions.create(
            model="grok-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.6,
            max_tokens=900
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"❌ Grok API hatası: {e}"

def check_new_posts(state):
    try:
        feed = feedparser.parse(RSS_URL)
        if not feed.entries:
            return state

        new_posts = []
        for entry in reversed(feed.entries):
            if state["last_guid"] and entry.id == state["last_guid"]:
                break
            new_posts.append(entry)

        for entry in new_posts:
            title = entry.title
            link = entry.link
            summary = entry.get('summary', '')[:1200]
            full_text = f"{title}\n\n{summary}"

            print(f"📊 Yeni post analiz ediliyor: {title[:70]}...")

            analysis = analyze_with_grok(full_text, is_periodic=False)

            message = f"🔔 **The Assembly - Grok AI Analiz**\n\n"
            message += f"**Orijinal İçerik:** {title}\n\n"
            message += f"{analysis}\n\n"
            message += f"🔗 {link}"

            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown', disable_web_page_preview=False)

            state["last_guid"] = entry.id
            save_state(state)
            time.sleep(7)

        return state
    except Exception as e:
        print(f"❌ Yeni post kontrol hatası: {e}")
        return state

def send_periodic_analysis(state):
    try:
        feed = feedparser.parse(RSS_URL)
        if not feed.entries:
            return

        # Son 30 gün içindeki postları al
        cutoff = datetime.now() - timedelta(days=30)
        recent_posts = []

        for entry in feed.entries:
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                pub_date = datetime(*entry.published_parsed[:6])
                if pub_date > cutoff:
                    recent_posts.append(entry)

        if len(recent_posts) < 3:
            return

        text_for_analysis = "\n\n".join([f"Başlık: {e.title}\nÖzet: {e.get('summary', '')[:400]}" for e in recent_posts[:15]])

        print("📅 Periyodik stratejik analiz yapılıyor...")
        analysis = analyze_with_grok(text_for_analysis, is_periodic=True)

        message = f"📊 **The Assembly - Stratejik Periyodik Rapor**\n\n"
        message += f"**Dönem:** Son 1 Ay Özeti\n\n"
        message += analysis

        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')

        state["last_periodic"] = int(time.time())
        save_state(state)

    except Exception as e:
        print(f"❌ Periyodik analiz hatası: {e}")

# ====================== MAIN ======================
if __name__ == "__main__":
    print("🚀 The Assembly Grok AI + Stratejik Bot BAŞLATILDI")
    
    state = load_state()

    while True:
        current_time = time.time()

        # Yeni post kontrolü
        state = check_new_posts(state)

        # Periyodik stratejik rapor (her 6 saatte bir)
        if current_time - state.get("last_periodic", 0) > PERIODIC_INTERVAL:
            send_periodic_analysis(state)

        time.sleep(CHECK_INTERVAL)
