"""Tests for Google Flights health metrics and threshold alerting (OLC-4 / B07)."""

import time
import unittest
from unittest import mock

from travel_engine.providers.google_flights_provider import GoogleFlightsProvider


class GoogleHealthMetricsTests(unittest.TestCase):
    def setUp(self):
        GoogleFlightsProvider.reset_shared_state()
        self.addCleanup(GoogleFlightsProvider.reset_shared_state)

    def test_health_metrics_empty_defaults(self):
        provider = GoogleFlightsProvider()
        health = provider.health_status()
        self.assertIn("metrics_1h", health)
        self.assertIn("metrics_24h", health)
        self.assertIn("metrics", health)

        m1h = health["metrics_1h"]
        self.assertEqual(m1h["page_count"], 0)
        self.assertEqual(m1h["block_rate"], 0.0)
        self.assertEqual(m1h["empty_page_rate"], 0.0)
        self.assertEqual(m1h["avg_page_duration"], 0.0)

    def test_health_metrics_calculated_accurately(self):
        provider = GoogleFlightsProvider()
        now = time.time()

        # 4 successful scrapes of 2.0s, 1 empty page of 3.0s, 1 blocked of 1.0s (total 6 pages, 12s)
        provider.record_page_scrape("ok", 2.0, timestamp=now - 50)
        provider.record_page_scrape("ok", 2.0, timestamp=now - 40)
        provider.record_page_scrape("ok", 2.0, timestamp=now - 30)
        provider.record_page_scrape("ok", 2.0, timestamp=now - 20)
        provider.record_page_scrape("empty_page", 3.0, timestamp=now - 10)
        provider.record_page_scrape("blocked", 1.0, timestamp=now - 5)

        health = provider.health_status()
        m1h = health["metrics_1h"]
        self.assertEqual(m1h["page_count"], 6)
        self.assertAlmostEqual(m1h["block_rate"], 1 / 6, places=3)
        self.assertAlmostEqual(m1h["empty_page_rate"], 1 / 6, places=3)
        self.assertAlmostEqual(m1h["avg_page_duration"], 12.0 / 6, places=3)

    def test_window_filtering_1h_vs_24h(self):
        provider = GoogleFlightsProvider()
        now = time.time()

        # 1 page 30 mins ago (in 1h and 24h)
        provider.record_page_scrape("ok", 2.0, timestamp=now - 1800)
        # 1 page 3 hours ago (in 24h, not 1h)
        provider.record_page_scrape("empty_page", 4.0, timestamp=now - 10800)
        # 1 page 26 hours ago (in neither)
        provider.record_page_scrape("blocked", 1.0, timestamp=now - 93600)

        health = provider.health_status()
        m1h = health["metrics_1h"]
        m24h = health["metrics_24h"]

        self.assertEqual(m1h["page_count"], 1)
        self.assertEqual(m1h["empty_page_rate"], 0.0)

        self.assertEqual(m24h["page_count"], 2)
        self.assertAlmostEqual(m24h["empty_page_rate"], 0.5, places=3)

    def test_threshold_alert_triggers_when_block_rate_exceeds_1_percent(self):
        alert_calls = []

        def on_alert(msg, data):
            alert_calls.append((msg, data))

        provider = GoogleFlightsProvider(on_threshold_alert=on_alert)
        now = time.time()

        # 98 ok, 2 blocked -> block_rate = 2% > 1%
        for i in range(98):
            provider.record_page_scrape("ok", 2.0, timestamp=now - 100 + i)
        self.assertEqual(len(alert_calls), 0)

        provider.record_page_scrape("blocked", 1.0, timestamp=now - 2)
        provider.record_page_scrape("blocked", 1.0, timestamp=now - 1)

        self.assertEqual(len(alert_calls), 1)
        self.assertIn("engel", alert_calls[0][0].lower())

    def test_threshold_alert_triggers_when_empty_page_rate_exceeds_20_percent(self):
        alert_calls = []

        def on_alert(msg, data):
            alert_calls.append((msg, data))

        provider = GoogleFlightsProvider(on_threshold_alert=on_alert)
        now = time.time()

        # 7 ok, 3 empty_page -> 30% > 20%
        for i in range(7):
            provider.record_page_scrape("ok", 2.0, timestamp=now - 50 + i)
        self.assertEqual(len(alert_calls), 0)

        for i in range(3):
            provider.record_page_scrape("empty_page", 2.0, timestamp=now - 10 + i)

        self.assertEqual(len(alert_calls), 1)
        self.assertIn("boş sayfa", alert_calls[0][0].lower())

    def test_single_system_alert_no_repeat_until_recovery(self):
        alert_calls = []

        def on_alert(msg, data):
            alert_calls.append((msg, data))

        provider = GoogleFlightsProvider(on_threshold_alert=on_alert)
        now = time.time()

        # Trigger empty page threshold
        for _ in range(5):
            provider.record_page_scrape("ok", 2.0, timestamp=now - 30)
        for _ in range(5):
            provider.record_page_scrape("empty_page", 2.0, timestamp=now - 20)

        self.assertEqual(len(alert_calls), 1)

        # Still above threshold with more empty pages -> NO repeat alert
        provider.record_page_scrape("empty_page", 2.0, timestamp=now - 15)
        self.assertEqual(len(alert_calls), 1)

        # Add many ok pages to recover below 20% (e.g. 50 ok pages)
        for _ in range(50):
            provider.record_page_scrape("ok", 2.0, timestamp=now - 10)

        # Still 1 alert
        self.assertEqual(len(alert_calls), 1)

        # Now trigger again -> new alert sent!
        for _ in range(50):
            provider.record_page_scrape("empty_page", 2.0, timestamp=now - 5)

        self.assertEqual(len(alert_calls), 2)

    def test_server_health_payload_contains_metrics(self):
        import server_v2
        health_payload = server_v2._health_payload()
        self.assertIn("flights", health_payload)
        self.assertIn("metrics_1h", health_payload["flights"])
        self.assertIn("metrics_24h", health_payload["flights"])
        self.assertIn("metrics", health_payload["flights"])


if __name__ == "__main__":
    unittest.main()
