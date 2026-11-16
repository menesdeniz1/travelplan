"""Tests for provider stats (Google Flights and RailBus) and trip search trace logging (A04)."""

import json
import time
import unittest
from io import BytesIO
from unittest import mock

import server_v2
from server_v2 import TravelPlanAPIHandler
from travel_engine.airports import AirportDB
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider
from travel_engine.providers.rail_bus_provider import RailBusProvider
from travel_engine.route_engine import RouteEngine


class MockAPIHandler(TravelPlanAPIHandler):
    def __init__(self, request_bytes):
        self.client_address = ("127.0.0.1", 12345)
        self.rfile = BytesIO(request_bytes)
        self.wfile = BytesIO()
        self.raw_requestline = self.rfile.readline()
        self.parse_request()

    def log_message(self, format, *args):
        pass


def _get(path):
    handler = MockAPIHandler(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("utf-8"))
    handler.do_GET()
    raw = handler.wfile.getvalue()
    header_blob, body = raw.split(b"\r\n\r\n", 1)
    status = int(header_blob.decode("latin-1").split("\r\n")[0].split(" ")[1])
    return status, json.loads(body.decode("utf-8"))


class GoogleFlightsStatsTests(unittest.TestCase):
    def setUp(self):
        GoogleFlightsProvider.reset_shared_state()

    def tearDown(self):
        GoogleFlightsProvider.reset_shared_state()

    def test_l1_cache_hit_increments_l1_hits(self):
        provider = GoogleFlightsProvider()
        norm_orig, _ = provider._normalize_city("Istanbul")
        norm_dest, _ = provider._normalize_city("Prague")
        cache_key = f"{norm_orig}_{norm_dest}_2026-10-25_oneway"
        provider.cache[cache_key] = {
            "timestamp": time.time(),
            "results": [{"total_price_try": 1500.0}],
        }
        results = provider.search_flights("Istanbul", "Prague", "2026-10-25", trip_type="oneway")
        self.assertEqual(len(results), 1)

        stats = GoogleFlightsProvider.stats()
        self.assertEqual(stats["l1_hits"], 1)
        self.assertEqual(stats["page_loads"], 0)

    def test_l2_cache_hit_increments_l2_hits(self):
        fake_store = mock.Mock()
        fake_store.get_results.return_value = {
            "status": "ok",
            "results": [{"total_price_try": 2000.0}],
            "stored_at": time.time(),
        }
        provider = GoogleFlightsProvider(flight_cache=fake_store)
        results = provider.search_flights("Istanbul", "Prague", "2026-10-25", trip_type="oneway")
        self.assertEqual(len(results), 1)

        stats = GoogleFlightsProvider.stats()
        self.assertEqual(stats["l2_hits"], 1)
        self.assertEqual(stats["page_loads"], 0)

    def test_run_scrape_increments_page_loads_and_reset_clears(self):
        provider = GoogleFlightsProvider()
        fake_state = {
            "raw_results": [],
            "page_read": True,
            "page_url": "https://www.google.com/travel/flights",
            "body_text": "Sample body",
            "card_count": 0,
            "error_detail": "",
        }
        def fake_scrape(*a, **kw):
            time.sleep(0.01)
            return fake_state

        with mock.patch("travel_engine.providers.google_flights_provider.PLAYWRIGHT_AVAILABLE", True):
            with mock.patch.object(provider, "_run_scrape", side_effect=fake_scrape):
                provider.search_flights("Istanbul", "Prague", "2026-10-25", trip_type="oneway")

        stats = GoogleFlightsProvider.stats()
        self.assertEqual(stats["page_loads"], 1)
        self.assertGreaterEqual(stats["page_secs"], 0.0)

        GoogleFlightsProvider.reset_shared_state()
        stats_reset = GoogleFlightsProvider.stats()
        self.assertEqual(stats_reset["page_loads"], 0)
        self.assertEqual(stats_reset["page_secs"], 0.0)


class RailBusStatsTests(unittest.TestCase):
    def test_source_calls_and_cache_hits(self):
        stub_source = mock.Mock()
        stub_source.can_resolve.return_value = True
        stub_source.search.return_value = []
        provider = RailBusProvider(live_provider=stub_source, ground_cache=None)

        provider.find_ground("Vienna", "Prague", "2026-11-12")
        provider.find_ground("Vienna", "Prague", "2026-11-12")

        stats = provider.stats()
        self.assertEqual(stats["source_calls"]["regiojet"], 1)
        self.assertEqual(stats["cache_hits"], 1)


class RouteEngineStatsTraceTests(unittest.TestCase):
    def test_trace_counters_present_and_zero_without_stats_provider(self):
        dummy_flights = mock.Mock(spec=["search_flights"])
        dummy_flights.search_flights.return_value = []
        engine = RouteEngine(dummy_flights, None, airport_db=AirportDB.load_default())
        res = engine.search("Istanbul", "Prague", ["2026-10-25"])
        counters = res["trace"]["counters"]
        self.assertEqual(counters["google_page_loads"], 0)
        self.assertEqual(counters["google_page_secs"], 0.0)
        self.assertEqual(counters["google_cache_hits"], 0)
        self.assertEqual(counters["google_blocked"], 0)
        self.assertEqual(counters["ground_source_calls"], 0)
        self.assertEqual(counters["ground_cache_hits"], 0)
        self.assertEqual(counters["maps_searches"], 0)

    def test_trace_diff_with_stateful_provider(self):
        class StatefulFlights:
            def __init__(self):
                self.loads = 0
                self.secs = 0.0
                self.hits = 0

            def search_flights(self, *a, **kw):
                self.loads += 2
                self.secs += 1.5
                self.hits += 1
                return []

            def stats(self):
                return {
                    "page_loads": self.loads,
                    "page_secs": round(self.secs, 3),
                    "l1_hits": self.hits,
                    "l2_hits": 0,
                    "blocked": 0,
                    "failed": 0,
                }

        stateful = StatefulFlights()
        engine = RouteEngine(stateful, None, airport_db=AirportDB.load_default())
        res = engine.search("Istanbul", "Prague", ["2026-10-25"])
        counters = res["trace"]["counters"]
        self.assertEqual(counters["google_page_loads"], stateful.loads)
        self.assertGreater(counters["google_page_loads"], 0)
        self.assertEqual(counters["google_page_secs"], round(stateful.secs, 3))
        self.assertGreater(counters["google_page_secs"], 0.0)
        self.assertEqual(counters["google_cache_hits"], stateful.hits)
        self.assertGreater(counters["google_cache_hits"], 0)


class ServerTripSearchTraceLogTests(unittest.TestCase):
    def test_trip_search_trace_logged(self):
        with mock.patch.object(server_v2.trip_planner, "plan_trip", return_value={"options": [], "trace": {"total_secs": 0.5}}):
            with self.assertLogs(server_v2.logger, level="INFO") as captured:
                status, data = _get("/api/trip-search?origin=Istanbul&destination=Prague&depart_dates=2026-10-25")
        self.assertEqual(status, 200)
        trace_logs = [msg for msg in captured.output if "trip_search_trace" in msg]
        self.assertEqual(len(trace_logs), 1)
        payload = json.loads(trace_logs[0].split("trip_search_trace ", 1)[1])
        self.assertEqual(payload["origin"], "Istanbul")
        self.assertEqual(payload["destination"], "Prague")
        self.assertEqual(payload["depart_dates"], ["2026-10-25"])
        self.assertEqual(payload["trace"], {"total_secs": 0.5})


if __name__ == "__main__":
    unittest.main()
