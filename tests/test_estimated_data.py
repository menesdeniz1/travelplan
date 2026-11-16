import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from travel_engine.geo_clusters import GeoClusterRegistry
from travel_engine.multimodal_router import MultiModalRouter
from travel_engine.price_history import PriceHistoryManager
from travel_engine.providers import google_flights_provider as gfp
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider
from travel_engine.providers.rail_bus_provider import RailBusProvider, ScheduledSegment


class PriceHistoryEstimateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = os.path.join(self._tmp.name, "price_history.json")
        self.mgr = PriceHistoryManager(storage_path=self.storage)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cold_start_returns_no_invented_points(self):
        pts = self.mgr.get_history(
            "Athens", "Prague", "2026-10-25", "oneway",
            current_price=11500.0, current_airline="Aegean",
        )
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0]["price"], 11500.0)
        self.assertFalse(pts[0]["is_estimated"])
        self.assertTrue(pts[0]["is_live"])

    def test_cold_start_without_any_price_returns_empty(self):
        pts = self.mgr.get_history("Athens", "Prague", "2026-10-25", "oneway")
        self.assertEqual(pts, [])

    def test_cold_start_with_one_recording_adds_only_now_point(self):
        self.mgr.record_price("Athens", "Prague", "2026-10-25", "oneway", 12000.0, airline="Aegean")
        pts = self.mgr.get_history(
            "Athens", "Prague", "2026-10-25", "oneway", current_price=11500.0,
        )
        self.assertEqual(len(pts), 2)
        self.assertEqual(pts[0]["price"], 12000.0)
        self.assertEqual(pts[-1]["price"], 11500.0)
        for p in pts:
            self.assertFalse(p["is_estimated"])
            self.assertTrue(p["is_live"])

    def test_multi_day_without_recordings_marks_past_days_estimated(self):
        pts = self.mgr.get_history(
            "Athens", "Prague", "2026-10-25", "oneway",
            current_price=11500.0, current_airline="Aegean", days_range=7,
        )
        self.assertEqual(len(pts), 7)
        for p in pts[:-1]:
            self.assertTrue(p["is_estimated"])
            self.assertFalse(p["is_live"])
        # Today carries the live search price, so it is not an estimate
        self.assertFalse(pts[-1]["is_estimated"])
        self.assertTrue(pts[-1]["is_live"])
        self.assertEqual(pts[-1]["price"], 11500.0)

    def test_recorded_point_for_today_is_not_estimated(self):
        self.mgr.record_price("Athens", "Prague", "2026-10-25", "oneway", 12000.0, airline="Aegean")
        pts = self.mgr.get_history(
            "Athens", "Prague", "2026-10-25", "oneway", days_range=7,
        )
        self.assertEqual(len(pts), 7)
        today = pts[-1]
        self.assertFalse(today["is_estimated"])
        self.assertTrue(today["is_live"])
        self.assertEqual(today["price"], 12000.0)
        for p in pts[:-1]:
            self.assertTrue(p["is_estimated"])

    def test_date_matrix_days_carry_is_estimated_false(self):
        matrix = self.mgr.get_date_matrix(
            "Athens", "Prague", "2026-10-25", "oneway", 11500.0, current_airline="Aegean",
        )
        self.assertEqual(len(matrix["days"]), 7)
        for d in matrix["days"]:
            self.assertIn("is_estimated", d)
            self.assertFalse(d["is_estimated"])


class ScheduledSegmentEstimateTests(unittest.TestCase):
    def test_scheduled_segment_defaults_to_estimated(self):
        seg = ScheduledSegment(
            origin_station="Wien Hbf",
            dest_station="Praha hl.n.",
            departure_time="06:00",
            arrival_time="10:15",
            duration_mins=255,
            price_try=750.0,
            operator="ÖBB Railjet / RegioJet",
            mode="train",
            date="2026-10-25",
        )
        self.assertTrue(seg.is_estimated)
        self.assertTrue(seg.to_dict()["is_estimated"])

    def test_provider_schedules_are_estimated(self):
        schedules = RailBusProvider().get_schedules("Wien Hbf", "Praha hl.n.", "2026-10-25")
        self.assertGreater(len(schedules), 0)
        for seg in schedules:
            self.assertTrue(seg.is_estimated)


