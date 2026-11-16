# 🗺️ TravelPlan Yol Haritası

> **Kimin için:** Sadece kendim. Çok kullanıcı, hosting, hesap sistemi yok — her iş "herhangi bir şehir çifti için kurallı aramayı ucuzlatıyor mu?" sorusuyla seçilir.
> **Son güncelleme:** 19 Ekim 2026 (kod ile karşılaştırılarak işaretlendi)

---

## Durum özeti

**Genel durum (19 Ekim 2026):** Faz 0–5 kodda uygulandı. Yerel testler yeşil: `pytest` 2038 geçti (+388 alt test), `unittest` 1599 OK (19 Ekim 2026; kabul regresyonları `tests/test_acceptance_chains.py` dahil). Canlı uçtan uca testler yapıldı: ülke geçişli arama (via-country), RegioJet + FlixBus ile yan yol (side route) aramaları, yalnız kara yolu (ground-only) senaryosu ve arayüzün masaüstü ve mobil ekran görüntüleri. Kalan işler ya kullanıcı aksiyonu (aşağıdaki bölüm) ya da aşağıda işaretli açık maddeler.

**Çalışıyor:**
- Rota motoru: otomatik 0–3 ara durak, sırayla verilen zorunlu duraklar, arama bütçesi, en geç varış / en erken kalkış, gidiş-dönüş, esnek tarih, minimum kalış.
- Google Flights canlı arama, sağlık izleme (3 ardışık hatada uyarı) ve engel algılama.
- RegioJet canlı tren verisi (gerçek saat ve EUR fiyat), statik tablo (21 koridor) ve mesafe tahmini; hepsi kaynağına göre etiketli.
- Canlı EUR/TRY kuru (Frankfurter, yedek open.er-api.com; `TRAVELPLAN_EUR_TRY` ile sabitlenebilir).
- Maliyet (bagaj, transfer, otel), aktarma riski, al/bekle önerisi.
- Takipler (klasik, kurallı, çok modlu), SQLite saklama, Telegram komutları (`/ara`, `/gezi`, `/takip`, `/liste`, `/sil`, `/durum`).
- Windows otomatik başlatma betiği (kurulum kullanıcı aksiyonu), GitHub Actions workflow'u hazır (çalıştırma kullanıcı aksiyonu).

**Kısmi veya bekliyor:**
- FlixBus sağlayıcısı kodda, testli ve sunucuda bağlı (`bus_provider`); arama uçları belgesiz.
- Transitous entegrasyonu: beklemede (lisans ve bildirim kararı kullanıcıda).
- Takvim fiyatlarıyla ön eleme, rota motorunda yok.
- Eski `optimizer.py` ve mock veri hâlâ `/api/routes` ve `/api/check-alerts` uçlarında.
- Canlı kabul testleri (§2) tekrarlanabilir değil; 19 Ekim çıktıları elle kaydedildi.

**Testler:** `python -m pytest -q`: 2038 geçti (+388 alt test), 0 başarısız (19 Ekim 2026 son çalıştırma). `python -m unittest discover tests`: 1599 OK. Unittest de geçici veritabanı kullanır (test izolasyonu, bkz. ARCHITECTURE §14). Şehir merkezleri: 10 büyük şehir referansa karşı test ediliyor; Wikidata ile otomatik doğrulama güvenilir çıkmadı (idari merkez koordinatları), diğerleri doğrulanmadı.

---

## Kullanıcı aksiyonu bekleyenler

Bunlar yalnızca sahibinin yapabileceği işler:

- **Telegram bot token ve chat ID:** Arayüzde "📲 Telegram" ayarlarından girilir (alarmlar ve `/gezi` komutları için).
- **GitHub Actions:** Workflow (`.github/workflows/tests.yml`) hazır; ilk çalıştırma GitHub hesabındaki ödeme/harcama limiti nedeniyle başlatılmadı. GitHub → *Settings → Billing & plans* kontrol edilmeli (public repoda Actions normalde ücretsizdir; hesap ödeme uyarısı giderilince otomatik çalışır).
- **Windows otomatik başlatma:** `powershell -File scripts/autostart_windows.ps1 -Install` (kurulmadı; bilgisayarda kalıcı zamanlanmış görev oluşturduğu için onay gerekir).
- **Transitous (Avrupa geneli tren tarifesi):** Kullanım koşulları için repoya açık kaynak lisansı (örn. MIT) eklenmeli ve Transitous ekibine Matrix kanalından bildirim yapılmalı; sonrasında entegrasyon hazır bir şablonla eklenebilir (teknik deneme başarılı).

---

## 1. Problem (tek cümle)

Bir yere gitmek istediğimde uçak çok pahalıysa sistem bana önce **ucuz uçuşu**, yoksa **yan yoldan** — yakın bir havalimanına uçup tren/otobüsle devam ederek — ulaşmanın en ucuz ve mantıklı yolunu bulsun ve fiyat düşerse haber versin.

> **Kapsam:** Avrupa şehirleri (Türkiye dahil). Dünya geneli kapsam bu sürümde hedef değil.

### Çekirdek fikir: A ➔ B yerine A ➔ C ➔ … ➔ B zincirleri

Direkt A ➔ B pahalıysa, araya bir veya birkaç ara durak (C, D) koyarak daha ucuza ulaşmak. Ara durak A'ya yakın, B'ye yakın ya da ikisinin arasında olabilir; her bacak uçak, tren veya otobüs olabilir.

