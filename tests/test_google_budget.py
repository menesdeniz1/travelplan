"""Unit tests for Google Budget Governor and Priority Classes (GOO-8, card B05)."""

import contextvars
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from travel_engine.google_budget import (
    GoogleBudgetGovernor,
    get_current_priority,
    get_google_budget,
    reset_google_budget,
    set_current_priority,
)


class GoogleBudgetGovernorTests(unittest.TestCase):
    def setUp(self):
        reset_google_budget()

    def test_import_and_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            gov = reset_google_budget()
            self.assertEqual(gov.hourly_limit, 60)
            self.assertEqual(gov.daily_limit, 300)
            self.assertEqual(gov.background_hourly_limit, 42)
            self.assertEqual(gov.background_daily_limit, 210)

    def test_env_overrides(self):
        with mock.patch.dict(
            os.environ,
            {"TRAVELPLAN_GOOGLE_HOURLY_LIMIT": "100", "TRAVELPLAN_GOOGLE_DAILY_LIMIT": "500"},
        ):
            gov = reset_google_budget()
            self.assertEqual(gov.hourly_limit, 100)
            self.assertEqual(gov.daily_limit, 500)
            self.assertEqual(gov.background_hourly_limit, 70)
            self.assertEqual(gov.background_daily_limit, 350)

    def test_priority_limits_and_reservation(self):
        """Background classes (watch, prefetch) can use at most 70%; interactive gets the rest."""
        current_time = 1000.0
        gov = GoogleBudgetGovernor(hourly_limit=10, daily_limit=50, clock=lambda: current_time)

        # 1. Background consumes up to 70% (7 requests)
        for _ in range(7):
            self.assertTrue(gov.take("watch"))

        # 2. 8th background request is refused (70% cap reached)
        self.assertFalse(gov.take("watch"))
        self.assertFalse(gov.take("prefetch"))

        # 3. Interactive request is granted (uses remaining 30%)
        self.assertTrue(gov.take("interactive"))
        self.assertTrue(gov.take("interactive"))
        self.assertTrue(gov.take("interactive"))

        # 4. Total hourly limit (10) reached: even interactive is now refused
        self.assertFalse(gov.take("interactive"))

        stats = gov.stats()
        self.assertEqual(stats["hourly_count"], 10)
        self.assertEqual(stats["refused_by_kind"]["watch"], 1)
        self.assertEqual(stats["refused_by_kind"]["prefetch"], 1)
        self.assertEqual(stats["refused_by_kind"]["interactive"], 1)

    def test_window_reset_on_clock_advance(self):
        """When clock advances past 1 hour, hourly counters reset."""
        current_time = 1000.0
        gov = GoogleBudgetGovernor(hourly_limit=2, daily_limit=10, clock=lambda: current_time)

        self.assertTrue(gov.take("interactive"))
        self.assertTrue(gov.take("interactive"))
        self.assertFalse(gov.take("interactive"))

        # Advance by 3601 seconds (past the 1-hour window)
        current_time += 3601.0
        self.assertTrue(gov.take("interactive"))

    def test_context_var_propagation_in_thread_pool(self):
        """ContextVar priority correctly carries into worker threads."""
        set_current_priority("watch")
        self.assertEqual(get_current_priority(), "watch")

        def worker_check():
            return get_current_priority()

        with ThreadPoolExecutor(max_workers=2) as pool:
            ctx = contextvars.copy_context()
            future = pool.submit(ctx.run, worker_check)
            result = future.result()

        self.assertEqual(result, "watch")
        set_current_priority("interactive")

    def test_google_flights_provider_respects_budget(self):
        """When budget is exhausted, GoogleFlightsProvider does not open page and records budget_exhausted."""
        from travel_engine.providers.google_flights_provider import GoogleFlightsProvider

        # Set limit to 0
        gov = reset_google_budget(hourly_limit=1, daily_limit=1)
        self.assertTrue(gov.take("interactive"))  # exhaust

        provider = GoogleFlightsProvider(min_interval_secs=0.0)
        results = provider.search_flights(
            origin="Athens",
            destination="Prague",
            departure_date="2026-11-20",
            trip_type="oneway",
        )
        self.assertEqual(results, [])
        self.assertEqual(provider.last_search_status["status"], "budget_exhausted")

    def test_health_includes_budget_stats(self):
        """Provider health snapshot includes budget metrics."""
        from travel_engine.providers.google_flights_provider import GoogleFlightsProvider

        gov = reset_google_budget(hourly_limit=50, daily_limit=200)
        gov.take("interactive")

        provider = GoogleFlightsProvider()
        health = provider.health_status()
        self.assertIn("budget", health)
        self.assertEqual(health["budget"]["hourly_limit"], 50)
        self.assertEqual(health["budget"]["hourly_count"], 1)


if __name__ == "__main__":
    unittest.main()