class StubFlightProvider:
    """Flights shaped like GoogleFlightsProvider output: 'times', 'airline', 'total_price_try'."""

    def search_flights(self, origin, destination, departure_date, return_date=None, trip_type="oneway"):
        dest = destination.upper()
        if "VIE" in dest:
            return [{"airline": "Aegean", "times": "09:30 – 11:45", "total_price_try": 3500.0}]
        if "PRG" in dest or "PRAGUE" in dest:
            return [{"airline": "Aegean", "times": "09:00 – 10:45", "total_price_try": 11500.0}]
        return []


class MultiModalEstimateTests(unittest.TestCase):
    def setUp(self):
        registry = GeoClusterRegistry()
        self.router = MultiModalRouter(
            flight_provider=StubFlightProvider(),
            rail_provider=RailBusProvider(registry),
            geo_registry=registry,
        )

    def test_segment_flags_and_journey_flag(self):
        result = self.router.search_multimodal(origin="Athens", destination="Prague", date="2026-10-25")
        self.assertTrue(result["has_multimodal"])
        for route in result["multimodal_routes"]:
            flight, transfer, ground = route["segments"]
            self.assertEqual(flight["type"], "flight")
            self.assertFalse(flight["is_estimated"])
            self.assertEqual(transfer["type"], "transfer")
            self.assertTrue(transfer["is_estimated"])
            self.assertIn(ground["type"], ("train", "bus"))
            self.assertTrue(ground["is_estimated"])
            self.assertTrue(route["has_estimated_segments"])


class CalendarTTLTests(unittest.TestCase):
    def setUp(self):
        self.provider = GoogleFlightsProvider(cache_ttl_seconds=600)
        self.key = (
            f"{self.provider._normalize_city('Athens')[0]}_"
            f"{self.provider._normalize_city('Prague')[0]}_oneway"
        )

    def test_fresh_calendar_entry_is_returned(self):
        prices = {"2026-10-25": 11500.0}
        self.provider.calendar_cache[self.key] = {
            "timestamp": datetime.now(timezone.utc).timestamp(),
            "prices": prices,
        }
        self.assertEqual(self.provider.get_calendar_prices("Athens", "Prague", "oneway"), prices)

    def test_calendar_entry_older_than_ttl_returns_empty(self):
        stale_ts = datetime.now(timezone.utc).timestamp() - 6 * 600 - 60
        self.provider.calendar_cache[self.key] = {
            "timestamp": stale_ts,
            "prices": {"2026-10-25": 11500.0},
        }
        self.assertEqual(self.provider.get_calendar_prices("Athens", "Prague", "oneway"), {})
        self.assertNotIn(self.key, self.provider.calendar_cache)


class PlaywrightMissingWarningTests(unittest.TestCase):
    def test_missing_playwright_logs_install_hint_once(self):
        provider = GoogleFlightsProvider()
        with mock.patch.object(gfp, "PLAYWRIGHT_AVAILABLE", False), \
                mock.patch.object(gfp, "_PLAYWRIGHT_MISSING_WARNED", False):
            with self.assertLogs(gfp.logger, level="WARNING") as cm:
                first = provider.search_flights("Athens", "Prague", "2026-10-25", None, "oneway")
                second = provider.search_flights("Athens", "Prague", "2026-10-25", None, "oneway")
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(len(cm.records), 1)
        self.assertIn("pip install playwright && playwright install chromium", cm.records[0].getMessage())


if __name__ == "__main__":
    unittest.main()