| Kalıp | Örnek | Bugün |
| :--- | :--- | :--- |
| **A ✈ B** (direkt / aktarmalı uçuş) | Atina ✈ Prag | ✅ |
| **A ✈ C 🚆 B** — C, **B'ye** yakın | Atina ✈ Bratislava 🚆 Prag | ✅ (8 şehir kümesi) |
| **A 🚌 C ✈ B** — C, **A'ya** yakın | İstanbul 🚌 Selanik ✈ Prag | ⚠️ Motor destekler; otobüs verisi FlixBus'tan (belgesiz arama) gelir, bulunamazsa tahmin |
| **A 🚌 C ✈ D 🚆 B** — iki tarafta da yan yol | İstanbul 🚌 Selanik ✈ Bratislava 🚆 Prag | ⚠️ Otobüs bacağı tahmine düşebilir |
| **A ✈ C ✈ D 🚆 B** — arada ayrı biletli ucuz uçuşlar | Atina ✈ Budapeşte ✈ … 🚆 Prag | ✅ Motor destekler (düşük maliyetli hub listesi) |
| **A → C (1 gece) → B** — durakta gezme | İstanbul 🚌 Selanik (gezi) ✈ Bratislava 🚆 Viyana 🚆 Prag | ⚠️ Minimum kalış var; gezi rotası arayışı yok |

**Hedef motor:** şehirleri düğüm, uçuş/tren/otobüs seferlerini kenar kabul eden bir graf üzerinde; saat ve aktarma payı kurallarına uyan en iyi K zinciri bulmak. → Uygulandı (`travel_engine/route_engine.py`).

### Kaç durak? İki ayrı mod

| Mod | Kim seçer | Sınır | Neden |
| :--- | :--- | :--- | :--- |
| **Otomatik keşif** — motor C/D'yi kendisi bulur | Kullanıcı `max ara durak` seçer | **Varsayılan 2, en fazla 3** | Her durak kombinasyonu katlar (aşağıdaki tablo) |
| **Sabit duraklar** — "Budapeşte ve Viyana üzerinden" | Kullanıcı durakları ve sırayı verir | Sınır koda konmadı | Sıra belli → her bacak tek arama |
| Karışık — "Budapeşte'den geç, gerisini sen bul" | İkisi birden | — | Kodda ayrı bir mod olarak yok; sabit duraklar veya otomatik keşif seçilir |

Otomatik keşifte maliyet (her adımda ~5 aday, canlı uçuş araması ~5 sn varsayımıyla):

| Ara durak | Uçuş araması | Süre (planlama tahmini, ölçülmedi) |
| :--- | :--- | :--- |
| 0 | 1 | ~5 sn |
| 1 | ~10 | ~20 sn |
| 2 | ~35 | ~1 dk |
| 3 | ~185 | ~5 dk + Google engelleme riski |

> Bugünkü kod varsayılan olarak 1 Chromium sayfası kullanır (`TRAVELPLAN_MAX_BROWSERS` ile artırılabilir) ve aramalar arasında en az 2 sn bekler. Süreler ölçülmedi.

**Asıl sınır durak sayısı değil, uçuş araması bütçesi** (varsayılan sorgu başına ≤20, 1–60 arasında ayarlanabilir). Pahalı olan sadece uçuş bacakları; tren/otobüs bacakları tablodan/API'den gelir. Bütçe dolunca motor kısmi sonuç döner, uyarı verir ve arayüzde "Derin arama (bütçe ×2)" düğmesi çıkar. Bütçeyi verimli kullanmak için: önbellek ve geçmişte ucuz çıkan durakları öne alma uygulanır; takvim fiyatlarıyla ön eleme **uygulanmadı** (bkz. Faz 2a).

## 2. Örnek test senaryoları

> Bu bölümdeki senaryolar gerçek bir seyahat planı değil, **test girdileridir**. Her biri sistemin ele alması gereken bir kural türünü gösterir: tarih penceresi, mutlak varış saati (en geç … ), zorunlu geçiş ülkesi, aynı gün varış ve ayrı bütçeler (direkt / yan yol). Motor bu kuralları **herhangi bir Avrupa şehir çifti için** (Milano, Paris, Barselona, Londra…) uygulamalıdır; senaryoya özel kod yazılmaz.

### Test 1 — Atina ➔ Prag (28 Ekim, en geç 12:00)

Kural türleri: tarih penceresi, mutlak varış saati (en geç 28 Ekim 12:00).

#### Canlı arama ham sonuçları (test sonucu, 19 Ekim)

Google Flights, tek yön, 28 Ekim:

| Hedef havalimanı | En ucuz uçuş | Not |
| :--- | :--- | :--- |
| **Prag (PRG)** | **16.286 ₺** (KLM, 1 aktarma, 13:45–19:30) | Direkt uçuş yok |
| Bratislava (BTS) | **4.447 ₺** (Wizz Air, 23:50–01:05⁺¹) | Direkt |
| Bratislava (BTS) | **5.524 ₺** (Ryanair, 07:55–09:10) | Direkt |
| Budapeşte (BUD) | 8.894 ₺ (Wizz Air, 09:55–11:05) | Direkt |
| Viyana (VIE) | 13.281 ₺ (Ryanair, 19:55–21:20) | Direkt |
| Münih (MUC) | 13.366 ₺ (Aegean, 15:45–17:25) | Direkt |
| Berlin / Dresden | 18.080 ₺ / 28.445 ₺ | Aktarmalı, pahalı |

