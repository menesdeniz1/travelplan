# TravelPlan Ürün Yol Haritası (v2)

> Tarih: 21 Ekim 2026. Bu dosya, claude.ai'deki "TravelPlan Ürün Yol Haritası" dokümanının repo kopyasıdır. Ajanların uyguladığı görev kartları `docs/agent/` altındadır; bir kart ile bu dosya çelişirse kart geçerlidir. Eski yol haritası (Faz 0–5): [ROADMAP.md](../ROADMAP.md).

TravelPlan'in sonraki sürümü aranacak havalimanlarını sabit listeden değil veriden seçecek, sınırlı Google aramasını en umut verici rotalara harcayacak ve sonucu kademeli verecek. Hedef: ilk sonuç 20 sn'de, tam sonuç 60 sn'de, arama başına en fazla 8 Google sayfası ve ekrandaki her fiyatın kaynağı ile zamanı belli. Plan 8 hafta sürüyor; 2. haftanın sonundaki keşif denemesi (çoklu havalimanı araması ve Keşfet) mimarinin hangi yoldan ilerleyeceğine karar veriyor.

## Durum: 22 Ekim

Faz A (17 kart) ve Faz B (10 kart) tamamlandı. Faz A Mac'te çalışıyor; Faz B `roadmap-v2` dalında, acil takip dönemi bitince Mac'e alınacak. Kapı 1 geçildi; Kapı 2 kararı dar yol (tek sayfada çoklu havalimanı çalışmıyor, Keşfet sayfası tarih sabitleyemiyor; raporlar `docs/agent/spikes/`).

| Konu | Önce | Şimdi |
| --- | --- | --- |
| Motorun işlemci süresi (1 tarih / 3 tarih / takvim biliniyor) | ~3,7 / ~18 / ~67 sn | ~0,75 / ~1,6 / ~11 sn, ilk 10 sonuç aynı |
| Görünen bacaklarla toplam | Gece transferinde tutarsız | Tutarlı |
| "Sadece gerçek veriler" takibi | Alarm üretmiyordu | Alarm veriyor; bilinmeyen ek masraf notta |
| Uydurma fiyat | Transitous tahmini, sınır ötesi tahmini otobüs | Yok ("fiyat yok") |
| Son sefer riski | Hesaplanmıyordu | Hesaplanıyor; sonraki sefer gösteriliyor |
| Google sayfa okuma | — | Sayfa başına ortalama %23 daha hızlı (5 sayfalık canlı ölçüm) |
| Gözlem | Yoktu | Arama izi; health'te son aramalar, günlük Google sayfası, Mac belleği |
| Google güvenliği | Sınır yoktu | Saatlik ve günlük sayfa sınırı, etkileşimli arama önce, engel uyarısı |

**Algoritma ne kadar kapsamlı?** Doğruluk ve hız düzeldi, arama stratejisi değişmedi. Yan havalimanları mesafe, sabit ucuz havayolu listesi ve arama geçmişiyle seçiliyor; arama başına 20 uçuş araması bütçesi var; zincir araması 5.000 zincirde kesiliyor. Bu üçü bazı ucuz yan yolların hiç denenmemesine yol açabilir. Kaçırma oranı henüz ölçülmüyor; bütçe dolduğunda ekranda uyarı çıkıyor.

**Tur vakası (23 Ekim).** Sahibin gerçek tatili (Atina → Prag → Yunanistan → İstanbul) elle, 7 ajanla araştırıldı; uygulamanın tek aramada kaçırdıkları ve kök nedenleri GOREVLER_TUR.md başında. Yeni iş kalemleri: DOG-11 (kurala kıl payı uymayan ucuz seçenekler), PLN-12 (ülke ara durağından kara bacağı), PLN-13 (ara durakta belirli tarihte bulunma), PLN-14 (zorunlu durak kipinde yan havalimanı), PLN-15 (çok parçalı tur planlayıcı), KARA-8 (karışık araç etiketi), SUN-9 (kıl payı ve şehir aktarması gösterimi), SUN-10 (bozuk ara durak adı mesajı). Kart sırası: T05 → T01 → T02 → T03, T04 T01'den sonra. Krakov ve Budapeşte kaçırması bütçe sırasıyla ilgili; C07'ye (zincir düzeyinde öncelik) bağlı. **24 Ekim:** T01–T05 main'de (be2d65c). İnceleme hataları ve kalanlar ikinci turda: T06 (hatalar), T07 (kıl payı zincir sınırı, ortak kara kodu), T10 (elenme nedeni dökümü), T08 (çift not), T09 (şehir adları). **25 Ekim:** T06–T10 main'de (02b21ff). Üçüncü tur: T11 (en düşük ara skor önce zincir araması; bench senaryoları 5.000 zincir sınırında daha ucuz seçenekleri kaçırıyordu), T12 (PLN-14), T13 (PLN-15a tur keşfi). C07/PLN-3/4 gözlem ve kara indeksi verisiyle tasarlanacak; C06 kapatıldı. Google katmanı için fli tabanlı HTTP taşıyıcısı (GOO-13…17, kartlar GOREVLER_FLI.md); Playwright yolu yedek olarak kalır.

**Sıradaki:** bir günlük gerçek ölçüm; ardından hafif Faz C (gözlem deposu, adayları bilinen fiyata göre sıralama ve erken durma, altın rota seti). Etiket araması gerekirse sonra. Telegram token yenilemesi (GUV-1) sahibin işi, bekliyor.

## Ürün hedefi

TravelPlan, tek bir yolcunun Avrupa ve Türkiye içindeki yolculukları için en ucuz ve en mantıklı yolu bulur; uçak, tren ve otobüs her sırayla birleşebilir. Kullanıcı kuralları koyar, sistem seçenekleri gerçek toplam maliyet, süre ve riskle sıralar ve takip edilen rotada fiyat düşünce haber verir.

**Kullanıcı ve bağlam**

- Tek kullanıcı. Sistem evdeki MacBook Air'de 7/24 çalışır; web arayüzünden ve Telegram'dan kullanılır.
- Fiyata duyarlı ve esnek: daha ucuzsa yakın bir havalimanına uçup trenle devam etmeye, beklemeye ya da ayrı biletle aktarma yapmaya razı.
- Tipik örnek: Atina→Prag direkt uçuş 16 bin TL iken Atina→Bratislava uçuşu ve Bratislava→Prag treni toplamda 5–6 bin TL.

**Çözdüğü üç iş**

1. "Bu tarihlerde A'dan B'ye en ucuz makul yol ne?" Kurallar: en geç varış, esnek tarih, zorunlu ara durak, kalış süresi, bütçe, aktarma sayısı.
2. "Bu yol gerçekte kaça mal olur, neyi riske atıyorum?" Bilet, bagaj, transfer ve gece konaklaması; ayrı bilet ve son sefer riski.
3. "Fiyat düşünce haber ver." Takip edilen rotalar için, telefona.

**'İyi rota' tanımı**

- Kullanıcının bütün kurallarına uyar.
- Sıralama puanı düşük olandır: gerçek toplam + süre × zaman değeri + gece beklemesi cezası + risk cezası. Bugünkü formül korunur, ağırlıklar ayarlanabilir kalır.
- Ekrandaki her fiyatın kaynağı ve görüldüğü an bellidir. Bilinmeyen fiyat "fiyat yok" olarak görünür; sıralamada yalnızca gösterilmeyen bir iç değer kullanılır.
- "Mantıklı" demek, direkte göre anlamlı tasarruf ve kabul edilebilir ek süre ile risk demektir. Her seçenek kartı bunu tek satırda söyler.

## Değişmez kurallar ve tasarım ilkeleri

**Değişmez kurallar**

