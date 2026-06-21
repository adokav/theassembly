# The Assembly Grok AI Bot

İlgili X (Twitter) hesabının paylaşımlarını bir RSS akışından çekip **Grok AI** ile
analiz eden ve Telegram üzerinden **hisse / yatırım stratejisi raporu** üreten bot.

## Özellikler
- **Çoklu hesap takibi (birleşik rapor):** The Assembly + Bora Özkent + Whale
  Receipts önerileri tek raporda, bir Wall Street analisti gözüyle birleştirilir.
  Yeni hesap eklemek `_ACCOUNT_DEFS`'e tek satır.
- **Makro & stratejik görünüm:** her raporun sonunda hisse önerilerinin ötesinde
  bir bölüm — piyasa rejimi, kurumsal konumlanma & insider akışları, sektör
  rotasyonu, riskler/katalizörler ve stratejik çıkarım (yalnızca paylaşımlara dayalı).
- Buton menüsünden dönem seçimi (Son 1/2/3 gün, 1/2 hafta, son ay)
- Yapay zeka ile paylaşımlara **dayalı** (uydurma yapmayan) stratejik analiz
- **Yapılandırılmış çıkarım + canlı piyasa verisi:** her tavsiye için sembol, tarih,
  gerekçe çıkarılır; Yahoo Finance'ten güncel fiyat / 50G ort. / 52H aralık çekilir
- **"Bugün alınır mı?" kararı:** geçmiş tavsiye, o günden bugüne fiyat hareketiyle
  karşılaştırılıp 🟢 geçerli / 🟡 kısmen / 🔴 geç kalındı olarak derecelendirilir
- Sağlam RSS çekimi: yeniden deneme (exponential backoff) + yedek ayna (mirror) desteği
- Güvenli Telegram gönderimi: 4096 karakter sınırı için otomatik bölme,
  Markdown hatasında düz metne otomatik geçiş
- UTC bazlı doğru tarih filtresi
- Her raporun sonunda yatırım uyarısı (disclaimer)

## Ortam Değişkenleri (.env)
| Değişken | Zorunlu | Açıklama |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | BotFather'dan alınan token |
| `RSS_URL` | ✅ | X hesabının RSS adresi (Nitter / RSSHub vb.) |
| `RSS_FALLBACK_URLS` | ➖ | Virgülle ayrılmış yedek RSS aynaları (opsiyonel) |
| _LLM anahtarı (biri zorunlu)_ | ✅ | Aşağıdaki tabloya bakın |

### LLM Sağlayıcısı (OpenAI-uyumlu, kod değişmeden seçilir)
Bot herhangi bir OpenAI-uyumlu API ile çalışır. Aşağıdakilerden **birini** tanımlamak yeterli:

| Değişken | Açıklama |
|---|---|
| `OPENAI_API_KEY` | OpenAI kullan (varsayılan model `gpt-4o-mini`) |
| `XAI_API_KEY` | xAI / Grok kullan (varsayılan model `grok-4`) — kredi gerekir |
| `LLM_API_KEY` | Genel anahtar; `LLM_BASE_URL` ve `LLM_MODEL` ile herhangi bir sağlayıcı (Groq, DeepSeek, OpenRouter…) |
| `LLM_MODEL` | _(ops.)_ Model adını değiştir (ör. `gpt-4o`) |
| `LLM_BASE_URL` | _(ops.)_ Özel uç nokta (custom endpoint) |

Öncelik sırası: `LLM_API_KEY` → `OPENAI_API_KEY` → `XAI_API_KEY`.
Örnek (Groq): `LLM_API_KEY=...`, `LLM_BASE_URL=https://api.groq.com/openai/v1`, `LLM_MODEL=llama-3.3-70b-versatile`.

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

---

# Kripto Sinyal Botu (`crypto_bot.py`)

Aynı repoda, **hisse botundan bağımsız** ikinci bir uygulama: bir kripto paranın
yükselme olasılığını **teknik + piyasa sinyallerini derleyerek** (konfluens skoru)
özetler ve Telegram'dan raporlar/alarm verir. Sadece `TELEGRAM_TOKEN` gerekir;
piyasa verisi **anahtarsız** çekilir (Binance public API + alternative.me Korku
& Açgözlülük endeksi), yani kutudan çıkar çıkmaz çalışır.

