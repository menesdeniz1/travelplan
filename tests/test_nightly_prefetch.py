"""Tests for nightly prefetch (cache warming) for trip watches (T1)."""

import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from server_v2 import _run_daemon_worker
from travel_engine.daemon_tracker import (
    DaemonTracker,
    is_in_prefetch_window,
    is_prefetch_enabled,
)
from travel_engine.tracker import PriceTracker


class FakeTripPlanner:
    def __init__(self, plan=None, raise_error=False):
        self.plan = plan if plan is not None else {"options": [], "warnings": []}
        self.calls = []
        self.raise_error = raise_error

    def plan_trip(self, origin, destination, depart_dates, **kwargs):
        if self.raise_error:
            raise RuntimeError("Trip planner error")
        self.calls.append(dict(origin=origin, destination=destination, depart_dates=list(depart_dates), **kwargs))
        return dict(self.plan)


class FakeNotifier:
    def __init__(self):
        self.alerts = []
        self.mm_alerts = []

    def send_alert(self, **kwargs):
        self.alerts.append(kwargs)
        return kwargs

    def send_multimodal_alert(self, **kwargs):
        self.mm_alerts.append(kwargs)
        return kwargs


class TestNightlyPrefetch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="prefetch_test_")
        self.storage = os.path.join(self.tmpdir, "watches.json")
        self.tracker = PriceTracker(storage_path=self.storage)
        self.notifier = FakeNotifier()
        self.planner = FakeTripPlanner()
        self.daemon = DaemonTracker(
            tracker=self.tracker,
            notifier=self.notifier,
            trip_planner=self.planner,
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def make_trip_watch(self, **overrides):
        kwargs = dict(
            origin="ATH",
            destination="PRG",
            target_budget_try=6000.0,
            trip_query={
                "depart_dates": ["2030-10-27"],
                "include_side_routes": True,
            },
        )
        kwargs.update(overrides)
        return self.tracker.add_watch(**kwargs)

    # 1. Window checking: inside / outside
    def test_window_inside_and_outside(self):
        # Default window: 03:00-05:00
        self.assertTrue(is_in_prefetch_window(datetime(2026, 10, 20, 3, 0)))
        self.assertTrue(is_in_prefetch_window(datetime(2026, 10, 20, 3, 30)))
        self.assertTrue(is_in_prefetch_window(datetime(2026, 10, 20, 4, 59)))
        self.assertTrue(is_in_prefetch_window(datetime(2026, 10, 20, 5, 0)))

        self.assertFalse(is_in_prefetch_window(datetime(2026, 10, 20, 2, 59)))
        self.assertFalse(is_in_prefetch_window(datetime(2026, 10, 20, 5, 1)))
        self.assertFalse(is_in_prefetch_window(datetime(2026, 10, 20, 12, 0)))
        self.assertFalse(is_in_prefetch_window(datetime(2026, 10, 20, 23, 59)))

        # Custom window: 01:00-02:30
        self.assertTrue(is_in_prefetch_window(datetime(2026, 10, 20, 1, 15), "01:00-02:30"))
        self.assertFalse(is_in_prefetch_window(datetime(2026, 10, 20, 3, 0), "01:00-02:30"))

    # 2. Prefetch executes plan_trip without alerts and only for active trip watches
    def test_prefetch_watches_calls_plan_trip_without_alerts(self):
        w1 = self.make_trip_watch()
        w2_inactive = self.make_trip_watch(
            watch_id="w2",
            trip_query={"depart_dates": ["2030-10-28"]},
        )
        w2_inactive.active = False
        self.tracker.save()

        # Regular flight watch (not a trip watch)
        self.tracker.add_watch(
            origin="VIE",
            destination="ATH",
            target_budget_try=4000.0,
            departure_date="2030-10-29",
            watch_id="w3_flight",
        )

        results = self.daemon.prefetch_watches(now=datetime(2026, 10, 20, 3, 30))

        # Only active trip watch w1 was prefetched
        self.assertEqual(len(self.planner.calls), 1)
        self.assertEqual(self.planner.calls[0]["origin"], "ATH")
        self.assertEqual(self.planner.calls[0]["destination"], "PRG")
        self.assertEqual(self.planner.calls[0]["depart_dates"], ["2030-10-27"])

        # No alerts were sent
        self.assertEqual(len(self.notifier.alerts), 0)
        self.assertEqual(len(self.notifier.mm_alerts), 0)
        # History untouched
        self.assertEqual(len(w1.history), 0)

    # 3. All future dates are sent in a single plan_trip call; past dates are filtered out
    def test_prefetch_watches_searches_all_future_dates_single_call(self):
        self.make_trip_watch(
            trip_query={
                "depart_dates": ["2026-10-10", "2030-10-27", "2030-10-28"],
                "include_side_routes": True,
            }
        )
        results = self.daemon.prefetch_watches(now=datetime(2026, 10, 20, 3, 30))

        # Single plan_trip call per watch
        self.assertEqual(len(self.planner.calls), 1)
        # All future dates preserved in order; past date (2026-10-10) filtered out
        self.assertEqual(self.planner.calls[0]["depart_dates"], ["2030-10-27", "2030-10-28"])
        self.assertEqual(results[0]["status"], "ok")
        self.assertTrue(results[0]["searched"])

    # 4. Past dates only are skipped
    def test_prefetch_watches_skips_past_dates(self):
        self.make_trip_watch(
            trip_query={
                "depart_dates": ["2026-10-15"],
                "include_side_routes": True,
            }
        )
        results = self.daemon.prefetch_watches(now=datetime(2026, 10, 20, 3, 30))

        self.assertEqual(len(self.planner.calls), 0)
        self.assertEqual(results[0]["status"], "past_dates")

    # 5. Resilience: planner error does not crash prefetch
    def test_prefetch_watches_error_does_not_crash(self):
        failing_planner = FakeTripPlanner(raise_error=True)
        daemon = DaemonTracker(
            tracker=self.tracker,
            notifier=self.notifier,
            trip_planner=failing_planner,
        )
        self.make_trip_watch()

        # Should not raise exception
        results = daemon.prefetch_watches(now=datetime(2026, 10, 20, 3, 30))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")

    # 6. Worker loop: runs inside window once per day, does not repeat on same day
    def test_worker_runs_prefetch_once_per_day(self):
        mock_daemon = mock.MagicMock()
        mock_daemon.prefetch_watches.return_value = []
        mock_daemon.scan_all_watches.return_value = []

        clock_ticks = [
            datetime(2026, 10, 20, 3, 30),  # Day 1: in window -> should prefetch
            datetime(2026, 10, 20, 4, 0),   # Day 1: still in window -> should NOT prefetch again
            datetime(2026, 10, 20, 12, 0),  # Day 1: outside window -> should NOT prefetch
            datetime(2026, 10, 21, 3, 15),  # Day 2: in window -> should prefetch once
        ]
        tick_index = [0]

        def fake_now():
            idx = tick_index[0]
            tick_index[0] += 1
            if idx < len(clock_ticks):
                return clock_ticks[idx]
            return clock_ticks[-1]

        _run_daemon_worker(
            tracker_inst=self.tracker,
            daemon_inst=mock_daemon,
            now_fn=fake_now,
            sleep_fn=lambda _: None,
            initial_delay=0,
            tick_secs=0,
            scan_interval=3600 * 24,  # avoid 6h scan triggering on each tick
            max_ticks=4,
        )

        # Pre-fetch should have been called exactly 2 times (once on Day 1, once on Day 2)
        self.assertEqual(mock_daemon.prefetch_watches.call_count, 2)

    # 7. Worker loop: TRAVELPLAN_PREFETCH=0 disables prefetch completely
    def test_worker_disabled_by_env_var(self):
        mock_daemon = mock.MagicMock()

        with mock.patch.dict(os.environ, {"TRAVELPLAN_PREFETCH": "0"}):
            self.assertFalse(is_prefetch_enabled())
            _run_daemon_worker(
                tracker_inst=self.tracker,
                daemon_inst=mock_daemon,
                now_fn=lambda: datetime(2026, 10, 20, 3, 30),
                sleep_fn=lambda _: None,
                initial_delay=0,
                tick_secs=0,
                scan_interval=3600 * 24,
                max_ticks=2,
            )

        self.assertEqual(mock_daemon.prefetch_watches.call_count, 0)

    # 8. Worker loop: does not crash on prefetch exception, and does not retry on the same day
    def test_worker_loop_does_not_crash_on_prefetch_exception(self):
        mock_daemon = mock.MagicMock()
        mock_daemon.prefetch_watches.side_effect = RuntimeError("database locked")

        ticks = [
            datetime(2026, 10, 20, 3, 30),  # In window -> prefetch attempted & fails
            datetime(2026, 10, 20, 4, 0),   # Still in window, same day -> should NOT retry
        ]
        tick_idx = [0]

        def fake_now():
            idx = tick_idx[0]
            tick_idx[0] += 1
            return ticks[min(idx, len(ticks) - 1)]

        # Must finish max_ticks without raising
        _run_daemon_worker(
            tracker_inst=self.tracker,
            daemon_inst=mock_daemon,
            now_fn=fake_now,
            sleep_fn=lambda _: None,
            initial_delay=0,
            tick_secs=0,
            scan_interval=3600 * 24,
            max_ticks=2,
        )
        # Prefetch attempted only once on the same day despite error
        self.assertEqual(mock_daemon.prefetch_watches.call_count, 1)


if __name__ == "__main__":
    unittest.main()
