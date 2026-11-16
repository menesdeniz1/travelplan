"""Tests for via on date constraint (PLN-13, T02)."""

import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server_v2 import _normalize_trip_query
from travel_engine.airports import AirportDB, haversine_km
from travel_engine.route_engine import RouteEngine
from travel_engine.trip_planner import TripPlanner


def make_flight(airline, frm, to, dep_date, times, price, *, direct=True, layover=""):
    return {
        "source": "test",
        "airline": airline,
        "times": times,
        "duration": "2 sa.",
        "airports": f"{frm} - {to}",
        "stops": "Aktarmasız" if direct else "1 aktarma",
        "stops_count": 0 if direct else 1,
        "is_direct": direct,
        "layover_info": layover,
        "price": price,
        "price_currency": "TL",
        "price_try": float(price),
        "total_price_try": float(price),
        "booking_url": f"https://example.com/book/{airline}",
    }


class FakeFlights:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]


class FakeGround:
    def __init__(self, segments=None):
        self.segments = segments or {}
        self.calls = []

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0):
        self.calls.append((from_place, to_place, date_str, earliest_dep_time))
        return []

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        return None


class ViaOnDateTests(unittest.TestCase):
    def setUp(self):
        self.airports = AirportDB.load_default()

    def _engine(self, flights_table):
        flights = FakeFlights(flights_table)
        ground = FakeGround()
        engine = RouteEngine(flights, ground, airport_db=self.airports)
        return engine, flights, ground

    def test_departure_before_via_date_pruned_departure_on_via_date_kept(self):
        """(kırmızı) 30 Eki 18:05'te ara duraktan çıkan seçenek yok; 31 Eki 07:00'de çıkan var."""
        table = {
            ("PRG", "ATH", "2026-10-30"): [
                make_flight("A3", "PRG", "ATH", "2026-10-30", "08:00 - 11:30", 3000),
            ],
            ("ATH", "IST", "2026-10-30"): [
                make_flight("TK_Early", "ATH", "IST", "2026-10-30", "18:05 - 19:30", 2000),
            ],
            ("ATH", "IST", "2026-10-31"): [
                make_flight("TK_Target", "ATH", "IST", "2026-10-31", "07:00 - 08:30", 2500),
            ],
        }
        engine, _, _ = self._engine(table)
        res = engine.search(
            "PRG", "IST", ["2026-10-30"],
            via_fixed=["ATH"],
            via_on_date={"ATH": "2026-10-31"},
        )

        options = res["options"]
        self.assertEqual(len(options), 1)
        # Option must have the 31 Oct departure, not the 30 Oct one
        second_leg = options[0]["legs"][-1]
        self.assertEqual(second_leg["operator"], "TK_Target")
        self.assertTrue(second_leg["depart_at"].startswith("2026-10-31"))

    def test_arrival_after_via_date_excluded(self):
        """(kırmızı) Ara durağa 1 Kas 00:30'da (yerel) varan seçenek yok."""
        table = {
            ("PRG", "ATH", "2026-10-31"): [
                make_flight("LateArr", "PRG", "ATH", "2026-10-31", "21:00 - 00:30+1", 3000),  # Arrives 1 Nov 00:30
            ],
            ("ATH", "IST", "2026-11-01"): [
                make_flight("TK", "ATH", "IST", "2026-11-01", "07:00 - 08:30", 2500),
            ],
        }
        engine, _, _ = self._engine(table)
        res = engine.search(
            "PRG", "IST", ["2026-10-31"],
            via_fixed=["ATH"],
            via_on_date={"ATH": "2026-10-31"},
        )
        self.assertEqual(len(res["options"]), 0)

    def test_arriving_30_oct_and_departing_31_oct_overnight_included(self):
        """(kırmızı) Durağa 30 Eki'de varıp 31 Eki'de çıkan (gece kalan) seçenek var."""
        table = {
            ("PRG", "ATH", "2026-10-30"): [
                make_flight("A3", "PRG", "ATH", "2026-10-30", "15:00 - 18:30", 3000),
            ],
            ("ATH", "IST", "2026-10-31"): [
                make_flight("TK", "ATH", "IST", "2026-10-31", "08:00 - 09:30", 2500),
            ],
        }
        engine, _, _ = self._engine(table)
        res = engine.search(
            "PRG", "IST", ["2026-10-30"],
            via_fixed=["ATH"],
            via_on_date={"ATH": "2026-10-31"},
        )
        self.assertEqual(len(res["options"]), 1)
        leg1 = res["options"][0]["legs"][0]
        leg2 = res["options"][0]["legs"][-1]
        self.assertTrue(leg1["arrive_at"].startswith("2026-10-30"))
        self.assertTrue(leg2["depart_at"].startswith("2026-10-31"))

    def test_unmatched_via_place_name_raises_400_error(self):
        """(kırmızı) Eşleşmeyen yer adı 400 (ValueError)."""
        raw_query = {
            "origin": "PRG",
            "destination": "IST",
            "depart_dates": "2026-10-30",
            "via": "ATH",
            "via_on_date": "MUC:2026-10-31",
        }
        with self.assertRaises(ValueError) as ctx:
            _normalize_trip_query(raw_query)
        self.assertIn("MUC", str(ctx.exception))

    def test_protection_identical_results_and_calls_without_via_on_date(self):
        """(koruma) via_on_date yokken sonuç ve sağlayıcı çağrıları bugünküyle aynı."""
        table = {
            ("PRG", "ATH", "2026-10-30"): [
                make_flight("A3", "PRG", "ATH", "2026-10-30", "08:00 - 11:30", 3000),
            ],
            ("ATH", "IST", "2026-10-30"): [
                make_flight("TK_Early", "ATH", "IST", "2026-10-30", "18:05 - 19:30", 2000),
            ],
            ("ATH", "IST", "2026-10-31"): [
                make_flight("TK_Target", "ATH", "IST", "2026-10-31", "07:00 - 08:30", 2500),
            ],
        }
        engine1, flights1, _ = self._engine(table)
        res1 = engine1.search("PRG", "IST", ["2026-10-30"], via_fixed=["ATH"], via_on_date=None)

        engine2, flights2, _ = self._engine(table)
        res2 = engine2.search("PRG", "IST", ["2026-10-30"], via_fixed=["ATH"])

        self.assertEqual(len(res1["options"]), len(res2["options"]))
        self.assertEqual(flights1.calls, flights2.calls)


if __name__ == "__main__":
    unittest.main()
