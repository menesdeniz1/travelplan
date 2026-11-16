# TravelPlan

## In English

TravelPlan is a personal route and fare tracker for trips within Europe, Turkey included. Besides direct flights, it builds chains that combine a flight to a nearby airport with a train or bus leg (for example Athens ✈ Bratislava 🚆 Prague), ranks the options by total cost and connection risk, keeps price watches locally and can send Telegram alerts when a price drops. It only searches and links to official sales sites; it does not sell tickets, take payments or store credentials.

**Run it** (CI uses Python 3.12 on Ubuntu and Windows; older versions are untested):

```bash
pip install -r requirements.txt
playwright install chromium   # needed only for live Google Flights search
python server_v2.py
```

Then open http://localhost:8085. The server listens on `127.0.0.1:8085` by default. Settings are environment variables or a `.env` file; see `.env.example`.

**Run the tests:**

```bash
pip install pytest
python -m pytest -q
```

The tests do not touch the network: providers are replaced with stubs or read captured fixtures under `tests/fixtures/`.

**Limits:**

- Flight prices come from reading Google Flights pages, not from an official API. Several ground sources (RegioJet, FlixBus, Trenitalia, Google Maps, obilet) are undocumented endpoints or page reads. Any of them can change, break or block access.
- Baggage, transfer and hotel costs are rough estimates and are labelled as such.
- Single user, no authentication. Run it on your own machine; do not expose it to the internet.
- Scope is Europe and Turkey.

**Note:** this is a tool for personal, non-commercial use. You are responsible for complying with the terms of use of every data source it contacts. The rest of this README is in Turkish.

---

Avrupa içi (Türkiye dahil) seyahatler için kişisel kullanıma yönelik bir rota ve fiyat takip aracı. Uçuş pahalıysa, uçağı yakın bir havalimanına ve oradan tren veya otobüse bağlayan zincirleri de değerlendirir (ör. **Atina ✈ Bratislava 🚆 Prag**), sonuçları gerçek toplam maliyet ve aktarma riskiyle sıralar, fiyat takiplerini yerel olarak tutar ve düşüş olduğunda Telegram'dan haber verir.

- **Kapsam:** Avrupa şehirleri ve Türkiye. Çok kullanıcı, hesap sistemi ve bilet satışı yoktur; araç yalnızca arar ve resmi satış sitelerine yönlendirir.
- **Çalışma yeri:** Kendi bilgisayarınızda, yalnızca `127.0.0.1` üzerinde.
- **Durum ve planlar:** [ROADMAP.md](ROADMAP.md). Mimari ayrıntılar: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Ne yapar

### Rota motoru (kurallı arama)

`/api/trip-search` ve arayüzdeki "Gezi Planı" bölümü, kalkış ile varış arasında doğrudan uçuşların yanında ara duraklı zincirleri de arar.

- **Yer girişi:** şehir, ülke veya havalimanı kodu (ör. `Atina`, `GR`, `ATH`).
- **Otomatik ara durak:** `max_stops` 0–3 arası (varsayılan 2). Motor ara durakları kendisi seçer.
- **Sabit duraklar:** `via` ile verilen yerler verilen sırayla ziyaret edilir (ör. `Budapeşte, Viyana`).
- **Arama bütçesi:** bir sorgudaki canlı uçuş araması sayısı 1–60 arasında (varsayılan 20). Bütçe dolduğunda sonuç kısmi olur ve arayüzde "Derin arama (bütçe ×2)" düğmesi çıkar.
- **Zaman kuralları:** `arrive_by` (en geç varış) ve `depart_after` (en erken kalkış) tam tarih-saat olarak uygulanır. Ertesi gün varış tarihiyle birlikte gösterilir.
- **Tarih:** en fazla 7 kalkış tarihi; `flex_days` ile her tarih ±0–3 gün genişletilebilir.
- **Minimum kalış:** ara durakta dakika cinsinden (`Selanik:480;Budapeşte:120`); gidiş-dönüşte saat cinsinden (`min_stay_hours`).
- **Gidiş-dönüş:** `return_dates` ve dönüş için ayrı zaman ve via kuralları. Uygun gidiş-dönüş kombinasyonları ayrıca listelenir.
- **Bagaj:** kabin bagajı (varsayılan) veya kayıtlı bagaj seçilerek maliyet hesabına dahil edilir.
- **Sıralama ve rozetler:** gerçek toplam + süre × zaman değeri (varsayılan 150 ₺/saat) + gece beklemesi cezası + risk cezası. Rozetler: *Önerilen*, *En Ucuz*, *En Hızlı*, *Düşük Risk*.
- **Gece beklemesi:** 00:00–06:00 arasında en az 3 saatlik bekleme uyarı ve tahmini otel maliyeti ile işaretlenir.

### Kara yolu (tren ve otobüs)

Ara bacaklar için kaynak sırası şöyledir (ayrıntı için [ARCHITECTURE.md](docs/ARCHITECTURE.md)):