1. Uydurma fiyat gösterilmez. Bilinmeyen fiyat "fiyat yok" olarak görünür; tahmin yalnızca sıralamada, gösterilmeyen bir iç değer olabilir.
2. Uçuş verisi yalnızca Google Flights sayfalarından, tarayıcıyla gelir. Ücretli API ve yeni uçuş kaynağı yok.
3. Google istekleri sınırlıdır, fazlası engellenir. Her tasarım kararı sayfa sayısını ölçer ve korur.
4. Tek kullanıcı, evdeki MacBook Air'de 7/24. Bellek ve işlemci bütçesi buna göre.
5. Kapsam Avrupa ve Türkiye.
6. Ayrı biletli bağlantıların riski açıkça yazılır. Paylar korunur: şehirden uçuşa transfer + 120 dk, ayrı biletli uçuşlar arası 180 dk. Her bacak resmi satış sayfasına bağlanır.

**Tasarım ilkeleri**

1. Keşif ucuz, doğrulama pahalı. Adaylar önbellek, geçmiş, takvim ve tek sayfalık toplu sorgularla bulunur; Google sayfası yalnızca sonucu değiştirebilecek aramaya harcanır.
2. Her rakamın kaynağı ve zamanı var: kaynak (Google Flights, RegioJet, FlixBus…), görüldüğü an ve durumu (gerçek, tahmini, fiyat yok).
3. Bayat veri planlamada kullanılır, ekranda gösterilmez. Ekrandaki fiyat son 45 dakikada doğrulanmıştır ya da yaşı yazılıdır.
4. Önce ölç, sonra değiştir. Her değişiklik arama izindeki süre ve sayaçlarla önce/sonra karşılaştırılır.
5. Tek motor. Web, Telegram ve takip aynı planlayıcıyı kullanır; eski çok modlu yol kaldırılır.
6. Kademeli cevap. İlk iyi sonuç hemen gösterilir, daha iyisi bulundukça güncellenir.
7. Coğrafya gerçek veriden gelir. "Kara yoluyla ulaşılabilir" kararı kuş uçuşu mesafeden değil, gerçek sefer ve süre bilgisinden çıkar.

## Başarı ölçütleri

| Ölçüt | Bugün | Hedef | Nasıl ölçülür |
| --- | --- | --- | --- |
| İlk sonucun görünmesi (1 tarih) | 150–180 sn; ilk sonuç son sonuçla aynı anda | ≤ 20 sn | Arama izi: ilk kademeli yanıt anı |
| Tam sonuç (1 tarih, en fazla 2 ara durak) | 150–180 sn | ≤ 60 sn | Arama izi: toplam süre |
| Arama başına Google sayfası (1 tarih) | ~20 (varsayılan bütçe) | ≤ 8 | Sayaç: Flights + Haritalar + Otel |
| Takip başına günlük Google sayfası | ~100 (4 tarama + gece ön taraması) | ≤ 20 | Günlük sayaç |
| Motorun işlemci süresi | 1 tarihte ~3,5 sn; 3 tarihte ~18 sn; takvim biliniyorsa ~67 sn | 1 tarihte ≤ 1 sn | `scripts/bench_engine.py` |
| Kurulumda ağ çağrısı (ilk uçuş aramasından önce) | Uç başına 40'a kadar kara yoklaması | 0 | Arama izi |
| Zincir aramasının kesilmesi (5.000 sınırı) | Tek tarihli gerçekçi senaryoda bile oluyor | Hiç | Arama izi |
| Altın setteki en ucuz bilinen rotanın ilk 3'te çıkması | Ölçülmüyor | ≥ %90 | Altın rota seti (kayıt-oynatma) |
| Ekrandaki fiyatlardan kaynağı ve zamanı belli olan | Kaynak var, zaman yok | %100 | Sözleşme testi |
| Görünen bacaklarla toplamın tutarlılığı | Gece transferinde tutarsız | %100 | Birim test |
| "Sadece gerçek veriler" takibinde alarm | Hiç üretmiyor (hata) | Bütçe altında her zaman | Birim test |
| Etkileşimli aramanın önceliği | Arka plan taramasıyla sıra paylaşıyor | Her zaman önde | Arama izi |

Google engellenme oranı bugün izlenmiyor; hedef sayfaların %1'inin altı.

## Bugünkü durum

İsteğin 14 maddesinden 3'ü iyi, 8'i kısmi, 3'ü zayıf karşılanıyor. Zayıf olanlar ürünün özü: en ucuz yolu bulmak, fiyat düşünce haber vermek ve hız.

| İstek | Karne | Bugün ne var | En büyük eksik |
| --- | --- | --- | --- |
| Uçak, tren, otobüs her kombinasyonda | Kısmi | Yakın havalimanı + tren, önce otobüs sonra uçak, iki tarafta yan yol, ayrı biletli aktarma, yalnız kara yolu; 0–3 otomatik ara durak | Kara yolu bacakları kuş uçuşu × 1,25 ile 700 km'de sınırlı; zincirler 5.000'de kesiliyor |
| En ucuz yolu bulma | Zayıf | Atina→Bratislava + tren gibi klasik örneği buluyor | Adaylar 28 havalimanlık sabit listeden; Münih, Nürnberg, Wrocław gibi adaylara sıra gelmiyor; bütçe sabit paylarla harcanıyor |
| En geç varış, en erken kalkış | İyi | Saat dilimine doğru, tam zaman damgasıyla filtre | — |
| Esnek tarih | Kısmi | ±3 gün, en fazla 7 tarih, en ucuz gün özeti | 7 tarih aynı 20 aramalık bütçeyi paylaşıyor; takvim tarih seçmekte kullanılmıyor |
| Zorunlu ara durak | Kısmi | Sıralı duraklar; şehir, havalimanı ya da ülke | "Buradan geç, gerisini sen bul" karma modu yok; uzun zincirde kesme uyarısı |
| Bir yerde kalma süresi | İyi | Durak başına en az kalış, otel gecesi | Gece beklemesinde tek varsayım otel |
| Bütçe | Kısmi | Direkt ve yan yol için ayrı bütçe, alarm ve etiket | Aramayı yönlendirmiyor |
| Aktarma sayısı | İyi | 0–3 ara durak | — |
| Gerçek toplam maliyet | Kısmi | Bilet + bagaj + transfer + otel; bilinmeyen parça alt sınır olarak işaretli | Gece transferi toplamda gündüz fiyatıyla; kodla girilen kalkışta görünmeyen transfer ekleniyor |
| Riskleri dürüst gösterme | Kısmi | Ayrı bilet, aktarma payı, gece geçişi gerekçeli | "Günün son seferi" riski hiç devrede değil; sonraki sefer bilgisi yok |
| "Fiyat yok" kuralı | Kısmi | Bilinmeyen fiyat boş, toplam alt sınır, "Sadece gerçek veriler" anahtarı | Transitous seferlerine mesafe tahmini fiyat; varsayılan kipte tahminler fiyat gibi görünüyor |
| Fiyat düşünce haber verme | Zayıf | Kurallı takip, ayrı bütçeler, Telegram, 6 saatte bir tarama | Gerçek veri kipinde hiç alarm yok; mantık "düştü" değil "bütçe altı"; takip başına ~100 sayfa/gün |
| Hız | Zayıf | Kalıcı önbellekler, takvim budaması, eşzamanlı kara kaynakları | 150–180 sn; işlemci darboğazları, kurulum yoklamaları; kademeli sonuç yok |
| Sınırlı Google isteği | Kısmi | Arama bütçesi, 2 sn aralık, engel algılama, 15 dk soğuma | Günlük ve saatlik toplam sınır yok; Haritalar ve Otel sayfaları bütçeye sayılmıyor |

## Ana sorunlar ve kök nedenleri

Sekiz sorunun ortak kökü aynı: arama neyi arayacağına veriyle değil sabit kurallarla karar veriyor, bulduğu her şeyi de sonradan kaba kuvvetle değerlendiriyor. Kanıtlar ve kod konumları en alttaki "Kanıtlar" bölümünde.