## Mimari
```
crypto_signals/
├── config.py      # env doğrulama (fail-fast)
├── storage.py     # SQLite repository (abone, watchlist, snapshot, alarm state)
├── indicators.py  # saf fonksiyonlar: SMA / EMA / RSI / MACD / ATR
├── providers.py   # Binance (OHLCV + 24s), alternative.me (Fear & Greed) adapter'ları
├── signals.py     # her sinyali verdict + ağırlığa çevirir → kompozit skor (engine)
├── formatting.py  # rapor → Telegram Markdown
├── scheduler.py   # periyodik tarama + rating geçişinde alarm (ayrı thread)
└── bot.py         # Telegram handler'ları + entrypoint
```

| Katman | Karar | Neden |
|---|---|---|
| **DB** | SQLite + ince repository | Sıfır bağımlılık; arayüz sayesinde Postgres'e geçiş tek dosya |
| **API** | Provider adapter deseni | Yeni borsa = yeni adapter; çekirdek değişmez |
| **UI** | Telegram persistent keyboard + komutlar | Sunucusuz arayüz, tek dokunuşla analiz |
| **State** | Kalıcı state SQLite'ta, scheduler ayrı thread'de | Restart'ta watchlist/abonelik kaybolmaz; alarm sadece **rating geçişinde** (spam yok) |

## Derlenen sinyaller
Trend (fiyat vs SMA50/200), Golden/Death Cross, RSI, MACD, Hacim trendi,
30g Kırılım (destek/direnç), 24s Momentum, Fear & Greed. Her biri `[-1,+1]` puan +
ağırlık üretir; **ağırlıklı ortalama** → `🟢 GÜÇLÜ / 🟡 NÖTR / 🔴 ZAYIF` + boğa
olasılığı %. Tek sinyal değil, **sinyallerin hemfikir olması** belirleyici.

## Komutlar
- `/sinyal BTC` — anlık sinyal raporu
- `/radar` — son taramadaki en güçlü boğa sinyalleri (skora göre sıralı)
- `/ekle SOL` · `/sil SOL` — takip listesi (watchlist)
- `/liste` — watchlist özeti (skora göre sıralı)
- `/korku` — piyasa Korku & Açgözlülük endeksi
- `/abonelik_iptal` — otomatik alarmları kapat

## Hangi coinler taranır?
- **Dinamik evren (varsayılan):** watchlist'i **boş** olan kullanıcılar için bot,
  Binance 24s hacmine göre **ilk `DYNAMIC_TOP_N` coin'i** (vars. 150) her taramada
  yeniden belirleyip tarar. Stablecoin/fiat çiftleri (USDC, FDUSD, EUR…) elenir;
  `EXCLUDE_BASES` ile ek hariç tutma yapılır.
- **Kişisel watchlist:** `/ekle`–`/sil` ile liste tanımlayan kullanıcı yalnızca
  kendi coinlerini izler.
- `DYNAMIC_TOP_N=0` yapılırsa dinamik mod kapanır ve `DEFAULT_SYMBOLS` kullanılır.

Tek toplu ticker çağrısı hem top-N seçimi hem 24s momentum için kullanılır
(coin başına ekstra istek yok); büyük taramada hız limiti için hafif throttle
uygulanır. Periyodik tarama `SCAN_INTERVAL_MIN` (vars. 30 dk) ile; bir sembol
**GÜÇLÜ** sinyale girince/çıkınca otomatik haber verir.

## Çalıştırma
```bash
pip install -r requirements.txt
python crypto_bot.py      # hisse botu hâlâ: python bot.py
pytest tests/             # saf indikatör + skor testleri
```
Render'da Background Worker olarak çalıştırın (`PORT` tanımlıysa otomatik health
endpoint açılır, Web Service de çalışır).

> ⚠️ Üretilen raporlar yatırım tavsiyesi değildir. Sinyaller olasılık gösterir, garanti vermez (DYOR).
