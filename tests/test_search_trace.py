"""Tests for search trace (OLC-1a): SearchTrace, engine trace, planner trace."""
import unittest

from travel_engine.airports import AirportDB
from travel_engine.route_engine import RouteEngine
from travel_engine.trace import SearchTrace
from travel_engine.trip_planner import TripPlanner


class SearchTraceTests(unittest.TestCase):
    def test_phases_total_and_nesting(self):
        # sahte saat iter([0.0, 1.0, 3.0, 5.0, 6.0, 10.0]) ile SearchTrace(clock=lambda: next(ticks));
        # phase("a") iki kez girilip çıkılır; to_dict() tam olarak {"total_secs": 10.0, "phases": {"a": 3.0}, "counters": {}}.
        ticks = iter([0.0, 1.0, 3.0, 5.0, 6.0, 10.0])
        trace = SearchTrace(clock=lambda: next(ticks))
        with trace.phase("a"):
            pass
        with trace.phase("a"):
            pass
        self.assertEqual(trace.to_dict(), {"total_secs": 10.0, "phases": {"a": 3.0}, "counters": {}})

    def test_add_and_set_counters(self):
        # add("x"), add("x", 4), set("y", True) → counters == {"x": 5, "y": True}.
        trace = SearchTrace()
        trace.add("x")
        trace.add("x", 4)
        trace.set("y", True)
        self.assertEqual(trace.counters, {"x": 5, "y": True})

    def test_exception_in_phase_records_elapsed_and_raises(self):
        # blok içinde istisna atılsa da süre kaydedilir ve istisna dışarı çıkar (assertRaises).
        ticks = iter([0.0, 1.0, 4.0, 5.0])
        trace = SearchTrace(clock=lambda: next(ticks))
        with self.assertRaises(ValueError):
            with trace.phase("err"):
                raise ValueError("boom")
        self.assertEqual(trace.phases.get("err"), 3.0)


class _SingleFlightProvider:
    def __init__(self):
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [{
            "airline": "Aegean",
            "times": "10:00 – 12:30",
            "duration": "2 sa. 30 dk.",
            "airports": "ATH ➔ PRG",
            "stops": "Aktarmasız",
            "stops_count": 0,
            "is_direct": True,
            "layover_info": "",
            "total_price_try": 3500.0,
            "google_flights_url": "https://test",
        }]


class EngineTraceTests(unittest.TestCase):
    def test_engine_search_includes_trace(self):
        flights = _SingleFlightProvider()
        engine = RouteEngine(flights, None, airport_db=AirportDB.load_default(), max_parallel_searches=1)
        result = engine.search("Atina", "Prag", ["2026-11-12"], max_stops=0, flight_search_budget=5)

        self.assertIn("trace", result)
        trace = result["trace"]
        self.assertIsInstance(trace, dict)
        phases = trace.get("phases", {})
        for expected_phase in ("setup", "flight_plan", "build_options", "result", "dfs", "options", "flight_fetch"):
            self.assertIn(expected_phase, phases)
            self.assertGreaterEqual(phases[expected_phase], 0.0)

        counters = trace.get("counters", {})
        expected_counters = (
            "flight_searches", "pruned_searches", "budget_exhausted", "truncated",
            "maps_page_loads", "hotel_page_loads", "dfs_steps", "complete_chains",
            "options_built", "best_score_recomputes", "ground_queries", "ground_query_cache_hits"
        )
        for expected_counter in expected_counters:
            self.assertIn(expected_counter, counters)

        self.assertEqual(counters["flight_searches"], result["flight_searches"])
        self.assertGreaterEqual(counters["options_built"], 1)


class _FakeEngineWithTrace:
    def search(self, origin, destination, depart_dates, **kwargs):
        return {
            "query": {"resolved": {"origin": [origin], "destination": [destination], "via": []}},
            "options": [],
            "best": None,
            "cheapest": None,
            "fastest": None,
            "flight_searches": 0,
            "budget_exhausted": False,
            "candidates": {},
            "warnings": [],
            "trace": {"total_secs": 1.5, "phases": {}, "counters": {}},
        }


class _StubFlightProvider:
    def search_flights(self, *args, **kwargs):
        return []


class PlannerTraceTests(unittest.TestCase):
    def test_planner_passes_through_trace(self):
        planner = TripPlanner(_StubFlightProvider(), None, route_engine=_FakeEngineWithTrace())
        result = planner.plan_trip("ATH", "PRG", ["2026-11-12"])
        self.assertEqual(result.get("trace"), {"total_secs": 1.5, "phases": {}, "counters": {}})


if __name__ == "__main__":
    unittest.main()