1. **Aday seçimi statik.** Yan yol adayı olmak için 28 havalimanlık ucuz havayolu listesinde olmak ya da arama geçmişine sahip olmak gerekiyor; kara yolu uzaklığı kuş uçuşu × 1,25. Atina→Prag'da Münih, Nürnberg, Wrocław ve Budapeşte'ye sıra gelmiyor; İzmir ve Bodrum ise Ege'yi kuş uçuşu ölçtüğü için "kara yoluyla yakın" sayılıyor.
2. **Arama bütçesi kör harcanıyor.** Kategoriler sabit paylarla (%40, %15, %15, %10, kalanı) aranıyor; bir aramanın sonucu değiştirip değiştiremeyeceğine bakılmıyor. Esnek tarihte 7 tarih aynı 20 aramayı paylaşıyor.
3. **Zincirler kaba kuvvetle sayılıyor.** Derinlik öncelikli arama bütün zincirleri üretip 5.000'de kesiliyor; kesilen kısım en kötüler değil, sıranın sonuna düşenler.
4. **Yavaşlığın yaklaşık yarısı Google dışı.** Benzer seçenekleri birleştirme karesel; takvim budaması her uçuş aramasından sonra tüm zincirleri ve seçenekleri baştan kuruyor; ilk uçuş aramasından önce uç başına 40'a kadar kara yoklaması yapılıyor.
5. **Toplam ve risk tam dürüst değil.** Gece transferi toplamda gündüz fiyatıyla hesaplanıyor; "günün son seferi" riski hiç beslenmiyor.
6. **"Fiyat yok" kuralı kenarlarda deliniyor.** Transitous seferlerine mesafe tahmini fiyat yazılıyor; mesafe tahmini gerçekte olmayan denizaşırı kara bacakları üretebiliyor.
7. **Takip pahalı ve yanlış ölçüyor.** Her tarama tam bir arama; gece ön taraması 45 dakikalık önbellek yüzünden uçuşlarda boşa gidiyor; "Sadece gerçek veriler" kipinde hiç alarm çıkmıyor; alarm düşüşe değil bütçeye bakıyor.
8. **İki motor, dağınık durum.** Telegram /ara ve klasik takipler eski çok modlu yönlendiriciyle, farklı kurallarla (90 dk pay, 8 sabit küme) çalışıyor. Gizli anahtarlı .env dosyası repoya girmiş.

## Hedef mimari

Google sayfası yalnız sonucu değiştirebilecek aramaya harcanır. İstek yukarıdan aşağı akar:

| Adım | Ne yapar |
| --- | --- |
| 1. İstek ve iş modeli | Web, Telegram ya da takip zamanlayıcısı bir iş başlatır; iş kimliği döner, sonuç kademeli gelir |
| 2. Çözümleme | Şehir, havalimanı, ülke ve duraklar AirportDB ve saat dilimleriyle çözülür |
| 3. Keşif ve alt sınırlar | Aday şehirler ve havalimanları; fiyat ve süre alt sınırları (depolardan okunur) |
| 4. Doğrulama kuyruğu | En umut verici arama önce; kalan hiçbir arama en iyiyi geçemiyorsa erken durur (dış kaynaklardan ister) |
| 5. Zincir birleştirme | Bellekte etiket araması; kesme yok, yalnız baskın olmayan yarım zincirler |
| 6. Sunum | İlk ~200 zincir için tam döküm; kaynak, zaman damgası, B planı |

| Depo | İçerik | Ömür |
| --- | --- | --- |
| Gözlem deposu | Her ayrıştırılmış uçuş ve fiyatı | 60 gün |
| Bağlantı indeksi | Gerçek tren/otobüs bağlantıları, süreler, en düşük fiyat | 30 gün |
| Takvim deposu | Rota başına günlük en düşük fiyat | 24 saat |
| Takip deposu | Takipler ve alarmlar (travelplan.db) | Kalıcı |

Dış kaynaklar: **İstek valisi** (bütün Google sayfaları buradan geçer; günlük sınır ve öncelik), Google Flights (çoklu sorgu, Keşfet), Google Haritalar (yalnız indeks için), kara kaynakları (RegioJet, FlixBus, Trenitalia, Transitous). Arka plan işleri (takip taraması, gece keşfi, indeks tazeleme, yedek) aynı planlayıcıyı düşük öncelikle kullanır ve depoları günceller.

## Yeni arama algoritması

Yeni planlayıcı, sabit kategori payları yerine "sonucu en çok değiştirebilecek aramayı önce yap" kuralıyla çalışır. Kalan hiçbir arama mevcut en iyiyi geçemeyecekse erken durur (dal-sınır, branch & bound).

1. **Çözümle.** Kalkış, varış ve duraklar havalimanı ve istasyon listelerine çevrilir (bugünkü AirportDB).
2. **Aday kümesini kur, ağsız.** Varışa ya da kalkışa bağlantı indeksinde gerçek tren/otobüs seferi olan ve süresi kurala uyan şehirler ve havalimanları seçilir. Deniz ve ada durumu gerçek veriden gelir.
3. **Alt sınırları topla.** Her olası uçuş bacağı için bilinen en düşük fiyat sırayla takvimden, Keşfet'ten ve gözlem deposundan alınır. Hiçbiri yoksa mesafe tabanlı bir öncül yalnız sıralamada kullanılır. Kara bacakları için indeksin en düşük fiyatı ve süresi alınır.
4. **İyimser skoru hesapla.** Her aday zincirin en iyi ihtimali aşağıdaki formülle bulunur. Her bekleyen uçuş araması, onu kullanan en iyi zincirin iyimser skoruyla kuyruğa girer.
5. **Doğrula.** Kuyruğun başındaki arama yapılır; mümkünse çoklu havalimanı sorgusuyla birkaç bacak tek sayfada. Gerçek fiyatlar gelince sınırlar ve kuyruk güncellenir.
6. **Dur.** Kuyruğun başındaki iyimser skor, doğrulanmış en iyi skorun (1 + ε) katını aşınca ya da sayfa bütçesi bitince durulur. Kullanıcının bütçesi de üst sınır olarak budar.
7. **Birleştir.** Zincirler bellekte kurulur ve her durakta yalnız "başka bir yarım zincirden kötü olmayanlar" tutulur: varış zamanı, maliyet, risk ve gece beklemesi bakımından. Bu yüzden 5.000 kesmesi gerekmez.
8. **Sun.** Tam döküm yalnız en iyi ~200 zincir için kurulur. İlk sonuç 5. adımın ilk turundan sonra gösterilir, sonra yerinde güncellenir.

Bir zincirin iyimser skoru: `ŝ(z) = Σ p̲_b + T_min(z) · v + R(z)` — z zincir, b bacakları (uçuş, tren, otobüs, transfer), p̲ bacağın fiyat alt sınırı, T_min zincirin mümkün en kısa süresi, v zaman değeri (TL/saat), R zincirin yapısından kesin gelen ceza (ayrı bilet riski gibi).

- Alt sınırlar gerçekten alt sınırsa durma kuralı en iyi sonucu kaçırmaz. Bayat ya da Keşfet kaynaklı sınırlar "yumuşak" sayılır ve durma kuralında güvenlik payıyla kullanılır.
- ε başlangıçta 0,1; altın setteki sonuçlara göre ayarlanır.
- Esnek tarih kuyruğun bir boyutudur: takvim en ucuz tarihleri öne alır, pahalı tarihler kendiliğinden budanır.
- Bu tasarım bugünkü üç parçanın yerini alır: sabit kategori payları, takvim budamasındaki tam yeniden hesap ve 5.000'de kesilen derinlik öncelikli arama.

## Öncelik matrisi

