import os
import time
import feedparser
import telegram
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

load_dotenv()

# ====================== CONFIG ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
RSS_URL = os.getenv("RSS_URL")
XAI_API_KEY = os.getenv("XAI_API_KEY")

client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")

def get_recent_posts(days: int):
    feed = feedparser.parse(RSS_URL)
    cutoff = datetime.now() - timedelta(days=days)
    posts = []
    for entry in feed.entries:
        if hasattr(entry, 'published_parsed') and entry.published_parsed:
            pub_date = datetime(*entry.published_parsed[:6])
            if pub_date > cutoff:
                posts.append({
                    "title": entry.title,
                    "summary": entry.get('summary', '')[:700],
                    "link": entry.link
                })
    return posts

async def analyze_posts(posts, period_text):
    if not posts:
        return "Bu dönemde veri bulunamadı."

    text = "\n\n".join([f"Başlık: {p['title']}\nÖzet: {p['summary']}" for p in posts[:25]])

    try:
        response = client.chat.completions.create(
            model="grok-4",
            messages=[
                {"role": "system", "content": "Sen profesyonel bir makro analist ve trading stratejistisin. Türkçe, net, aksiyon odaklı ve stratejik rapor yaz."},
                {"role": "user", "content": f"""
The Assembly (@InTheAssembly) hesabının **{period_text}** içindeki paylaşımlarını analiz et.

Posts:
{text}

Rapor formatı:
• **Önerilen Hisseler / Varlıklar**
• **Her birinin neden önerildiği (katalizör, makro olay, piyasa yapısı)**
• **Zamanlama ve Risk Seviyesi**
• **Genel Stratejik Tavsiye**
                """}
            ],
            temperature=0.6,
            max_tokens=1100
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"❌ Grok API hatası: {e}"

async def show_report(update, context, days):
    query = update.callback_query
    await query.answer()

    period_text = f"Son {days} Gün" if days <= 3 else f"Son {days//7} Hafta" if days <= 14 else "Geçtiğimiz Ay"
    
    await query.edit_message_text("🔄 Grok AI analiz yapıyor, lütfen bekleyin... (10-15 sn)")

    posts = get_recent_posts(days)
    analysis = await analyze_posts(posts, period_text)

    message = f"📊 **The Assembly - {period_text} Stratejik Rapor**\n\n"
    message += analysis

    await query.edit_message_text(message, parse_mode='Markdown')

async def start(update, context):
    keyboard = [
        [InlineKeyboardButton("🔥 Son 1 Gün", callback_data="1")],
        [InlineKeyboardButton("🔥 Son 2 Gün", callback_data="2")],
        [InlineKeyboardButton("🔥 Son 3 Gün", callback_data="3")],
        [InlineKeyboardButton("📅 Son 1 Hafta", callback_data="7")],
        [InlineKeyboardButton("📅 Son 2 Hafta", callback_data="14")],
        [InlineKeyboardButton("📊 Geçtiğimiz Ay", callback_data="30")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🎯 **The Assembly Stratejik Rapor Botu**\n\n"
        "Hangi dönemi analiz etmek istersin?",
        reply_markup=reply_markup
    )

async def button_handler(update, context):
    query = update.callback_query
    days = int(query.data)
    await show_report(update, context, days)

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("rapor", start))
    application.add_handler(CallbackQueryHandler(button_handler))

    print("🚀 Butonlu Grok AI Botu BAŞLATILDI")
    print("Telegram’da /start veya /rapor yazarak butonları açabilirsiniz.")
    application.run_polling()

if __name__ == "__main__":
    main()
