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

### Render
Başlatma komutu: `python bot.py`

- **Background Worker** olarak çalıştırmak en temizidir (port gerekmez).
- **Web Service** olarak çalıştırırsan da çalışır: bot, `PORT` tanımlıysa otomatik
  olarak küçük bir health endpoint açar, böylece Render'ın port/health kontrolü geçer.
  (Aksi halde Web Service polling botunu sürekli yeniden başlatır ve bot Telegram'a
  yanıt veremez — "yanıt yok" sorununun en yaygın sebebi budur.)

Bot başlarken log'da `Telegram bağlantısı OK → @kullanıcı_adı` satırını görmelisin.
Görmüyorsan `TELEGRAM_TOKEN` yanlıştır ya da ağ `api.telegram.org`'a çıkamıyordur.
`409 Conflict` görüyorsan aynı token'la ikinci bir kopya çalışıyordur.

## Notlar
- Veri kaynağı (RSS aynası) kapanırsa bot kullanıcıyı bilgilendirir.
  Güvenilirliği artırmak için `RSS_FALLBACK_URLS` ile birden fazla ayna tanımlayın.
- Üretilen raporlar yatırım danışmanlığı değildir.