| | Düşük efor | Yüksek efor |
| --- | --- | --- |
| **Yüksek etki** | **Hemen yap:** GUV güvenlik ve hijyen · OLC ölçüm ve arama izi · DOG doğruluk ve dürüstlük · MOT motor darboğazları | **Planla ve yatırım yap:** PLN planlayıcı ve adaylar · GOO Google katmanı · TKP takip ve alarmlar · SUN kademeli sonuç · KARA tren ve otobüs verisi · TST altın set ve testler |
| **Düşük etki** | **Arada yap:** OPS Mac ve işletim · TEL Telegram | **En sona bırak:** KOD kod sağlığı · FYT al/bekle |

## Yol haritası

Hafta 1 = 26 Ekim haftası.

| Faz | Tarih | İçerik | Çıkış kapısı |
| --- | --- | --- | --- |
| A · Temizlik ve ölçüm | 26 Eki – 1 Kas | Güvenlik, arama izi, doğruluk hataları, motor darboğazları | Kapı 1 (1 Kas): iz var, işlemci 1 tarihte ≤ 1 sn, toplamlar tutarlı, gerçek-veri takibi alarm veriyor |
| B · Google katmanı | 2 – 8 Kas | Çoklu havalimanı ve Keşfet denemeleri, sorgu adları, sayfa hızı, istek valisi, öncelik | Kapı 2 (8 Kas) — **mimari kararı**: çoklu sorgu ve Keşfet sonucu |
| C · Akıllı planlayıcı | 9 – 29 Kas | Gözlem deposu, bağlantı indeksi, veriye dayalı adaylar, en umut verici önce, etiket araması, gidiş-dönüş tabanı, esnek tarih | Kapı 3 (29 Kas): altın set ≥ %90, arama başına ≤ 8 sayfa |
| D · Kademeli sonuç | 23 Kas – 6 Ara | İş modeli, arayüz, Telegram, yeni kurallar | Kapı 4 (6 Ara): ilk sonuç ≤ 20 sn, tam ≤ 60 sn |
| E · Takip ve alarmlar | 30 Kas – 13 Ara | Artımlı tarama, alarm anlamı, sıklık | Kapı 5 (13 Ara): takip başına ≤ 20 sayfa/gün |
| F · Tren ve otobüs | 9 Kas – 13 Ara | Transitous, indeks taraması, fiyat eşleştirme | — |
| G · Kod, test, işletim | 26 Eki – 20 Ara | Sürekli | — |
| H · Al/bekle, fiyat | 21 Ara sonrası | En son | — |

Kapı 2'deki karar, C'nin geniş yoldan (çoklu sorgu ve Keşfet) mı yoksa dar yoldan (tekli aramalar, takvim ve gözlem deposu) mı ilerleyeceğini belirler.

## Uygulama yöntemi

Her iş kalemi tek bir ajan görevine dönüşür ve aynı anda çalışan görevler aynı dosyaya dokunmaz. Kurallar ve görev kartları bu açık kopyada yer almıyor. Kurallar: docs/agent/AJAN_KURALLARI.md; başlatma promptları: docs/agent/PROMPTLAR.md; görev kartları: Faz A, Faz B, Faz C, hafif, Tur vakası. Faz D ve sonrasının kartları Faz C ölçümünden sonra yazılır.

Dallar: ajan her kart için `rv2/<KİMLİK>-<ad>` dalı açar ve PR'ı `roadmap-v2` dalına açar. Claude her PR'ı yeniden çalıştırarak inceler; sahibi birleştirir. Her kapı geçildiğinde `roadmap-v2`, tek bir PR ile `main`'e alınır; Mac yalnız `main`'i çeker.

## Riskler ve önlemler

| Risk | Olasılık | Etki | Önlem | Erken işaret |
| --- | --- | --- | --- | --- |
| Google sayfa yapısı değişir, ayrıştırma bozulur | Orta | Yüksek | Gerçek sayfa fixture seti (GOO-12), ağ yanıtından yapısal okuma (GOO-7), sağlık uyarısı | Ayrıştırma hatası ve boş sayfa oranı artar |
| Google engeller (captcha, /sorry/) | Orta | Yüksek | İstek valisi ve günlük sınır (GOO-8), artımlı takip (TKP-1), gözlem deposuyla planlama (PLN-1) | Engel oranı %1'i geçer, soğuma sıklaşır |
| Çoklu havalimanı sorgusu ya da Keşfet çalışmaz | Orta | Orta | Dar yol: planlayıcı takvim, gözlem ve tekli aramalarla yine en umut verici sırada çalışır | GOO-1 ve GOO-2 raporları |
| Keşfet ve takvim fiyatları bayat ya da tutarsız | Orta | Düşük | Yalnız sıralama ve yumuşak sınır; ekranda yalnız doğrulanmış fiyat | Doğrulanan fiyatla aradaki fark |
| Yeni planlayıcı gerileme getirir | Orta | Yüksek | Bayrak, eski yol yedekte, altın sette yan yana karşılaştırma (TST-2) | Altın set skoru düşer |
| Fiyatı bilinmeyen kara bacakları yanlış sıralanır | Orta | Orta | Fiyatlı seferle eşleştirme (KARA-4), "…'den başlayan" ipucu, "fiyat yok" görünürlüğü | Fiyatsız bacak oranı |
| Transitous kullanım koşulları ya da erişim sorunu | Düşük | Orta | Bildirim, nazik istek aralığı, kapatılabilir ayar, indeks önbelleği | HTTP 429 ve hata oranı |
| MacBook Air kaynakları yetmez | Orta | Orta | İşlemci düzeltmeleri (MOT), tek tarayıcı ve en fazla 2 sekme, bellek izleme (OPS-3) | Bellek ve arama süresi artar |
| Kapsam şişer, fazlar bitmez | Orta | Yüksek | Faz çıkış kapıları, kapsam dışı listesi, al/bekle en sona | Faz süresi %50'den fazla aşılır |

## Karar bekleyen konular

- [ ] **Tahmini rakamların gösterimi.** Öneri: bilet fiyatında tahmin hiç gösterilmesin ("fiyat yok"); bagaj, transfer ve otel ayrı satırda "tahmini" etiketiyle kalsın. (DOG-7 bunu bekliyor.)
- [ ] **"Sadece gerçek veriler" anahtarının varsayılanı.** Öneri: açık.
- [ ] **Transitous varsayılan açılsın mı?** Matrix bildirimi sahibin aksiyonu. Öneri: evet.
- [ ] **Günlük ve saatlik Google sayfa sınırı.** Öneri: günde 300, saatte 60; engellenme ölçümüne göre ayarlanır.
- [ ] **Gece beklemesinin varsayılanı: otel mi, havalimanında bekleme mi?** Öneri: ikisi de gösterilsin, sıralamada otel varsayılsın.
- [ ] **Mac'te aynı tarayıcıda 2 sekme denensin mi?** Öneri: GOO-10 ile Faz D haftalarında, bir hafta engellenme ölçülerek.
- [ ] **Zaman değeri varsayılanı (bugün 150 TL/saat).** Öneri: korunsun, arayüzde açıklansın.
- [ ] **Git geçmişinden .env temizliği.** Öneri: token yenilendikten sonra gerekmez; repo herkese açılacaksa yapılsın.
- [ ] **Altın rota setinin içeriği (15–20 senaryo).** Öneri: Atina→Prag, İstanbul→Prag (Selanik üzerinden), Prag→İstanbul (Yunanistan üzerinden), Londra→Viyana, Milano→Barselona, Paris→Barselona (tren) ile başla.

## Kapsam dışı

