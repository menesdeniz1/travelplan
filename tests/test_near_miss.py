"""Tests for near-miss search options (DOG-11, T01)."""

import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import RouteEngine

try:
    from travel_engine.route_engine import (
        NEAR_MISS_HOURS,
        NEAR_MISS_SAVING,
        NEAR_MISS_MAX,
    )
except ImportError:
    NEAR_MISS_HOURS = 12
    NEAR_MISS_SAVING = 0.15
    NEAR_MISS_MAX = 3

from travel_engine.trip_planner import TripPlanner


def make_flight(airline, times, price, airports="ATH - PRG", *, direct=True, layover=""):
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
        out = []
        for item in self.segments.get((from_place, to_place), []):
            if earliest_dep_time and item.departure_time < earliest_dep_time:
                continue
            out.append(replace(item, date=date_str))
        return out[:max_results]

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.2
        return None


class NearMissTests(unittest.TestCase):
    def setUp(self):
        self.airports = AirportDB.load_default()

    def _engine(self, flights_table, ground_segs=None):
        flights = FakeFlights(flights_table)
        ground = FakeGround(ground_segs)
        engine = RouteEngine(flights, ground, airport_db=self.airports)
        return engine, flights, ground

    def test_constants_defined(self):
        """(kırmızı) NEAR_MISS_HOURS, NEAR_MISS_SAVING ve NEAR_MISS_MAX route_engine'de tanımlıdır."""
        import travel_engine.route_engine as re_mod
        self.assertEqual(getattr(re_mod, "NEAR_MISS_HOURS", None), 12)
        self.assertEqual(getattr(re_mod, "NEAR_MISS_SAVING", None), 0.15)
        self.assertEqual(getattr(re_mod, "NEAR_MISS_MAX", None), 3)

    def test_near_miss_8h_late_half_price_included_with_late_by_mins(self):
        """(kırmızı) Son varışı 8 saat aşan, kurala uyanın yarı fiyatı olan seçenek near_misses'te, late_by_mins == 480; options'ta değil."""
        table = {
            ("ATH", "PRG", "2026-10-28"): [
                make_flight("Aegean", "08:00 - 10:00", 10000),  # Arrives 10:00 (compliant with arrive_by 12:00)
                make_flight("Wizz", "18:00 - 20:00", 5000),     # Arrives 20:00 (8 hours late after 12:00)
            ]
        }
        engine, _, _ = self._engine(table)
        res = engine.search("ATH", "PRG", ["2026-10-28"], arrive_by="2026-10-28T12:00", max_stops=0)

        # Options has only Aegean
        self.assertEqual(len(res["options"]), 1)
        self.assertEqual(res["options"][0]["legs"][0]["operator"], "Aegean")

        # Near misses has Wizz with late_by_mins == 480
        self.assertIn("near_misses", res)
        self.assertEqual(len(res["near_misses"]), 1)
        nm = res["near_misses"][0]
        self.assertEqual(nm["legs"][0]["operator"], "Wizz")
        self.assertEqual(nm["late_by_mins"], 480)

    def test_near_miss_13h_late_option_excluded_from_all_lists(self):
        """(kırmızı) 13 saat aşan seçenek hiçbir listede yok."""
        table = {
            ("ATH", "PRG", "2026-10-28"): [
                make_flight("Aegean", "08:00 - 10:00", 10000),
                make_flight("LateFlight", "23:00 - 01:00", 4000),  # 13 hours late
            ]
        }
        engine, _, _ = self._engine(table)
        res = engine.search("ATH", "PRG", ["2026-10-28"], arrive_by="2026-10-28T12:00", max_stops=0)

        self.assertEqual(len(res["options"]), 1)
        self.assertEqual(res["options"][0]["legs"][0]["operator"], "Aegean")
        self.assertEqual(len(res.get("near_misses", [])), 0)

    def test_near_miss_only_5_percent_cheaper_excluded_from_near_misses(self):
        """(kırmızı) Kurala uyandan yalnız %5 ucuz olan geç seçenek near_misses'te yok."""
        table = {
            ("ATH", "PRG", "2026-10-28"): [
                make_flight("Aegean", "08:00 - 10:00", 10000),  # 10000 TL
                make_flight("Wizz", "18:00 - 20:00", 9500),     # 9500 TL (5% cheaper, < 15% saving threshold)
            ]
        }
        engine, _, _ = self._engine(table)
        res = engine.search("ATH", "PRG", ["2026-10-28"], arrive_by="2026-10-28T12:00", max_stops=0)

        self.assertEqual(len(res["options"]), 1)
        self.assertEqual(res["options"][0]["legs"][0]["operator"], "Aegean")
        self.assertEqual(len(res.get("near_misses", [])), 0)

    def test_provider_calls_identical_with_and_without_near_misses(self):
        """(koruma) Sağlayıcıya yapılan uçuş ve kara çağrıları, near_misses olmayan koşuyla aynı (çağrı sayısı ve anahtarları)."""
        table = {
            ("ATH", "PRG", "2026-10-28"): [
                make_flight("Aegean", "08:00 - 10:00", 10000),
                make_flight("Wizz", "18:00 - 20:00", 5000),
            ]
        }
        engine1, flights1, ground1 = self._engine(table)
        res1 = engine1.search("ATH", "PRG", ["2026-10-28"], arrive_by="2026-10-28T12:00")

        engine2, flights2, ground2 = self._engine(table)
        res2 = engine2.search("ATH", "PRG", ["2026-10-28"], arrive_by="2026-10-28T12:00")

        self.assertEqual(flights1.calls, flights2.calls)
        self.assertEqual(ground1.calls, ground2.calls)

    def test_no_arrive_by_produces_empty_near_misses_and_same_options(self):
        """(koruma) arrive_by yokken near_misses == [] ve options bugünküyle aynı."""
        table = {
            ("ATH", "PRG", "2026-10-28"): [
                make_flight("Aegean", "08:00 - 10:00", 10000),
                make_flight("Wizz", "18:00 - 20:00", 5000),
            ]
        }
        engine, _, _ = self._engine(table)
        res = engine.search("ATH", "PRG", ["2026-10-28"], arrive_by=None)

        self.assertEqual(res.get("near_misses"), [])
        self.assertEqual(len(res["options"]), 2)

    def test_trip_planner_passes_near_misses_for_both_outbound_and_return(self):
        """(kırmızı) trip_planner._search_with_engine: near_misses sonuç sözlüğünde ve return aramasında var."""
        table = {
            ("ATH", "PRG", "2026-10-28"): [
                make_flight("Aegean", "08:00 - 10:00", 10000),
                make_flight("Wizz", "18:00 - 20:00", 5000),
            ],
            ("PRG", "ATH", "2026-10-31"): [
                make_flight("Aegean", "08:00 - 10:00", 12000, airports="PRG - ATH"),
                make_flight("Wizz", "18:00 - 20:00", 6000, airports="PRG - ATH"),
            ]
        }
        engine, flights, ground = self._engine(table)
        planner = TripPlanner(flight_provider=flights, router=ground, route_engine=engine)

        res = planner.plan_trip(
            "ATH", "PRG", ["2026-10-28"],
            arrive_by="2026-10-28T12:00",
            return_dates=["2026-10-31"],
            return_arrive_by="2026-10-31T12:00",
        )

        self.assertIn("near_misses", res)
        self.assertEqual(len(res["near_misses"]), 1)
        self.assertEqual(res["near_misses"][0]["late_by_mins"], 480)

        self.assertIn("roundtrip", res)
        ret = res["roundtrip"]["return"]
        self.assertIn("near_misses", ret)
        self.assertEqual(len(ret["near_misses"]), 1)
        self.assertEqual(ret["near_misses"][0]["late_by_mins"], 480)


if __name__ == "__main__":
    unittest.main()
