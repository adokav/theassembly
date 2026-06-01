# The Assembly Grok AI Bot

İlgili X (Twitter) hesabının paylaşımlarını bir RSS akışından çekip **Grok AI** ile
analiz eden ve Telegram üzerinden **hisse / yatırım stratejisi raporu** üreten bot.

## Özellikler
- Buton menüsünden dönem seçimi (Son 1/2/3 gün, 1/2 hafta, son ay)
- Grok AI ile paylaşımlara **dayalı** (uydurma yapmayan) stratejik analiz
- Sağlam RSS çekimi: yeniden deneme (exponential backoff) + yedek ayna (mirror) desteği
- Güvenli Telegram gönderimi: 4096 karakter sınırı için otomatik bölme,
  Markdown hatasında düz metne otomatik geçiş
- UTC bazlı doğru tarih filtresi
- Her raporun sonunda yatırım uyarısı (disclaimer)

## Ortam Değişkenleri (.env)
| Değişken | Zorunlu | Açıklama |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | BotFather'dan alınan token |
| `XAI_API_KEY` | ✅ | console.x.ai'dan alınan Grok API anahtarı |
| `RSS_URL` | ✅ | X hesabının RSS adresi (Nitter / RSSHub vb.) |
| `RSS_FALLBACK_URLS` | ➖ | Virgülle ayrılmış yedek RSS aynaları (opsiyonel) |

Eksik bir zorunlu değişken varsa bot başlangıçta anlaşılır bir hata verip durur.

## Çalıştırma
```bash
pip install -r requirements.txt
python bot.py
```

Render gibi platformlarda **Background Worker** olarak, başlatma komutu `python bot.py`
ile çalıştırın.

## Notlar
- Veri kaynağı (RSS aynası) kapanırsa bot kullanıcıyı bilgilendirir.
  Güvenilirliği artırmak için `RSS_FALLBACK_URLS` ile birden fazla ayna tanımlayın.
- Üretilen raporlar yatırım danışmanlığı değildir.