- Yeni uçuş kaynağı (Ryanair ya da Wizz Air'e doğrudan bağlanmak), ücretli API ve tarayıcısız Google erişimi.
- Bot korumalı sitelerden fiyat (DB, ÖBB, SNCF, Renfe, TCDD); captcha aşma, proxy ya da kimlik gizleme.
- Kullanıcı hesapları, kimlik doğrulama, çok kullanıcı ve internete açık sunucu.
- Bilet satın alma ve ödeme; sistem yalnız bulur ve resmi satış sayfasına yönlendirir.
- Avrupa ve Türkiye dışı rotalar.
- Feribot ve ada bağlantıları bu yol haritası süresince yok; adalara yalnız uçuşla gidilir.

---

# İş listesi

95 iş kalemi 14 iş akışında toplandı; toplam efor yaklaşık 92 iş günü, 8 haftalık takvim ortalama 2–3 paralel görev varsayıyor. Efor iş günü cinsinden tahmindir. Faz A ve B kalemlerinin ajan kartları `docs/agent/` altında; diğerleri bu tablodaki satırlar üzerinden, Kapı 2'den sonra karta dönüşür.

## GUV · Güvenlik ve hijyen

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| GUV-1 | Telegram bot token'ını BotFather'dan yenile; yenisini yalnız Mac'teki .env'e yaz. | Eski token çalışmıyor, bot yeni token'la mesaj atıyor. | — (sahibin işi) | 0,1 | A |
| GUV-2 | .env ve deploy/travelplan.seed.db dosyalarını git takibinden çıkar; Mac'e güvenli kopyalama adımını yaz. | git ls-files bu iki dosyayı listelemiyor; kurulum adımı güncel. | .gitignore, README.md, scripts | 0,25 | A |
| GUV-3 | Repo hijyen testi: izlenen dosyalarda .env, veritabanı ya da Telegram token'ı varsa test kırmızı. | Kirli repoda kırmızı, temiz repoda yeşil. | yeni tests/test_repo_hygiene.py | 0,25 | A |

## OLC · Ölçüm ve gözlemlenebilirlik

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| OLC-1 | Arama izi: aşama süreleri ve sayaçlar; yanıtta trace, günlükte tek satır. | Aşamalar ve sayaçlar yanıtta; sonuçlar değişmiyor. | route_engine.py, trip_planner.py, sağlayıcılar, server_v2.py | 1 | A |
| OLC-2 | Arama koşuları tablosu; /api/health'te son 20 arama ve bugünkü Google sayfası. | Health'te yeni alanlar. | search_log.py, server_v2.py | 0,5 | A |
| OLC-3 | Ağsız kıyas düzeneği. | `scripts/bench_engine.py` (kurulumda eklendi); replay senaryoları TST-1 sonrası. | scripts/bench_engine.py | 1 | A |
| OLC-4 | Google sağlık metrikleri ve eşik uyarıları. | Health'te yeni alanlar; eşikte tek uyarı. | google_flights_provider.py, server_v2.py, notifier.py | 0,5 | B |
| OLC-5 | Haftalık canlı duman testi (3 rota, ≤ 12 sayfa). | Haftalık rapor; bütçe aşılmıyor. | yeni scripts/smoke_live.py | 0,5 | G |

## DOG · Doğruluk ve dürüstlük

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| DOG-1 | Toplamı ekrandaki bacaklardan hesapla. | Gece inişli örnekte görünen taksi = toplamdaki transfer; görünmeyen transfer yok. | costs.py, route_engine.py | 0,5 | A |
| DOG-2 | Son sefer ve B planı (sonraki sefer). | Risk gerekçesinde "günün son seferi"; bacakta sonraki sefer. | route_engine.py, costs.py; gösterim app.js, telegram_bot.py | 1 | A |
| DOG-3 | Transitous seferlerinde fiyat boş kalsın (saat gerçek). | "Sadece gerçek veriler" açıkken Transitous seferleri atılmıyor. | transitous_provider.py | 0,25 | A |
| DOG-4 | Takip okuması bilet toplamıyla; ek masraf bilinmiyorsa alarm notu. | Gerçek veri kipindeki takipte okuma ve alarm var. | daemon_tracker.py, notifier.py | 0,5 | A |
| DOG-5 | Bütçe karşılaştırması fiyatı tam seçenekler arasından en ucuzla. | Fiyatı eksik en ucuz yan yol varken bütçe altındaki tam fiyatlı yan yol alarm üretiyor. | trip_planner.py, daemon_tracker.py | 0,25 | A |
| DOG-6 | Arama geçmişi sinyalini düzelt (yön, anahtar). | Geçmişi olan aday tanınıyor. | route_engine.py, search_log.py | 0,5 | A |
| DOG-7 | Fiyat gösterim politikası (KARAR sonrası). | Ekranda kaynaksız bilet fiyatı yok. | costs.py, route_engine.py, trip_planner.py, app.js, telegram_bot.py | 1 | B |
| DOG-8 | Sınır ötesi mesafe tahmini yok (bağlantı indeksi gelene kadar). | Atina→İzmir için tahmini bacak üretilmiyor. | rail_bus_provider.py | 0,5 | A |
| DOG-9 | Fiyat tazelik damgası. | Uçuş bacaklarında görüldüğü an; arayüzde "X dk önce". | google_flights_provider.py, route_engine.py, app.js | 0,5 | B |
| DOG-10 | Gece beklemesine iki seçenek: otel ve havalimanında bekleme. | Gece zincirlerinde iki varyant. | route_engine.py, costs.py | 1 | C |
| DOG-11 | Kurala kıl payı uymayan ucuz seçenekler (son varışı ≤ 12 saat aşan, ≥ %15 ucuz); ek arama yok. | Dönüş örneğinde 1 Kas 08:00 varışlı ~8.000 TL seçenek `near_misses`'te. | route_engine.py, trip_planner.py | 1 | C |

## MOT · Motor performansı

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| MOT-1 | Benzer seçenek birleştirmeyi doğrusal yap. | 3 tarihli senaryo 17,6 sn → ~3 sn; sonuçlar aynı. | route_engine.py | 0,5 | A |
| MOT-2 | Takvim budamasında zincir başına skor belleği (yalnız yeni zincirler hesaplanır). | Takvimli senaryo ≤ 20 sn; sonuçlar ve budama aynı. | route_engine.py | 0,5 | A |
| MOT-3 | Tam seçenek dökümü yalnız gereken zincirler için. | MOT-1/2/4/5 sonrası 1 tarih ~0,9 sn ölçüldü; etiket aramasıyla (MOT-6) birlikte yeniden değerlendirilir. | route_engine.py | 1 | C |
| MOT-4 | AirportDB aramalarını arama başına önbelleğe al. | Profilde payı < %5; sonuçlar aynı. | route_engine.py | 0,25 | A |
| MOT-5 | `_supports_kwarg` sonucunu önbelleğe al. | Profilde görünmüyor. | route_engine.py | 0,1 | A |
| MOT-6 | Baskınlık budamalı etiket araması; 5.000 kesmesi kalkar. | Kesme uyarısı yok; küçük senaryolarda sonuç aynı. | yeni route_search.py, route_engine.py | 4 | C |
| MOT-7 | Zincir araması sırasında ağ çağrısı yok. | İzde zincir aramasının ağ çağrısı 0. | route_engine.py, rail_bus_provider.py | 1 | C |

## GOO · Google Flights katmanı

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| GOO-1 | Deneme: tek sayfada çoklu havalimanı. | Rapor ve geniş/dar yol kararı. | (deneme) | 1 | B |
| GOO-2 | Deneme: Keşfet sayfası. | Rapor ve karar. | (deneme) | 1 | B |
| GOO-3 | Sorguyu havalimanı kodları ve İngilizce metro adlarıyla kur. | "Münih", "Selanik", "Budapeşte" doğru havalimanlarına gidiyor. | google_flights_provider.py | 1 | B |
| GOO-4 | Çoklu havalimanı sorgusu sağlayıcıda (GOO-1 olumluysa). | 5 varış adayı tek sayfada. | google_flights_provider.py, flight_cache.py, route_engine.py | 2 | C |
| GOO-5 | Keşfet sağlayıcısı (GOO-2 olumluysa). | Tek sayfada ≥ 30 varış. | yeni providers/google_explore_provider.py | 2 | C |
| GOO-6 | Sayfa okumayı hızlandır. | Sayfa başına süre ≥ %30 düşüyor. | google_flights_provider.py | 1 | B |
| GOO-7 | Deneme: ağ yanıtından yapısal veri. | 20 sayfada %100 uyum; karar. | (deneme dalı) | 1,5 | B |
| GOO-8 | Google istek valisi ve öncelik sınıfları. | Sınırlar ayarlanabilir; aşımda arka plan erteleniyor. | yeni google_budget.py, Google sağlayıcıları | 1,5 | B |
| GOO-9 | Tarayıcı kuyruğunda etkileşimli öncelik. | Takip sürerken etkileşimli ilk sayfa ≤ 10 sn. | google_flights_provider.py | 1 | B |
| GOO-10 | Aynı tarayıcıda 2 sekme. | ~1,8 kat sayfa/sn; engel yok. | google_flights_provider.py | 2 | G |
| GOO-11 | Takvim verisini 24 saat sakla (planlama için). | Planlayıcı alt sınır olarak kullanıyor. | flight_cache.py, google_flights_provider.py | 0,5 | C |
| GOO-12 | Gerçek sayfa fixture seti (≥ 10). | Fixture testleri CI'da. | tests/fixtures/google | 0,5 | G |
| GOO-13 | Deneme: fli (tarayıcısız HTTP + sayfa içi `ds:1` verisi) ile bugünkü sağlayıcıyı 20 rotada karşılaştır. | Rapor ve devam/dur kararı (uçuş eşleşmesi ≥ %90, fiyat farkı ≤ %2, süre yarıdan az). | (deneme) | 0,75 | C |
| GOO-14 | HTTP taşıyıcısı ve dönüştürücü (fli → bugünkü uçuş sözlüğü; aktarma havalimanı, uçuş no). | Fixture testleri; motor sözlüğü aynen okuyor. | yeni providers/google_http_transport.py | 1 | C |
| GOO-15 | Taşıyıcı bayrağı (`browser`/`auto`/`http`); `auto`'da HTTP önce, hata ya da engelde aynı arama tarayıcıdan (yedek), tek bütçe hakkı. | Varsayılan davranış aynı; yedeğe düşme testli; health'te sayaçlar. | google_flights_provider.py, server_v2.py | 1 | C |
| GOO-16 | Yapısal aktarma bilgisi motorda ve sonuçta ("Belgrad'da 1 sa 35 dk aktarma"). | Aktarma kodu yapısal veriden; gösterimde satır. | route_engine.py, telegram_bot.py, app.js | 0,5 | C |
| GOO-17 | Mac'e geçiş ve izleme belgesi (`auto`'ya geçiş, geri alma, eşikler). | scripts/README.md'de adımlar. | scripts/README.md | 0,25 | C |

