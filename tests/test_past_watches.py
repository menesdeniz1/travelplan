"""TKP-6: skip scanning watches with past departure dates."""
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from travel_engine.daemon_tracker import DaemonTracker
from travel_engine.tracker import PriceTracker


class FakeProvider:
    def __init__(self):
        self.calls = 0

    def search_flights(self, *args, **kwargs):
        self.calls += 1
        return []


class FakePlanner:
    def __init__(self):
        self.calls = 0

    def plan_trip(self, *args, **kwargs):
        self.calls += 1
        return {
            "query": {"origin": "IST", "destination": "ATH"},
            "options": [],
            "warnings": [],
        }


class PastWatchesTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="past_watches_")
        self.storage = os.path.join(self.tmpdir, "watches.json")
        self.tracker = PriceTracker(storage_path=self.storage)
        self.provider = FakeProvider()
        self.planner = FakePlanner()
        self.now_val = datetime(2026, 11, 20, 9, 0)
        self.daemon = DaemonTracker(
            tracker=self.tracker,
            provider=self.provider,
            trip_planner=self.planner,
            now_fn=lambda: self.now_val,
        )

        sleep_patch = mock.patch("travel_engine.daemon_tracker.time.sleep")
        self.mock_sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def test_past_trip_watch_skipped_and_kept_active(self):
        # (kırmızı) depart_dates ["2026-11-01", "2026-11-02"] → planlayıcı çağrılmaz, durum past_dates, takip aktif.
        watch = self.tracker.add_watch(
            origin="IST", destination="ATH", target_budget_try=5000.0,
            trip_query={"depart_dates": ["2026-11-01", "2026-11-02"]},
        )
        res = self.daemon.scan_all_watches()
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "past_dates")
        self.assertEqual(res[0]["watch_id"], watch.id)
        self.assertFalse(res[0]["triggered"])
        self.assertEqual(self.planner.calls, 0)
        self.mock_sleep.assert_not_called()
        # Takip aktif kalmalı
        self.assertTrue(self.tracker.watches[watch.id].active)

    def test_trip_watch_with_flex_days_reaching_future_is_scanned(self):
        # (kırmızı) ["2026-11-18"] + flex_days 3 → aranır.
        watch = self.tracker.add_watch(
            origin="IST", destination="ATH", target_budget_try=5000.0,
            trip_query={"depart_dates": ["2026-11-18"], "flex_days": 3},
        )
        res = self.daemon.scan_all_watches()
        self.assertEqual(len(res), 1)
        self.assertNotEqual(res[0]["status"], "past_dates")
        self.assertEqual(self.planner.calls, 1)

    def test_trip_watch_today_is_scanned(self):
        # (koruma) ["2026-11-20"] (bugün) → aranır.
        watch = self.tracker.add_watch(
            origin="IST", destination="ATH", target_budget_try=5000.0,
            trip_query={"depart_dates": ["2026-11-20"]},
        )
        res = self.daemon.scan_all_watches()
        self.assertEqual(len(res), 1)
        self.assertNotEqual(res[0]["status"], "past_dates")
        self.assertEqual(self.planner.calls, 1)

    def test_past_flight_watch_skipped(self):
        # (kırmızı) Uçuş takibi departure_date "2026-11-01" → sağlayıcı çağrılmaz.
        watch = self.tracker.add_watch(
            origin="IST", destination="ATH", target_budget_try=5000.0,
            departure_date="2026-11-01",
        )
        res = self.daemon.scan_all_watches()
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "past_dates")
        self.assertEqual(self.provider.calls, 0)
        self.mock_sleep.assert_not_called()
        self.assertTrue(self.tracker.watches[watch.id].active)


if __name__ == "__main__":
    unittest.main()
