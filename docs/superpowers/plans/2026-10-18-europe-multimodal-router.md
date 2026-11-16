# Avrupa Çok Modlu Akıllı Rota Motoru (Europe Multi-Modal Router) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kullanıcının zaman kısıtları (hedef varış saati, maks. toplam süre) dahilinde, doğrudan pahalı uçuşlar yerine çevre havalimanları ve hızlı tren/otobüs hatlarını birleştirerek bütçede büyük tasarruf sağlayan çok modlu Avrupa rota motorunu geliştirmek.

**Architecture:** Python tabanlı havalimanı-tren kümeleme modeli (`geo_clusters.py`), tren/otobüs sağlayıcı katmanı (`rail_bus_provider.py`), Google Flights ile birleşik çalışan zaman pencereli rota motoru (`multimodal_router.py`), REST API endpoint'i (`server_v2.py`) ve zaman tüneli görselleştirmesi içeren zengin web arayüzü (`app_v2.html`).

**Tech Stack:** Python 3 (stdlib `http.server`, `urllib`, `json`), Vanilla HTML5/CSS/JavaScript, unittest.

**Spec:** [`ROADMAP.md`](../../../ROADMAP.md)

## Global Constraints

- Harici ağır kütüphane bağımlılığı eklenmeyecek (Python standart kütüphanesi ve mevcut projenin yapısıyla uyumlu).
- Uçak inişi ile tren kalkışı arasında asgari 90 dakika emniyet tamponu (transfer buffer) zorunlu tutulacak.
- Mevcut `server_v2.py` ve `app_v2.html` uçuş arama ve KeepMoney özellikleri geriye dönük uyumlu kalacak, bozulmayacak.
- Tüm testler sandboxed modda offline/mock uyumlu çalışabilecek.

## Review Focus

1. Gece/gün aşımı zaman hesaplamaları (+1 gün varışlar ve gece yarısı geçişleri).
2. Transfer süresi yetersizliği durumunda rota elenmesi (90 dk'dan az aktarma süresi kabul edilmemeli).
3. Doğrudan uçuş bulunamadığında veya doğrudan uçuş zaten daha ucuz olduğunda mantıklı davranılması (negatif tasarruf durumunda kullanıcıya doğrudan uçuşun önerilmesi).
4. Geçersiz şehir veya küme dışı sorgularda sistemin çökmeden standart uçuş aramasına zarifçe geri düşmesi (graceful fallback).
5. Frontend arayüzünde slider ve saat kısıtlarının sunucu sorgusuyla anlık ve hatasız senkronize olması.

---

### Task 1: Avrupa Hub ve Küme Veritabanı Modeli (`geo_clusters.py`)

**Files:**
- Create: `travel_engine/geo_clusters.py`
- Test: `tests/test_geo_clusters.py`

- [ ] Write failing unit test `tests/test_geo_clusters.py` for catchment airport lookup, transit connection lookup, and transfer duration buffer calculation.
- [ ] Implement `travel_engine/geo_clusters.py` defining European city hubs (Prague, Vienna, Bratislava, Budapest, Munich, Berlin, Athens, Thessaloniki), airport codes, train stations, connecting rail corridors, and station-to-terminal transfer times.
- [ ] Run `python3 -m unittest tests/test_geo_clusters.py` and confirm pass.
- [ ] Commit Task 1 changes to branch.

---

### Task 2: Tren ve Otobüs Servis Sağlayıcı Katmanı (`rail_bus_provider.py`)

**Files:**
- Create: `travel_engine/providers/rail_bus_provider.py`
- Test: `tests/test_rail_bus_provider.py`

- [ ] Write failing unit test `tests/test_rail_bus_provider.py` testing schedule queries between European hubs (e.g. Vienna Hbf to Prague hl.n., Bratislava to Vienna), frequency, pricing, and timetable extraction.
- [ ] Implement `travel_engine/providers/rail_bus_provider.py` with schedule generator and real provider interface (HAFAS/RegioJet/ÖBB patterns with deterministic fallback for offline testing).
- [ ] Run `python3 -m unittest tests/test_rail_bus_provider.py` and confirm pass.
- [ ] Commit Task 2 changes to branch.

---

### Task 3: Zaman Pencereli Çok Modlu Rota Motoru (`multimodal_router.py`)

**Files:**
- Create: `travel_engine/multimodal_router.py`
- Test: `tests/test_multimodal_router.py`

- [ ] Write failing unit test `tests/test_multimodal_router.py` testing multi-modal route creation, time feasibility filtering (arrival deadline, max duration, min 90 min transfer buffer), savings calculation, and best-value tagging.
- [ ] Implement `travel_engine/multimodal_router.py` combining flight segments and rail segments into unified `MultiModalJourney` models.
- [ ] Run `python3 -m unittest tests/test_multimodal_router.py` and confirm pass.
- [ ] Commit Task 3 changes to branch.

---

### Task 4: Sunucu REST API Endpoint'i (`server_v2.py`)

**Files:**
- Modify: `server_v2.py`
- Test: `tests/test_server_endpoints.py`

- [ ] Write failing test `tests/test_server_endpoints.py` testing `GET /api/multimodal-search` and `GET /api/clusters`.
- [ ] Wire `multimodal_router` and `geo_clusters` into `server_v2.py` handling query params: `origin`, `destination`, `date`, `arrival_deadline`, `max_duration_hours`.
- [ ] Run `python3 -m unittest tests/test_server_endpoints.py` and verify all tests pass.
- [ ] Commit Task 4 changes to branch.

---

### Task 5: Web Arayüzü & Çok Modlu Zaman Tüneli (`app_v2.html`)

**Files:**
- Modify: `app_v2.html`

- [ ] Add Multi-Modal controls in `app_v2.html`: "Çevre Havalimanları & Tren/Otobüs" aktif/pasif anahtarı, "En Geç Varış Saati" input'u, "Maksimum Süre (Saat)" slider'ı.
- [ ] Implement Multi-Modal Journey card rendering with visual timeline steps (Flight ➔ Terminal Transfer ➔ Train/Bus) and prominent green "₺X.XXX Tasarruf" badge.
- [ ] Verify live rendering and filtering behavior.
- [ ] Commit Task 5 changes to branch.

---

### Task 6: Bütünleşik Doğrulama & Smoke-Testing

**Files:**
- Run full test suite: `python3 -m unittest discover tests`
- Smoke test local server: `python3 server_v2.py` and execute simulated queries.

- [ ] Verify 100% unit tests pass across old and new suites.
- [ ] Test end-to-end API response for Athens ➔ Prague multi-modal search.
- [ ] Verify clean git status and document verification evidence.

---

### Task 7: Bağımsız Kod İncelemesi (Independent Review Gate)

- [ ] Launch independent reviewer context/subagent to audit code for bugs, edge cases, security, and strict YAGNI.
- [ ] Resolve any valid findings and confirm all tests green.
- [ ] Final commit and handoff report.
