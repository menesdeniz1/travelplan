# TravelPlan: Mimari

Bu belge, kodu ilk kez okuyan bir geliştiricinin sistemi anlaması için yazıldı. Ürün kapsamı ve planlar [ROADMAP.md](../ROADMAP.md), kullanım ve kurulum [README.md](../README.md) içindedir. Burada yer alan sayılar ve eşikler kaynak koddan alınmıştır; kod değişirse bu belge de güncellenmelidir.

---

## 1. Bileşenler

```
 ┌──────────────────────────┐
 │ Tarayıcı (index.html)    │  tek sayfa; Leaflet haritası (unpkg)
 └────────────┬─────────────┘
              │ HTTP/JSON  (yalnız 127.0.0.1:8085)
 ┌────────────▼──────────────────────────────────────────────────────────┐
 │ server_v2.py  (ThreadingHTTPServer)                                   │
 │   TravelPlanAPIHandler: Host/Origin kontrolü, doğrulama, JSON yanıt   │
 │   iş parçacığı: tarama (ilk 10 sn sonra, 6 saatte bir)               │
 │   iş parçacığı: Telegram long polling (TelegramCommandBot)           │
 └──┬──────────────┬───────────────┬────────────────┬───────────────────┘
    │              │               │                │
    ▼              ▼               ▼                ▼
 TripPlanner   MultiModalRouter  DaemonTracker   PriceTracker / PriceHistory / AlertNotifier
 (kurallı      (eski çok modlu   (takip tarama,  (watches, price_history, alerts)
  arama)        yönlendirici)     uyarılar)            │
    │              │               │                    ▼
    ▼              │               │             ┌─────────────────────┐
 RouteEngine       │               │             │ storage.Store       │
 (zincir motoru)   │               │             │ travelplan.db (WAL) │
    │              │               │             └─────────────────────┘
    ├──────────────┴───────┬───────┘
    ▼                      ▼
 GoogleFlightsProvider   RailBusProvider.find_ground
 (Playwright, L1/L2      ├─ LiveRailProvider (RegioJet, belgesiz JSON API)
  önbellek, slot,        ├─ FlixBusProvider  (sunucuda bağlı; belgesiz arama)
  sağlık)                ├─ TrenitaliaProvider (İtalya trenleri, fiyatlı)
    │                    ├─ TransitousProvider (isteğe bağlı açık tarife)
    ▼                    ├─ GoogleMapsTransitProvider (yedek, bütçeli)
 Chromium (tek sayfa     ├─ ObiletFromPrices (otobüs fiyat ipucu)
 işçisi)                 ├─ statik koridor tablosu (21 koridor, tahmini)
    │                    └─ mesafe tahmini (tahmini)
    ▼                           │
 flight_cache.db                ▼
 (SQLite WAL, L2 uçuş)   ground_cache.db (SQLite WAL, gün boyu önbellek)

SearchLog (search_log.db) ◄── GF araması ve TripPlanner (advisor)
fx.get_eur_try_rate (Frankfurter → open.er-api.com → sabit)
```

Tek bir süreç çalışır: sunucu. Telegram, tarama ve HTTP istekleri aynı süreçte farklı iş parçacıklarında yürür. Tarayıcı ve veritabanı erişimi bu iş parçacıklarının ortak kullandığı kaynaklardır ve kendi kilitlerine sahiptir (bkz. §12).

---

## 2. Modüller

