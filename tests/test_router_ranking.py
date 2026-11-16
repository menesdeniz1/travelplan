import inspect
import unittest
from collections import Counter

from travel_engine.geo_clusters import GeoClusterRegistry
from travel_engine.multimodal_router import MultiModalRouter, extract_flight_day_offset
from travel_engine.providers.rail_bus_provider import RailBusProvider

DATE = "2026-10-28"


class StubFlightProvider:
    """Returns canned flights per destination code (no network). Flights use the Google "times" format."""

    def __init__(self, flights_by_dest):
        self.flights_by_dest = {k.upper(): v for k, v in flights_by_dest.items()}
        self.calls = []

    def search_flights(self, origin, destination, departure_date, return_date=None, trip_type="oneway"):
        self.calls.append(destination)
        key = destination.strip().upper()
        if key == "PRAGUE":
            key = "PRG"
        return [dict(f) for f in self.flights_by_dest.get(key, [])]


def flight(airline, times, price):
    return {"airline": airline, "flight_number": "", "times": times, "total_price_try": price}


PRG_DIRECT = [{"airline": "KLM", "flight_number": "", "times": "13:45 – 19:30", "total_price_try": 16286.0}]
WIZZ_BTS = flight("Wizz Air", "23:50 – 01:05+1", 4447.0)
RYANAIR_BTS = flight("Ryanair", "07:55 – 09:10", 5524.0)

# The real live scenario from ROADMAP section 2 (Athens -> Prague, 2026-10-28)
REAL_SCENARIO = {"PRG": PRG_DIRECT, "BTS": [WIZZ_BTS, RYANAIR_BTS]}


def _search(flights_by_dest, **kwargs):
    registry = GeoClusterRegistry()
    router = MultiModalRouter(
        flight_provider=StubFlightProvider(flights_by_dest),
        rail_provider=RailBusProvider(registry),
        geo_registry=registry,
    )
    return router.search_multimodal(origin="Athens", destination="Prague", date=DATE, **kwargs)


def _is_wizz(route):
    return route["segments"][0]["operator"] == "Wizz Air"


def _is_ryanair(route):
    return route["segments"][0]["operator"] == "Ryanair"


class TestFlightDayOffset(unittest.TestCase):
    def test_plus_one_suffix_is_next_day(self):
        self.assertEqual(extract_flight_day_offset({"times": "23:50 – 01:05+1"}), 1)

    def test_no_suffix_is_same_day(self):
        self.assertEqual(extract_flight_day_offset({"times": "07:55 – 09:10"}), 0)

    def test_missing_or_unparsable_times_is_zero(self):
        self.assertEqual(extract_flight_day_offset({}), 0)
        self.assertEqual(extract_flight_day_offset({"times": "Bilinmiyor"}), 0)


