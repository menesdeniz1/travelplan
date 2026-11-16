import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock
from travel_engine.models import TripWatch
from travel_engine.storage import DB_ENV_VAR
from travel_engine.tracker import PriceTracker
from travel_engine.notifier import AlertNotifier
from travel_engine.daemon_tracker import DaemonTracker
from travel_engine.multimodal_router import MultiModalRouter

class MockNotifier(AlertNotifier):
    def __init__(self):
        self.config = {"notifications_enabled": False}
        self.sent_alerts = []
        self.sent_mm_alerts = []

    def _save_to_alerts_file(self, alert_record):
        pass

    def send_multimodal_alert(self, origin, destination, multimodal_price, target_budget, savings_vs_direct, journey_duration_str, segments_summary, departure_date="", route_id=""):
        rec = {
            "type": "multimodal",
            "origin": origin,
            "destination": destination,
            "current_price": multimodal_price,
            "target_budget": target_budget,
            "savings_vs_direct": savings_vs_direct,
            "duration": journey_duration_str,
            "segments_summary": segments_summary,
            "departure_date": departure_date,
            "route_id": route_id,
        }
        self.sent_mm_alerts.append(rec)
        return rec

class MockProviderForDaemon:
    def search_flights(self, origin, destination, departure_date, return_date=None, trip_type="oneway"):
        orig = origin.upper()
        dest = destination.upper()
        # Direct flight is expensive: 12000 TL
        if ("ATH" in orig or "ATHENS" in orig) and ("PRG" in dest or "PRAGUE" in dest):
            return [{
                "airline": "Aegean Airlines",
                "origin": "ATH",
                "destination": "PRG",
                "departure_time": "10:00",
                "arrival_time": "12:00",
                "total_price_try": 12000.0,
                "stops_count": 0,
            }]
        # Catchment flight Athens -> Vienna is 3500 TL
        elif ("ATH" in orig or "ATHENS" in orig) and ("VIE" in dest or "VIENNA" in dest):
            return [{
                "airline": "Ryanair",
                "origin": "ATH",
                "destination": "VIE",
                "departure_time": "09:00",
                "arrival_time": "11:15",
                "total_price_try": 3500.0,
                "stops_count": 0,
            }]
        return []

class TestMultiModalDaemon(unittest.TestCase):
    def setUp(self):
        self.mock_notifier = MockNotifier()
        # Own temp directory and database for every test; removed in cleanup.
        tmp = tempfile.TemporaryDirectory(prefix="travelplan_daemon_")
        self.addCleanup(tmp.cleanup)
        self.test_storage = os.path.join(tmp.name, "test_watches_daemon.json")
        env = mock.patch.dict(os.environ, {DB_ENV_VAR: os.path.join(tmp.name, "travelplan.db")})
        env.start()
        self.addCleanup(env.stop)
        self.tracker = PriceTracker(storage_path=self.test_storage)
        self.daemon = DaemonTracker(tracker=self.tracker, notifier=self.mock_notifier)
        self.daemon.provider = MockProviderForDaemon()
        # Re-wire router with mock provider
        self.daemon.multimodal_router.flight_provider = self.daemon.provider

    def test_tripwatch_include_multimodal_attribute(self):
        watch = TripWatch(
            id="watch_test_1",
            origin="Athens",
            destination="Prague",
            target_budget_try=6000.0,
            departure_date="2026-10-25"
        )
        self.assertTrue(getattr(watch, "include_multimodal", True))
        data = watch.to_dict()
        self.assertIn("include_multimodal", data)
        self.assertTrue(data["include_multimodal"])

    def test_daemon_triggers_multimodal_alert_when_under_budget(self):
        watch = TripWatch(
            id="watch_ath_prg",
            origin="Athens",
            destination="Prague",
            target_budget_try=6000.0,  # Direct flight is 12000, but hybrid route is ~4250 TL
            departure_date="2026-10-25"
        )
        res = self.daemon.scan_watch(watch)
        self.assertTrue(res.get("triggered"))
        self.assertGreater(len(self.mock_notifier.sent_mm_alerts), 0)
        alert = self.mock_notifier.sent_mm_alerts[0]
        self.assertEqual(alert["destination"], "Prague")
        self.assertLess(alert["current_price"], 6000.0)
        self.assertGreater(alert["savings_vs_direct"], 5000.0)

    def test_daemon_deduplicates_multimodal_alerts(self):
        watch = TripWatch(
            id="watch_dedup",
            origin="Athens",
            destination="Prague",
            target_budget_try=6000.0,
            departure_date="2026-10-25"
        )
        # First scan triggers alert
        self.daemon.scan_watch(watch)
        self.assertEqual(len(self.mock_notifier.sent_mm_alerts), 1)

        # Second scan with same price should NOT trigger duplicate alert
        self.daemon.scan_watch(watch)
        self.assertEqual(len(self.mock_notifier.sent_mm_alerts), 1)

    def test_tracker_persists_and_reloads_include_multimodal(self):
        w = self.tracker.add_watch(
            origin="Athens",
            destination="Prague",
            target_budget_try=5000.0,
            include_multimodal=False
        )
        self.assertFalse(w.include_multimodal)

        # Reload in a new tracker instance
        new_tracker = PriceTracker(storage_path=self.test_storage)
        reloaded_watch = new_tracker.get_watch(w.id)
        self.assertIsNotNone(reloaded_watch)
        self.assertFalse(reloaded_watch.include_multimodal)

if __name__ == "__main__":
    unittest.main()