| Modül | Sorumluluk |
| :--- | :--- |
| `server_v2.py` | HTTP uçları, parametre doğrulama (`_normalize_trip_query` ve yardımcıları), bileşenlerin oluşturulması, tarama iş parçacığı, Telegram başlatma. |
| `travel_engine/trip_planner.py` | Kurallı arama girişi (`plan_trip`). Rota motorunu çağırır, seçenekleri ortak biçime çevirir, esnek tarih, gidiş-dönüş ve al/bekle önerisini ekler. Eski (legacy) arama yolunu da içerir; sunucu her zaman rota motorunu kullanır. |
| `travel_engine/route_engine.py` | Zincir araması: aday havalimanları, arama bütçesi, DFS, maliyet ve risk, puanlama, rozetler. |
| `travel_engine/airports.py` | Havalimanı veritabanı (`airports.csv`, 707 satır, 53 ülke) ve şehir merkezleri (`city_centres.csv`, 426 satır; 10 büyük şehir referansa karşı test edilir, diğerleri doğrulanmadı). Çözümleme, mesafe, şehir ⇄ havalimanı grupları. |
| `travel_engine/geo_clusters.py` | 8 şehir kümesi (Prag, Viyana, Bratislava, Budapeşte, Münih, Berlin, Selanik, Atina) için havalimanı ve tren kümesi verisi ve koordinatlar. Çok modlu yönlendirici ve kara yolu katmanı kullanır. |
| `travel_engine/costs.py` | Bagaj, transfer, otel tahmini; yolculuk maliyet dökümü; bağlantı ve yolculuk riski. |
| `travel_engine/roundtrip.py` | Gidiş ve dönüş seçeneklerinden kombinasyon üretimi (en fazla 30 × 30 aday). |
| `travel_engine/flexdates.py` | Tarih genişletme (±gün) ve tarih başına en ucuz seçenek. |
| `travel_engine/advisor.py` | Geçmiş gözlemlerden al / bekle / belirsiz önerisi. |
| `travel_engine/multimodal_router.py` | Çok modlu yönlendirici (uçuş + havalimanı transferi + tren/otobüs). Telegram `/ara`, takip tarayıcısı (`daemon_tracker`) ve planlayıcının eski yolu bunu kullanır. Arayüzün Çok Modlu paneli `/api/trip-search` kullanır. |
| `travel_engine/providers/google_flights_provider.py` | Google Flights kazıma, tarayıcı yönetimi, önbellek, sağlık, arama kaydı. |
| `travel_engine/providers/live_rail_provider.py` | RegioJet canlı tren verisi (sefer, fiyat, istasyon çözümleme). |
| `travel_engine/providers/flixbus_provider.py` | FlixBus otobüs verisi (belgesiz arama uçları). `server_v2.py` içinde `rail_provider`'a `bus_provider` olarak bağlıdır; daemon aynı örneği kullanır. |
| `travel_engine/providers/trenitalia_provider.py` | Trenitalia (İtalya trenleri, lefrecce.it), gerçek sefer saati ve fiyat. |
| `travel_engine/providers/transitous_provider.py` | Transitous (isteğe bağlı açık taşıma yönlendiricisi, `api.transitous.org`). |
| `travel_engine/providers/google_maps_transit_provider.py` | Google Haritalar (yedek tren ve otobüs sefer saatleri; bütçeli sayfa yükleme). |
| `travel_engine/providers/obilet_fares.py` | obilet otobüs fiyat ipuçları (operatör bazında "ından başlayan" fiyat). |
| `travel_engine/providers/rail_bus_provider.py` | Kara yolu kaynaklarının birleştirilmesi (`find_ground`), statik koridor tablosu, mesafe tahmini. |
| `travel_engine/flight_cache.py` | `flight_cache.db`: Google Flights ayrıştırılmış sonuçları ve takvim fiyatları için L2 kalıcı SQLite önbelleği. |
| `travel_engine/ground_cache.py` | `ground_cache.db`: Kara yolu yanıtları için gün bazlı kalıcı SQLite önbelleği. |
| `travel_engine/fx.py` | EUR→TRY (ve CZK/HUF/PLN→TRY) kurları. |
| `travel_engine/storage.py` | SQLite erişimi, şema, eski JSON içe aktarma. |
| `travel_engine/tracker.py` | Takipler (`PriceTracker`). `TripWatch` modelini saklar. |
| `travel_engine/price_history.py` | Fiyat geçmişi ve ±3 günlük tarih matrisi. |
| `travel_engine/daemon_tracker.py` | Takipleri tarar, eşikleri kontrol eder, uyarı gönderir. Ayrıca `python daemon_tracker.py --once|--loop` ile bağımsız çalışır. |
| `travel_engine/notifier.py` | Uyarı kaydı (SQLite `alerts`), Telegram gönderimi, `config.json`. |
| `travel_engine/telegram_bot.py` | Telegram komutları (long polling) ve yanıt biçimleme. |
| `travel_engine/search_log.py` | `search_log.db`: her canlı arama kaydı, 180 gün ve 50.000 satır sınırı. |
| `static/index.html`, `static/css/app.css`, `static/js/app.js` | Arayüz. Sunucu yalnızca bu üç dosyayı, açık bir listeyle (`STATIC_FILES`), `Cache-Control: no-cache` ile sunar. |
| `docs/examples/plan.md` | Kabul örneği: eski seyahat planı ("Gidiş 2" senaryosu). Kod tarafından okunmaz. |

---

## 3. İstek akışı: `GET /api/trip-search`

```
Tarayıcı / Telegram (/gezi)
  │
  │ GET /api/trip-search?origin=Atina&destination=Prag&depart_dates=2026-10-28&arrive_by=2026-10-28T12:00 ...
  ▼
server_v2.TravelPlanAPIHandler.do_GET
  1. Host başlığı kontrolü                                  → 403 (yabancı Host)
  2. _validate_place, _normalize_trip_query                  → 400 (Türkçe mesaj; ör. tarih biçimi)
  3. trip_planner.plan_trip(origin, destination, time_value_try_per_hour, **trip)
     exception                                               → 500 {"status":"error", ...}
  ▼
TripPlanner.plan_trip
  4. requested tarihler; flex_days > 0 ise _expand_flex_dates (en fazla 7 tarih)
  5. _search_one(giden) → _search_with_engine
       RouteEngine.search(...)  (aşağıda §4)
         └ Kara yolu (RegioJet, FlixBus, Trenitalia, Transitous, Google Maps, obilet, tablo, tahmin): find_ground (§5)
         └ uçuş: GoogleFlightsProvider.search_flights (§7)
       motor seçenekleri → ortak biçim (_map_engine_option: direct / side_route / multi_flight / via_*)
       include_side_routes=False → yalnız uçuş seçenekleri; ground tek bacaklar çıkarılır
       sonuç: options (cap'e kırpılmış), best/cheapest/fastest, best_direct/best_side_route,
              cheapest_direct/cheapest_side_route, warnings, budget_exhausted, flight_searches, candidates
  6. return_dates varsa: _search_one(dönüş) (yer değiştirmiş) → roundtrip.combine_roundtrip
  7. search_log varsa: advisor.advice_for_route(...)  → "advice" (hata olursa None; arama başarısız olmaz)
  8. flex_summary (flex_days > 0 ise)
  ▼
JSON yanıt (200): query, options, best_*, cheapest_*, fastest, searched_dates, flight_searches,
                  warnings, budget_exhausted, candidates, [flex], [roundtrip], [advice]
```