**Beklenen cevap (Test 1):**

> 🏆 **Önerilen:** Ryanair Atina ➔ Bratislava 07:55–09:10 (5.524 ₺) → havalimanından gara geçiş (~30 dk, ~€1-2) → Bratislava ➔ Prag treni (RegioJet/ČD, ~4 sa 20 dk; tahmini tabloya göre 12:00 kalkış).
> **Toplam ≈ 6.200 ₺ — direkt rotaya göre ≈ 10.000 ₺ tasarruf. Aynı gün ~16:30'da Prag'dasın.** *(Tren saati/fiyatı gerçek siteden doğrulanmalı.)*
>
> 💸 **En ucuz ama yorucu:** Wizz Air 23:50 (4.447 ₺) → Bratislava'ya **gece 01:05** iniş → sabah 06:00 trenine kadar **5 saat bekleme** (veya ~1 gece otel) → Prag 10:20 (**29 Ekim**).
> Gerçek toplam ≈ 5.100 ₺ + otel. Sadece ~900 ₺ daha ucuz; gece uykusuz.
>
> ⚠️ Uçak ve tren ayrı bilet: uçak gecikirse tren biletini sen kaybedersin. Treni esnek/iade edilebilir al veya 2+ saat pay bırak.

#### Motor bu cevabın ne kadarını veriyor? (test sonucu, 19 Ekim, Faz 0 sonrası)

| Sıra | Gerçek toplam | Rota |
| :--- | :--- | :--- |
| 1 🏆 | 6.174 ₺ | Ryanair ATH➔BTS 07:55–09:10 → tren 12:00–16:20 (aynı gün) |
| 3 | 7.297 ₺ | Wizz ATH➔BTS 23:50–01:05 → 🌙 295 dk gece beklemesi (+~2.200 ₺ otel) → tren 06:00–10:20 (+1 gün) |
| 5 | 9.994 ₺ | Wizz ATH➔BUD 09:55–11:05 → tren 14:00–20:50 |

Direkt en ucuz: 16.509 ₺. Aşağıdaki liste Faz 0 öncesi durumdur:

#### Faz 0 öncesi motor bu cevabın ne kadarını veriyordu? (test sonucu, Faz 0 öncesi)