1. **Fiyatlı canlı kaynaklar (RegioJet, FlixBus, Trenitalia, isteğe bağlı Transitous):** gerçek sefer saati ve, gösterildiğinde, gerçek fiyat. RegioJet belgesiz halka açık API'dir (EUR); FlixBus otobüs seferleri belgesiz arama uçlarıdır; Trenitalia İtalya'daki trenler için gerçek fiyatı verir (bkz. Veri kaynakları). Sonuçları birleştirilir.
2. **Google Haritalar (yedek):** yalnızca yukarıdaki kaynakların hiçbiri o çift ve tarih için sonuç vermezse sorgulanır. Gerçek sefer saati verir; fiyat yalnızca Maps bir fiyat gösterirse gelir, aksi halde fiyat bilinmiyor (`price_try` boş) olarak kalır. Bir aramada en fazla `TRAVELPLAN_MAPS_BUDGET` (varsayılan 4) sayfa yüklemesi yapılır; sınır dolunca atlanan bağlantılar için sonuç sayfasında uyarı çıkar.
3. **Statik tablo:** 21 tren/otobüs koridorunun tipik saat ve fiyatları. Bu veri **tahmini** olarak etiketlenir.
4. **Mesafe tahmini:** yol mesafesi 700 km'yi geçmeyen ve yukarıdakilerin hiçbirinin bilmediği çiftler için. 06:00–20:00 arasında 2 saatte bir kalkış varsayar. **Tahmini** olarak etiketlenir.

"Sadece gerçek veriler" seçildiğinde 3 ve 4. adımlar hiç çalıştırılmaz; yalnızca 1 ve 2. adımların sonuçları kullanılır.

> **Otobüs fiyat ipucu (obilet):** obilet yalnızca "ından başlayan" fiyatı verir; bu bir ipucudur, bağlantının fiyatı değildir ve `price_try` boş kalır (`price_from_try` alanında görünür). Yalnızca fiyatı bilinmeyen otobüs bacaklarına eklenir.

> **FlixBus notu:** FlixBus sunucuda bağlıdır: `server_v2.py` `RailBusProvider`'a `bus_provider=FlixBusProvider()` verir ve daemon aynı kaynak örneğini kullanır. Otobüs bacakları `source="flixbus"` ile gelir; fiyatı tahmin değilse `price_is_estimated=false` olur. Her seçenek bacağında `source` ve `price_is_estimated` alanları bulunur; arayüz ve Telegram kaynak etiketini (RegioJet, FlixBus, tahmin) gösterir. "Tahmini tarife" uyarısı bacağın `is_estimated` alanından gelir; `price_is_estimated` ayrıca gösterilmez.

### Gerçek toplam maliyet ve risk

- **Toplam maliyet:** biletler, bagaj, havalimanı ⇄ şehir transferi ve gece beklemesinde otel. Bu değerlerin çoğu tablolardan gelir ve `tahmini` olarak işaretlenir.
- **Aktarma riski:** düşük / orta / yüksek. Ayrı biletli uçuş bağlantılarında en az 180 dakika pay zorunludur; bu tür bağlantılar ayrıca risk puanını yükseltir.

### Esnek tarih, gidiş-dönüş ve "al / bekle" önerisi

- **Esnek tarih:** her tarih için en ucuz gün ve kazanç gösterilir.
- **Al / bekle:** geçmiş aramalardan (aynı rota ve tarih için en az 3 gözlem) sezgisel bir öneri üretir: *al*, *bekle* veya *belirsiz*. Bu bir tahmin değildir; sonuçta nedeni yazılır.

### Takipler ve arka plan taraması

- **Kurallı takip (trip watch):** direkt uçuş bütçesi ve yan yol bütçesi ile birlikte tüm arama kuralları saklanır. Bütçenin altına düşen fiyat için bildirim gelir; önceki bildirilen fiyattan daha düşük olmadıkça tekrar bildirilmez.
- **Klasik takip:** tek rota için hedef bütçe. Çok modlu tasarruf 1.000 ₺'yi aşarsa da bildirilir.
- **Otomatik tarama:** sunucu açıkken ilk tarama başlangıçtan yaklaşık 10 saniye sonra, sonra 6 saatte bir çalışır. Arayüzdeki tarama düğmesi (`/api/tracker/scan-now`) beklemeden çalıştırır.

### Telegram

Telegram komutları yalnızca `telegram_chat_id` olarak kayıtlı sohbetten kabul edilir; 10 dakikadan eski mesajlar yok sayılır.

```
/ara Atina Prag 28.10                         direkt ve yan rota (en iyi sonuçlar)
/ara "New York" Londra 3.11                   şehir adı boşluk içeriyorsa tırnak
/gezi Atina Prag 27.10-28.10                  tarih aralığı (en fazla 7 gün)
/gezi Atina Prag 27.10 en geç 28.10 12:00     varış sınırı
/gezi Atina Prag 27.10 üzerinden Yunanistan   ara durak
/gezi Atina Prag 27.10 durak 1                en fazla 1 otomatik ara durak (0–3)
/gezi Atina Prag 27.10 bütçe 40               uçuş araması bütçesi (en fazla 60)
/gezi Atina Prag 27.10 esnek 2                tarihler ±2 gün taransın
/gezi Atina Prag 27.10 dönüş 30.10            gidiş-dönüş
/gezi ... bagajsız | valiz                    yalnız kabin bagajı | kayıtlı bagaj
/takip 9500 6500                              son aramaya göre: direkt 9.500 ₺, yan yol 6.500 ₺
/takip 9500                                   yalnızca direkt bütçe
/liste                                        aktif takipler
/sil 2                                        2 numaralı takibi sil
/durum                                        sunucu ve Google Flights sağlığı
/yardim                                       komut özeti
```

