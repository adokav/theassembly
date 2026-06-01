import os
import time
import feedparser
import telegram
from dotenv import load_dotenv

load_dotenv()

# ================== CONFIG ==================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RSS_URL = os.getenv("RSS_URL")

LAST_GUID_FILE = "last_guid.txt"

bot = telegram.Bot(token=TELEGRAM_TOKEN)

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

def check_new_posts():
    try:
        feed = feedparser.parse(RSS_URL)
        
        if not feed.entries:
            print("⚠️ RSS'den veri çekilemedi.")
            return

        last_guid = get_last_guid()
        new_posts = []

        for entry in reversed(feed.entries):
            if last_guid and entry.id == last_guid:
                continue
            new_posts.append(entry)

        for entry in new_posts:
            title = entry.title
            link = entry.link
            summary = entry.get('summary', '')[:700]

            message = f"🔔 **The Assembly Yeni Paylaşım**\n\n"
            message += f"**{title}**\n\n"
            
            if summary and len(summary) > 20:
                message += f"{summary}\n\n"
                
            message += f"🔗 {link}"

            bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode='Markdown',
                disable_web_page_preview=False
            )

            print(f"✅ Gönderildi → {title[:60]}...")
            save_last_guid(entry.id)
            time.sleep(4)

    except Exception as e:
        print(f"❌ Hata: {e}")

if __name__ == "__main__":
    print("🚀 The Assembly RSS Bot BAŞLATILDI")
    print(f"RSS: {RSS_URL[:70]}...")
    
    while True:
        check_new_posts()
        time.sleep(300)