- ✅ Bratislava kombinasyonunu buldu, ~11.000 ₺ tasarrufu hesapladı.
- ❌ İlk 5 sonucun hepsi **aynı Wizz uçuşu**, sadece tren saati farklı → daha mantıklı Ryanair seçeneği listenin altında kaldı. *(Faz 0'da düzeltildi.)*
- ❌ Gece 01:05 → 06:00 arası 5 saatlik bekleme "transfer, 0 ₺" olarak gösterildi; uyarı yok, otel maliyeti yok. *(Faz 0'da düzeltildi.)*
- ❌ Varışın **ertesi gün (29 Ekim)** olduğu belirtilmedi. *(Gezi kartlarında varış tarihi-saati artık açık yazılıyor; ayrı "+1 gün" rozeti yalnızca eski çok modlu kartlarda var.)*
- ❌ **Budapeşte** Prag'ın çevre havalimanı listesinde değil. *(Faz 0'da eklendi.)*
- ❌ Tren saat ve fiyatları **tahmini** sabit tablodan geliyordu. *(Artık RegioJet'ten gerçek veri gelebiliyor; yoksa "tahmini" etiketi var.)*

**Test 1 kabul testidir.** Motor yukarıdaki beklenen cevabı kendisi üretene kadar Faz 1 bitmiş sayılmaz. Durum: 19 Ekim çıktısında büyük ölçüde sağlanmış görünüyor, ancak bu kontrol otomatik bir test olarak yazılmadı; canlı doğrulama gerekiyor.

### Test 2 — Prag ➔ İstanbul (31 Ekim, Yunanistan üzerinden, aynı gün varış)

Kural türleri: tarih penceresi (31 Ekim), zorunlu geçiş ülkesi (Yunanistan), aynı gün varış.

- **Girdi:** kalkış Prag, varış İstanbul, tarih 31 Ekim 2026, zorunlu geçiş: Yunanistan (Yunan bir şehirde bağlantı), varış aynı gün (31 Ekim).
- **Beklenen davranış:**
  - Sonuçlar Yunanistan'dan geçer (ör. Prag ✈ Atina / Selanik, ardından İstanbul'a uçak veya otobüs). Yunanistan'dan geçen zincir yoksa bunu açıkça söyler.
  - Ayrı biletli bağlantılarda ≥3 saat pay uygulanır ve ayrı bilet uyarısı gösterilir.
  - 31 Ekim'de varmayan seçenekler elenir veya "+1 gün" rozetiyle ayrıca işaretlenir.
  - Gece beklemesi ≥3 saat ise "🌙" uyarısı ve tahmini otel alanı çıkar.
- **Canlı test:** 19 Ekim'de canlı çalıştırıldı (kullanıcı bildirimi; QA bu turda yeniden çalıştırmadı): 10 seçenek, Yunanistan üzerinden (Atina), en iyi **15.762 TRY**, tarih 2026-11-15.

---

## 3. Bugünkü durum (kodla doğrulanmış envanter)

| Bileşen | Durum |
| :--- | :--- |
| Canlı uçuş fiyatı (Google Flights scraping) | ✅ Çalışıyor. Sayfa yapısı değişirse "sorunlu" sağlık uyarısı gelir (eskiden sessiz 0 sonuç) |
| Rota motoru (A ➔ C ➔ … ➔ B) | ✅ Otomatik 0–3 ara durak, sabit duraklar, arama bütçesi, zaman kuralları |
| Kara yolu verisi | ✅ RegioJet ve FlixBus canlı (belgesiz arama; RegioJet'in bildiği şehir çiftleri); her bacakta `source` ve `price_is_estimated` var; diğerleri statik tablo veya mesafe tahmini, "tahmini" etiketli |
| FlixBus | ⚠️ Sunucuda bağlı (`bus_provider`); belgesiz arama uçları, bulunamazsa RegioJet / tablo / tahmin |
| Gerçek toplam maliyet (bagaj, transfer, otel) | ✅ Tahmini tablolarla; tahmin olduğu işaretli |
| Aktarma riski | ✅ Düşük / orta / yüksek; ayrı biletli bağlantılarda 180 dk pay zorunlu |
| Fiyat takibi + Telegram alarmı | ✅ Doğrudan, kurallı ve çok modlu takipler; Telegram komutları |
| Güvenlik | ✅ Sadece localhost; Host ve Origin kontrolü; arayüzde HTML kaçışı |
| Testler | ✅ 2038 test geçer (+388 alt test, pytest), 0 başarısız; unittest 1599 OK; CI workflow'u hazır (Ubuntu ve Windows), çalıştırma kullanıcı aksiyonu |
| Kalkış tarafı aktarma (İstanbul ➔ Selanik otobüsü) | ⚠️ Motor aday üretir; otobüs verisi FlixBus'tan, bulunamazsa tahmin |
| Çok duraklı zincirler (A ➔ C ➔ D ➔ B) | ✅ Motor destekler (otomatik en fazla 3 durak, sabit sırayla) |
| Gidiş-dönüş kombinasyonları | ✅ `roundtrip.py` |
| Ara şehirde konaklamalı "macera" rotaları | ⚠️ Minimum kalış ve gece beklemesi var; gezi rotası arayışı yok |
| Eski `optimizer.py` / mock veri | ⚠️ `/api/routes` ve `/api/check-alerts` hâlâ kullanıyor; arayüz kullanmıyor |

---

## 4. Fazlar

### 🔥 Faz 0 — İlk kullanılabilir sürüm
Amaç: aracı gerçekten kullanmak ve §2 Test 1'deki ❌'leri kapatmak.

- [x] **Aynı uçuşun varyantlarını birleştir:** rota motoru bir uçuş seti için en fazla 2 seçenek bırakır (`MAX_OPTIONS_PER_FLIGHT_SET`).
- [x] **Gece beklemesi uyarısı:** 00:00–06:00 arasında ≥3 saat bekleme → "🌙" rozeti + tahmini otel maliyeti.
- [~] **Ertesi gün varış:** Gezi kartlarında varış tarihi-saati açık yazılır. "+N gün" rozeti yalnızca eski çok modlu kartlarda (`/api/multimodal-search`) var.
- [x] **Sıralamayı insanca yap:** `gerçek toplam + süre × zaman değeri + gece cezası + risk cezası`. Zaman değeri arayüzden ayarlanır (varsayılan 150 ₺/saat).
- [x] **Budapeşte'yi Prag kümesine ekle** (+ Münih → Prag otobüs, tahmini).
- [x] **Kurallı arama:** tarih aralığı, en geç varış, zorunlu geçiş, ayrı bütçeler. `/api/trip-search` + "🧳 Gezi Planı" bölümü.
- [x] **Kurallı takipler:** direkt ve yan yol için ayrı bütçe; trip watch tarihleri ve tüm kuralları saklar; alarm ayrı bütçelere göre.
- [ ] Gidiş/dönüş takiplerini kur ve Telegram'ı aç. *Kişisel kurulum adımı. Kod hazır (`return_dates` ile takip destekleniyor); bu çalışma kopyasında `config.json` bulunmuyor.*
  - *Test sonucu (19 Ekim): QA doğrulayamadı; kişisel kurulum adımı (Telegram ve `config.json` yok).* Kod tarafı: kurallı trip watch POST, GET ve scan-now geçti (201/200). Watch geçici DB'de kalıcı; tarama 11.654 TRY kaydetti, hedef 5.000 TRY olduğu için alarm yok. Gidiş/dönüş (`return_dates`) takibi canlı test edilmedi.
- [x] ~~Tren saatini elle kontrol et~~ → RegioJet'ten gerçek saat ve fiyat (Faz 1).
- [x] *(Opsiyonel)* Telegram `/ara Atina Prag 28.10` → en iyi sonuçlar.

### 📡 Faz 1 — Güvenilir veri (Kasım)
Amaç: sistemin söylediği fiyata güvenebilmek.

- [x] **Gerçek tren verisi:** RegioJet API'si (belgesiz, kimlik doğrulamasız) ile RegioJet'in bildiği şehir çiftleri için gerçek saat ve EUR fiyat. DB HAFAS (`v6.db.transport.rest`) kalıcı olarak kapandı; istekler 503 döner.
- [~] **Daha fazla koridor:** RegioJet genel arama sayesinde birçok çift canlı. ÖBB, ČD, DB ve Hellenic için canlı kaynak yok (statik tablo). FlixBus sunucuda bağlı (belgesiz arama). **Beklemede: lisans + Transitous'a bildirim gerekli (kullanıcı kararı).** Transitous entegrasyonu yapılmadı.
- [x] **Tren fiyatları EUR olarak saklanır** (`price_eur`); kur canlı (`fx.py`: Frankfurter → open.er-api.com, 12 saat önbellek) ve `TRAVELPLAN_EUR_TRY` ile sabitlenebilir.
- [x] **Scraper sağlık alarmı:** 3 ardışık hatalı arama → web uyarısı ve Telegram; düzelince bildirim.
- [~] **Kaydedilmiş Google Flights örnekleriyle testler:** kart verisi JSON'ları (`tests/fixtures/google/`) ile testler var. Ham HTML örneği yok.
- [~] **GitHub Actions:** her push ve PR'da testler (Ubuntu ve Windows, Python 3.12). workflow hazır; hesap faturalama nedeniyle çalışmadı (kullanıcı aksiyonu).
- [x] **Arama kaydı:** her canlı arama `search_log.db` dosyasına yazılır (rota, tarih, sonuç, en ucuz fiyat; 180 gün, en fazla 50.000 satır).
- [x] **Google'a nazik ol:** aramalar arası en az 2 sn; engel sayfası algılanınca 15 dakika canlı arama durur; Telegram uyarısı.
- ✅ Kabul: §2 Test 1 cevabı 19 Ekim'de elle doğrulandı (~yukarıdaki tablo); tekrarlanabilir otomatik kabul testi ve canlı doğrulama hâlâ gerekli. **[~]**

### 🧭 Faz 2 — Genel rota motoru: A ➔ C ➔ … ➔ B
Amaç: §1'deki tablodaki her kalıbı tek bir motorla aramak.

**2a — Aday ara durakları üret**
- [x] **Kara yolu komşuları:** AirportDB ve mesafe yarıçapları (`ground_reachable`, `ground_distance_km`) ile veriden aday üretilir; elle tutulan 8 şehir listesi kullanılmaz.
- [x] **Kalkış tarafı adayları** (A'ya yakın havalimanları) ve **varış tarafı adayları** (B'ye yakın).
- [x] **Düşük maliyetli hub listesi** (`LCC_HUBS`, 27 havalimanı) — ayrı biletli ucuz uçuş zincirleri için.
- [x] **Aday sayısı sınırlı ve önceliklendirilmiş:** kategori bütçe payları, SearchLog geçmişi, yol mesafesi; bütçe varsayılan 20.
- [x] **Uzak yakın şehirler (düşük öncelik, `far_side`):** LCC veya büyük havalimanı; kara yolu en fazla 650 km; canlı (RegioJet/FlixBus) ya da tablo bağlantısı olan. Tahmini bağlantı yalnız bütçe ≥ 20 ise. Bütçe payı %10; önce LCC, sonra küçük listedeki büyük şehirler, sonra diğer büyük havalimanları (en yakın önce). Yalnızca kara yolu sorgusu yapılır; uçuş bütçesi kullanılmaz.
- [x] **Sabit duraklarda kara bacağı:** iki durak arasında hem uçuş hem kara (en fazla 800 km yol); kara seferleri en erken saatle ve akşam (18:00+) için ayrı sorgulanır, sonuç sınırı 12.
- [ ] **Takvim ön elemesi:** rota motoru takvim fiyatlarını kullanmıyor. Her tarih ve çift bütçeden sayılır.

**2b — Graf araması**
- [x] Düğüm = şehir/havalimanı, kenar = sefer (uçuş, tren, otobüs, havalimanı ⇄ şehir transferi); zaman damgalı durum araması (DFS).
- [~] **Parametreler:** `max ara durak` (0–3, varsayılan 2) ✅, `zorunlu duraklar` (sıralı) ✅, `uçuş araması bütçesi` ✅, aktarma kuralları ✅ (şehirden uçuşa: transfer süresi + 120 dk; ayrı biletli uçuşlar arası 180 dk; kara bacakları arası 30 dk), en geç varış ve en erken kalkış ✅. **Maksimum seyahat süresi** rota motorunda yok (yalnız eski `/api/multimodal-search` ucunda).
- [x] **Sabit durakta havalimanı geçişi:** durak havalimanında duraksız geçilince şehre gidip dönmek yerine havalimanı → havalimanı (ayrı bilet, 180 dk); kalış varsa ya da bir sonraki bacak kara yoluyla şehirden çıkıyorsa şehir transferi kullanılır. Regresyon testi: `ViaAirportPassThroughTests` (sahte veri).
- [x] Bütçe dolunca kısmi sonuç + uyarı + "Derin arama (bütçe ×2)".
- [x] Sonuç: en iyi zincirler; aynı uçuş setinden en fazla 2 seçenek (liste aynı uçuşla dolmaz).
- [ ] **Eski `optimizer.py` yeni motorla değiştirilmedi:** hâlâ `/api/routes` ve `/api/check-alerts` uçlarında, sahte veriyle çalışıyor.

**2c — Karşılaştırma ve güven**
- [x] **Gerçek toplam maliyet:** bagaj ücreti (kabin/kayıtlı seçeneği), havalimanı ⇄ gar transferi, gece beklemesinde otel. Değerler tahmini, işaretli.
- [x] **Aktarma risk puanı:** düşük / orta / yüksek (renkli rozet). Ayrı biletler, pay süresi, gece geçişi dikkate alınır.
- [x] **Gidiş-dönüş kombinasyonları:** gidiş ve dönüş ayrı optimize edilir, uygun çiftler (`roundtrip.py`) listelenir.

**Kabul testleri**
- [~] §2 Test 1 (Atina ➔ Prag): A ✈ C 🚆 B cevabı ve Budapeşte denemesi 19 Ekim'de görüldü; otomatik test yok.
- [x] İstanbul ➔ Prag: İstanbul 🚌 Selanik ✈ Bratislava 🚆 Prag zinciri. Motor yeteneği: ✅ (sahte veriyle `test_acceptance_chains` geçiyor: gece otobüsü → ertesi gün uçuş → tren). Canlı (19 Ekim, 12–13 Kasım): zincir değerlendirildi ama gerçek tarifede o tarihlerde 120 dk havalimanı kuralına uyan bağlantı yok (35 dk eksik) — motor doğru olarak eledi.
  - *Canlı, otomatik mod, bütçe 60 (19 Ekim, 11-12 ve 11-13, `max_stops=2`):* 60 uçuş araması (bütçe doldu), 10 seçenek, hepsi SAW kalkışlı (4 doğrudan SAW → PRG, 6 SAW → BER + Berlin → Prag otobüsü); Selanik/SKG seçeneği yok. SKG aday listesinde (`origin_far`) ve yalnız 13 Kasım SKG → PRG araması yapıldı (10 sonuç); İstanbul → SKG tarafı uzak aday bütçesi dolduğu için tam taranamadı (uyarı: uzak yakın şehirler). Selanik zincirinin bulunup bulunmadığı kanıtlanamadı: yanıt yalnız en iyi 10 seçeneği taşıyor ve kara yolu aramaları günlüğe yazılmıyor.
  - *Test sonucu (19 Ekim): FAIL.* Otomatik arama (2026-11-12, bütçe 30, en fazla 2 durak): 10 seçenek, hiçbiri Selanik üzerinden değil. Aday çıkış havalimanları yalnız IST ve SAW; Selanik (SKG) aday değil. Sebep: origin yarıçapı 300 km düz hat, İstanbul–Selanik ~510 km. Sabit duraklı arama (Selanik, Bratislava; 11-12 ve 11-13): 8 seçenek, hepsi IST→SKG uçuşuyla başlıyor (SKG→Selanik ve geri tahmini bacaklar, SKG→BTS uçuşu, BTS→Prag); İstanbul→Selanik otobüs bacağı hiçbirinde yok. En ucuz gerçek toplam ≈ 11.654 TRY. Olası neden (kod doğrulanmadı): İstanbul→Selanik kara bacağı üretilmiyor (kara veri kaynağı yok ya da mesafe/yarıçap kuralı).
  - *Kod düzeltmesi ve yeni canlı sonuç (19 Ekim, sonra): FAIL.* Otomatik test: `tests/test_acceptance_chains.py` (sabit durak: gece otobüsü + ertesi gün uçuşu; otomatik: uzak aday). Canlı sabit duraklar (`via=Selanik,Bratislava`, 11-12 ve 11-13, bütçe 30): 10 seçenek, hepsi uçuşla başlıyor; İstanbul→Selanik otobüsü içeren seçenek yok. Sebep: canlı FlixBus 23:00 → 12:25 (+1) kalkışlı bir otobüs veriyor; aynı gün SKG→BTS uçuşu 14:35. Şehirden havalimanına transfer (45 dk) + 120 dk aktarma kuralı ile en erken uçuş 15:10; 14:35 uçuşa yetişilmiyor. Motor artık bu bacağı değerlendiriyor (kural ihlali yok).
  - *Otomatik mod (`max_stops=2`, bütçe 30), canlı: FAIL.* Selanik (SKG) artık uzak aday olarak bulunuyor (canlı FlixBus bağlantısıyla; `candidates.origin_far`), ama %10 uzak-aday payı ve toplam bütçe, SKG'nin çiftlerine sıra gelmeden tükendi. Sonuç: 10 seçenek, hiçbiri İstanbul→Selanik otobüsü içermiyor. Keşif için daha büyük bütçe ya da uzak adayların önceliklendirilmesi gerekir.
  - *Canlı QA, 19 Ekim 2026 (sunucu 8899, `via=Selanik,Bratislava`, bütçe 30), tarihler 10-28, 11-01, 11-15: FAIL.* Sağlayıcı taraması: 10-28 23:00 → 12:25 (+1) FlixBus ile SKG→BTS 10-29 15:20 uçuşu, 45 dk transfer + 120 dk kuralına (en erken 15:10) göre sınırda uygun; 11-01 → 11-02 15:50 ve 11-15 → 11-16 15:50 de uygun. Motor aynı otobüsü 13:25 olarak okuyor (FlixBus'ın 12:25'inden +1 saat; 25 Ekim saat değişiminden sonra Yunanistan ile İstanbul arasındaki fark; olası saat dilimi karışıklığı, kodda doğrulanmadı) ve 15:20 uçuşu eliyor. Üç tarihli canlı arama (21 uçuş araması): 10 seçeneğin tamamı 11-15 tarihli ve IST→SKG uçuşuyla (metro + uçuş) başlıyor; İstanbul→Selanik otobüs bacağı yok, 10-28 ve 11-01 seçeneklerinin hiçbiri ilk 10'a girmedi. Yalnız 10-28 ile tekrar (7 uçuş araması, 9 seçenek): otobüs bacağı yok, en erken SKG→BTS 10-29 15:20 uçuşu geliyor ama IST→SKG ile bağlanıyor. Sonraki adım: saat dilimi karşılaştırmasını düzelt ve tek tarihte yeniden test et.
  - *Saat dilimi düzeltmesi sonrası canlı retest, 19 Ekim 2026 (sunucu 8899, izole veritabanı, `TRAVELPLAN_TRANSITOUS` kapalı, `via=Selanik,Bratislava`, bütçe 30, tarihler 10-28, 11-01, 11-15): PASS.* 10 seçenek, 21 uçuş araması. Zincir içeren seçenek (toplam ≈ 10.035 TRY, tahmini bacaklar dahil): 🚌 İstanbul → Selanik (FlixBus, 28 Ekim 23:00 İstanbul yerel, UTC+3 → 29 Ekim 12:25 Atina yerel, UTC+2, yani 10:25 UTC), Selanik → SKG (tahmini transfer, 13:10 varış), ✈ SKG → BTS (Google Flights, 29 Ekim 15:20 → 16:10 Bratislava yerel), BTS → Bratislava (tahmini transfer), 🚌 Bratislava → Prag (FlixBus, 17:15 → 21:35 Prag yerel). Önceki turda motor aynı otobüsü 13:25 okumuştu; bu turda FlixBus'ın 12:25 Atina yerel saati gösteriliyor (saat dilimi düzeltmesi). 1 Kasım tarihli varyantlar da aynı zinciri veriyor; 15 Kasım kalkışı için uygun seçenek yok (uyarı).
  - *Aynı sunucu, `via=Selanik,Bratislava,Viyana`, `min_stay=Selanik:480;Viyana:600`, 28 Ekim, bütçe 30: PASS (yeniden).* 10 seçenek, HTTP 200 (84 sn). En ucuz seçenek ≈ 15.011 TRY: 🚌 İstanbul → Selanik (28 Ekim 23:00 → 29 Ekim 12:25 Atina), Selanik'te 8 saat (480 dk), ✈ SKG → BTS (30 Ekim 14:35 → 15:25), 🚌 Bratislava → Viyana (31 Ekim 02:45 → 04:30), Viyana'da 10 saat (600 dk), 🚌 Viyana → Prag (31 Ekim 14:30 → 18:40). Uyarı: "Arama sınırına ulaşıldı; sonuçlar eksik olabilir" geldi (`budget_exhausted` false).

### 🎒 Faz 3 — Macera modu (duraklarda gezme)
Amaç: Faz 2 motoruna "durakta X saat / 1 gece kal" seçeneği eklemek.

- [x] Ara durakta minimum kalış süresi (`min_stay`, dakika, durak adıyla; sonuçta `stopover` alanı).
- [~] Konaklamalı geceler: gece beklemesi ve otel tahmini var. Gezi için bağımsız bir "1 gece kal" rota modu yok.
- [x] Kabul: plan.md'deki "Gidiş 2" (İstanbul 🚌 Selanik — gezi ✈ Bratislava — 1 gece 🚆 Viyana — 1 gece 🚆 Prag) rotasını motor kendisi bulur. Duraklı kalış, otel ve çoklu durak ✅ (canlı 10 seçenek, 2 otel gecesi; sahte veriyle `test_acceptance_chains` geçiyor). İlk bacağın otobüs olması tarifeye bağlı: canlı tarifede o tarihlerde İstanbul → Selanik otobüsü uygun bağlantı vermedi (bkz. Faz 2), motor eledi.
  - *Test sonucu (19 Ekim): FAIL.* Sabit duraklar Selanik, Bratislava, Viyana; `min_stay` Selanik 480 dk, Viyana 600 dk; 2026-11-12: 0 seçenek, 6 uçuş araması, 8 sn. Uyarı: "48 uçuş bulundu ama aktarma süresi/son varış saati kurallarına uymadı". Olası neden (kod doğrulanmadı): kalış sonrası bacaklar yalnız aynı kalkış tarihinde aranıyor (searched_dates = [2026-11-12]), bu yüzden gece sonrası uçuşlar uymuyor.
  - *Kod düzeltmesi sonrası canlı sonuç (19 Ekim, sonra): kalış kısmı PASS, Gidiş 2 rotası FAIL.* Kalış sonrası bacaklar ertesi günlerde aranıyor; `stopovers` her iki durağı (Selanik 480, Viyana 600 dk) ve otel gecelerini (2 gece) taşıyor. Canlı: 0 → 10 seçenek. Ancak hepsi IST→SKG uçuşuyla başlıyor; plandaki İstanbul 🚌 Selanik bacağı yok (aynı canlı otobüs saatleri nedeniyle, bkz. Faz 2). Ayrıca motor DFS sınırına ulaştı (uyarı: sonuçlar eksik olabilir).
  - *Canlı QA, 19 Ekim 2026 (2026-10-28, `via=Selanik,Bratislava,Viyana`, `min_stay=Selanik:480;Viyana:600`, bütçe 30): PASS.* 10 seçenek, 12 uçuş araması, HTTP 200 (181 sn). İlk seçenek (en ucuz gerçek toplam ≈ 15.011 TRY, tahmini bacaklar dahil): 🚌 İstanbul → Selanik (Kamil Koç/FlixBus, 10-28 23:00 → 10-29 13:25, 2.897 TRY), Selanik'te 8 saat kalış (480 dk), ✈ SKG → BTS (Ryanair, 10-30 14:35 → 15:25, 4.007 TRY), BTS → Bratislava (tahmini transfer), 🚌 Bratislava → Viyana (FlixBus, 10-31 02:45 → 04:30), Viyana'da 10 saat kalış (600 dk), 🚌 Viyana → Prag (FlixBus, 10-31 14:30 → 18:40). Notlar: uçuş 10-29 15:20 değil 10-30 14:35 seçildi, çünkü Selanik kalışı 8 saat; SKG'ye 22:10'da varıp 14:35'e kadar havalimanında beklemek seçenekte gece bekleme olarak kalıyor. Uyarı: "Arama sınırına ulaşıldı; sonuçlar eksik olabilir" (bütçe 30 yetti, `budget_exhausted` false; uyarı yine de geldi).

### 📲 Faz 4 — Telefondan kullanım
Amaç: bilgisayar başında olmadan sorgu ve takip.

- [x] **Telegram bot komutları:** `/ara`, `/gezi`, `/takip`, `/liste`, `/sil`, `/durum`, `/yardim`. Yalnızca sahip sohbetinden kabul edilir.
- [x] **Bilgisayar açılınca otomatik başlat:** `scripts/autostart_windows.ps1` (Windows zamanlanmış görev).
- [x] **JSON dosyaları yerine SQLite:** takipler, fiyat geçmişi, uyarılar (`travelplan.db`); arama kaydı ayrı `search_log.db`. Eski JSON dosyaları bir kez içe aktarılır.

### 🧠 Faz 5 — Akıllı öneriler
- [x] **Esnek tarih:** arama ±0–3 gün (`flex_days`); trip watch da aynı kuralı saklar. "En ucuz gün" ve tasarruf raporlanır.
- [x] **Fiyat geçmişinden "şimdi al / bekle" önerisi** (`advisor.py`): sezgisel; en az 3 gözlem gerekir, az veride "belirsiz" döner.
- [x] **Aynı rotayı birden fazla tarihte inceleme:** en fazla 7 kalkış tarihi; sonuçta en ucuz gün ve kazanç.

---

## 5. Riskler

| Risk | Etki | Önlem (durum) |
| :--- | :--- | :--- |
| **Google Flights tek veri kaynağı** — sayfa yapısı değişir veya captcha/engelleme gelir | Canlı uçuş verisi durur | ✅ 10 dk önbellek, aramalar arası 2 sn, engel algılama + 15 dk soğuma, sağlık uyarısı (Telegram ve web), arama bütçesi. ⚠️ Yedek uçuş kaynağı yok; Amadeus adaptörü yalnız eski `/api/routes` ucunda. Takvim ön elemesi yok. |
| Ücretsiz tren/otobüs API'leri erişilemez / kapanır | Gerçek saat kaybolur | ✅ Kaynak sırası: RegioJet → statik tablo → mesafe tahmini. Her biri kaynağına göre etiketli. DB HAFAS kapandı; bu kaynak kaldırıldı. |
| Belgesiz API'ler (RegioJet, FlixBus) değişir | Parse hataları | ✅ Her hata `None` döner, diğer kaynak çalışmaya devam eder. FlixBus sunucuda bağlı; belgesiz uç değişirse yalnızca o kaynak düşer. |
| Ayrı biletli zincirlerde kaçırılan bağlantı | Gerçek para kaybı | ✅ 120 dk şehir→uçuş, 180 dk ayrı bilet bağlantısı, risk puanı, ayrı bilet uyarısı. |
| Kapsam şişmesi (kişisel araç ürüne dönüşür) | Hiçbir faz bitmez | Her iş "herhangi bir şehir çifti için kurallı aramayı ucuzlatıyor mu?" testinden geçer; §6 kapsam dışı listesi |
| Maliyet tabloları yanlış | Yanlış toplam | ✅ Tümü `tahmini` işaretli; tablolar `costs.py` içinde elle gözden geçirilebilir. |

## 6. Kapsam dışı (bilerek yapılmayacak)
- Kullanıcı hesapları, giriş sistemi, çok kullanıcı.
- İnternete açık hosting / mobil uygulama mağazası.
- Bilet satın alma / ödeme — sistem sadece bulur ve resmi siteye yönlendirir.
- Avrupa dışı rotalar (dünya geneli kapsam) — şimdilik hedef değil.
- **Ryanair / Wizz Air için ayrı doğrudan entegrasyon (API veya scraper):** bilinçli karar. Uçuşlar Google Flights üzerinden gelir; bu yeterli kabul edildi.

## 7. Değişmez kurallar
1. **Tahmini veri her zaman "tahmini" diye gösterilir.** Uydurma fiyat gerçek gibi sunulmaz.
2. **Ayrı bilet uyarısı:** uçak + tren farklı sağlayıcıdansa kaçırma riski açıkça yazılır.
3. **Transfer payı:** şehirden uçuşa 120 dk, ayrı biletli uçuşlar arası 180 dk; gece beklemesi ve son sefer durumları işaretlenir. (Eski çok modlu yönlendirici `/api/multimodal-search` hâlâ 90 dk kullanır.)
4. Her bacak için resmi satın alma linki.
5. **Yan yol motoru, uçuş aramasının üstüne eklenen bir katmandır.** Normal uçuş araması (`/api/live-flights`) ve direkt uçuş takibi bağımsız çalışmaya devam eder. Rota motoru uçuş sağlayıcısını sadece *kullanır*, davranışını değiştirmez.
