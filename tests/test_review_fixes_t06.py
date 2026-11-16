"""Tests for review fixes in T06."""

import dataclasses
from datetime import datetime, date
import unittest
from unittest.mock import MagicMock

from travel_engine.airports import AirportDB
from travel_engine.providers.rail_bus_provider import ScheduledSegment, segment_from_dict, RailBusProvider
from travel_engine.route_engine import RouteEngine, _Run
from travel_engine.trip_planner import TripPlanner
from server_v2 import _parse_via_on_date


class FakeFlights:
    def __init__(self, table=None):
        self.table = table or {}
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
            out.append(dataclasses.replace(item, date=date_str))
        return out[:max_results]

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        return 100.0


def make_run(engine, origin="PRG", destination="IST", dates=None, via_tokens=None, via_on_date=None, arrive_by=None):
    return _Run(
        engine=engine,
        origin=origin,
        destination=destination,
        dates=dates or ["2026-10-30"],
        arrive_by=arrive_by,
        depart_after=None,
        max_stops=2,
        via_tokens=via_tokens,
        budget=10,
        time_value=0.0,
        min_stay={},
        cabin_bag=False,
        checked_bag=False,
        costs=None,
        db=engine.airport_db,
        ground=engine.ground_provider,
        via_on_date_input=via_on_date,
    )


