"""Tests for obilet route schedule provider (T16 - KARA-9)."""

import os
import unittest

from travel_engine.airports import AirportDB
from travel_engine.providers.obilet_schedule import (
    ObiletScheduleProvider,
    parse_schedule,
)
from travel_engine.providers.rail_bus_provider import (
    RailBusProvider,
    ScheduledSegment,
)
from travel_engine.route_engine import RouteEngine

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "obilet")


def _read_fixture(filename: str) -> str:
    path = os.path.join(FIXTURES_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _make_flight(airline, frm, to, times, price):
    return {
        "source": "test",
        "airline": airline,
        "times": times,
        "duration": "2 sa.",
        "airports": f"{frm} - {to}",
        "stops": "Aktarmasız",
        "stops_count": 0,
        "is_direct": True,
        "layover_info": "",
        "price": price,
        "price_currency": "TL",
        "price_try": float(price),
        "total_price_try": float(price),
        "booking_url": f"https://example.com/book/{airline}",
    }


class _FakeFlights:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        for (f_orig, f_dest, f_date), flist in self.table.items():
            if departure_date == f_date:
                match_orig = origin in (f_orig, "PRG", "Prag") if f_orig in ("PRG", "Prag") else origin in (f_orig, "ATH", "Atina")
                match_dest = destination in (f_dest, "ATH", "Atina") if f_dest in ("ATH", "Atina") else destination in (f_dest, "AXD", "Alexandroupolis", "Dedeağaç")
                if match_orig and match_dest:
                    return [dict(f) for f in flist]
        return []


class TestObiletSchedule(unittest.TestCase):
    def setUp(self):
        self.dedeagac_html = _read_fixture("dedeagac-istanbul.html")
        self.selanik_html = _read_fixture("selanik-istanbul.html")

    def test_parse_schedule_dedeagac_istanbul(self):
        """1. Sayfadan sefer listesi (en az 6 satır, saatler ve fiyat doğru)."""
        provider = ObiletScheduleProvider(fetch_html=lambda url: self.dedeagac_html)
        segs = provider.departures("Dedeağaç", "İstanbul", date_str="2026-10-25")

        self.assertGreaterEqual(len(segs), 6)
        # Check first departure: 01:45 -> 06:30 Express Turizm, 840 TL
        first = segs[0]
        self.assertEqual(first.operator, "Express Turizm")
        self.assertEqual(first.departure_time, "01:45")
        self.assertEqual(first.arrival_time, "06:30")
        self.assertEqual(first.origin_station_name, "Dedeağaç Liman Girişi")
        self.assertEqual(first.dest_station_name, "Esenler Otogarı")
        self.assertEqual(first.price_from_try, 840.0)
        self.assertIsNone(first.price_try)
        self.assertTrue(first.is_estimated)
        self.assertEqual(first.source, "obilet_page")
        self.assertEqual(first.mode, "bus")
        self.assertEqual(first.arrival_day_offset, 0)
        self.assertEqual(first.duration_mins, 285)

        # Check 14:15 daytime departure
        mid = next(s for s in segs if s.departure_time == "14:15")
        self.assertEqual(mid.operator, "Express Turizm")
        self.assertEqual(mid.arrival_time, "20:00")
        self.assertEqual(mid.price_from_try, 840.0)
        self.assertIsNone(mid.price_try)

    def test_arrival_day_offset(self):
        """2. Varış ertesi güne geçince arrival_day_offset doğru."""
        provider = ObiletScheduleProvider(fetch_html=lambda url: self.selanik_html)
        segs = provider.departures("Selanik", "İstanbul", date_str="2026-10-25")

        # Overnight service: 19:00 -> 04:15
        overnight = next(s for s in segs if s.departure_time == "19:00")
        self.assertEqual(overnight.arrival_time, "04:15")
        self.assertEqual(overnight.arrival_day_offset, 1)
        self.assertEqual(overnight.duration_mins, 555)

        # Daytime service: 09:30 -> 20:00
        daytime = next(s for s in segs if s.departure_time == "09:30")
        self.assertEqual(daytime.arrival_time, "20:00")
        self.assertEqual(daytime.arrival_day_offset, 0)
        self.assertEqual(daytime.duration_mins, 630)

    def test_no_requests_for_non_tr_gr_bg_pairs(self):
        """3. Türkiye–Yunanistan/Bulgaristan dışındaki çiftte istek yok."""
        urls_called = []

        def spy_fetch(url: str) -> str:
            urls_called.append(url)
            return self.dedeagac_html

        provider = ObiletScheduleProvider(fetch_html=spy_fetch)
        rail_provider = RailBusProvider(
            airport_db=AirportDB.load_default(),
            obilet_schedule_provider=provider,
            timetable_maps_provider=None,
        )

        non_qualifying_pairs = [
            ("Berlin", "München"),
            ("Rome", "İstanbul"),
            ("Athens", "Thessaloniki"),
            ("İstanbul", "Ankara"),
            ("Paris", "London"),
        ]

        for orig, dst in non_qualifying_pairs:
            rail_provider.find_ground(orig, dst, "2026-10-25", allow_estimate=False)

        self.assertEqual(urls_called, [], "TR <-> GR/BG dışındaki çiftlerde hiç istek yapılmamalı")

        # When asking Dedeağaç -> İstanbul, it should query obilet schedule
        rail_provider.find_ground("Dedeağaç", "İstanbul", "2026-10-25", allow_estimate=False)
        self.assertEqual(len(urls_called), 1)

    def test_corrupted_page_returns_empty_list_no_exception(self):
        """4. Bozuk sayfa -> boş liste, istisna yok."""
        corrupted_html = "<html><body><h1>Internal Error</h1><p>Broken page without busTrip</p></body></html>"
        provider = ObiletScheduleProvider(fetch_html=lambda url: corrupted_html)
        segs = provider.departures("Dedeağaç", "İstanbul")
        self.assertEqual(segs, [])

        # Network or server exception
        def failing_fetch(url: str) -> str:
            raise RuntimeError("Connection dropped")

        provider_fail = ObiletScheduleProvider(fetch_html=failing_fetch)
        segs_fail = provider_fail.departures("Dedeağaç", "İstanbul")
        self.assertEqual(segs_fail, [])

    def test_route_engine_prag_greece_istanbul_chain(self):
        """5. Motor (sahte uçuş + bu kaynak) Prag -> (Yunanistan) -> İstanbul'da

        Atina -> Dedeağaç uçuşu + Dedeağaç -> İstanbul otobüsü zincirini kuruyor.
        """
        table = {
            ("PRG", "ATH", "2026-10-25"): [
                _make_flight("Smartwings", "PRG", "ATH", "06:00 – 08:30", 2000.0)
            ],
            ("ATH", "AXD", "2026-10-25"): [
                _make_flight("Olympic Air", "ATH", "AXD", "11:45 – 12:45", 1500.0)
            ],
        }
        flight_provider = _FakeFlights(table)
        schedule_provider = ObiletScheduleProvider(fetch_html=lambda url: self.dedeagac_html)
        db = AirportDB.load_default()
        ground_provider = RailBusProvider(
            airport_db=db,
            obilet_schedule_provider=schedule_provider,
            timetable_maps_provider=None,
        )

        engine = RouteEngine(
            flight_provider,
            ground_provider,
            airport_db=db,
        )

        result = engine.search(
            "PRG",
            "İstanbul",
            ["2026-10-25"],
            via_fixed=["Atina", "Alexandroupolis"],
        )

        options = result.get("options", [])
        self.assertGreaterEqual(len(options), 1, "En az bir rota bulunmalı")

        chain_found = False
        for opt in options:
            legs = opt.get("legs", [])
            has_ath_axd = any(l.get("from") == "ATH" and l.get("to") == "AXD" for l in legs)
            has_bus = any(
                l.get("source") == "obilet_page" or "Express Turizm" in (l.get("operator") or "")
                for l in legs
            )
            if has_ath_axd and has_bus:
                chain_found = True
                break

        self.assertTrue(chain_found, "Prag -> Atina -> Dedeağaç uçuşu + Dedeağaç -> İstanbul otobüsü zinciri bulunmalı")


if __name__ == "__main__":
    unittest.main()
