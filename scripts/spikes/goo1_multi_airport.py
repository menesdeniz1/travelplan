"""Spike B01 (GOO-1): Multi-airport queries in Google Flights.

Tests whether Google Flights accepts multiple origin or destination airports
in a single search query (e.g. ATH -> PRG, VIE, BTS).
Respects strict safety bounds: <= 10 pages, >= 8s pause, captcha stop, isolated env.
"""

import os
import sys

# Ensure repository root is on sys.path
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import time
import urllib.parse
from typing import Any, Dict, List, Optional

from travel_engine.providers.google_flights_provider import (
    GoogleFlightsProvider,
    _get_browser_worker,
    classify_page,
)


class SpikeMultiAirportProvider(GoogleFlightsProvider):
    """Subclass of GoogleFlightsProvider to hook screenshot saving without code duplication."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pending_screenshot_path: Optional[str] = None

    def _scrape_page(
        self,
        context: Any,
        state: Dict[str, Any],
        google_url: str,
        is_one_way: bool,
        cal_key: str,
        now_ts: float,
        want_calendar: bool = False,
    ) -> None:
        super()._scrape_page(
            context,
            state,
            google_url,
            is_one_way,
            cal_key,
            now_ts,
            want_calendar=want_calendar,
        )
        if self.pending_screenshot_path and context.pages:
            try:
                context.pages[-1].screenshot(path=self.pending_screenshot_path, full_page=False)
            except Exception as e:
                print(f"[WARN] Screenshot failed: {e}", file=sys.stderr)


def run_spike():
    out_dir = os.path.join("docs", "agent", "spikes", "GOO-1")
    os.makedirs(out_dir, exist_ok=True)
    raw_log_path = os.path.join(out_dir, "ham_cikti.txt")

    # Target queries (max 8 queries to strictly stay <= 10 pages)
    queries = [
        # (type, label, q_text, screenshot_filename)
        (
            "(a)",
            "IATA virgüllü (boşluksuz)",
            "one-way flights from ATH to PRG,VIE,BTS on 2026-11-12",
            "screenshot_1_iata_nospace.png",
        ),
        (
            "(a)",
            "IATA virgüllü (boşluklu)",
            "one-way flights from ATH to PRG, VIE, BTS on 2026-11-12",
            "screenshot_2_iata_spaced.png",
        ),
        (
            "(a)",
            "IATA 2 varış",
            "one-way flights from ATH to PRG,VIE on 2026-11-12",
            None,
        ),
        (
            "(a)",
            "IATA çoklu kalkış",
            "one-way flights from ATH,SKG to PRG on 2026-11-12",
            None,
        ),
        (
            "(b)",
            "Şehir adı virgüllü (boşluklu)",
            "one-way flights from Athens to Prague, Vienna, Bratislava on 2026-11-12",
            "screenshot_3_cities_spaced.png",
        ),
        (
            "(b)",
            "Şehir adı virgüllü (boşluksuz)",
            "one-way flights from Athens to Prague,Vienna,Bratislava on 2026-11-12",
            None,
        ),
        (
            "Kontrol",
            "Tekli arama PRG (kontrol)",
            "one-way flights from ATH to PRG on 2026-11-12",
            None,
        ),
        (
            "Kontrol",
            "Tekli arama VIE (kontrol)",
            "one-way flights from ATH to VIE on 2026-11-12",
            None,
        ),
    ]

    total_pages = len(queries)
    assert total_pages <= 10, f"Page limit exceeded: {total_pages} > 10"

    provider = SpikeMultiAirportProvider(min_interval_secs=8.5)
    log_lines = []

    def log(msg: str):
        print(msg, flush=True)
        log_lines.append(msg)

    log("=" * 80)
    log("GOO-1 · Spike: Tek Sayfada Çoklu Havalimanı Canlı Denemesi")
    log(f"Tarih/Saat: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    log(f"Toplam planlanan sayfa: {total_pages} (üst sınır 10)")
    log("=" * 80)

    try:
        for idx, (q_type, label, q_text, ss_name) in enumerate(queries, start=1):
            enc_q = urllib.parse.quote(q_text)
            url = f"https://www.google.com/travel/flights?q={enc_q}&hl=tr&gl=tr&curr=TRY"

            ss_path = os.path.join(out_dir, ss_name) if ss_name else None
            provider.pending_screenshot_path = ss_path

            log(f"\n--- Sayfa [{idx}/{total_pages}] ---")
            log(f"Tür: {q_type} - {label}")
            log(f"Sorgu: {q_text}")
            log(f"URL: {url}")

            t0 = time.monotonic()
            state = provider._run_scrape(
                google_url=url,
                is_one_way=True,
                cal_key=f"spike_b01_{idx}",
                now_ts=time.time(),
                want_calendar=False,
            )
            duration = time.monotonic() - t0

            page_url = state.get("page_url", "")
            card_count = state.get("card_count", 0)
            raw_results = state.get("raw_results", [])
            body_text = state.get("body_text", "")

            status = classify_page(page_url, body_text, card_count, len(raw_results))
            log(f"Süre: {duration:.2f} s")
            log(f"Dönüş URL: {page_url}")
            log(f"Durum (classify_page): {status}")
            log(f"DOM Kart Sayısı: {card_count}")
            log(f"Ayrıştırılan Uçuş Sayısı: {len(raw_results)}")

            # Captcha / Bot Block check
            if status == "blocked" or "/sorry/" in page_url:
                log("\n" + "!" * 80)
                log("DUR KOŞULU: Google bot engeli (/sorry/ veya captcha) tespit edildi! Deneme durduruldu.")
                log("!" * 80)
                break

            # Extract airport pairs from the first 30 cards
            first_30 = raw_results[:30]
            pairs = []
            for r in first_30:
                p = r.get("airports", "").strip()
                if p:
                    pairs.append(p)

            unique_pairs = list(dict.fromkeys(pairs))
            log(f"İlk 30 karttaki benzersiz havalimanı çiftleri ({len(unique_pairs)} adet): {unique_pairs}")

            prices = [r.get("total_price_try") for r in raw_results if r.get("total_price_try") is not None]
            min_price = min(prices) if prices else None
            log(f"En düşük fiyat: {min_price} TRY" if min_price else "Fiyat: Bulunamadı")

            if ss_name and os.path.exists(ss_path):
                log(f"Ekran görüntüsü kaydedildi: {ss_name}")

            # Inter-page pause (>= 8 seconds) if not last page
            if idx < total_pages:
                log("Sayfalar arası bekleme: 8.5 saniye...")
                time.sleep(8.5)

    finally:
        try:
            worker = _get_browser_worker()
            if worker:
                worker.shutdown(wait=True)
        except Exception as e:
            print(f"[WARN] Browser shutdown error: {e}", file=sys.stderr)

    with open(raw_log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    print(f"\nHam çıktı kaydedildi: {raw_log_path}")


if __name__ == "__main__":
    run_spike()
