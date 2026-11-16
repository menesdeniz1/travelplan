"""Wiring of the search log, flight health alerts, /api/health and the Telegram bot in server_v2."""

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from io import BytesIO
from logging.handlers import RotatingFileHandler
from unittest import mock

import server_v2
from server_v2 import TravelPlanAPIHandler
from travel_engine.providers.flixbus_provider import FlixBusProvider
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider
from travel_engine.providers.google_maps_transit_provider import GoogleMapsTransitProvider
from travel_engine.providers.live_rail_provider import LiveRailProvider
from travel_engine.providers.obilet_fares import ObiletFromPrices
from travel_engine.providers.rail_bus_provider import RailBusProvider
from travel_engine.providers.trenitalia_provider import TrenitaliaProvider
from travel_engine.search_log import SearchLog
from travel_engine.tracker import PriceTracker

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MockAPIHandler(TravelPlanAPIHandler):
    def __init__(self, request_bytes):
        self.client_address = ("127.0.0.1", 12345)
        self.rfile = BytesIO(request_bytes)
        self.wfile = BytesIO()
        self.raw_requestline = self.rfile.readline()
        self.parse_request()


def _get(path):
    """Run do_GET for `path` and return (status_line_block, parsed_json_body)."""
    handler = MockAPIHandler(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("utf-8"))
    handler.do_GET()
    raw = handler.wfile.getvalue().decode("utf-8")
    header_part, body_part = raw.split("\r\n\r\n", 1)
    return header_part, body_part


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant in response: {name}")


class FakeFlightProvider:
    """Stands in for the module-level GoogleFlightsProvider; never touches Playwright."""

    def resource_status(self):
        return {
            "browser_running": False,
            "active_pages": 0,
            "max_browsers": 2,
            "searches_since_launch": 0,
            "process_rss_mb": None,
        }

    def health_status(self):
        return {
            "healthy": True,
            "consecutive_failures": 0,
            "last_ok_at": "2026-10-19T10:00:00+00:00",
            "last_status": "ok",
            "recent": ["ok"],
            "last_search": None,
            "blocked_cooldown_remaining_secs": 0.0,
        }


class NonFiniteFlightProvider(FakeFlightProvider):
    def health_status(self):
        status = super().health_status()
        status["blocked_cooldown_remaining_secs"] = float("inf")
        status["extra"] = float("nan")
        return status


class FakeRailProvider:
    """Stands in for the module-level RailBusProvider; reports fixed ground-source health."""

    def sources_health(self):
        return {
            "regiojet": {"source": "regiojet", "status": "ok", "requests": 3, "last_ok_at": "2026-10-19T10:00:00"},
            "flixbus": {"source": "flixbus", "status": "error", "consecutive_failures": 2, "last_error": "HTTP 403"},
        }