class ReviewFixesT06Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.airports = AirportDB.load_default()

    def test_scheduled_segment_dataclass_and_serialization(self):
        """Fix 1: vehicle_types dataclasses.replace ve to_dict -> segment_from_dict ile korunur."""
        seg = ScheduledSegment(
            origin_station="VIE",
            dest_station="BUD",
            departure_time="08:00",
            arrival_time="10:30",
            duration_mins=150,
            price_try=500.0,
            operator="RegioJet",
            mode="train",
            date="2026-10-31",
            vehicle_types=["bus", "train"],
        )
        # dataclasses.replace preserves vehicle_types
        replaced = dataclasses.replace(seg)
        self.assertEqual(replaced.vehicle_types, ["bus", "train"])

        # to_dict carries vehicle_types
        d = seg.to_dict()
        self.assertIn("vehicle_types", d)
        self.assertEqual(d["vehicle_types"], ["bus", "train"])

        # segment_from_dict reconstructs vehicle_types
        restored = segment_from_dict(d)
        self.assertEqual(restored.vehicle_types, ["bus", "train"])

        # segment_from_dict without vehicle_types falls back to empty list
        d_no_vt = dict(d)
        del d_no_vt["vehicle_types"]
        fallback = segment_from_dict(d_no_vt)
        self.assertEqual(fallback.vehicle_types, [])

    def test_live_provider_cached_and_route_engine_vehicle_types(self):
        """Fix 1: Canlı sağlayıcıdan gelen vehicle_types hem ilk hem önbellekli çağrıda ve motorda korunur."""
        class FakeLiveProvider:
            def __init__(self):
                self.calls = 0

            def can_resolve(self, place: str) -> bool:
                return True

            def search(self, from_place, to_place, date_str, earliest_dep_time=None, max_transfers=2):
                self.calls += 1
                seg = ScheduledSegment(
                    origin_station=from_place,
                    dest_station=to_place,
                    departure_time="09:00",
                    arrival_time="11:30",
                    duration_mins=150,
                    price_try=600.0,
                    operator="RegioJet",
                    mode="train",
                    date=date_str,
                    price_eur=15.0,
                    price_is_estimated=False,
                    is_estimated=False,
                    source="live",
                    vehicle_types=["bus", "train"],
                )
                return [seg]

        live = FakeLiveProvider()
        provider = RailBusProvider(live_provider=live)

        # 1. İlk çağrı (live)
        segs1 = provider.find_ground_live("Vienna", "Budapest", "2026-10-31")
        self.assertTrue(segs1)
        self.assertEqual(getattr(segs1[0], "vehicle_types", None), ["bus", "train"])

        # 2. İkinci çağrı (önbellekten)
        segs2 = provider.find_ground_live("Vienna", "Budapest", "2026-10-31")
        self.assertTrue(segs2)
        self.assertEqual(getattr(segs2[0], "vehicle_types", None), ["bus", "train"])

        # 3. RouteEngine ile arama: seçeneğin kara bacağında vehicle_types dolu
        engine = RouteEngine(FakeFlights(), provider, airport_db=self.airports)
        res = engine.search("Vienna", "Budapest", ["2026-10-31"])
        options = res.get("options") or []
        self.assertTrue(options, "Option should be found")
        ground_legs = [l for opt in options for l in opt.get("legs", []) if l.get("type") in ("train", "bus") or opt.get("kind") == "ground"]
        self.assertTrue(ground_legs, "Ground leg expected")
        self.assertEqual(ground_legs[0].get("vehicle_types"), ["bus", "train"])

    def test_via_on_date_matching_order(self):
        """Fix 2: via=['Yunanistan','Selanik'], via_on_date={'Selanik': '2026-10-31'} kuralı Selanik durağına bağlar."""
        engine = RouteEngine(FakeFlights(), FakeGround(), airport_db=self.airports)
        run = make_run(
            engine,
            origin="PRG",
            destination="IST",
            dates=["2026-10-30"],
            via_tokens=["Yunanistan", "Selanik"],
            via_on_date={"Selanik": "2026-10-31"},
        )
        ok = run.setup_via()
        self.assertTrue(ok)
        # places: [0: PRG, 1: Yunanistan, 2: Selanik, 3: IST]
        self.assertIn(2, run.via_on_date)
        self.assertNotIn(1, run.via_on_date)
        self.assertEqual(run.via_on_date[2], date(2026, 10, 31))

    def test_incomplete_price_near_miss_rejected(self):
        """Fix 3: Bir bacağı fiyatsız (price_complete=False) geç seçenek near_misses'te yok."""
        engine = RouteEngine(FakeFlights(), FakeGround(), airport_db=self.airports)
        run = make_run(
            engine,
            origin="ATH",
            destination="PRG",
            dates=["2026-10-28"],
            arrive_by=datetime(2026, 10, 28, 12, 0),
        )
        # Ana seçenek (kurallara uyan): 1000 TL, tam fiyatlı
        main_option = {
            "legs": [{"origin": "ATH", "destination": "PRG", "kind": "flight", "price_try": 1000.0}],
            "price_complete": True,
            "true_total_try": 1000.0,
            "total_price_try": 1000.0,
            "rank_total_try": 1000.0,
            "duration_mins": 120,
            "score": 1000.0,
            "depart_at": "2026-10-28T08:00",
            "arrive_at": "2026-10-28T10:00",
            "kind": "direct",
            "badges": [],
            "risk": {"level": "low"},
        }
        # Kıl payı adayı: geç kalmış (16:00), fiyatı 500 TL görünüyor ama price_complete=False
        incomplete_late_candidate = {
            "legs": [{"origin": "ATH", "destination": "PRG", "kind": "flight", "price_try": None}],
            "price_complete": False,
            "true_total_try": 500.0,
            "total_price_try": 500.0,
            "rank_total_try": 500.0,
            "duration_mins": 120,
            "score": 500.0,
            "depart_at": "2026-10-28T14:00",
            "arrive_at": "2026-10-28T16:00",
            "kind": "direct",
            "late_by_mins": 240,
            "badges": [],
            "risk": {"level": "low"},
        }
        res = engine._result(
            run,
            found=[(main_option, "2026-10-28")],
            near_miss_found=[(incomplete_late_candidate, "2026-10-28")],
            origin="ATH",
            destination="PRG",
            dates=["2026-10-28"],
            arrive_by="2026-10-28T12:00",
            depart_after=None,
            max_stops=0,
            via_tokens=None,
            budget=10,
            time_value=0.0,
            min_stay_mins={},
            cabin_bag=False,
            checked_bag=False,
            cap=10,
        )
        self.assertEqual(len(res["near_misses"]), 0, "Fiyatı eksik kıl payı listeye girmemeli")

    def test_via_on_date_validation_in_server(self):
        """Fix 4: via_on_date non-string date TypeError -> ValueError; case-insensitive place match."""
        # 1. Non-string integer date: ValueError raises with "Geçersiz tarih formatı" (not TypeError 500)
        with self.assertRaises(ValueError) as ctx:
            _parse_via_on_date({"Yunanistan": 20261031}, ["Yunanistan"])
        self.assertIn("Geçersiz tarih formatı", str(ctx.exception))

        # 2. Case-insensitive place matching: "gr:2026-10-31" with via_codes=["GR"]
        parsed = _parse_via_on_date("gr:2026-10-31", ["GR"])
        self.assertEqual(parsed, {"gr": "2026-10-31"})

    def test_side_routes_off_drops_ground_near_misses(self):
        """Fix 5: include_side_routes=False iken near_misses'teki kind == 'ground' atılır."""
        planner = TripPlanner(FakeFlights(), FakeGround())
        mock_engine = MagicMock()
        mock_engine.search.return_value = {
            "options": [],
            "near_misses": [
                {
                    "legs": [{"origin": "VIE", "destination": "BUD", "kind": "ground", "price_try": 300.0}],
                    "price_complete": True,
                    "true_total_try": 300.0,
                    "duration_mins": 150,
                    "score": 300.0,
                    "depart_at": "2026-10-28T14:00",
                    "arrive_at": "2026-10-28T16:30",
                    "kind": "ground",
                    "late_by_mins": 180,
                }
            ],
            "query": {},
        }
        planner.route_engine = mock_engine
        all_options, result = planner._search_with_engine(
            "Vienna", "Budapest", ["2026-10-28"],
            arrive_by="2026-10-28T12:00",
            depart_after=None,
            via=None,
            include_side_routes=False,
            time_value=0.0,
            cap=10,
            max_stops=0,
            flight_search_budget=10,
            min_stay_mins=None,
            cabin_bag=False,
            checked_bag=False,
        )
        self.assertEqual(result.get("near_misses"), [], "include_side_routes=False iken ground near miss atılmalı")

    def test_legacy_warns_on_via_on_date(self):
        """Fix 6: route_engine yokken (_plan_legacy) via_on_date verilirse uyarı verilir."""
        planner = TripPlanner(FakeFlights(), FakeGround(), route_engine=None)
        res = planner.plan_trip("ATH", "PRG", ["2026-10-28"], via="SKG", via_on_date={"SKG": "2026-10-28"})
        warnings = res.get("warnings") or []
        expected_msg = "Tarih kuralı (via_on_date) bu arama kipinde uygulanmadı."
        self.assertIn(expected_msg, warnings)


if __name__ == "__main__":
    unittest.main()