## PLN · Planlayıcı ve aday keşfi

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| PLN-1 | Gözlem deposu (60 gün). | Tablo, API, testler. | yeni observations.py, google_flights_provider.py | 1,5 | C |
| PLN-2 | Kara bağlantı indeksi (14–30 gün). | Prag çevresi 30 şehir dolu; aday seçimi ağsız. | yeni ground_index.py, rail_bus_provider.py | 2 | C |
| PLN-3 | Aday seçimini veriye dayandır; puan = beklenen tasarruf. | Atina→Prag'da Münih, Nürnberg, Wrocław, Budapeşte değerlendiriliyor; İzmir, Bodrum değil. | yeni candidates.py, route_engine.py | 3 | C |
| PLN-4 | En umut verici önce planlayıcı (bayrakla). | Altın sette aynı ya da daha iyi; sayfa sayısı ≤ %60. | yeni planner.py, route_engine.py | 4 | C |
| PLN-5 | Alt sınır kaynakları ve doğrulaması. | Sert sınırlar gerçek fiyatı aşmıyor. | planner.py | 1,5 | C |
| PLN-6 | Gidiş-dönüş tabanı (A⇄B tek bilet). | Sonuçta tek bilet gidiş-dönüş seçeneği. | trip_planner.py, roundtrip.py, route_engine.py | 1 | C |
| PLN-7 | Esnek tarih takvimle. | ±3 günde sayfa ≤ 1,5 kat. | trip_planner.py, planner.py | 1 | C |
| PLN-8 | Kara sınırlarını süreye çevir. | 8 saatten kısa uzun kara seçenekleri değerlendiriliyor. | route_engine.py, rail_bus_provider.py | 0,5 | C |
| PLN-9 | Karma mod: zorunlu durak + otomatik ara durak. | Birlikte çalışıyor. | planner.py, route_engine.py, server_v2.py | 2 | E |
| PLN-10 | Yeni kurallar: azami süre, gece yok, kara süresi sınırı, operatör hariç. | API, arayüz, Telegram. | server_v2.py, route_engine.py, trip_planner.py, app.js, telegram_bot.py | 1,5 | D |
| PLN-11 | Sonuç çeşitliliği ve kısa gerekçe. | İlk 10'da ≥ 4 desen (varsa). | route_engine.py | 0,5 | C |
| PLN-12 | Ülke ara durağından kara bacağıyla çıkış. | Prag → (Yunanistan) → İstanbul'da Selanik → İstanbul otobüsü deneniyor. | route_engine.py | 1,5 | C |
| PLN-13 | Ara durakta belirli tarihte bulunma kuralı (`via_on_date`). | 30 Eki'de Yunanistan'dan çıkan seçenek "31 Eki'de Yunanistan" kuralında yok. | route_engine.py, trip_planner.py, server_v2.py | 1 | C |
| PLN-14 | Zorunlu durak kipinde yan havalimanları (kara bacağıyla giriş ve çıkış). | Prag → Budapeşte otobüsü + BUD → SKG uçuşu bulunuyor. | route_engine.py | 2 | E |
| PLN-15 | Çok parçalı tur planlayıcı: tarih aralıkları, kalış süreleri, bulunma kuralları tek sorguda. | Tur vakası tek sorguda; elle bulunan en iyi seçeneklerden ≤ %5 pahalı. | yeni tour_planner.py, server_v2.py, app.js | 4 | E |

## KARA · Tren ve otobüs verisi

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| KARA-1 | Transitous'u varsayılan aç (bildirim sonrası). | Varsayılan açık; health'te durum. | transitous_provider.py, .env.example | 0,5 | F |
| KARA-2 | Bağlantı indeksi için haftalık toplu tarama. | Google bütçesine uyuyor. | ground_index.py, server_v2.py | 1 | F |
| KARA-3 | Haritalar yalnız indeks ve son çare. | Etkileşimli aramada ≤ 1 Haritalar sayfası. | rail_bus_provider.py, route_engine.py | 0,5 | F |
| KARA-4 | Zaman kaynağına fiyatlı kaynaktan fiyat eşleştir. | Fiyat varsa gösteriliyor, yoksa ipucu. | rail_bus_provider.py | 1 | F |
| KARA-5 | Uzak aday yoklamasını indeksle değiştir. | Kurulumda ağ çağrısı 0. | route_engine.py | 0,5 | F |
| KARA-6 | Kaynak kapsamı raporu. | Health'te kapsam özeti. | rail_bus_provider.py, docs | 0,5 | F |
| KARA-7 | Statik tablodaki elle fiyatları kaldır. | Tablo bacağında "fiyat yok". | rail_bus_provider.py | 0,25 | F |
| KARA-8 | Karışık otobüs + tren bağlantılarında araç türleri (`vehicle_types`). | Krakov → Prag gibi bağlantı "otobüs + tren" görünüyor. | live_rail_provider.py, route_engine.py | 0,25 | C |