Tarih biçimleri: `28.10`, `28.10.2026`, `2026-10-28`, `yarın`, `bugün`. Yıl verilmezse en yakın gelecek tarih alınır.

- `/ara` doğrudan uçuşları ve eski çok modlu yönlendiriciyi (`multimodal_router.py`) kullanır.
- `/gezi` rota motorunu kullanır.
- `/takip`, o sohbetteki son `/ara` veya `/gezi` sorgusunu saklar. Bu bilgi bellekte tutulur; sunucu yeniden başlarsa kaybolur.

**Kurulum:** Telegram'da `@BotFather` ile bot oluşturun, botunuza `/start` yazın, chat ID'nizi (ör. `@userinfobot`) öğrenin. Ardından arayüzdeki **Telegram** düğmesinden token ve chat ID'yi girip *Test Bildirimi Gönder* ile deneyin. Alternatif olarak `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` ortam değişkenlerini kullanabilirsiniz; `config.json` içinde bu alanlar varsa onlar öncelikli olur.

### Sağlık göstergesi

- Google Flights üst üste **3** aramada başarısız olursa sağlık "sorunlu" olur. Hem arayüzde hem Telegram'da uyarı gelir; düzelince ayrıca bildirilir.
- Google engel sayfası (`/sorry/`, captcha, onay duvarı) algılanırsa tüm süreç için **15 dakika** canlı uçuş araması durdurulur.
- `GET /api/health` ve Telegram `/durum` aynı bilgiyi verir: uçuş sağlığı, EUR/TRY kuru ve kaynağı, takip sayıları, son aramalar, tarayıcı durumu.

---

## Hızlı başlangıç

