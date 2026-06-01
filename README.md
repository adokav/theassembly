# The Assembly Telegram Bot

`@InTheAssembly` hesabının yeni paylaşımlarını RSS ile takip edip Telegram'a bildiren bot.

### Kurulum (Render.com)

1. Environment Variables ekleyin:
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `RSS_URL`

2. Background Worker olarak deploy edin.

Bot her 5 dakikada bir kontrol eder.