class NonFiniteRailProvider(FakeRailProvider):
    def sources_health(self):
        health = super().sources_health()
        health["regiojet"]["last_latency"] = float("nan")
        return health


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_dir = tmp_dir.name

        self.search_log = SearchLog(":memory:")
        self.addCleanup(self.search_log.close)
        patches = [
            mock.patch.object(server_v2, "tracker", PriceTracker(storage_path=os.path.join(self.tmp_dir, "watches.json"))),
            mock.patch.object(server_v2, "google_flights_provider", FakeFlightProvider()),
            mock.patch.object(server_v2, "rail_provider", FakeRailProvider()),
            mock.patch.object(server_v2, "search_log", self.search_log),
            mock.patch.object(server_v2, "get_eur_try_rate", return_value=(54.3, "frankfurter", True)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_health_shape(self):
        server_v2.tracker.add_watch(
            origin="Istanbul",
            destination="Prague",
            target_budget_try=5000.0,
            departure_date="2026-10-27",
            trip_type="oneway",
        )
        self.search_log.record("ATH", "PRG", "2026-10-27", "ok", 3, 4200.0, source="google_flights")

        header, body = _get("/api/health")
        self.assertIn("200 OK", header)
        data = json.loads(body, parse_constant=_reject_constant)

        self.assertEqual(set(data), {"flights", "fx", "watches", "search_log", "search_runs", "resources", "ground_sources"})
        self.assertEqual(set(data["ground_sources"]), {"regiojet", "flixbus"})
        self.assertEqual(data["ground_sources"]["regiojet"]["status"], "ok")
        self.assertEqual(data["ground_sources"]["flixbus"]["status"], "error")
        self.assertEqual(data["ground_sources"]["flixbus"]["last_error"], "HTTP 403")
        self.assertEqual(
            set(data["resources"]),
            {"browser_running", "active_pages", "max_browsers", "searches_since_launch", "process_rss_mb"},
        )
        self.assertEqual(data["flights"]["last_status"], "ok")
        self.assertTrue(data["flights"]["healthy"])
        self.assertEqual(data["fx"], {"rate": 54.3, "source": "frankfurter", "is_live": True})
        self.assertEqual(data["watches"], {"active": 1, "total": 1})
        self.assertEqual(len(data["search_log"]["recent"]), 1)
        entry = data["search_log"]["recent"][0]
        self.assertEqual((entry["origin"], entry["destination"], entry["date"]), ("ATH", "PRG", "2026-10-27"))
        self.assertEqual(entry["min_price"], 4200.0)

    def test_recent_searches_are_capped_at_ten(self):
        for day in range(12):
            self.search_log.record("ATH", "PRG", f"2026-10-{day + 1:02d}", "ok", 1, 1000.0 + day)

        _, body = _get("/api/health")
        recent = json.loads(body)["search_log"]["recent"]
        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[0]["date"], "2026-10-12")  # newest first

    def test_health_is_strict_json_even_with_non_finite_values(self):
        with mock.patch.object(server_v2, "google_flights_provider", NonFiniteFlightProvider()), \
                mock.patch.object(server_v2, "rail_provider", NonFiniteRailProvider()), \
                mock.patch.object(server_v2, "get_eur_try_rate", return_value=(float("nan"), "default", False)):
            header, body = _get("/api/health")

        self.assertIn("200 OK", header)
        data = json.loads(body, parse_constant=_reject_constant)  # would raise on NaN/Infinity
        self.assertIsNone(data["fx"]["rate"])
        self.assertIsNone(data["flights"]["blocked_cooldown_remaining_secs"])
        self.assertIsNone(data["flights"]["extra"])
        self.assertIsNone(data["ground_sources"]["regiojet"]["last_latency"])

    def test_health_does_not_create_missing_search_log_file(self):
        missing = os.path.join(self.tmp_dir, "never", "search_log.db")
        with mock.patch.object(server_v2, "search_log", server_v2._LazySearchLog(missing)):
            header, body = _get("/api/health")

        self.assertIn("200 OK", header)
        self.assertEqual(json.loads(body)["search_log"]["recent"], [])
        self.assertFalse(os.path.exists(missing))

    def test_search_log_read_failure_still_returns_health(self):
        broken = mock.Mock()
        broken.recent.side_effect = RuntimeError("disk gone")
        with mock.patch.object(server_v2, "search_log", broken):
            header, body = _get("/api/health")

        self.assertIn("200 OK", header)
        data = json.loads(body)
        self.assertEqual(data["search_log"]["recent"], [])
        self.assertIn("flights", data)


class GroundProviderWiringTests(unittest.TestCase):
    """One RailBusProvider (RegioJet + FlixBus) is shared by every consumer in the server."""

    def test_rail_provider_has_regiojet_flixbus_trenitalia_maps_and_obilet(self):
        rail = server_v2.rail_provider
        self.assertIsInstance(rail, RailBusProvider)
        self.assertIsInstance(rail.live_provider, LiveRailProvider)
        self.assertIsInstance(rail.bus_provider, FlixBusProvider)
        self.assertIsInstance(rail.trenitalia_provider, TrenitaliaProvider)
        self.assertIsInstance(rail.timetable_maps_provider, GoogleMapsTransitProvider)
        self.assertIsInstance(rail.bus_from_price_provider, ObiletFromPrices)

    def test_route_engine_trip_planner_router_and_daemon_share_it(self):
        rail = server_v2.rail_provider
        self.assertIs(server_v2.multimodal_router.rail_provider, rail)
        self.assertIs(server_v2.route_engine.ground_provider, rail)
        self.assertIs(server_v2.trip_planner.router, server_v2.multimodal_router)
        self.assertIs(server_v2.trip_planner.route_engine, server_v2.route_engine)
        self.assertIs(server_v2.daemon_tracker.rail_provider, rail)
        self.assertIs(server_v2.daemon_tracker.multimodal_router.rail_provider, rail)

    def test_health_reports_the_real_sources_as_json(self):
        search_log = SearchLog(":memory:")
        self.addCleanup(search_log.close)
        tmp_dir = tempfile.mkdtemp(prefix="ground_health_")
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        patches = [
            mock.patch.object(server_v2, "get_eur_try_rate", return_value=(54.0, "default", False)),
            mock.patch.object(server_v2, "google_flights_provider", FakeFlightProvider()),
            mock.patch.object(server_v2, "search_log", search_log),
            mock.patch.object(server_v2, "tracker", PriceTracker(storage_path=os.path.join(tmp_dir, "watches.json"))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        data = server_v2._health_payload()
        self.assertEqual(set(data["ground_sources"]),
                         {"regiojet", "flixbus", "trenitalia", "transitous", "google_maps", "obilet"})
        self.assertEqual(data["ground_sources"]["regiojet"]["source"], "regiojet")
        self.assertEqual(data["ground_sources"]["flixbus"]["source"], "flixbus")
        self.assertEqual(data["ground_sources"]["transitous"]["source"], "transitous")
        self.assertEqual(data["ground_sources"]["trenitalia"]["source"], "trenitalia")
        self.assertEqual(data["ground_sources"]["google_maps"]["source"], "google_maps")
        self.assertEqual(data["ground_sources"]["obilet"]["source"], "obilet")
        json.dumps(data, allow_nan=False)  # every value is JSON-safe


class SearchLogWiringTests(unittest.TestCase):
    def test_module_objects_are_wired_together(self):
        provider = server_v2.google_flights_provider
        self.assertIsInstance(provider, GoogleFlightsProvider)
        self.assertIs(provider.search_log, server_v2.search_log)
        self.assertIs(provider.on_health_change, server_v2._on_flight_health_change)

    def test_importing_server_does_not_create_the_log_and_first_write_does(self):
        tmp = tempfile.mkdtemp(prefix="wiring_log_")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = os.path.join(tmp, "nested", "search_log.db")
        env = dict(
            os.environ,
            TRAVELPLAN_SEARCH_LOG=target,
            TRAVELPLAN_DB=os.path.join(tmp, "travelplan.db"),
            TRAVELPLAN_NO_MIGRATE="1",
        )
        code = (
            "import os, server_v2 as s; p = s.search_log._path; "
            "before = os.path.exists(p); "
            "s.search_log.record('ATH', 'PRG', '2026-10-27', 'ok', 1, 100.0); "
            "print(before, os.path.exists(p))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "False True")
        self.assertTrue(os.path.exists(target))


class FlightHealthAlertTests(unittest.TestCase):
    """Health transitions of the real module-level provider reach the notifier."""

    def setUp(self):
        self.notifier = mock.Mock()
        self.notifier.send_system_message.return_value = True
        patches = [
            mock.patch.object(server_v2, "notifier", self.notifier),
            mock.patch.object(
                server_v2.google_flights_provider,
                "health",
                {
                    "consecutive_failures": 0,
                    "last_ok_at": None,
                    "last_status": None,
                    "recent": deque(maxlen=20),
                },
            ),
            # Run the background send inline so the assertions below are deterministic
            mock.patch.object(server_v2, "_start_background", side_effect=lambda fn, *args: fn(*args)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_unhealthy_then_recovered_messages(self):
        provider = server_v2.google_flights_provider
        at = "2026-10-19T10:00:00+00:00"
        for _ in range(2):
            provider._update_health("error", at)
        self.notifier.send_system_message.assert_not_called()

        provider._update_health("parse_error", at)  # third consecutive failure: unhealthy
        self.notifier.send_system_message.assert_called_once_with(
            "⚠️ Google Flights okunamıyor: son 3 arama başarısız (parse_error). Sonuçlar eksik olabilir."
        )

        provider._update_health("error", at)  # still unhealthy: no repeat message
        self.assertEqual(self.notifier.send_system_message.call_count, 1)

        provider._update_health("ok", at)  # recovery
        self.assertEqual(self.notifier.send_system_message.call_count, 2)
        self.assertEqual(
            self.notifier.send_system_message.call_args_list[-1][0][0],
            "✅ Google Flights araması tekrar çalışıyor.",
        )

    def test_callback_message_uses_snapshot_fields(self):
        server_v2._on_flight_health_change({"healthy": False, "consecutive_failures": 4, "last_status": "blocked"})
        self.notifier.send_system_message.assert_called_once_with(
            "⚠️ Google Flights okunamıyor: son 4 arama başarısız (blocked). Sonuçlar eksik olabilir."
        )


class FlightHealthCallbackIsAsyncTests(unittest.TestCase):
    """The Telegram send runs on a background thread, so a slow Telegram API never blocks a search."""

    def test_callback_returns_before_a_blocked_send_finishes(self):
        release = threading.Event()
        started = threading.Event()
        sent = []

        def slow_send(text):
            started.set()
            release.wait(5)
            sent.append(text)
            return True

        notifier = mock.Mock()
        notifier.send_system_message.side_effect = slow_send
        with mock.patch.object(server_v2, "notifier", notifier):
            begin = time.monotonic()
            server_v2._on_flight_health_change({"healthy": False, "consecutive_failures": 3, "last_status": "error"})
            elapsed = time.monotonic() - begin
            self.assertLess(elapsed, 1.0)           # the callback did not wait for the send
            self.assertTrue(started.wait(2))         # ...but the send did start in the background
            self.assertEqual(sent, [])               # and it is still blocked
            release.set()
            deadline = time.monotonic() + 5
            while not sent and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(len(sent), 1)
        self.assertIn("okunamıyor", sent[0])

    def test_failing_send_is_logged_not_raised(self):
        notifier = mock.Mock()
        notifier.send_system_message.side_effect = RuntimeError("telegram down")
        with mock.patch.object(server_v2, "notifier", notifier), self.assertLogs(server_v2.logger, "ERROR"):
            server_v2._send_flight_health_alert("x")
        notifier.send_system_message.assert_called_once_with("x")

    def test_start_background_runs_target_on_a_daemon_thread(self):
        done = threading.Event()
        thread = server_v2._start_background(done.set)
        self.assertTrue(done.wait(2))
        self.assertTrue(thread.daemon)


class NotifierCallbackFailureTests(unittest.TestCase):
    def test_failing_notifier_does_not_break_a_health_update(self):
        broken = mock.Mock()
        broken.send_system_message.side_effect = RuntimeError("telegram down")
        provider = GoogleFlightsProvider(on_health_change=lambda snap: broken.send_system_message("x"))
        with mock.patch.object(provider, "health", {
            "consecutive_failures": 2, "last_ok_at": None, "last_status": None, "recent": deque(maxlen=20),
        }):
            # Crossing into unhealthy fires the callback, which raises; the provider logs it and keeps going
            provider._update_health("error", "2026-10-19T10:00:00+00:00")
            broken.send_system_message.assert_called_once()
            self.assertEqual(provider.health_status()["last_status"], "error")
            self.assertFalse(provider.health_status()["healthy"])


class _Terminal(io.StringIO):
    def isatty(self):
        return True


class ServerLoggingTests(unittest.TestCase):
    """_configure_logging runs only from __main__; these tests call it against a temp BASE_DIR."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)  # runs last, after the handlers below are closed
        self.base_dir = tmp.name
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        saved_hook = sys.excepthook

        def restore():
            for handler in list(root.handlers):
                if handler not in saved_handlers:
                    root.removeHandler(handler)
                    handler.close()
            root.setLevel(saved_level)
            sys.excepthook = saved_hook

        self.addCleanup(restore)
        base = mock.patch.object(server_v2, "BASE_DIR", self.base_dir)
        base.start()
        self.addCleanup(base.stop)

    def test_importing_server_does_not_add_a_file_handler(self):
        self.assertFalse(any(isinstance(h, RotatingFileHandler) for h in logging.getLogger().handlers))

    def test_configure_writes_rotating_server_log_under_base_dir(self):
        server_v2._configure_logging()
        file_handlers = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
        self.assertEqual(len(file_handlers), 1)
        self.assertEqual(file_handlers[0].maxBytes, 5 * 1024 * 1024)
        self.assertEqual(file_handlers[0].backupCount, 3)

        logging.getLogger("travelplan.test").info("hello from the rotating log")
        for handler in logging.getLogger().handlers:
            handler.flush()
        path = os.path.join(self.base_dir, "logs", "server.log")
        with open(path, encoding="utf-8") as fh:
            self.assertIn("hello from the rotating log", fh.read())

    @staticmethod
    def _stream_handlers():
        return [h for h in logging.getLogger().handlers
                if type(h) is logging.StreamHandler]  # exact type: pytest's capture handler is a subclass

    def _configure(self, stream, console_env=None):
        env = {k: v for k, v in os.environ.items() if k != "TRAVELPLAN_LOG_CONSOLE"}
        if console_env is not None:
            env["TRAVELPLAN_LOG_CONSOLE"] = console_env
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stderr", stream):
            server_v2._configure_logging()

    def test_no_console_handler_when_stderr_is_not_a_terminal(self):
        self._configure(io.StringIO())
        self.assertEqual(self._stream_handlers(), [])
        self.assertTrue(any(isinstance(h, RotatingFileHandler) for h in logging.getLogger().handlers))

    def test_console_handler_on_a_terminal(self):
        stream = _Terminal()
        self._configure(stream)
        handlers = self._stream_handlers()
        self.assertEqual(len(handlers), 1)
        self.assertIs(handlers[0].stream, stream)

    def test_log_console_one_forces_the_console_without_a_terminal(self):
        stream = io.StringIO()
        self._configure(stream, console_env="1")
        handlers = self._stream_handlers()
        self.assertEqual(len(handlers), 1)
        self.assertIs(handlers[0].stream, stream)

    def test_other_log_console_values_do_not_force_the_console(self):
        with mock.patch.object(sys, "stderr", io.StringIO()):  # not a terminal, whatever runs the tests
            for value in ("0", "true", ""):
                with self.subTest(value=value), mock.patch.dict(os.environ, {"TRAVELPLAN_LOG_CONSOLE": value}):
                    self.assertFalse(server_v2._console_log_wanted())

    def test_missing_stderr_as_under_pythonw_has_no_console_handler(self):
        with mock.patch.object(sys, "stderr", None):
            with mock.patch.dict(os.environ, {"TRAVELPLAN_LOG_CONSOLE": "1"}):
                self.assertFalse(server_v2._console_log_wanted())
        self._configure(None, console_env="1")
        self.assertEqual(self._stream_handlers(), [])


if __name__ == "__main__":
    unittest.main()