class TestRouterRankingRealScenario(unittest.TestCase):
    def setUp(self):
        self.result = _search(REAL_SCENARIO)
        self.routes = self.result["multimodal_routes"]

    def test_scenario_has_routes(self):
        self.assertTrue(self.result["has_multimodal"])
        self.assertEqual(self.result["cheapest_direct_price"], 16286.0)

    def test_first_journey_is_the_ryanair_morning_flight(self):
        first = self.routes[0]
        self.assertTrue(_is_ryanair(first))
        self.assertEqual(first["segments"][0]["departure_time"], "07:55")
        # The earliest viable train after 09:10 landing + 90 min buffer is 12:00
        self.assertEqual(first["segments"][2]["departure_time"], "12:00")

    def test_ryanair_ranks_above_every_wizz_journey(self):
        first_wizz = next(i for i, r in enumerate(self.routes) if _is_wizz(r))
        last_ryanair = max(i for i, r in enumerate(self.routes) if _is_ryanair(r))
        self.assertLess(last_ryanair, first_wizz)

    def test_wizz_journey_is_night_wait_next_day_with_hotel(self):
        wizz_0600 = [r for r in self.routes if _is_wizz(r) and r["segments"][2]["departure_time"] == "06:00"]
        self.assertEqual(len(wizz_0600), 1)
        route = wizz_0600[0]
        self.assertTrue(route["night_wait"])
        self.assertGreaterEqual(route["night_wait_mins"], 180)
        self.assertEqual(route["night_wait_mins"], 295)  # 01:05 -> 06:00
        self.assertEqual(route["arrival_day_offset"], 1)
        self.assertGreater(route["estimated_hotel_try"], 0)
        self.assertEqual(route["estimated_hotel_try"], 2200.0)  # Bratislava catchment estimate
        self.assertTrue(route["hotel_is_estimated"])
        self.assertTrue(route["segments"][1]["is_night_wait"])
        self.assertAlmostEqual(route["true_total_try"], route["total_price_try"] + route["estimated_hotel_try"])
        self.assertAlmostEqual(route["savings_try"], 16286.0 - route["true_total_try"])

    def test_ryanair_journey_has_no_night_wait(self):
        for route in (r for r in self.routes if _is_ryanair(r)):
            self.assertFalse(route["night_wait"])
            self.assertEqual(route["night_wait_mins"], 0)
            self.assertIsNone(route["estimated_hotel_try"])
            self.assertFalse(route["hotel_is_estimated"])
            self.assertFalse(route["segments"][1]["is_night_wait"])
            self.assertEqual(route["arrival_day_offset"], 0)
            self.assertAlmostEqual(route["true_total_try"], route["total_price_try"])

    def test_score_is_true_total_plus_time_value(self):
        ryanair = self.routes[0]
        expected = ryanair["true_total_try"] + (ryanair["total_duration_mins"] / 60.0) * 150.0
        self.assertAlmostEqual(ryanair["score"], expected)
        # 6174 TRY price + 505 min at 150 TRY/h
        self.assertAlmostEqual(ryanair["score"], 6174.0 + (505 / 60.0) * 150.0)

    def test_badges_on_real_scenario(self):
        ryanair = self.routes[0]
        self.assertIn("En Ucuz Hibrit", ryanair["badges"])  # lowest true total (6174)
        self.assertIn("En Hızlı Hibrit", ryanair["badges"])  # shortest duration
        self.assertIn("En İyi Değer", ryanair["badges"])  # best score with positive savings
        wizz = next(r for r in self.routes if _is_wizz(r))
        self.assertNotIn("En Ucuz Hibrit", wizz["badges"])
        self.assertNotIn("En İyi Değer", wizz["badges"])

    def test_no_flight_exceeds_default_max_variants(self):
        counts = Counter((r["segments"][0]["operator"], r["segments"][0]["departure_time"]) for r in self.routes)
        self.assertTrue(all(c <= 2 for c in counts.values()), counts)
        self.assertEqual(counts[("Wizz Air", "23:50")], 2)
        self.assertEqual(counts[("Ryanair", "07:55")], 2)

    def test_variants_keep_earliest_arrival(self):
        ryanair = sorted(
            (r for r in self.routes if _is_ryanair(r)),
            key=lambda r: r["segments"][2]["departure_time"],
        )
        self.assertEqual([r["segments"][2]["departure_time"] for r in ryanair], ["12:00", "14:00"])

    def test_max_variants_one_keeps_one_per_flight(self):
        result = _search(REAL_SCENARIO, max_variants_per_flight=1)
        counts = Counter((r["segments"][0]["operator"], r["segments"][0]["departure_time"]) for r in result["multimodal_routes"])
        self.assertEqual(dict(counts), {("Wizz Air", "23:50"): 1, ("Ryanair", "07:55"): 1})

    def test_max_variants_three_keeps_three_per_flight(self):
        result = _search(REAL_SCENARIO, max_variants_per_flight=3)
        counts = Counter((r["segments"][0]["operator"], r["segments"][0]["departure_time"]) for r in result["multimodal_routes"])
        self.assertEqual(counts[("Wizz Air", "23:50")], 3)
        self.assertEqual(counts[("Ryanair", "07:55")], 3)

    def test_arrival_deadline_excludes_next_day_arrivals(self):
        # Wizz lands 29 Oct; it must not pass a same-day arrival deadline. Ryanair (arrives 16:20) does.
        result = _search(REAL_SCENARIO, arrival_deadline="17:00")
        routes = result["multimodal_routes"]
        self.assertFalse(any(_is_wizz(r) for r in routes))
        self.assertTrue(any(_is_ryanair(r) and r["segments"][2]["departure_time"] == "12:00" for r in routes))


class TestRouterRankingKnobs(unittest.TestCase):
    def test_default_kwargs(self):
        params = inspect.signature(MultiModalRouter.search_multimodal).parameters
        self.assertEqual(params["time_value_try_per_hour"].default, 150.0)
        self.assertEqual(params["max_variants_per_flight"].default, 2)

    def test_time_value_zero_ranks_pure_price(self):
        # Austrian 13:00-15:15 into VIE: cheapest true total (4400 TRY) but a 555 min journey
        stub = {**REAL_SCENARIO, "VIE": [flight("Austrian", "13:00 – 15:15", 3650.0)]}
        routes = _search(stub, time_value_try_per_hour=0)["multimodal_routes"]
        cheapest = min(r["true_total_try"] for r in routes)
        self.assertEqual(routes[0]["true_total_try"], cheapest)
        self.assertEqual(routes[0]["segments"][0]["operator"], "Austrian")
        self.assertEqual(routes[0]["segments"][2]["departure_time"], "18:00")
        for r in routes:
            if not r["night_wait"]:
                self.assertAlmostEqual(r["score"], r["true_total_try"])

    def test_high_time_value_ranks_shortest_journey_first(self):
        stub = {**REAL_SCENARIO, "VIE": [flight("Austrian", "13:00 – 15:15", 3650.0)]}
        routes = _search(stub, time_value_try_per_hour=10000)["multimodal_routes"]
        # Ryanair 12:00 train is 505 min; the Vienna option is 555 min
        self.assertTrue(_is_ryanair(routes[0]))
        self.assertEqual(routes[0]["segments"][2]["departure_time"], "12:00")

    def test_default_time_value_keeps_cheaper_vienna_first(self):
        stub = {**REAL_SCENARIO, "VIE": [flight("Austrian", "13:00 – 15:15", 3650.0)]}
        routes = _search(stub)["multimodal_routes"]
        self.assertEqual(routes[0]["segments"][0]["operator"], "Austrian")


