"""Spike B02 (GOO-2): Google Flights 'Keşfet' (Explore) page analysis.

Analyzes the Google Flights Explore page for Athens and Istanbul departures
(one-way, November 2026), measures destination count, prices, dates, page times,
and compares 3 destinations with direct single-route Google Flights searches.
Strict safety constraints: <= 10 pages total, >= 8s pause, captcha stop, isolated env.
"""

import os
import re
import sys

# Ensure repository root is on sys.path
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright

from travel_engine.providers.google_flights_provider import (
    GoogleFlightsProvider,
    _get_browser_worker,
    _route_request,
    classify_page,
)


def parse_price(txt: str) -> Optional[float]:
    """Extract numeric TRY price from text like '₺1.540' or '1.540 TL' or '1540'."""
    clean = re.sub(r"[^\d,\.]", "", txt)
    if not clean:
        return None
    # Turkish format: 1.540 -> 1540.0
    clean = clean.replace(".", "").replace(",", ".")
    try:
        return float(clean)
    except ValueError:
        return None


def extract_explore_destinations(page) -> List[Dict[str, Any]]:
    """Extract destination cards from Google Travel Explore page."""
    destinations = []
    seen = set()

    # Look for candidate cards: explore cards typically have role="listitem" or role="article" or specific classes
    cards = page.query_selector_all("div[role='listitem'], div[role='article'], ol > li, div.k1G19c, div.YfE42e")

    for card in cards:
        try:
            txt = card.inner_text()
            if not txt:
                continue
            lines = [line.strip() for line in txt.split("\n") if line.strip()]
            if len(lines) < 2:
                continue

            # Check if there is a price in any of the lines
            price_val = None
            price_line_idx = -1
            for idx, line in enumerate(lines):
                if ("TL" in line or "₺" in line or "TRY" in line) and any(c.isdigit() for c in line):
                    p = parse_price(line)
                    if p and p > 100:
                        price_val = p
                        price_line_idx = idx
                        break

            if price_val is None:
                continue

            # First line is usually destination city name
            dest_name = lines[0]
            if len(dest_name) > 30 or any(c.isdigit() for c in dest_name):
                # not a clean city name
                continue

            # Find date line: often contains month like 'Kas', 'Kasım', 'Nov' or numbers
            date_str = None
            for line in lines[1:]:
                if any(m in line.lower() for m in ["kas", "kasım", "nov", "november", "ara", "aralık", "dec"]):
                    date_str = line
                    break

            item_key = (dest_name, date_str, price_val)
            if item_key not in seen:
                seen.add(item_key)
                destinations.append({
                    "destination": dest_name,
                    "date_text": date_str or "Belirtilmemiş",
                    "price_try": price_val,
                    "raw_card": " | ".join(lines[:4]),
                })
        except Exception:
            continue

    # Fallback to regex over full body text if specific card containers weren't found
    if not destinations:
        try:
            body = page.inner_text("body")
            # Pattern: City Name \n Date \n Details \n Price
            pattern = re.compile(
                r"([A-ZÇĞİÖŞÜ][a-zçğıöşüA-Z\s]{2,20})\n"
                r"(\d{1,2}(?:\s*[-–]\s*\d{1,2})?\s+(?:Kas|Kasım|Nov|Aralık|Dec|Eki))\n"
                r"(?:[^\n]+\n)?"
                r"([₺]?\s*[\d\.,]+\s*(?:TL|TRY)?)"
            )
            for m in pattern.finditer(body):
                city = m.group(1).strip()
                dt = m.group(2).strip()
                pr_str = m.group(3).strip()
                p_val = parse_price(pr_str)
                if p_val and p_val > 100:
                    k = (city, dt, p_val)
                    if k not in seen:
                        seen.add(k)
                        destinations.append({
                            "destination": city,
                            "date_text": dt,
                            "price_try": p_val,
                            "raw_card": f"{city} | {dt} | {pr_str}",
                        })
        except Exception:
            pass

    return destinations