Önemli noktalar:
- Sunucu tarafında `max_stops`, `budget`, bagaj bayrakları ve kuralların hepsi `_normalize_trip_query` ile doğrulanır. Hatalı değerler 400 döner, arama başlamaz.
- `time_value_try_per_hour` varsayılanı 150; arayüz `readTimeValueSetting()` ile değeri tarayıcıda saklar (`localStorage`).
- `flight_searches`, motorun yaptığı `search_flights` çağrılarını sayar; motorun kendi (kalkış, varış, tarih) önbelleğinden gelen sorgular sayılmaz. Google sağlayıcısının 10 dk'lık önbelleği bu sayıyı değiştirmez.

---

## 4. Rota motoru (`route_engine.py`)

Modülün başındaki docstring algoritmanın ayrıntılı tanımıdır; burada özet verilir.

### 4.1 Çözümleme ve adaylar
- Kalkış ve varış yerleri `AirportDB` ile havalimanı listesine çözülür (havalimanı kodu, şehir, metro grubu veya ülke).
- **Kendi havalimanları:** yerin kendi havalimanları.
- **Yan yol adayları:** kendi havalimanlarından kara yoluyla ulaşılabilen havalimanları (`ground_reachable`). Ada havalimanları `FALLBACK_ISLAND_AIRPORTS` ile sınırlıdır.
  - Düşük maliyetli hub (`LCC_HUBS`, 27 havalimanı) veya arama kaydında geçmişi olan havalimanı olmalıdır.
  - Düz çizgi mesafesi yarıçap içinde ve yol mesafesi (`ground_distance_km`) yarıçapın 1,3 katını aşmamalıdır. LCC hub'ları en fazla 450 km yol mesafesine kadar kullanılabilir.
  - Puan: kendi havalimanı +10; büyük +3, orta +1; yakın LCC +4 (uzaksa +1); geçmiş +1; eksi yol km / 100. Kendi havalimanları en fazla 6, kalkış tarafı 4, varış tarafı 5 aday tutulur.
- **Hub'lar:** LCC hub'ları arasından, sapma oranı `(d(A,H)+d(H,B))/d(A,B)` en fazla 1,6 olanlar; en iyi 4 tanesi.

### 4.2 Arama bütçesi ve kategoriler
Her yeni `search_flights` çağrısı bütçeden düşer. Önbellekten dönen sorgular düşmez. Kategoriler sırayla planlanır:

| Kategori | Ne arar | Koşul | Bütçe payı |
| :--- | :--- | :--- | :--- |
| C1 direkt | kendi havalimanları arası, her gidiş tarihi | — | sınırsız |
| C2 | kendi havalimanı → LCC hedef yan yol | ara durak ≥ 1 | ≤ %40 (ilk geçiş) |
| C3 | LCC kalkış yan yolu → kendi hedef havalimanı | ara durak ≥ 1 | ≤ %15 |
| C4 | diğer yan yol çiftleri (en az bir ucu kendi değil) | ara durak ≥ 1 (tek uç yan yol); ≥ 2 (iki uç da yan yol) | ≤ %15 |
| C5 | hub zincirleri: A→H ve H→B; kara + hub; hub→hub | ara durak ≥ 1 / 2 / 3 | kalanın tamamı (en az %30) |

- Politika: C1 hiç kısıtlanmaz. İlk geçişte C2–C4 kendi payları kadar harcar; C5 kalanı kullanır. İkinci geçiş, C2–C4'ten artanları aynı sırayla arar. Üçüncü geçiş yalnızca bütçe kaldıysa "ertesi gün ikinci uçuş" aramalarını yapar.
- Her kategori içinde, SearchLog'da geçmiş en ucuz fiyatı bilinen çiftler önce gelir. Tarihler round-robin ile dönüşümlü dağıtılır.
- Bütçe dolunca `budget_exhausted=True` ve `BUDGET_WARNING` eklenir.

### 4.3 Zincir araması (DFS)
Durum: konum (şehir veya havalimanı), zaman, bacaklar. Kurallar:
- **Şehirden uçuşa:** transfer (şehir → havalimanı) süresi + 120 dk. Zincir başlangıçta ise transfer geriye doğru planlanır (en geç kalkış).
- **Uçuştan sonra:** havalimanı → şehir transferi, ardından kara bacağı en az transfer + 30 dk sonra kalkar.
- **Ayrı biletli uçuş → uçuş:** en az 180 dk. Bağlantı havalimanında ertesi gün kalkış yalnızca ikinci uçuş aramasının bulduğu uçuşlarla mümkündür.
- **Kara → kara** şehirde: en az 30 dk.
- **Minimum kalış** (`min_stay_mins`): belirtilen durakta bekleme bu değerden kısa olamaz; seçenekte `stopover` alanı taşınır.
- **Sabit duraklar** (`via_fixed`): her durak sırayla, kendi havalimanı kümesiyle ziyaret edilir. İki durak arasında tek ana bacak olur (`max_stops` 0). Tek bir uçuş bir durağın havalimanında aktarma yapıyorsa tek biletle o durağı geçer.
- **Gece yarısı:** uçuş zamanı `HH:MM+N` olabilir; varış kalkıştan önceyse ertesi gün sayılır.
- **Sınırlar:** en fazla 200.000 durum (`MAX_DFS_STATES`) ve 5.000 tamamlanmış zincir. Sınırlara ulaşılırsa `SEARCH_LIMIT_WARNING` eklenir.
- Her zincirde `arrive_by` ve `depart_after` tam zaman damgasıyla filtrelenir.

