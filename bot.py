import os
import time
import feedparser
import telebot
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from telebot import types

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
RSS_URL = os.getenv("RSS_URL")
XAI_API_KEY = os.getenv("XAI_API_KEY")

bot = telebot.TeleBot(TOKEN)
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
    return posts[:25]

def analyze_posts(posts, period_text):
    if not posts:
        return "Bu dönemde veri bulunamadı."
    
    text = "\n\n".join([f"Başlık: {p['title']}\nÖzet: {p['summary']}" for p in posts])
    
    try:
        response = client.chat.completions.create(
            model="grok-4",
            messages=[
                {"role": "system", "content": "Sen profesyonel bir makro analist ve trading stratejistisin. Türkçe, net ve aksiyon odaklı rapor yaz."},
                {"role": "user", "content": f"""
The Assembly hesabının **{period_text}** içindeki paylaşımlarını analiz et.

Posts:
{text}

Rapor formatı:
• **Önerilen Hisseler / Varlıklar**
• **Her birinin neden önerildiği**
• **Zamanlama ve Risk Seviyesi**
• **Genel Stratejik Tavsiye**
                """}
            ],
            temperature=0.6,
            max_tokens=1100
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"❌ Grok API hatası: {str(e)[:200]}"

@bot.message_handler(commands=['start', 'rapor'])
def send_menu(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🔥 Son 1 Gün", callback_data="1"))
    markup.add(types.InlineKeyboardButton("🔥 Son 2 Gün", callback_data="2"))
    markup.add(types.InlineKeyboardButton("🔥 Son 3 Gün", callback_data="3"))
    markup.add(types.InlineKeyboardButton("📅 Son 1 Hafta", callback_data="7"))
    markup.add(types.InlineKeyboardButton("📅 Son 2 Hafta", callback_data="14"))
    markup.add(types.InlineKeyboardButton("📊 Geçtiğimiz Ay", callback_data="30"))

    bot.send_message(message.chat.id, "🎯 **The Assembly Stratejik Rapor Botu**\n\nHangi dönemi analiz etmek istersin?", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    days = int(call.data)
    period_text = f"Son {days} Gün" if days <= 3 else f"Son {days//7} Hafta" if days <= 14 else "Geçtiğimiz Ay"

    bot.edit_message_text("🔄 Grok AI analiz yapıyor... (10-20 sn)", call.message.chat.id, call.message.message_id)

    posts = get_recent_posts(days)
    analysis = analyze_posts(posts, period_text)

    result = f"📊 **The Assembly - {period_text} Stratejik Rapor**\n\n{analysis}"
    bot.edit_message_text(result, call.message.chat.id, call.message.message_id, parse_mode="Markdown")

if __name__ == "__main__":
    print("🚀 The Assembly Butonlu Grok AI Botu BAŞLATILDI")
    bot.infinity_polling()
