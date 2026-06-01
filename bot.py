import os
import time
import feedparser
import telegram
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ================== CONFIG ==================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RSS_URL = os.getenv("RSS_URL")
XAI_API_KEY = os.getenv("XAI_API_KEY")

LAST_GUID_FILE = "last_guid.txt"

bot = telegram.Bot(token=TELEGRAM_TOKEN)

client = OpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1"
)

def get_last_guid():
    if os.path.exists(LAST_GUID_FILE):
        try:
            with open(LAST_GUID_FILE, "r") as f:
                return f.read().strip()
        except:
            return None
    return None

def save_last_guid(guid):
    with open(LAST_GUID_FILE, "w") as f:
        f.write(guid)

def analyze_with_grok(text):
    try:
        response = client.chat.completions.create(
            model="grok-4",
            messages=[
                {"role": "system", "content": "Sen deneyimli bir makro analist ve trading uzmanısın. Kısa, net, profesyonel ve Türkçe rapor yaz."},
                {"role": "user", "content": f"""
Aşağıdaki The Assembly tweet'ini detaylı analiz et ve **madde madde** rapor ver:

Tweet: "{text}"

Rapor Formatı:
• **Ana Konu ve Önem Seviyesi:**
• **Piyasa Etkisi:**
• **Fırsat / Risk Değerlendirmesi:**
• **Trading / Yatırım Önerisi:**
• **İzlenmesi Gereken Diğer Unsurlar:**
                """}
            ],
            temperature=0.7,
            max_tokens=800
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Analiz sırasında hata oluştu: {str(e)[:200]}"

def check_new_posts():
    try:
        feed = feedparser.parse(RSS_URL)
        if not feed.entries:
            print("RSS boş")
            return

        last_guid = get_last_guid()
        new_posts = [entry for entry in reversed(feed.entries) if not last_guid or entry.id != last_guid]

        for entry in new_posts:
            title = entry.title
            link = entry.link
            summary = entry.get('summary', '')[:1000]

            full_text = f"{title}\n\n{summary}"

            print(f"📊 Grok analiz ediliyor: {title[:60]}...")
            analysis = analyze_with_grok(full_text)

            message = f"🔔 **The Assembly - Grok AI Analiz Raporu**\n\n"
            message += f"**Orijinal İçerik:**\n{title}\n\n"
            message += f"{analysis}\n\n"
            message += f"🔗 {link}"

            bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode='Markdown',
                disable_web_page_preview=False
            )

            print(f"✅ Grok analiz raporu gönderildi!")
            save_last_guid(entry.id)
            time.sleep(6)

    except Exception as e:
        print(f"❌ Genel Hata: {e}")

if __name__ == "__main__":
    print("🚀 The Assembly Grok AI Botu BAŞLATILDI")
    print(f"RSS: {RSS_URL[:60]}...")
    
    while True:
        check_new_posts()
        time.sleep(300)