def run_spike():
    out_dir = os.path.join("docs", "agent", "spikes", "GOO-2")
    os.makedirs(out_dir, exist_ok=True)
    raw_log_path = os.path.join(out_dir, "ham_cikti.txt")
    log_lines = []

    def log(msg: str):
        print(msg, flush=True)
        log_lines.append(msg)

    log("=" * 80)
    log("GOO-2 · Spike: Google Flights 'Keşfet' (Explore) Sayfası Canlı Denemesi")
    log(f"Tarih/Saat: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    log("Kapsam: Atina ve İstanbul kalkışları, tek yön, Kasım 2026")
    log("Bütçe: En fazla 10 sayfa (sayfalar arası >= 8.5 sn)")
    log("=" * 80)

    page_counter = 0
    max_pages = 10

    athens_destinations: List[Dict[str, Any]] = []
    istanbul_destinations: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        )

        try:
            # -------------------------------------------------------------
            # Sayfa 1: Explore Atina
            # -------------------------------------------------------------
            page_counter += 1
            log(f"\n--- Sayfa [{page_counter}/{max_pages}] ---")
            log("Tür: Keşfet (Explore) - Atina Kalkış")
            athens_url = (
                "https://www.google.com/travel/explore?q=one-way%20flights%20from%20Athens%20in%20November%202026"
                "&hl=tr&gl=tr&curr=TRY"
            )
            log(f"URL: {athens_url}")

            page = context.new_page()
            page.route("**/*", _route_request)

            t0 = time.monotonic()
            page.goto(athens_url, wait_until="domcontentloaded", timeout=25000)

            # Cookie consent
            try:
                accept_btn = page.query_selector("button:has-text('Tümünü kabul et'), button:has-text('Accept all')")
                if accept_btn and accept_btn.is_visible():
                    accept_btn.click()
            except Exception:
                pass

            page.wait_for_timeout(4000)
            duration = time.monotonic() - t0
            current_url = page.url
            body_text = page.inner_text("body")

            log(f"Süre: {duration:.2f} s")
            log(f"Dönüş URL: {current_url}")

            # Check captcha/blocked
            if "/sorry/" in current_url or any(m in body_text.lower() for m in ["unusual traffic", "captcha", "robot"]):
                log("\n" + "!" * 80)
                log("DUR KOŞULU: Google bot engeli (/sorry/ veya captcha) tespit edildi! Deneme durduruldu.")
                log("!" * 80)
                return

            ss_athens = os.path.join(out_dir, "screenshot_explore_athens.png")
            try:
                page.screenshot(path=ss_athens, full_page=False)
                log("Ekran görüntüsü kaydedildi: screenshot_explore_athens.png")
            except Exception as e:
                log(f"Ekran görüntüsü alınamadı: {e}")

            athens_destinations = extract_explore_destinations(page)
            log(f"Bulunan varış sayısı: {len(athens_destinations)}")
            for d in athens_destinations[:15]:
                log(f"  • {d['destination']}: {d['price_try']} TRY (Tarih: {d['date_text']}) | Kart: {d['raw_card']}")

            page.close()
            log("Sayfalar arası bekleme: 8.5 saniye...")
            time.sleep(8.5)

            # -------------------------------------------------------------
            # Sayfa 2: Explore İstanbul
            # -------------------------------------------------------------
            page_counter += 1
            log(f"\n--- Sayfa [{page_counter}/{max_pages}] ---")
            log("Tür: Keşfet (Explore) - İstanbul Kalkış")
            istanbul_url = (
                "https://www.google.com/travel/explore?q=one-way%20flights%20from%20Istanbul%20in%20November%202026"
                "&hl=tr&gl=tr&curr=TRY"
            )
            log(f"URL: {istanbul_url}")

            page = context.new_page()
            page.route("**/*", _route_request)

            t0 = time.monotonic()
            page.goto(istanbul_url, wait_until="domcontentloaded", timeout=25000)

            # Cookie consent
            try:
                accept_btn = page.query_selector("button:has-text('Tümünü kabul et'), button:has-text('Accept all')")
                if accept_btn and accept_btn.is_visible():
                    accept_btn.click()
            except Exception:
                pass

            page.wait_for_timeout(4000)
            duration = time.monotonic() - t0
            current_url = page.url
            body_text = page.inner_text("body")

            log(f"Süre: {duration:.2f} s")
            log(f"Dönüş URL: {current_url}")

            if "/sorry/" in current_url or any(m in body_text.lower() for m in ["unusual traffic", "captcha", "robot"]):
                log("\n" + "!" * 80)
                log("DUR KOŞULU: Google bot engeli tespit edildi! Deneme durduruldu.")
                log("!" * 80)
                return

            ss_ist = os.path.join(out_dir, "screenshot_explore_istanbul.png")
            try:
                page.screenshot(path=ss_ist, full_page=False)
                log("Ekran görüntüsü kaydedildi: screenshot_explore_istanbul.png")
            except Exception as e:
                log(f"Ekran görüntüsü alınamadı: {e}")

            istanbul_destinations = extract_explore_destinations(page)
            log(f"Bulunan varış sayısı: {len(istanbul_destinations)}")
            for d in istanbul_destinations[:15]:
                log(f"  • {d['destination']}: {d['price_try']} TRY (Tarih: {d['date_text']}) | Kart: {d['raw_card']}")

            page.close()
            log("Sayfalar arası bekleme: 8.5 saniye...")
            time.sleep(8.5)

            # -------------------------------------------------------------
            # Sayfa 3, 4, 5: 3 Varış İçin Tekli Arama ile Fiyat Karşılaştırması
            # -------------------------------------------------------------
            # Select 3 targets to compare:
            # 1. Athens -> target 1
            # 2. Athens -> target 2
            # 3. Istanbul -> target 3
            comparison_targets = []
            if athens_destinations:
                for d in athens_destinations:
                    # Parse day if present
                    m_day = re.search(r"(\d{1,2})", d["date_text"])
                    day = int(m_day.group(1)) if m_day else 12
                    comparison_targets.append({
                        "origin": "Athens",
                        "dest": d["destination"],
                        "date_str": f"2026-11-{day:02d}",
                        "explore_price": d["price_try"],
                    })
                    if len(comparison_targets) >= 2:
                        break

            if istanbul_destinations:
                for d in istanbul_destinations:
                    m_day = re.search(r"(\d{1,2})", d["date_text"])
                    day = int(m_day.group(1)) if m_day else 12
                    comparison_targets.append({
                        "origin": "Istanbul",
                        "dest": d["destination"],
                        "date_str": f"2026-11-{day:02d}",
                        "explore_price": d["price_try"],
                    })
                    if len(comparison_targets) >= 3:
                        break

            # Fallback targets if explore parsing didn't find specific dates
            if len(comparison_targets) < 3:
                fallbacks = [
                    {"origin": "ATH", "dest": "Rome", "date_str": "2026-11-12", "explore_price": None},
                    {"origin": "ATH", "dest": "Vienna", "date_str": "2026-11-12", "explore_price": None},
                    {"origin": "IST", "dest": "Prague", "date_str": "2026-11-12", "explore_price": None},
                ]
                for fb in fallbacks:
                    if len(comparison_targets) < 3:
                        comparison_targets.append(fb)

            comparison_results = []
            for target in comparison_targets[:3]:
                page_counter += 1
                orig = target["origin"]
                dest = target["dest"]
                dep_date = target["date_str"]
                exp_price = target.get("explore_price")

                log(f"\n--- Sayfa [{page_counter}/{max_pages}] ---")
                log(f"Tür: Tekli Arama Karşılaştırma - {orig} ➔ {dest} ({dep_date})")
                q_text = f"one-way flights from {orig} to {dest} on {dep_date}"
                single_url = f"https://www.google.com/travel/flights?q={urllib.parse.quote(q_text)}&hl=tr&gl=tr&curr=TRY"
                log(f"URL: {single_url}")

                page = context.new_page()
                page.route("**/*", _route_request)

                t0 = time.monotonic()
                page.goto(single_url, wait_until="domcontentloaded", timeout=25000)
                try:
                    accept_btn = page.query_selector("button:has-text('Tümünü kabul et'), button:has-text('Accept all')")
                    if accept_btn and accept_btn.is_visible():
                        accept_btn.click()
                except Exception:
                    pass

                try:
                    page.wait_for_selector("li.pIav2d", timeout=6000)
                except Exception:
                    pass

                duration = time.monotonic() - t0
                current_url = page.url
                items = page.query_selector_all("li.pIav2d")
                card_count = len(items)

                prices = []
                for it in items[:15]:
                    txt = it.inner_text()
                    m_pr = re.search(r"₺\s*([\d\.,]+)|([\d\.,]+)\s*TL", txt)
                    if m_pr:
                        p_val = parse_price(m_pr.group(1) or m_pr.group(2))
                        if p_val and p_val > 100:
                            prices.append(p_val)

                single_min_price = min(prices) if prices else None
                log(f"Süre: {duration:.2f} s")
                log(f"DOM Kart Sayısı: {card_count}")
                log(f"Tekli Arama En Düşük Fiyat: {single_min_price} TRY" if single_min_price else "Fiyat: Bulunamadı")
                log(f"Keşfet (Explore) Fiyatı: {exp_price} TRY" if exp_price else "Keşfet Fiyatı: Yok")

                if exp_price and single_min_price:
                    diff = single_min_price - exp_price
                    diff_pct = (diff / exp_price) * 100.0
                    log(f"Fark (Tekli - Keşfet): {diff:+.0f} TRY ({diff_pct:+.1f}%)")

                comparison_results.append({
                    "route": f"{orig} -> {dest}",
                    "date": dep_date,
                    "explore_price": exp_price,
                    "single_price": single_min_price,
                    "url": single_url,
                })

                page.close()
                if page_counter < max_pages:
                    log("Sayfalar arası bekleme: 8.5 saniye...")
                    time.sleep(8.5)

        finally:
            try:
                context.close()
                browser.close()
            except Exception as e:
                print(f"[WARN] Browser close error: {e}", file=sys.stderr)

    with open(raw_log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    print(f"\nHam çıktı kaydedildi: {raw_log_path}")


if __name__ == "__main__":
    run_spike()