## SUN · Sonuç sunumu

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| SUN-1 | İş modeli (kademeli API, iptal). | İlk sonuç ≤ 20 sn. | server_v2.py, trip_planner.py, planner.py | 2 | D |
| SUN-2 | Arayüzde kademeli gösterim. | Elle test. | app.js, index.html, app.css | 1,5 | D |
| SUN-3 | Dürüst döküm kartı. | Her rakamın yanında kaynağı. | app.js, telegram_bot.py | 1 | D |
| SUN-4 | "Neden bu rota?" satırı. | Her yan yol kartında. | app.js, route_engine.py | 0,5 | D |
| SUN-5 | Aranmayanların gerekçesi (sınırlarla). | Uyarıda sınır ve kaynak. | planner.py, app.js | 0,5 | D |
| SUN-6 | Kart düğmeleri: takip et, yeniden doğrula, ±1 gün. | Çalışıyor. | app.js, server_v2.py | 0,5 | D |
| SUN-7 | Arama formu: otomatik tamamlama, ön ayarlar. | Elle test. | app.js, index.html, server_v2.py | 1 | D |
| SUN-8 | Panelleri tek arama deneyiminde birleştir. | Tek form. | app.js, index.html | 1 | D |
| SUN-9 | Kıl payı seçenekleri ve şehir aktarması notu ("Krakov'da 3 sa 30 dk serbest"). | Arayüz ve Telegram'da görünüyor. | app.js, telegram_bot.py | 0,75 | C |
| SUN-10 | Bozuk karakterli ara durak adında anlaşılır hata; İngilizce ad ve IATA kodu kabul. | "M�nih" için kodlama mesajı; Munich, MUC, Münih çalışıyor. | server_v2.py, telegram_bot.py | 0,1 | C |

## TEL · Telegram

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| TEL-1 | /gezi kademeli (mesaj güncelleme). | İlk mesaj ≤ 20 sn. | telegram_bot.py | 1 | D |
| TEL-2 | Satır içi düğmeler. | Callback testleri. | telegram_bot.py | 1 | D |
| TEL-3 | Doğal dil sağlamlığı. | 30 cümlelik test seti. | telegram_bot.py | 1,5 | D |
| TEL-4 | Meşgulken kuyruk. | İki istek de sonuçlanıyor. | telegram_bot.py | 0,5 | D |
| TEL-5 | Alarm mesajı biçimi. | Şablon testi. | notifier.py, telegram_bot.py | 0,5 | E |

