"""Tests for Schengen single-entry visa rule (KUR-1 / T15).

A single-entry Schengen visa allows entering the Schengen zone only once.
Leaving the Schengen zone and re-entering (e.g. PRG -> BEG -> SKG) is prohibited.
"""

import unittest
from datetime import datetime
from typing import Any, Dict, List, Optional

from tests.test_route_engine import FakeCosts
from travel_engine.airports import AirportDB, haversine_km
from travel_engine.route_engine import RouteEngine, _Flight
from travel_engine.trip_planner import TripPlanner
from server_v2 import _normalize_trip_query

DAY = "2026-10-28"
NEXT = "2026-10-29"


def make_flight(airline, times, price, airports, *, direct=True, layover=""):
    return {
        "source": "test",
        "airline": airline,
        "times": times,
        "duration": "2 sa.",
        "airports": airports,
        "stops": "Aktarmasız" if direct else "1 aktarma",
        "stops_count": 0 if direct else 1,
        "is_direct": direct,
        "layover_info": layover,
        "total_price_try": float(price),
        "currency": "TRY",
        "trip_type": "Tek yön",
        "badge": None,
        "google_flights_url": "https://www.google.com/travel/flights?q=test",
    }


class FakeFlights:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]


class FakeGround:
    def __init__(self):
        self.calls = []

    def find_ground(self, *args, **kwargs):
        return []

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.25
        return None


def make_engine(table):
    flights = FakeFlights(table)
    engine = RouteEngine(flights, FakeGround(), airport_db=AirportDB.load_default(), costs=FakeCosts())
    return engine, flights