class TestLateLandingAndPricePriority(unittest.TestCase):
    def test_late_same_day_landing_connects_to_next_morning_train(self):
        # Real 2026-10-28 option: Ryanair ATH->VIE 19:55-21:20, too late for the last Vienna->Prague train
        res = _search({"PRG": PRG_DIRECT, "VIE": [flight("Ryanair", "19:55 – 21:20", 13281.0)]})
        vie = [r for r in res["multimodal_routes"] if r["segments"][0]["to"] == "VIE"]
        self.assertTrue(vie, "late-evening landing must not vanish")
        first = vie[0]
        self.assertTrue(first["night_wait"])
        self.assertEqual(first["arrival_day_offset"], 1)
        self.assertTrue(first["segments"][1]["is_night_wait"])
        self.assertGreater(first["estimated_hotel_try"], 0)

    def test_time_value_zero_ranks_cheaper_night_route_first(self):
        cheap_wizz = flight("Wizz Air", "23:50 – 01:05+1", 1000.0)
        res = _search({"PRG": PRG_DIRECT, "BTS": [cheap_wizz, RYANAIR_BTS]}, time_value_try_per_hour=0)
        routes = res["multimodal_routes"]
        self.assertTrue(_is_wizz(routes[0]))
        self.assertEqual(routes[0]["score"], routes[0]["true_total_try"])

    def test_default_time_value_still_prefers_day_route_over_slightly_cheaper_night(self):
        res = _search(REAL_SCENARIO)
        self.assertTrue(_is_ryanair(res["multimodal_routes"][0]))


class TestRouterBudapestAndMunich(unittest.TestCase):
    def test_budapest_routes_appear_when_bud_flight_returned(self):
        stub = {"PRG": PRG_DIRECT, "BUD": [flight("Wizz Air", "09:55 – 11:05", 8894.0)]}
        routes = _search(stub)["multimodal_routes"]
        bud = [r for r in routes if r["segments"][0]["to"] == "BUD"]
        self.assertTrue(bud)
        for r in bud:
            self.assertEqual(r["destination_city"], "Prague")
            self.assertEqual(r["segments"][2]["from"], "Budapest Kelenföld")
            self.assertEqual(r["segments"][2]["to"], "Praha hl.n.")
            self.assertEqual(r["segments"][2]["type"], "train")
            self.assertEqual(r["segments"][2]["price_try"], 1100.0)
            self.assertTrue(r["segments"][2]["is_estimated"])
            self.assertFalse(r["night_wait"])
            self.assertEqual(r["map_points"]["catchment"]["code"], "BUD")
        # Arrives 11:05 + 90 min buffer -> first train 14:00
        self.assertEqual(min(r["segments"][2]["departure_time"] for r in bud), "14:00")
        self.assertEqual(bud[0]["total_price_try"], 8894.0 + 1100.0)

    def test_munich_bus_routes_appear_when_muc_flight_returned(self):
        stub = {"PRG": PRG_DIRECT, "MUC": [flight("Lufthansa", "06:30 – 08:00", 6000.0)]}
        routes = _search(stub)["multimodal_routes"]
        muc = [r for r in routes if r["segments"][0]["to"] == "MUC"]
        self.assertTrue(muc)
        for r in muc:
            self.assertEqual(r["segments"][2]["type"], "bus")
            self.assertEqual(r["segments"][2]["operator"], "FlixBus")
            self.assertEqual(r["segments"][2]["from"], "München ZOB")
            self.assertEqual(r["segments"][2]["to"], "Praha hl.n.")
            self.assertEqual(r["map_points"]["catchment"]["code"], "MUC")
        # Arrives 08:00 + 90 min buffer -> first bus 11:00
        self.assertEqual(min(r["segments"][2]["departure_time"] for r in muc), "11:00")


if __name__ == "__main__":
    unittest.main()