## TKP · Takip ve alarmlar

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| TKP-1 | Artımlı tarama. | Takip başına ≤ 20 sayfa/gün. | daemon_tracker.py, planner.py | 2 | E |
| TKP-2 | Gece taramasını gözlem deposuna bağla. | Sabah izinde alt sınır kaynağı "gözlem". | daemon_tracker.py, observations.py | 0,5 | E |
| TKP-3 | Alarm anlamı: düşüş eşiği, histerezis, yeni rota, sessiz saat. | Senaryolu testler. | daemon_tracker.py, models.py, notifier.py | 1 | E |
| TKP-4 | Takip kartı ve fiyat grafiği. | Arayüzde kart. | app.js, server_v2.py | 1 | E |
| TKP-5 | Kalkışa kalan güne göre sıklık. | Zamanlayıcı testi. | server_v2.py, daemon_tracker.py | 0,5 | E |
| TKP-6 | Geçmiş tarihli takipleri tarama. | Geçmiş tarihli takip taranmıyor. | daemon_tracker.py | 0,25 | E (kart Faz A'da) |
| TKP-7 | Takip düzenleme ve takip başına bütçe. | API ve arayüzde. | server_v2.py, app.js, tracker.py | 0,5 | E |
| TKP-8 | Yeniden başlatmada hemen tam tarama yapma. | 3 yeniden başlatmada ek tarama yok. | server_v2.py | 0,25 | E (kart Faz A'da) |

## OPS · Mac sunucu ve işletim

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| OPS-1 | Uyku engeli, kapak kapalı çalışma, günlük döndürme. | 48 saat kesintisiz. | scripts/macos, README.md | 0,5 | G |
| OPS-2 | Günlük veritabanı yedeği (14 gün). | Geri yükleme testi. | yeni scripts/backup.py | 0,5 | G |
| OPS-3 | macOS'ta bellek ölçümü. | Health'te bellek görünüyor. | google_flights_provider.py | 0,25 | G (kart Faz A'da) |
| OPS-4 | Tek komutla güncelleme. | Health'te commit kimliği. | scripts/macos, server_v2.py | 0,5 | G |
| OPS-5 | İşletim uyarıları Telegram'a. | Her uyarı test edilebiliyor. | server_v2.py, notifier.py | 0,5 | G |

## KOD · Kod sağlığı

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| KOD-1 | Eski multimodal_router ve geo_clusters yolunu kaldır. | Router importu yok. | multimodal_router.py, trip_planner.py, daemon_tracker.py, telegram_bot.py, server_v2.py | 2 | G |
| KOD-2 | route_engine.py'yi modüllere böl. | Davranış aynı. | route_engine.py ve yeni modüller | 2 | G |
| KOD-3 | Seçenek ve bacak için tipli modeller, sözleşme testi. | Sözleşme testi yeşil. | yeni modeller, tests | 1 | G |
| KOD-4 | Yapılandırmayı tek modülde topla. | Health'te etkin ayarlar. | yeni config.py, sağlayıcılar | 1 | G |
| KOD-5 | Doküman senkronu. | Belgelerdeki sayılar kodla aynı. | docs, README.md | 0,5 | G |

## TST · Test ve kalite

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| TST-1 | Kayıt-oynatma altyapısı. | Kaydedilen arama ağsız aynı sonucu veriyor. | yeni replay.py, sağlayıcılar | 1,5 | B |
| TST-2 | Altın rota seti (15–20 senaryo). | Kalite skoru; Kapı 3 bu skora bakıyor. | tests/golden | 2 | C |
| TST-3 | Performans gerileme testi. | Eşik aşılınca kırmızı. | tests | 0,5 | G |
| TST-4 | Saat dilimi rastgele testleri. | 1.000 zincirde tutarlılık. | tests | 1 | G |
| TST-5 | CI'yı çalıştır. | Her değişiklikte otomatik test. | .github/workflows | 0,25 | G |

## FYT · Fiyat zekâsı ve al/bekle

| Kimlik | İş | Kabul ölçütü | Dosyalar | Efor | Faz |
| --- | --- | --- | --- | --- | --- |
| FYT-1 | Uçuş bazında fiyat geçmişi. | Takip kartında seri. | observations.py, app.js | 1 | H |
| FYT-2 | Al/bekle önerisini gözlem deposuna taşı. | "Garanti değil" ifadeleri duruyor. | advisor.py | 1 | H |
| FYT-3 | Fiyat dağılımı özetleri (bilgi). | Rota sayfasında özet. | advisor.py, app.js | 1 | H |
| FYT-4 | Otel aramasını istek valisine bağla. | Otel sayfaları bütçeye sayılıyor. | google_hotels_provider.py | 0,5 | H |

---

# Kanıtlar

Bu bölümdeki bulgular 20–21 Ekim 2026'da kod okunarak ya da motor ağsız çalıştırılarak doğrulandı; o tarihte 2.056 testin tamamı yeşildi.

## İşlemci ölçümleri

Ağ hiç yokken bile motor tek tarihte ~3,5 sn, üç tarihte ~18 sn, takvim fiyatları biliniyorsa ~67 sn işlemci harcıyor. Araç: `python scripts/bench_engine.py` (sahte ama gerçekçi yoğunlukta sağlayıcılar: arama başına 30 uçuş, saatlik tren/otobüs; gerçek havalimanı verisi ve maliyet modülü).

| Senaryo | Uçuş araması | Kara sorgusu | Tamamlanan zincir | Süre (profilsiz) | Darboğaz |
| --- | --- | --- | --- | --- | --- |
| auto1 · Atina→Prag, 1 tarih, en fazla 2 ara durak, bütçe 20 | 20 | 67 | 5.000 (sınır) | 3,2–3,7 sn | Birleştirme 1,9 sn (%58); doğrusal sürümle toplam 1,4 sn, ilk 10 aynı |
| auto3 · İstanbul→Prag, 3 tarih, bütçe 30 | 30 | 362 | 11.847 | 17,6–18,3 sn | Birleştirme 14,8 sn (%84); doğrusal sürümle toplam 2,8 sn, ilk 10 aynı |
| cal · aynısı, takvim fiyatları biliniyor | 30 | 522 | 244.888 (ara hesaplar dahil) | 66,9 sn | Takvim budaması ~49 sn (%73): 30 kez tüm zincir araması ve ~8.200 seçenek baştan |
| via · İstanbul→Prag, Selanik→Bratislava→Viyana, kalış süreleriyle, bütçe 30 | 12 | 54 | 5.000 (sınır) | 1,6–2,1 sn | Seçenek kurma; "sonuçlar eksik olabilir" uyarısı |

İlk ölçüm cProfile açıkken yapılmıştı ve süreler yaklaşık iki katı çıkmıştı (7,0 / 33,4 / 117,3 / 3,3 sn); yukarıdaki değerler profilsizdir.

## Google sayfa süreleri

19 Ekim günlüğünde tarayıcı açıkken bir Google araması takvim okuma dahil 3–4 sn sürüyor; 20 arama yaklaşık 80 saniyede bitti. Bu, 2,5–3 dakikalık toplamın yaklaşık yarısı. Kalan süre kara yolu sorgularına (FlixBus istekleri arası 0,8 sn, Haritalar yedeğinde sayfalar arası 8 sn), ilk uçuş aramasından önceki uzak aday yoklamalarına ve işlemci işine gidiyor; kesin dağılımı OLC-1 izi gösterecek.

## Doğrulanan hatalar

| Bulgu | Nasıl doğrulandı | Etki | Konum (21 Ekim, main) | İş kalemi |
| --- | --- | --- | --- | --- |
| Gece inişinde toplam yanlış: ekranda 1.250 TL taksi, toplamda 75 TL gündüz otobüsü; kalkış "ATH" diye girilince görünmeyen 500 TL transfer ekleniyor | ATH→BTS 23:50–01:05 uçuşu ve 06:00 treniyle senaryo çalıştırıldı | Toplam bu örnekte 675 TL düşük | costs.py:797, route_engine.py:1835 | DOG-1 |
| "Sadece gerçek veriler" kipinde direkt uçuş bile "fiyatı eksik"; takip okuması ve alarm yok | Direkt Atina→Prag senaryosu iki kipte çalıştırıldı | Gerçek veri kipindeki takipler tamamen sessiz | daemon_tracker.py:49, route_engine.py:2245 | DOG-4 |
| Bütçe karşılaştırması tek bir "en ucuz yan yol"a bakıyor; onun fiyatı eksikse diğerleri alarm üretmiyor | Kod yolu izlendi | Bütçe altındaki rota bildirilmeyebilir | daemon_tracker.py:321–338 | DOG-5 |
| "Günün son seferi" riski hiç beslenmiyor | Alan yalnız okunuyor, hiçbir yerde atanmıyor | Risk olduğundan düşük görünüyor | costs.py:699 | DOG-2 |
| Transitous gerçek saatli sefere mesafe tahmini fiyat yazıyor; gerçek veri kipinde bu seferler atılıyor | Kod okundu | "Fiyat yok" kuralı deliniyor | transitous_provider.py:626 | DOG-3 |
| Kara uzaklığı Ege'yi kuş uçuşu ölçüyor: İzmir Atina'nın yakın ek adayı, Bodrum uzak adayı olabiliyor; varsayılan kipte uydurma tahmini otobüs üretilebiliyor | Aday simülasyonu çalıştırıldı | Arama bütçesi imkânsız adaylara gidiyor | rail_bus_provider.py:570, route_engine.py:940 | DOG-8, PLN-3 |
| Arama geçmişi sinyali çalışmıyor: varış tarafında yön ters, "ATH" ile "atina" anahtarları eşleşmiyor | Kod yolu izlendi | Geçmişte ucuz çıkan adaylar öne alınmıyor | route_engine.py:963, search_log.py:202 | DOG-6 |
| Gece ön taraması uçuşlarda boşa: uçuş sonuçları ve takvim 45 dakika yaşıyor | Kod okundu | Her gece takip başına ~20 sayfa boşa | flight_cache.py:52 | TKP-2 |
| Takvim budaması her uçuş aramasından sonra tüm zincirleri baştan kuruyor | Profilsiz ölçüm (cal senaryosu, ~67 sn) | Çok tarihli aramada dakikalar | route_engine.py:1268 | MOT-2 |
| Benzer seçenek birleştirme karesel | Profilsiz ölçüm | 1,9–14,8 sn işlemci | route_engine.py:2166 | MOT-1 |
| Zincir araması 5.000 tamamlanmış zincirde kesiliyor | İki senaryoda sınıra ulaşıldı | En iyi seçenekler kaybolabilir | route_engine.py:215, 1989 | MOT-6 |
| Uzak aday yoklaması ilk uçuş aramasından önce uç başına 40'a kadar kara sorgusu yapıyor, Haritalar bütçesini de harcayabiliyor | Kod okundu | Gecikme ve boşa Google sayfası | route_engine.py:998 | KARA-5 |
| macOS'ta bellek ölçümü yok: süreç bellek fonksiyonu yalnız Windows'ta değer döndürüyor | Kod okundu | Mac sunucusunda bellek izlenemiyor | google_flights_provider.py:194 | OPS-3 |
| Gizli anahtarlı .env 4fe686c ile repoya girip GitHub'a gönderildi; repo özel görünüyor | Git geçmişi ve uzak dal kontrol edildi | Token sızıntısı riski | .env | GUV-1, GUV-2 |

## Aday seçimi simülasyonu

Bütçe 20 ile Atina→Prag'da 20 aramanın hiçbiri Prag'a kara yoluyla bağlı Münih, Nürnberg, Wrocław, Dresden, Linz ya da Budapeşte'ye gitmedi. Dağılım: 1 direkt, 4 varış tarafı adayı (BER, VIE, BTS, KTW), 3 uzak kalkış adayı (Kavala, Bodrum ve Ohrid'den Prag'a), 12 aktarma merkezi araması (Sofya, Bükreş, Roma Ciampino, İstanbul). Simülasyonda kara sağlayıcısı 650 km içindeki her çifte sefer verdi; uzak aday listeleri bu yüzden üst sınırdır.

| Rota | Yan yol adayları | Uzak adaylar | Aktarma merkezleri | Dışarda kalan örnekler |
| --- | --- | --- | --- | --- |
| Atina→Prag | Kalkış: ATH. Varış: PRG, BER, VIE, BTS, KTW | Kalkış: KVA, BJV, OHD, PDV, EDO, DLM, SKP, TIA. Varış: KRK, BUD, WMI, WAW, DRS, JCL, LNZ, WRO, NUE, ERF, MUC, SZG | SOF, OTP, CIA, IST | BRQ (Brno) hiç aday değil; SKG (Selanik) yalnız artık bütçe kategorisinde |
| Londra→Viyana | Kalkış: STN, LTN, LGW, LHR, SEN, LCY, BVA, CRL. Varış: VIE, BTS, BUD, KTW, KRK | Kalkış: BRS, CWL, LPL, RTM, AMS, EIN, DUS, LUX, NTE, OST, LIL, MAN. Varış: BER, JCL, SZG, PRG, PEV, WRO, KLV, MUC, DRS, INN, DEB, NUE | BGY, MXP, CPH | BRQ (Brno) hiç aday değil |