class TestSchengenRule(unittest.TestCase):
    def test_prg_skg_belgrade_layover_filtered_when_rule_on(self):
        """Prag -> Selanik: Belgrad aktarmalı ucuz uçuş + Schengen içi pahalı uçuş.
        Kural açık -> yalnız Schengen içi; kapalı -> ikisi de."""
        table = {
            ("PRG", "SKG", DAY): [
                make_flight("JU", "08:00 – 13:00", 1500, "PRG - SKG", direct=False, layover="BEG"),
                make_flight("A3", "10:00 – 14:00", 4000, "PRG - SKG", direct=True),
            ]
        }
        engine, _ = make_engine(table)

        # 1. Kural kapalı: ikisi de gelir
        res_off = engine.search("PRG", "SKG", [DAY], schengen_single_entry=False)
        self.assertEqual(len(res_off["options"]), 2, f"Off options: {len(res_off['options'])}")

        # 2. Kural açık: yalnız Schengen içi (A3) gelir, BEG aktarmalı elenir
        res_on = engine.search("PRG", "SKG", [DAY], schengen_single_entry=True)
        self.assertEqual(len(res_on["options"]), 1, f"On options: {len(res_on['options'])}")
        opt = res_on["options"][0]
        self.assertEqual(opt["legs"][0]["operator"], "A3")

    def test_prg_ist_via_greece_or_sofia_valid(self):
        """Prag -> İstanbul (Yunanistan üzerinden): son bacak Türkiye'ye çıkıyor -> geçerli.
        Sofya üzerinden (BG Schengen) -> geçerli."""
        table = {
            ("PRG", "ATH", DAY): [make_flight("A3", "08:00 – 11:30", 2500, "PRG - ATH")],
            ("ATH", "IST", DAY): [make_flight("TK", "15:00 – 16:30", 2000, "ATH - IST")],
            ("PRG", "SOF", DAY): [make_flight("FB", "09:00 – 12:00", 2200, "PRG - SOF")],
            ("SOF", "IST", DAY): [make_flight("TK", "15:30 – 17:00", 1800, "SOF - IST")],
        }
        engine, _ = make_engine(table)

        # Athens üzerinden
        res_ath = engine.search("PRG", "IST", [DAY], via_fixed=["ATH"], schengen_single_entry=True)
        self.assertEqual(len(res_ath["options"]), 1, f"ATH via options: {len(res_ath['options'])}")

        # Sofya üzerinden
        res_sof = engine.search("PRG", "IST", [DAY], via_fixed=["SOF"], schengen_single_entry=True)
        self.assertEqual(len(res_sof["options"]), 1, f"SOF via options: {len(res_sof['options'])}")

    def test_unknown_layover_rejected_when_rule_on(self):
        """Aktarma yeri bilinmeyen aktarmalı uçuş kural açıkken elenir, reject_schengen 1."""
        table = {
            ("PRG", "ATH", DAY): [
                make_flight("XX", "10:00 – 16:00", 1800, "PRG - ATH", direct=False, layover=""),
            ]
        }
        engine, _ = make_engine(table)

        # Kural kapalıyken seçenek bulunur
        res_off = engine.search("PRG", "ATH", [DAY], schengen_single_entry=False)
        self.assertEqual(len(res_off["options"]), 1)

        # Kural açıkken elenir ve reject_schengen sayacı 1 olur
        res_on = engine.search("PRG", "ATH", [DAY], schengen_single_entry=True)
        self.assertEqual(len(res_on["options"]), 0)
        self.assertEqual(res_on["trace"]["counters"].get("reject_schengen"), 1)
        self.assertTrue(any("Schengen tek giriş: 1" in w for w in res_on.get("warnings", [])))

    def test_non_schengen_origin_single_entry(self):
        """Kalkış Schengen dışında (İstanbul -> Atina -> Prag): bir giriş serbest;
        İstanbul -> Belgrad -> Atina -> Viyana -> Prag geçerli;
        İstanbul -> Atina -> Belgrad -> Prag geçersiz."""
        table_valid = {
            ("IST", "BEG", DAY): [make_flight("JU", "06:00 – 07:00", 1000, "IST - BEG")],
            ("BEG", "ATH", DAY): [make_flight("JU", "10:30 – 13:00", 1200, "BEG - ATH")],
            ("ATH", "VIE", DAY): [make_flight("OS", "16:30 – 18:00", 1500, "ATH - VIE")],
            ("VIE", "PRG", DAY): [make_flight("OS", "21:30 – 22:30", 1100, "VIE - PRG")],
        }
        engine_valid, _ = make_engine(table_valid)
        res_valid = engine_valid.search(
            "IST", "PRG", [DAY], via_fixed=["BEG", "ATH", "VIE"], schengen_single_entry=True
        )
        self.assertEqual(len(res_valid["options"]), 1, f"Valid chain options: {len(res_valid['options'])}")

        table_invalid = {
            ("IST", "ATH", DAY): [make_flight("TK", "06:00 – 07:30", 1500, "IST - ATH")],
            ("ATH", "BEG", DAY): [make_flight("JU", "11:00 – 12:30", 1200, "ATH - BEG")],
            ("BEG", "PRG", DAY): [make_flight("JU", "16:00 – 18:00", 1400, "BEG - PRG")],
        }
        engine_invalid, _ = make_engine(table_invalid)
        res_invalid = engine_invalid.search(
            "IST", "PRG", [DAY], via_fixed=["ATH", "BEG"], schengen_single_entry=True
        )
        self.assertEqual(len(res_invalid["options"]), 0, f"Invalid chain options: {len(res_invalid['options'])}")
        self.assertEqual(res_invalid["trace"]["counters"].get("reject_schengen"), 1)

    def test_api_schengen_single_entry_param(self):
        """API: schengen_single_entry=true motora geçer; hatalı değer 400."""
        # 1. Doğru değer: true / false
        parsed_true = _normalize_trip_query({
            "depart_dates": ["2026-10-28"],
            "schengen_single_entry": "true",
        })
        self.assertIn("schengen_single_entry", parsed_true)
        self.assertTrue(parsed_true["schengen_single_entry"])

        parsed_false = _normalize_trip_query({
            "depart_dates": ["2026-10-28"],
            "schengen_single_entry": "false",
        })
        self.assertIn("schengen_single_entry", parsed_false)
        self.assertFalse(parsed_false["schengen_single_entry"])

        # 2. Hatalı değer -> ValueError (server_v2 400 döner)
        with self.assertRaises(ValueError) as ctx:
            _normalize_trip_query({
                "depart_dates": ["2026-10-28"],
                "schengen_single_entry": "maybe",
            })
        self.assertIn("Tek girişli Schengen", str(ctx.exception))

    def test_trip_planner_legacy_mode_warning(self):
        """trip_planner eski kipte (route_engine yok) uyarı verir."""
        planner = TripPlanner(flight_provider=None, router=None, route_engine=None)
        res = planner.plan_trip("PRG", "SKG", ["2026-10-28"], schengen_single_entry=True)
        self.assertTrue(
            any("Schengen kuralı bu arama kipinde uygulanmadı" in w for w in res.get("warnings", [])),
            f"Warnings: {res.get('warnings')}"
        )


    def test_telegram_ara_schengen_token(self):
        """Telegram /ara isteğe bağlı schengen sözcüğünü tanır."""
        from datetime import date
        from travel_engine.telegram_bot import parse_search_command
        cmd = parse_search_command("/ara Prag İstanbul 2026-10-30 schengen", date(2026, 10, 25))
        self.assertIsNotNone(cmd)
        self.assertTrue(cmd.get("schengen_single_entry"))
        self.assertEqual(cmd["origin"], "Prag")
        self.assertEqual(cmd["destination"], "İstanbul")

if __name__ == "__main__":
    unittest.main()


class SchengenUnknownPointTests(unittest.TestCase):
    def test_unknown_point_between_schengen_points_is_skipped(self):
        from travel_engine.route_engine import _violates_schengen, _State, _Leg
        from datetime import datetime, timezone
        t = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
        legs = (
            _Leg("bus", "X", "Prague", "Nowhere Station Xyz", t, t, 10.0, False, "", True),
            _Leg("bus", "X", "Nowhere Station Xyz", "Vienna", t, t, 10.0, False, "", True),
        )
        state = _State(("p", 1), t, legs, 2, 1, True, ())
        self.assertFalse(_violates_schengen(state, AirportDB.load_default()))