### 4.4 Seçenek, maliyet ve puan
- Her seçenek için: `cost_breakdown` (biletler, bagaj, transferler, otel, toplam, tahmin parçaları) ve `journey_risk` (düşük/orta/yüksek).
- **Gece beklemesi:** iki bacak arasında 00:00–06:00 ile en az 180 dk çakışma varsa `night_wait` işaretlenir ve bir otel gecesi eklenir.
- **Puan:** `gerçek toplam + saat × zaman değeri + (gece beklemesi: 1500 × zaman değeri / 150) + risk cezası` (düşük 0, orta 400, yüksek 1200 TL). Puan düşük olan daha iyidir.
- **Rozetler:** *Önerilen* (en düşük puan), *En Ucuz* (en düşük gerçek toplam), *En Hızlı* (en kısa süre), *Düşük Risk* (düşük risk ve puanı en iyinin %15 içinde).
- Aynı bacak dizisi birleştirilir.

---

## 5. Kara yolu kaynakları ve birleştirme (`rail_bus_provider.find_ground`)

Sıra:

```
find_ground(from, to, date, earliest_dep_time)
 1. Ülke adı ("Yunanistan", "GR") koordinatsız tek başına yer değildir → []
 2. Canlı kaynaklar (_live_found):
      a. RegioJet   (LiveRailProvider):   can_resolve kontrolü → search(...)  [source="live"]
      b. FlixBus    (FlixBusProvider):    sunucuda bağlı; şehir çözümleme → arama [source="flixbus"]
      c. Trenitalia (TrenitaliaProvider): İtalya trenleri, gerçek saat ve fiyat [source="trenitalia"]
      d. Transitous (TransitousProvider): TRAVELPLAN_TRANSITOUS=1 ise gerçek saat [source="transitous"]
      Kaynaklar eşzamanlı sorgulanır (LIVE_WORKERS=4), ground_cache.db ile önbelleklenir,
      birleştirilir (_merge_ground).
      → sonuç varsa obilet fiyat ipuçları eklenir (_attach_bus_from_prices) ve döner
 3. Yoksa Google Haritalar (yedek, GoogleMapsTransitProvider):
      Canlı kaynaklar boş dönerse; TRAVELPLAN_MAPS_BUDGET (varsayılan 4) sayfa bütçesiyle [source="google_maps"].
      Fiyat yalnızca Maps gösterirse gelir, aksi halde None.
      → sonuç varsa obilet fiyat ipuçları eklenir ve döner
 4. Yoksa statik tablo (_table_ground; 21 koridor)  [source="table", is_estimated=True]
 5. Yoksa mesafe tahmini (_estimate_ground)         [source="estimate", is_estimated=True]
     - yol mesafesi ≤ 700 km (ROAD_LIMIT_KM), en az 15 km
     - 06:00–20:00 arası 2 saatte bir kalkış
     - ücret: max(8 €, 0,07 €/km) ve TRY'ye çevrilir
     (allow_estimate=False ise 4 ve 5 atlanır)
 6. Sırala (kalkış saati), max_results ile kırp
```

### Birleştirme kuralları (`_merge_ground`)
- Kaynaklar öncelik sırasıyla işlenir. İki segment **aynı servis** sayılır: aynı mod (tren/otobüs), kalkış farkı ≤ 5 dk ve varış farkı ≤ 10 dk.
- Farklı kaynaktan aynı servis gelirse `_prefers` kazanan seçer: önce gerçek ücret (tahmin olmayan), sonra daha ucuz.
- Aynı kaynak içindeki tekrarlar korunur.
- Sonuç kalkışa göre sıralanır ve `max_results` ile kırpılır. Kırpma sırasında her kaynaktan en az bir segment korunmaya çalışılır (`_with_source_diversity`).

### RegioJet (`live_rail_provider.py`)
- Belgesiz halka açık JSON API (`brn-ybus-pubapi.sa.cz`). Kimlik doğrulama yoktur. Zaman aşımı 8 sn.
- Konum listesi 24 saat önbellekte; hata durumunda 5 dk sonra yeniden denenir. Arama sonuçları 30 dk önbellekte.
- `LIVE_CORRIDORS` (4 çift) yalnızca `get_schedules` için kullanılır. `search()` ise RegioJet'in bildiği herhangi bir şehir çifti için çalışır.
- DB HAFAS (`v6.db.transport.rest`) kapalıdır (503).
- Fiyatlar EUR olarak gelir; `price_eur` alanında saklanır, `price_try` `fx` ile çevrilir.

### FlixBus (`flixbus_provider.py`) — sunucuda bağlı
- Şehir çözümleme (`autocomplete/cities`, 7 gün önbellek), sefer arama (`search/service/v4/search`, 30 dk önbellek).
- İstekler arasında en az 1,5 sn bekleme var (`min_interval`, süreç genelinde). 403/429 gelirse 15 dk sessiz kalır. Bir yer en fazla 2 FlixBus şehrine çözülür.
- Yalnızca arama yapar; rezervasyon yapmaz.
- Sunucuda `server_v2.py` içinde `RailBusProvider(geo_registry, live_provider=LiveRailProvider(), bus_provider=FlixBusProvider(), ...)` ile bağlıdır. `DaemonTracker` bu `rail_provider` örneğini sunucudan alır.