Gereksinimler: Python (CI'da 3.12 ile test edilir; daha eski sürümler denenmedi), Windows veya Linux, güncel bir tarayıcı.

```bash
pip install -r requirements.txt
playwright install chromium
python server_v2.py
```

Tarayıcıda **http://localhost:8085** adresini açın. Sunucu varsayılan olarak `127.0.0.1:8085` üzerinde dinler; `localhost` de kabul edilir.

- `playwright` kurulu değilse canlı uçuş araması boş döner ve bir uyarı yazılır. Kara yolu, takip ve diğer özellikler çalışmaya devam eder.
- Windows'ta `scripts\run_server.bat` sunucuyu bir pencerede başlatır. Oturum açılınca otomatik başlatma için bkz. [scripts/README.md](scripts/README.md).
- Çalışan sunucuyu durdurmak için konsolda `Ctrl+C` kullanın.

### Yapılandırma (ortam değişkenleri)

| Değişken | Varsayılan | Açıklama |
| :--- | :--- | :--- |
| `TRAVELPLAN_HOST` | `127.0.0.1` | Dinlenecek adres. LAN için Mac'in LAN IP'si (`0.0.0.0` olmaz: Host kontrolü LAN isteklerini reddeder). Bkz. aşağıdaki "Mac'te sunucu olarak çalıştırma" bölümü. |
| `TRAVELPLAN_PORT` | `8085` | Port. |
| `TRAVELPLAN_ALLOWED_HOSTS` | boş | LAN modunda sunucunun ek kabul ettiği Host/Origin adları, virgülle ayrılmış (ör. `mac.local,192.168.1.20`). Yalnızca ad veya IPv4; port ve joker yok. Bkz. "Gizlilik ve güvenlik". |
| `TRAVELPLAN_LOG_CONSOLE` | boş | `1` ise günlük konsola da yazılır. Varsayılan: stderr bir terminal ise konsola yazılır; launchd gibi arka plan çalıştırmalarında yalnızca `logs/server.log`. |
| `TRAVELPLAN_DB` | repo kökü `travelplan.db` | Ana SQLite veritabanı yolu. |
| `TRAVELPLAN_SEARCH_LOG` | repo kökü `search_log.db` | Arama kaydı veritabanı yolu. |
| `TRAVELPLAN_GROUND_CACHE` | repo kökü `ground_cache.db` | Kara yolu (tren ve otobüs) gün bazlı önbellek dosyası. `off` önbelleği kapatır. |
| `TRAVELPLAN_GROUND_CACHE_TTL_SECS` | `21600` (6 saat) | Dolu bir kara yolu yanıtının geçerlilik süresi. Boş yanıtlar her zaman 1 saat geçerlidir; hata yanıtları hiç saklanmaz. |
| `TRAVELPLAN_GROUND_CACHE_MAX_ROWS` | `5000` | Kara yolu önbelleğinde en fazla satır; süresi dolanlar ve en eskiler silinir. |
| `TRAVELPLAN_FLIGHT_CACHE` | repo kökü `flight_cache.db` | Google Flights ayrıştırılmış sonuçları ve takvim fiyatları (L2) önbellek dosyası. `off` önbelleği kapatır. |
| `TRAVELPLAN_FLIGHT_CACHE_TTL_SECS` | `2700` (45 dk) | Dolu uçuş yanıtı ("ok") geçerlilik süresi. Boş ("no_flights") yanıtlar 10 dk geçerlidir; hata durumları saklanmaz. |
| `TRAVELPLAN_FLIGHT_CACHE_MAX_ROWS` | `5000` | Uçuş önbelleğinde en fazla satır; süresi dolanlar ve en eskiler silinir. |
| `TRAVELPLAN_NO_MIGRATE` | boş | `1` ise eski JSON dosyaları içe aktarılmaz. |
| `TRAVELPLAN_EUR_TRY` | canlı kur | Sabit EUR/TRY kuru. Ayarlıysa her zaman canlı kurun önüne geçer. |
| `TRAVELPLAN_MAX_BROWSERS` | `1` | Aynı anda çalışabilecek Chromium sayfası sayısı (en az 1). |
| `TRAVELPLAN_BROWSER_IDLE_SECS` | `300` | Bu kadar süre arama yoksa paylaşımlı tarayıcı kapanır. |
| `TRAVELPLAN_BROWSER_REUSE` | `1` | `0` yaparsanız her arama için yeni tarayıcı açılır (daha yavaş, daha fazla bellek). |
| `TELEGRAM_BOT_TOKEN` | boş | Telegram bot token'ı. `config.json` içinde varsa o kullanılır. |
| `TELEGRAM_CHAT_ID` | boş | Sahip sohbet ID'si. `config.json` içinde varsa o kullanılır. |
| `TRAVELPLAN_TRANSITOUS` | `0` (kapalı) | `1` ise Transitous'tan gerçek tren/otobüs saatleri alınır (isteğe bağlı kaynak). Açmadan önce Transitous ekibine bildirin; bkz. Veri kaynakları. |
| `TRAVELPLAN_MAPS_BUDGET` | `4` | Bir arama için en fazla Google Haritalar sayfa yüklemesi (yedek kaynak). `0` Maps'i kapatır. Bkz. Veri kaynakları. |
| `TRAVELPLAN_HOTELS` | `0` (kapalı) | `1` ise gece beklemesinde Google Hotels üzerinden gerçek otel fiyatı aranır (isteğe bağlı / opt-in kaynak). |
| `TRAVELPLAN_HOTELS_BUDGET` | `2` | Bir rota araması için en fazla Google Hotels sayfa yüklemesi. |
| `TRAVELPLAN_HOTELS_CACHE` | repo kökü `hotel_cache.db` | Google Hotels fiyat önbellek dosyası (24 saat geçerli). `off` önbelleği kapatır. |

---

## Mac'te sunucu olarak çalıştırma

TravelPlan bir Mac'te sürekli açık sunucu olarak çalışabilir: kurulum, oturum açılınca otomatik başlatma, günlükler ve güncelleme `scripts/macos/` altındaki betiklerle yapılır. Ayrıntılar: [scripts/README.md](scripts/README.md) (macOS bölümü).

```bash
scripts/macos/setup_macos.sh                      # Python, .venv, paketler, Chromium
scripts/macos/autostart_macos.sh install --lan    # otomatik başlatma; ev ağından erişim için
scripts/macos/autostart_macos.sh status
```

- Sunucu varsayılan olarak yalnızca Mac'in kendisinden erişilir (`127.0.0.1`). `--lan` Mac'in LAN IP'sini dinler ve Mac'in Bonjour adını (ör. `mac-mini.local`) kabul eder; diğer cihazlar `http://<LAN-IP>:8085` ile bağlanır. Başka bir ad için `--allowed-hosts`.
- Yalnızca güvenilir ev ağında kullanın. Arayüz ve API'nin kimlik doğrulaması yoktur; internete açmayın. Uzaktan kullanım için Telegram komutları önerilir.

---

## Veri kaynakları ve kullanım koşulları

Bu araç kişisel kullanım için tasarlanmıştır. Aşağıdaki kaynakların şartlarını ve değişme riskini kendiniz değerlendirin.

- **Google Flights (uçuş fiyatları):** resmi API değildir. Playwright ile sayfa okunur (kazıma). Google'ın kullanım koşulları bu erişimi kısıtlayabilir. Sayfa yapısı değişirse sonuçlar sessizce boşalabilir; bu yüzden sağlık göstergesi vardır. Arama dili `hl=tr`, para birimi TRY olarak sabittir.
- **RegioJet (tren):** belgesiz, kimlik doğrulaması olmayan halka açık JSON API. Değişebilir veya kapanabilir.
- **FlixBus:** belgesiz uç noktalar (şehir çözümleme ve sefer arama). Yalnızca arama yapar, rezervasyon yapmaz. Sunucuya bağlıdır (bkz. yukarıdaki not).
- **Google Haritalar (tren ve otobüs saatleri, yedek):** Maps'in yol tarifi sayfası tarayıcıda açılıp okunur (kazıma; resmi API değildir). Yalnızca diğer fiyatlı kaynaklar sonuç vermediğinde çalışır. Sınırlar: iki sayfa yüklemesi arasında en az 8 saniye, tarayıcı havuzu paylaşılır, sonuçlar 30 dakika önbelleklenir, bir arama en fazla `TRAVELPLAN_MAPS_BUDGET` sayfa yükler. Maps bir engel sayfası döndürürse 30 dakika boyunca yeni sayfa açılmaz. Fiyat yalnızca Maps gösterdiğinde gelir; tahmin üretilmez.
- **Google Hotels (gece beklemesi için gerçek otel fiyatı, isteğe bağlı):** Playwright ile Google Hotels üzerinden 1 yetişkin, 1 gece için şehir bazında en ucuz otel fiyatı okunur (kazıma; resmi API değildir). Yalnızca `TRAVELPLAN_HOTELS=1` iken çalışır. Sınırlar: iki sayfa yüklemesi arasında en az 8 saniye, tarayıcı havuzu paylaşılır, sonuçlar 24 saat önbelleklenir (bellek + SQLite), bir arama en fazla `TRAVELPLAN_HOTELS_BUDGET` (varsayılan 2) sayfa yükler. Engel/captcha (`/sorry/`) durumunda 30 dakika soğuma başlar. Bulunamazsa mevcut tahmin kullanılır.
- **obilet (otobüs, yalnızca "ından başlayan" fiyatlar):** obilet.com rota sayfasındaki operatör bazında en düşük fiyat okunur (`otobus-bileti/<kalkış>-<varış>`). Bilet satılmaz; fiyat ipucu olarak gösterilir. Sayfa isteği en az saniyede bir, rota başına 24 saat önbellek.
- **Trenitalia (İtalya trenleri):** lefrecce.it'in belgesiz çözümleme ve çözüm uçları. Yalnızca arama yapılır; gerçek sefer saati ve, servis gösterdiğinde, gerçek EUR fiyatı alınır. Bir engelleme (HTTP 403/429) 15 dakika bekleme başlatır.
- **Transitous (tren ve otobüs, isteğe bağlı):** topluluk tarafından işletilen açık taşıma yönlendiricisi (`api.transitous.org`). Varsayılan olarak kapalıdır; yalnızca `TRAVELPLAN_TRANSITOUS=1` ile açılır. Açmadan önce Transitous ekibine bildirilmesi gerekir: kullanım politikası gereği uygulama kendini User-Agent ile tanıtır, en fazla saniyede bir istek atar ve sonuçları 30 dakika önbellekler. Ticari olmayan kişisel kullanım içindir. Gerçek sefer saatleri verir; fiyat vermez ("fiyat yok"). Proje MIT lisanslıdır (`LICENSE`).
- **OurAirports (havalimanları):** kamu malıdır (public domain). `travel_engine/data/airports.csv` bu veriden süzülmüştür.
- **Şehir merkezleri:** `travel_engine/data/city_centres.csv`. 10 büyük şehir referansa karşı test ediliyor; Wikidata ile otomatik doğrulama güvenilir çıkmadı (idari merkez koordinatları), diğerleri doğrulanmadı.
- **Döviz kurları:** ECB referans kurları (Frankfurter, `api.frankfurter.dev`), yedek olarak `open.er-api.com`. Kur 12 saat önbelleklenir. Hata durumunda son bilinen değer ve sonra sabit 54 ₺ kullanılır.
- **Maliyet tabloları:** bagaj, transfer ve otel değerleri kabaca tahminlerdir ve `costs.py` içinde elle girilmiştir. Kullanmadan önce gözden geçirin.
- **Harita:** Leaflet 1.9.4, unpkg üzerinden yüklenir; harita için internet gerekir.
- **Telegram Bot API:** bildirimler ve komutlar için.

---

## Gizlilik ve güvenlik

- Varsayılan olarak sunucu yalnızca yerel makinede dinler (`127.0.0.1:8085`). Yabancı bir `Host` başlığı (DNS rebinding) ve başka sitelerden gelen POST istekleri (`Origin` kontrolü) **403** ile reddedilir.
- **İsteğe bağlı LAN modu:** `TRAVELPLAN_HOST=<LAN IP>` ile sunucu ev ağındaki cihazlardan da erişilir olur; `TRAVELPLAN_ALLOWED_HOSTS=mac.local,192.168.1.20` ile cihazların kullandığı ad ve IP'ler Host ve Origin kontrolünden geçer (port yine sunucunun portudur). Bu modda **kimlik doğrulama yoktur**: yalnızca güvenilir ev ağında açın, router'da port yönlendirmesi (port forwarding) yapmayın ve sunucuyu internete açmayın. Uzaktan kullanım için Telegram komutlarını kullanın; Telegram botu giden bağlantıyla çalışır, Mac'e gelen bağlantı açmaz.
- Yalnızca arayüz dosyaları (`static/index.html`, `static/css/app.css`, `static/js/app.js`) ve `/api/` uçları sunulur. `config.json`, veritabanı, `watches.json` ve `.git/` gibi dosyalar 404 döner.
- İstek gövdesi en fazla 1 MB'dır.
- Telegram komutlarını yalnızca sahip sohbeti işler.
- **Telegram token'ı `config.json` içinde düz metin olarak saklanır.** `config.json`, veritabanları ve `logs/` `.gitignore` içindedir; yine de bu dosyaları paylaşmayın.
- Dışarıya giden istekler: Google Flights, Google Haritalar (yol tarifi sayfası, yedek kaynak), RegioJet, FlixBus (belgesiz arama uçları), Trenitalia (lefrecce.it), obilet.com (rota sayfası, yalnızca fiyat ipucu), ECB/Frankfurter ve open.er-api.com, Telegram API, unpkg (harita), yalnızca `TRAVELPLAN_HOTELS=1` iken Google Hotels ve yalnızca `TRAVELPLAN_TRANSITOUS=1` iken Transitous (`api.transitous.org`).
- Uygulama bilet satın almaz, ödeme bilgisi istemez ve kimlik bilgisi tutmaz.

---

## Veri saklama

- **`travelplan.db` (SQLite, WAL modu):** takipler (`watches`), fiyat geçmişi (`price_history`), uyarılar (`alerts`) ve sürüm bilgisi (`meta`). Konum `TRAVELPLAN_DB` ile değiştirilebilir.
- **`ground_cache.db` (SQLite, WAL modu):** kara yolu yanıtları, çift ve tarih başına tüm gün (RegioJet, FlixBus, Trenitalia, Transitous ve Google Haritalar). Konum `TRAVELPLAN_GROUND_CACHE` ile değiştirilir, `off` ile kapatılır. `.gitignore` bu dosyayı henüz listelemiyor; git'e eklemeyin.
- **`flight_cache.db` (SQLite, WAL modu):** canlı Google Flights aramalarının ayrıştırılmış sonuçları (L2 önbellek) ve takvim fiyatları. Konum `TRAVELPLAN_FLIGHT_CACHE` ile değiştirilir, `off` ile kapatılır.
- **`hotel_cache.db` (SQLite, WAL modu):** Google Hotels üzerinden alınan gerçek otel fiyatları (24 saat geçerli). Konum `TRAVELPLAN_HOTELS_CACHE` ile değiştirilir, `off` ile kapatılır.
- **`search_log.db` (SQLite):** her canlı Google Flights aramasının rotası, tarihi, sonucu ve en ucuz fiyatı. Kayıtlar 180 gün ve en fazla 50.000 satır tutulur; temizlik günde en fazla bir kez yapılır. Al/bekle önerisi bu veriyi kullanır.
- **Eski JSON dosyaları:** `watches.json`, `price_history.json`, `alerts.json` ilk açılışta, ilgili tablo boşsa veritabanına aktarılır; dosya `*.migrated.bak` adına taşınır ve silinmez. Tablo doluysa dosya yerinde kalır. Her dosya bir kez işlenir; bundan sonra aynı adlı bir JSON dosyası yeniden oluşsa bile uygulama onu okumaz.
- **`config.json`:** Telegram ayarları (`/api/settings/telegram` ile yazılır).
- **`logs/server.log`:** sunucu günlüğü; 5 MB'lık 3 dosya olarak döner.
- **Yedek:** sunucuyu durdurup `travelplan.db` dosyasını `-wal` ve `-shm` dosyalarıyla birlikte kopyalayın.

---

## Kaynak kullanımı

- Google Flights için tek bir paylaşımlı Chromium çalışır. Aramalar arasında en az 2 saniye bekleme vardır. Görsel, font ve izleme istekleri engellenir.
- Tarayıcı 300 saniye boşta kalınca kapanır ve her 50 aramada bir yeniden başlatılır.
- Canlı uçuş sonuçları 10 dakika önbelleklenir. Boş sonuçlar önbelleğe alınmaz.
- Eşzamanlı arama sayfası üst sınırı `TRAVELPLAN_MAX_BROWSERS` ile belirlenir (varsayılan 1).
- Kara yolu kaynakları (RegioJet, FlixBus, Trenitalia, Transitous) bir çift ve tarih için aynı anda sorgulanır; her kaynak gün boyunca istenir (Trenitalia ve Transitous sayfa sayfa), kalkış saati filtresi sonradan yerelde uygulanır. Google Haritalar yalnızca yedek kaynaktır ve kendi başlangıç saatine göre ayrı önbelleklenir; sayfa yükleme bütçesi değişmedi. Yanıtlar bellekte ve `ground_cache.db` içinde önbelleklenir: aynı çift ve gün için sonraki aramalar, başka bir süreçte bile, kaynağa istek atmaz. Bu süreçte bir kaynağın sonucu alınamazsa (hata, engel) o yanıt saklanmaz.
- Google Haritalar sayfa yüklemeleri de aynı tarayıcı havuzunu ve sınırı kullanır; bir aramada en fazla `TRAVELPLAN_MAPS_BUDGET` yükleme yapılır (varsayılan 4), iki yükleme arası en az 8 saniye.
- Bellek kullanımı `GET /api/health` içinde (`process_rss_mb`) görünür; bu ölçüm yalnızca Windows'ta yapılır.

---

## Testler

```bash
python -m pytest -q
```

- Resmi test komutu `pytest`'tir. CI (`.github/workflows/tests.yml`) Ubuntu ve Windows üzerinde Python 3.12 ile çalışır ve yalnızca `pytest` kurar; Playwright kurulmaz.
- **Son çalıştırma (19 Ekim 2026):** `python -m pytest -q`: 2038 geçti (+388 alt test), 0 başarısız. `python -m unittest discover tests`: 1599 geçti, 0 başarısız.
- Testler ağa çıkmaz: Google Flights, RegioJet, FlixBus ve kur çağrıları sahte (stub) yanıtlarla değiştirilir.
- `tests/conftest.py` testleri geçici bir veri dizinine yönlendirir ve her test kendi veritabanı dosyasını kullanır. Gerçek `travelplan.db` testlerle değişmez.
- `python -m unittest discover tests` de çalışır ve gerçek veriye dokunmaz. `conftest.py` bu komutta yüklenmediği için koruma `travel_engine/storage.py` içinde yapılır: test çalıştırması (giriş noktası `unittest` veya `pytest`) algılanırsa ve `TRAVELPLAN_DB` ayarlı değilse veritabanı süreç başına geçici bir dizinde olur, arama günlüğü de geçici olur ve eski JSON göçü kapanır. `TRAVELPLAN_DB` açıkça verilirse bu koruma kapalıdır. Korumayı `tests/test_unittest_isolation.py` test eder.

---

## Bilinen sınırlamalar

- **Google Flights kırılgandır.** Sayfa yapısı değişirse, captcha gelirse veya dil/bölge ayarı değişirse sonuç boşalabilir. Yedek uçuş kaynağı yoktur.
- **Kara yolu verisi çoğunlukla tahmini:** RegioJet dışındaki tren/otobüs saatleri ve fiyatları statik tablodan veya mesafeden gelir. Gecikme bilgisi yoktur.
- **Google Haritalar kırılgandır ve engellenebilir.** Sayfa yapısı değişirse ya da engel sayfası gelirse yedek sonuç alınamaz; sağlık göstergesinde `google_maps` olarak görünür.
- **FlixBus belgesiz uçtur.** Sunucuda bağlıdır, ama arama uçları belgesizdir ve değişebilir. Sonuç yoksa RegioJet, statik tablo veya mesafe tahmini kullanılır.
- **Transitous isteğe bağlıdır ve varsayılan olarak kapalıdır** (kullanım koşulları gereği Transitous ekibine bildirim sonrası açılmalı). Açıkken istasyon çapası yalnızca `HUB_STATIONS` listesindeki şehirlerde kullanılır; diğer yerlerde şehir merkezinden aranır ve merkez istasyondan uzaksa sonuç boş dönebilir.
- **Ryanair ve Wizz doğrudan entegre değildir.** Bilinçli bir karardır: uçuşlar Google Flights üzerinden gelir.
- **Takvim fiyatlarıyla ön eleme** rota motorunda yoktur; her tarih ve çift arama bütçesinden sayılır.
- **Maksimum seyahat süresi** rota motorunda yoktur; Çok Modlu panelindeki "Maks. Süre" filtresi yalnızca tarayıcıda uygulanır.
- **Gezi kartlarında "+N gün" rozeti yoktur:** varış tarihi-saati açık yazılır, ancak ayrı rozet yalnızca eski çok modlu kartlarda vardır.
- **Otel, bagaj, transfer ve kur dışındaki maliyetler kabadır.** Havalimanı ⇄ şehir transferi, havayolunun bagaj politikası ve otel fiyatları tahminidir.
- **Al/bekle önerisi sezgiseldir.** Az veri varsa "belirsiz" döner; kesin bir tahmin değildir.
- **Şehir merkezleri sınırlı doğrulanmıştır:** yalnızca 10 büyük şehir referansa karşı test edilir; diğer merkezler doğrulanmadı.
- **Tek kullanıcı:** kimlik doğrulama yoktur ve olmayacaktır (kapsam dışı).
- **Telegram `/takip`** son sorguyu bellekte tutar.
- **Kapsam:** Avrupa ve Türkiye. Sayfa başlığı artık Avrupa'yı söylüyor; arayüzdeki varış alanının etiketi ve yer tutucusu hâlâ "dünya" diyor (kozmetik; kapsam değişmez).

---

## REST API

Tüm uçlar `http://localhost:8085` altındadır. Yanıtlar JSON'dur (UTF-8). Tarih biçimi `YYYY-AA-GG`, saat biçimi `SS:DD`, tarih-saat `YYYY-AA-GGTSS:DD`'dir.

### GET

| Uç nokta | Ne döner | Önemli parametreler |
| :--- | :--- | :--- |
| `/api/trip-search` | Kurallı arama: sıralı seçenekler, en iyi/en ucuz/en hızlı, tarih ve gidiş-dönüş sonuçları, al/bekle önerisi. | `origin`, `destination`, `depart_dates` (zorunlu, virgüllü, en fazla 7), `arrive_by`, `depart_after`, `via`, `include_side_routes` (0/1), `max_stops` (0–3), `budget` (1–60), `min_stay` (`Yer:dk;…`), `cabin_bag`, `checked_bag` (0/1), `flex_days` (0–3), `return_dates`, `return_arrive_by`, `return_depart_after`, `return_via`, `min_stay_hours`, `time_value_try_per_hour` |
| `/api/live-flights` | Canlı uçuş listesi, fiyat geçmişi ve ±3 günlük fiyat matrisi. Arayüzün ana arama ucu. | `origin`, `destination`, `departure_date`, `return_date`, `trip_type` (`roundtrip` / `oneway`) |
| `/api/price-history` | Bir rota ve tarih için geçmiş fiyatlar. | `origin`, `destination`, `departure_date`, `trip_type`, `current_price`, `airline`, `days` |
| `/api/price-matrix` | Tarih esnekliği matrisi (arayüz bunu `live-flights` içinden kullanır). | `origin`, `destination`, `departure_date`, `trip_type`, `current_price`, `airline` |
| `/api/watches` | Tüm takipler. | — |
| `/api/alerts` | Son 50 uyarı (sistem uyarıları dahil). | — |
| `/api/health` | Uçuş sağlığı, EUR/TRY, takip sayıları, son 10 arama, tarayıcı ve bellek. | — |
| `/api/clusters` | Şehir kümeleri ve çevre havalimanları. | — |
| `/api/settings/telegram` | Kayıtlı Telegram ayarları (token maskeli). | — |

### POST

| Uç nokta | Gövde | Ne yapar |
| :--- | :--- | :--- |
| `/api/watches` | `origin`, `destination`, `target_budget_try`, `direct_budget_try`, `departure_date`, `return_date`, `trip_type`, `include_multimodal`, `trip` (trip-search ile aynı kurallar, JSON nesnesi olarak) | Takip ekler ve hemen tarar. `trip` verilmişse `target_budget_try` yan yol bütçesidir; verilmezse 0 (yan yol uyarısı yok). |
| `/api/watches/delete` | `{ "id": "watch_…" }` | Takibi siler. |
| `/api/tracker/scan-now` | — | Tüm aktif takipleri hemen tarar. |
| `/api/alerts/clear` | — | Uyarı geçmişini temizler. |
| `/api/settings/telegram` | `bot_token`, `chat_id`, `enabled`, `test` (true ise test mesajı gönderir) | Telegram ayarlarını kaydeder. |

---

## Dosya yapısı (özet)

```
server_v2.py                  HTTP sunucusu, API uçları, 6 saatlik tarama iş parçacığı, Telegram başlatma
static/index.html             Arayüz (tek sayfa, HTML iskeleti)
static/css/app.css            Arayüz stilleri
static/js/app.js              Arayüz betiği (Gezi Planı, hızlı arama, takipler, Leaflet haritası)
ROADMAP.md                    Fazlar, durum ve riskler
docs/ARCHITECTURE.md          Mimari ve akışlar
docs/examples/plan.md         Kabul örneği: eski seyahat planı (Gidiş 2 senaryosu)
scripts/                      Windows başlatma ve otomatik başlatma
travel_engine/
  route_engine.py             Rota motoru (zincir araması, bütçe, puanlama)
  trip_planner.py             Kurallı arama girişi; motoru çağırır, sonuçları biçimlendirir
  multimodal_router.py        Çok modlu yönlendirici (Telegram /ara, takip tarayıcısı, planlayıcının eski yolu)
  costs.py, roundtrip.py, flexdates.py, advisor.py   Maliyet/risk, gidiş-dönüş, esnek tarih, al/bekle
  airports.py, geo_clusters.py, data/                 Havalimanları, şehir kümeleri, merkezler
  fx.py                       EUR/TRY ve diğer kurlar
  storage.py                  SQLite erişimi ve JSON içe aktarma
  flight_cache.py, ground_cache.py   Google Flights (L2) ve kara yolu için kalıcı SQLite önbellekleri
  search_log.py               Canlı arama kaydı
  tracker.py, daemon_tracker.py, notifier.py          Takip, tarama, uyarılar
  telegram_bot.py             Telegram komutları
  price_history.py            Fiyat geçmişi ve tarih matrisi
  providers/google_flights_provider.py   Google Flights (Playwright)
  providers/live_rail_provider.py        RegioJet
  providers/flixbus_provider.py          FlixBus (sunucuda bağlı)
  providers/trenitalia_provider.py       Trenitalia (İtalya trenleri, fiyatlı)
  providers/transitous_provider.py       Transitous (isteğe bağlı açık taşıma yönlendiricisi)
  providers/google_maps_transit_provider.py  Google Haritalar (yedek, sayfa yüklemesi budanmış)
  providers/google_hotels_provider.py        Google Hotels (gece beklemesi için gerçek otel fiyatı)
  providers/obilet_fares.py              obilet otobüs fiyat ipuçları
  providers/rail_bus_provider.py         Kara yolu birleştirme, statik tablo, tahmin
tests/                        pytest testleri ve fixture'lar
```