### Trenitalia (`trenitalia_provider.py`)
- lefrecce.it'in belgesiz çözümleme ve çözüm uçları. Yalnızca arama yapılır.
- İtalya içi trenler için gerçek sefer saati ve, servis gösterdiğinde, gerçek EUR fiyatı alınır (`source="trenitalia"`).
- Engelleme durumunda (HTTP 403/429) 15 dakika soğuma süresi uygulanır.

### Google Haritalar (`google_maps_transit_provider.py`) — yedek kaynak
- Yalnızca canlı kaynakların (RegioJet, FlixBus, Trenitalia, Transitous) hiçbiri sonuç vermediğinde çağrılır.
- Yol tarifi sayfası Playwright tarayıcısı üzerinden okunur; her arama için en fazla `DEFAULT_MAPS_BUDGET = 4` (`TRAVELPLAN_MAPS_BUDGET`) sayfa yükleme bütçesi tanınır (`0` Maps'i kapatır). İki sayfa yüklemesi arasında en az 8 saniye beklenir.
- Gerçek sefer saatleri verir; fiyat yalnızca Maps gösterirse gelir, aksi halde `price_try` boş kalır (`price_is_estimated=False`).
- Bir engel sayfası (`/sorry/`) algılanırsa 30 dakika soğuma başlatılır.

### obilet otobüs fiyat ipucu (`obilet_fares.py`)
- obilet.com rota sayfasındaki operatör bazında "ından başlayan" fiyatı okur (`otobus-bileti/<kalkış>-<varış>`).
- Fiyatı bilinmeyen otobüs bacaklarına operatör eşleşmesiyle `price_from_try` ipucu olarak eklenir (`price_try` boş kalır; bu bağlantının gerçek bilet fiyatı değildir).

### Transitous (`transitous_provider.py`) — isteğe bağlı
- Açık topluluk taşıma yönlendiricisi (`api.transitous.org`). Varsayılan kapalıdır (`TRAVELPLAN_TRANSITOUS=1` ile açılır).
- Gerçek sefer saatleri sağlar; fiyat mesafeye dayalı tahmindir (`price_is_estimated=True`).

### Statik tablo (`_corridors`)
21 koridor (tren ve bir otobüs hattı). Her koridor: `(ilk kalkış saati, sefer aralığı, süre dk, fiyat ₺, operatör, mod)`. Bunların tamamı tahmini veridir (`source="table"`).

### Seçenek bacaklarındaki kaynak alanları
Her kara bacağı `source` (`live`, `flixbus`, `trenitalia`, `transitous`, `google_maps`, `table`, `estimate`) ve `price_is_estimated` alanını taşır; `trip_planner` bunları seçenek bacaklarına aktarır. Arayüz ve Telegram kaynak etiketini gösterir; "Tahmini tarife" uyarısı bacağın `is_estimated` alanından gelir.

---

## 6. Maliyet ve risk (`costs.py`)

- **Bilet:** rota motorundaki her bacağın fiyatı; uçuşlar için havayolu bagaj politikası (düşük maliyetli / tam hizmet ayrımı) ve kabin/kayıtlı bagaj seçeneği.
- **Transfer:** havalimanı ⇄ şehir için süre ve fiyat; bilinen havalimanları tabloda, diğerleri mesafeden hesaplanır.
- **Otel:** gece beklemesi için şehir bazlı tahmin; tablo yoksa varsayılan değer.
- **Risk:** ayrı biletli bağlantılarda pay süresi, gece geçişi, son sefer ve tahmini bacak sayısı → düşük / orta / yüksek ve gerekçe listesi.
- Her değer `is_estimated` ile işaretlenir; arayüz "tahmini" etiketini bu bayraktan çıkarır.
- Tablolar modül başında elle girilmiş "kaba tahmin" değerleridir. Gözden geçirilmeleri gerekir.

---

## 7. Google Flights sağlayıcısı (`google_flights_provider.py`)

### 7.1 Arama akışı
1. Önbellek: `(origin, destination, date, return_date)` anahtarı; TTL 600 sn. Yalnızca boş olmayan sonuçlar önbelleğe alınır. Önbellek 100'ü geçince en eski 20 kayıt silinir.
2. Playwright yoksa `unavailable`; engel soğuması (`_blocked_until`) aktifse `blocked_cooldown`.
3. Tarayıcı slotu (`TRAVELPLAN_MAX_BROWSERS`, varsayılan 1). Slot 300 sn içinde alınamazsa `error` ("busy").
4. Aralık: aramalar arasında en az `min_interval_secs` (2 sn), süreç genelinde paylaşılan bir zamanlayıcıyla.
5. İşçi (`_BrowserWorker`): tek bir Chromium süreci ve tek bir iş parçacığı. Her iş bu iş parçacığında sırayla çalışır (Playwright senkron API'si iş parçacığına bağlıdır). Tarayıcı ilk kullanımda açılır, 50 aramada bir yeniden başlatılır, 300 sn boşta kalınca kapanır, bağlantısı koparsa yeniden açılır. İş 180 sn içinde bitmezse `_WorkerTimeout`.
6. Sayfa okunur (`_scrape_page`): kartlar ayrıştırılır, `_filter_and_rank` aykırı değerleri ayıklar.
7. Sayfa sınıflandırılır (`classify_page`):

| Durum | Anlamı | Sağlık sayacı |
| :--- | :--- | :--- |
| `ok` | En az bir uçuş ayrıştırıldı | başarı |
| `no_flights` | "sonuç yok" metni | başarı |
| `parse_error` | Kart var, ayrıştırılamadı | hata |
| `empty_page` | Ne kart ne metin | hata |
| `blocked` | `/sorry/`, captcha, onay duvarı | hata + 15 dk soğuma |
| `error` | İstisna, slot zaman aşımı, meşgul | hata |

### 7.2 Engelleme ve tarayıcı kaynakları
- Görsel, font, medya ve izleme/reklam alan adlarına giden istekler engellenir.
- Tarayıcı başlatma argümanları disk ve GPU kullanımını azaltacak şekilde ayarlanmıştır.
- Bağlam ayarları: `tr-TR` yerel ayarı, sabit bir Chrome 122 kullanıcı aracısı.

### 7.3 Sağlık
- Sağlık durumu `consecutive_failures`, son başarı zamanı, son durum, son 20 durum ve son aramanın özetini tutar.
- `consecutive_failures >= 3` → `healthy: false`.
- Durum değiştiğinde (sağlıklıdan sorunluya veya tersi) `on_health_change` çağrılır. Sunucu bunu bir iş parçacığında çalıştırır ve `notifier.send_system_message` ile web uyarısı (tip `system`) ve Telegram mesajı gönderir.

---

## 8. Depolama

### 8.1 `travelplan.db` (SQLite, WAL)
Konum: `TRAVELPLAN_DB` veya eski JSON yolunun bulunduğu klasördeki `travelplan.db` (sunucuda repo kökü).

| Tablo | İçerik |
| :--- | :--- |
| `watches` | Takip kayıtları (JSON gövdeli): rota, bütçeler, `trip_query`, okumalar, son uyarı bilgisi. |
| `price_history` | Fiyat gözlemleri (JSON gövdeli). |
| `alerts` | Uyarılar (JSON gövdeli); en fazla 50 satır tutulur. |
| `meta` | `schema_version` (`1`) ve `migrated:<ad>` işaretleri. |

- `Store`: her işlem kendi bağlantısını açar ve kapatır (Windows'ta açık bağlantı dosya kilidi tutar). Yazmalar `BEGIN IMMEDIATE` ile yapılır; `busy_timeout` 10 sn.
- Aynı süreçteki iş parçacıkları `RLock` ile sıralanır; süreçler arası sıralama SQLite'a bırakılır.
- Eski JSON içe aktarma (`import_legacy_json`): tablo boşsa ve `migrated:<ad>` yoksa dosya okunur, satırlar eklenir, işaret yazılır (aynı işlemde) ve dosya `<ad>.migrated.bak` olarak yeniden adlandırılır. Tablo doluysa dosya yerinde kalır. `TRAVELPLAN_NO_MIGRATE=1` ise hiçbir şey yapılmaz.

### 8.2 `search_log.db` (SQLite)
- Konum: `TRAVELPLAN_SEARCH_LOG` veya repo kökü. `_LazySearchLog` dosyayı ilk kayıtta açar; yalnızca okuma dosyayı oluşturmaz.
- Her canlı arama bir satır yazar (rota, tarih, durum, en ucuz fiyat, kaynak).
- Temizlik: 180 günden eski satırlar silinir ve en fazla 50.000 satır kalır. En fazla günde bir kez çalışır; hata yazma işlemini başarısız kılmaz.
- Okuyanlar: `advisor` (al/bekle), rota motoru (geçmiş ucuz çiftleri öne alma, aday puanı).

### 8.3 `ground_cache.db` (SQLite, WAL)
- Konum: `TRAVELPLAN_GROUND_CACHE` veya repo kökü `ground_cache.db`. `"off"` (veya `0`, `none`, `false`, `no`) önbelleği kapatır. Test çalışmasında her sağlayıcı geçici dizinde izole dosya kullanır.
- Kara yolu yanıtları gün bazlı saklanır (RegioJet, FlixBus, Trenitalia, Transitous ve saat çapalı Google Haritalar).
- Dolu bir yanıt `TRAVELPLAN_GROUND_CACHE_TTL_SECS` (varsayılan 6 saat), boş yanıt `1 saat` önbellekte kalır; hata yanıtları saklanmaz.
- Temizlik: Yazma sırasında süresi dolanlar ve en fazla `TRAVELPLAN_GROUND_CACHE_MAX_ROWS` (varsayılan 5.000) satır sınırını aşan en eski kayıtlar silinir.

### 8.4 `flight_cache.db` (SQLite, WAL)
- Konum: `TRAVELPLAN_FLIGHT_CACHE` veya repo kökü `flight_cache.db`. `"off"` (veya `0`, `none`, `false`) önbelleği kapatır. Test çalışmasında geçici dizinde izole dosya kullanılır.
- Canlı Google Flights aramalarının ayrıştırılmış sonuçları (L2 önbellek) ve takvim fiyatları (rota bazlı).
- Dolu ("ok") uçuş yanıtları `TRAVELPLAN_FLIGHT_CACHE_TTL_SECS` (varsayılan 45 dk), boş ("no_flights") yanıtlar `10 dk` saklanır; engel veya ayrıştırma hataları saklanmaz.
- Temizlik: Yazma sırasında süresi dolanlar ve en fazla `TRAVELPLAN_FLIGHT_CACHE_MAX_ROWS` (varsayılan 5.000) satır sınırını aşan en eski kayıtlar silinir.

### 8.5 Diğer dosyalar
- `config.json`: Telegram token, chat ID, bildirim açık/kapalı (`notifier` yazar). Ortam değişkenleri ile birleştirilir; dosyadaki alanlar öncelikli.
- `logs/server.log`: sunucu günlüğü, 5 MB × 3 döndürme (yalnızca sunucu süreci).
- Eski JSON dosyaları (`watches.json`, `price_history.json`, `alerts.json`) içe aktarım sonrası `.migrated.bak` olur.

---

## 9. Takip, tarama ve uyarılar

### 9.1 Takip türleri
- **Klasik takip** (`trip_query` yok): `target_budget_try` ve `include_multimodal`. Doğrudan uçuş ve çok modlu fırsatlar taranır.
- **Kurallı takip** (`trip_query` var): `TRIP_QUERY_KEYS` içindeki anahtarlar `TripPlanner.plan_trip`'e iletilir. Doğrudan bütçesi (`direct_budget_try`) ve yan yol bütçesi (`target_budget_try`) ayrıdır. `return_dates` varsa yan yol bütçesi gidiş + dönüş toplamına göre karşılaştırılır.

### 9.2 Tarama
- `DaemonTracker.scan_watch`: planı alır, en ucuz doğrudan ve yan yol seçeneklerini bulur. Fiyat okumasını takip geçmişine ekler.
- **Uyarı kuralı (kurallı takip):** doğrudan fiyat ≤ doğrudan bütçe veya yan yol fiyatı ≤ yan yol bütçesi **ve** o kategorinin fiyatı son bildirilen fiyattan düşük ise bildirim gönderilir (`_should_alert`).
- **Uyarı kuralı (klasik takip):** hedef bütçe altı veya çok modlu tasarruf > 1.000 ₺ ve çok modlu fiyat mevcut fiyattan düşükse.
- Uyarılar `notifier` üzerinden hem `alerts` tablosuna hem (yapılandırılmışsa) Telegram'a gider.
- `scan_all_watches`: yalnızca aktif takipleri tarar; taramalar arasında 1 sn bekler. Önce `tracker.load(prune=True)` ile başka süreçte silinen takipleri düşürür.

### 9.3 Zamanlama
- **Sunucu içi:** başlangıçtan 10 sn sonra, sonra her 6 saatte bir (`_run_daemon_worker`). Aktif takip yoksa tarama yapılmaz.
- **Hemen tarama:** yeni takip eklenince ve `POST /api/tracker/scan-now` ile.
- **Bağımsız süreç:** `python daemon_tracker.py --once` veya `--loop --interval 6`. Sunucu çalışırken bu kullanılırsa iki süreç aynı veritabanını paylaşır (SQLite kilitleriyle korunur).

---

## 10. Sağlık

`GET /api/health` (ve Telegram `/durum`) şunları döner:
- `flights`: Google Flights sağlık durumu (§7.3).
- `fx`: EUR/TRY kuru, kaynağı (`env`, `live`, `last-known`, `default`) ve canlı olup olmadığı.
- `watches`: aktif ve toplam takip sayısı.
- `search_log.recent`: son 10 arama.
- `resources`: tarayıcı çalışıyor mu, aktif sayfa sayısı, üst sınır, son aramadan beri sayı, süreç belleği (Windows).

Kur kaynağı zinciri (`fx.get_eur_try_rate`): `TRAVELPLAN_EUR_TRY` → canlı (12 saat önbellek; Frankfurter, sonra open.er-api.com) → son bilinen canlı değer (`last-known`) → sabit 54. Başarısız yenileme 10 dk tekrar denenmez. Fonksiyon hata fırlatmaz.

---

## 11. Telegram (`telegram_bot.py`)

- Uzun yoklama (`getUpdates`, 25 sn). Hata olursa üssel bekleme ile yeniden dener (`run_forever`).
- Yalnızca `telegram_chat_id` ile eşleşen sohbetin mesajları işlenir; diğerleri sessizce yok sayılır. 10 dk'dan eski mesajlar yok sayılır.
- Komutlar:
  - `/ara`: doğrudan uçuşlar (`google_flights_provider.search_flights`) ve çok modlu yönlendirici (`multimodal_router.search_multimodal`).
  - `/gezi`: rota motoru (`trip_planner.plan_trip`); `durak`, `bütçe`, `esnek`, `dönüş`, `bagajsız/valiz`, `üzerinden` ve `en geç` seçenekleri ayrıştırılır.
  - `/takip`: son `/ara` veya `/gezi` sorgusunu kullanarak kurallı takip oluşturur (`_last_queries` bellekte; yeniden başlatmada kaybolur).
  - `/liste`, `/sil <no>`, `/durum`, `/yardim`.
- Arama sırasında `_search_lock` tutulur; ikinci bir arama "meşgul" yanıtı alır (`is_busy`).
- Yanıtlar HTML olarak gönderilir; dinamik alanlar kaçırılır ve en fazla 80 karakterle sınırlanır. Mesaj 3.900 karakterde kesilir.

---

## 12. Eski yollar (legacy)

Bu yollar hâlâ çalışır ve testleri vardır; yeni geliştirme için kullanılmamalıdır.

| Yol | Ne kullanır | Durum |
| :--- | :--- | :--- |
| `/ara` (Telegram) | `multimodal_router` + doğrudan uçuşlar | Aktif. |
| `GET /api/price-matrix` | `price_history` + Google takvim fiyatları | Arayüz kullanmaz; `live-flights` içindeki matris aynı mantığı kullanır. |
| `TripPlanner._plan_legacy` | `multimodal_router` | Yalnızca `route_engine` verilmediğinde; sunucu her zaman verir. |
| `RailBusProvider.find_ground_next` | — | Üretim kodunda çağrılmaz; gün kayması rota motorunda `take_ground` içinde yapılır. |
| `GET /api/clusters` | `geo_clusters` | Yalnızca JSON bilgi ucu; arayüz kullanmaz. |

---

## 13. Eşzamanlılık, güvenlik ve hata toleransı

- **Sunucu:** `ThreadingHTTPServer`; her istek kendi iş parçacığında. `daemon_threads=True`.
- **Host ve Origin:** GET ve POST'ta `Host` yalnızca `localhost`, `127.0.0.1` veya `TRAVELPLAN_HOST` olmalıdır (DNS rebinding). POST'ta `Origin` yoksa (aynı kaynak veya curl) veya localhost:PORT ise kabul edilir.
- **Statik dosyalar:** yalnızca `STATIC_FILES` içindeki beş yol sunulur. Dizin geçişi yapılamaz.
- **Gövde:** en fazla 1 MB, JSON nesnesi olmalıdır.
- **Hata toleransı:** kaynak hataları (`find_ground`, uçuş araması, kur) çağıranı düşürmez. Her kaynak `None` veya boş liste döner ve bir uyarı eklenir.
- **Google durumu süreç genelinde:** `_blocked_until`, `_next_launch_at` ve tarayıcı işçisi sınıf düzeyindedir; tüm `GoogleFlightsProvider` örnekleri (sunucu ve daemon) aynı tarayıcıyı ve soğumayı paylaşır.
- **Telegram token:** `config.json` içinde düz metin; `GET /api/settings/telegram` yalnızca maskeli döner.
- **XSS:** arayüzde dış veriler `escapeHtml` ile yazılır.

---

## 14. Testler

- Çalıştırma: `python -m pytest -q` (`pytest.ini` içinde `testpaths = tests`); `python -m unittest discover tests` de çalışır (aşağıdaki izolasyon notuna bakın).
- `tests/conftest.py`: oturum başında `TRAVELPLAN_DB`, `TRAVELPLAN_SEARCH_LOG` geçici dizine yönlendirilir ve `TRAVELPLAN_NO_MIGRATE=1` ayarlanır. Her test kendi `travelplan.db` dosyasını kullanır (autouse fixture). Bu sayede `server_v2`'nin import sırasında oluşturduğu bileşenler gerçek veriye dokunmaz.
- Ağ çağrıları: Google Flights, RegioJet, FlixBus ve kur çağrıları stub'lanır (`mock.patch`). `tests/fixtures/` altında kayıtlı yanıtlar (Google kart JSON'ları, RegioJet ve FlixBus örnekleri, kur verisi) kullanılır.
- Örnek testler: `test_route_engine*.py` (motor kuralları, bütçe, DFS), `test_trip_planner.py`, `test_server_*.py` (HTTP doğrulama, güvenlik), `test_storage.py` (göç ve eşzamanlılık), `test_telegram_bot.py`, `test_daemon_filters.py`, `test_google_flights_health.py`, `test_resource_limits.py`, `test_city_centres_data.py` (veri dosyası), `test_unittest_isolation.py` (unittest izolasyonu).
- CI: `.github/workflows/tests.yml`; `push` ve `pull_request` (main), Ubuntu ve Windows, Python 3.12, yalnız `pytest` kurulur.
- Test izolasyonu (unittest dahil): `travel_engine/storage.py` içindeki `is_test_run()` test çalıştırmasını giriş noktasından algılar (`unittest` veya `pytest`). `TRAVELPLAN_DB` ayarlı değilse veritabanı süreç başına geçici bir dizinde olur (her JSON yolu için ayrı dosya), arama günlüğü `server_v2.py` tarafından geçici dosyaya yönlendirilir ve eski JSON göçü kapanır. `TRAVELPLAN_DB` açıkça verilirse koruma kapalıdır. `tests/test_unittest_isolation.py` bunu alt süreçlerde test eder.

---

## 15. Yeni koridor, şehir veya havalimanı eklemek

1. **Havalimanı:** `travel_engine/data/airports.csv` (OurAirports biçimi; `iata,name,city,country,lat,lon,size`). Şehir merkezi gerekiyorsa `city_centres.csv`.
2. **Şehir kümesi (eski yönlendirici için):** `GeoClusterRegistry._bootstrap_clusters` ve `CITY_COORDINATES`.
3. **Canlı RegioJet koridoru:** `live_rail_provider.py` içinde `REGIOJET_STATION_IDS` (istasyon ID'leri) ve gerekirse `LIVE_CORRIDORS`. Yeni çiftler için `search()` çoğu zaman zaten çalışır; ID'leri RegioJet konum listesinden alın.
4. **Statik tablo:** `rail_bus_provider.py` içinde `_corridors` sözlüğüne `(ilk kalkış, aralık, süre, fiyat, operatör, mod)` ekleyin. Bu veri tahmini sayılır.
5. **FlixBus'ı kapatmak veya değiştirmek:** `server_v2.py` içindeki `RailBusProvider(...)` çağrısından `bus_provider=` argümanını çıkarın ya da başka bir sağlayıcı verin. Testler (`test_ground_sources.py`) bu düzeni kapsar.
6. Test: `python -m pytest -q`.
