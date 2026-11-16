"""Unknown prices, their ranking, and the "Sadece gerçek veriler" (real_data_only) rule of the route engine.

No network: fake flight and ground providers, the real costs module with the EUR/TRY rate patched to 50.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from travel_engine import costs as costs_module
from travel_engine.airports import AirportDB
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.providers.rail_bus_provider import DEFAULT_MAPS_BUDGET
from travel_engine.route_engine import (
    MAPS_BUDGET_WARNING,
    REAL_ONLY_EMPTY_WARNING,
    RouteEngine,
    _Leg,
    _Run,
)

DAY = "2026-10-28"
RATE = 50.0


def flight(airline, times, price, airports, *, direct=True, layover=""):
    """A dict in the GoogleFlightsProvider.search_flights shape."""
    return {
        "source": "test",
        "airline": airline,
        "times": times,
        "duration": "2 sa.",
        "airports": airports,
        "stops": "Aktarmasız" if direct else "1 aktarma",
        "stops_count": 0 if direct else 1,
        "is_direct": direct,
        "layover_info": layover,
        "total_price_try": float(price),
        "currency": "TRY",
        "trip_type": "Tek yön",
        "badge": None,
        "google_flights_url": "https://www.google.com/travel/flights?q=test",
    }


def segment(origin_station, dest_station, dep, mins, price, *, est=False, price_is_estimated=None,
            price_from=None, operator="RegioJet"):
    """A ground segment. price None = real timetable, unknown fare (the contract for unpriced real timetables)."""
    if price_is_estimated is None:
        price_is_estimated = est
    seg_ = ScheduledSegment(
        origin_station=origin_station, dest_station=dest_station, departure_time=dep,
        arrival_time="", duration_mins=mins, price_try=None if price is None else float(price),
        operator=operator, mode="train", date="", is_estimated=est, price_eur=None,
        price_is_estimated=price_is_estimated, source="table" if est else "live",
    )
    if price_from is not None:
        seg_.price_from_try = float(price_from)  # optional attribute of the shared contract
    return seg_


class FakeFlights:
    def __init__(self, table):
        self.table = table

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]


class FakeGround:
    """Timetable keyed by (from_place, to_place); "*" answers every pair without its own entry.

    Does NOT accept allow_estimate (a provider that cannot skip its estimate); see FakeGroundAllow."""

    def __init__(self, segments=None):
        self.segments = segments or {}
        self.calls = []

    def _lookup(self, from_place, to_place):
        return self.segments.get((from_place, to_place), self.segments.get("*", []))

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0):
        self.calls.append((from_place, to_place))
        out = []
        for item in self._lookup(from_place, to_place):
            if earliest_dep_time and item.departure_time < earliest_dep_time:
                continue
            out.append(item)
        return out[:max_results]

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if from_coord and to_coord:
            from travel_engine.airports import haversine_km
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.2
        return None


class FakeGroundAllow(FakeGround):
    """A ground provider that accepts allow_estimate and records what it was asked."""

    def __init__(self, segments=None):
        super().__init__(segments)
        self.allow_calls = []

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0, allow_estimate=True):
        self.allow_calls.append(allow_estimate)
        return super().find_ground(from_place, to_place, date_str, earliest_dep_time, from_coord,
                                   to_coord, max_results, max_road_km)


class FakeGroundMaps(FakeGround):
    """Takes one Maps page load from the search's budget per find_ground call, like RailBusProvider does."""

    def __init__(self, segments=None, limit=None):
        super().__init__(segments)
        if limit is not None:
            self.maps_budget_limit = limit
        self.refused = 0
        self.asked = []

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0, maps_budget=None):
        self.asked.append((from_place, to_place))
        if maps_budget is not None and not maps_budget.take():
            self.refused += 1
            return []  # the Maps page load was refused: this pair is not tried
        return super().find_ground(from_place, to_place, date_str, earliest_dep_time, from_coord,
                                   to_coord, max_results, max_road_km)


class ForwardingGround:
    """A wrapper around a ground provider that declares only *args/**kwargs and forwards them (like a timing shim)."""

    def __init__(self, inner):
        self.inner = inner
        self.maps_budget_limit = inner.maps_budget_limit

    def find_ground(self, *args, **kwargs):
        return self.inner.find_ground(*args, **kwargs)

    def __getattr__(self, name):  # every other method of the provider is the inner one's
        return getattr(self.inner, name)


class RealOnlyTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch("travel_engine.fx.get_eur_try_rate", return_value=(RATE, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = AirportDB.load_default()

    def engine(self, table, ground):
        return RouteEngine(FakeFlights(table), ground, airport_db=self.db, costs=costs_module)

    @staticmethod
    def ground_legs(option):
        return [leg for leg in option["legs"] if leg["type"] in ("train", "bus")]

    @staticmethod
    def transfer_legs(option):
        return [leg for leg in option["legs"] if leg["type"] == "transfer"]


class MapsBudgetRouteTests(RealOnlyTestCase):
    def test_search_warns_when_the_maps_budget_refused_a_lookup(self):
        ground = FakeGroundMaps({"*": [segment("X", "Y", "10:00", 60, 300)]}, limit=1)
        result = self.engine(MILAN_TABLE, ground).search("Milano", "Barcelona", [DAY], max_stops=2,
                                                         flight_search_budget=30)
        self.assertGreater(len(ground.asked), 1, "the search asks for more than one pair")
        self.assertGreater(ground.refused, 0)
        self.assertIn(MAPS_BUDGET_WARNING, result["warnings"])

    def test_no_warning_when_the_budget_covers_the_search(self):
        ground = FakeGroundMaps({"*": [segment("X", "Y", "10:00", 60, 300)]}, limit=1000)
        result = self.engine(MILAN_TABLE, ground).search("Milano", "Barcelona", [DAY], max_stops=2,
                                                         flight_search_budget=30)
        self.assertEqual(ground.refused, 0)
        self.assertNotIn(MAPS_BUDGET_WARNING, result["warnings"])

    def test_budget_limit_comes_from_the_provider_or_the_default(self):
        self.assertEqual(self.engine({}, FakeGroundMaps(limit=2))._new_maps_budget().limit, 2)
        self.assertEqual(self.engine({}, FakeGround())._new_maps_budget().limit, DEFAULT_MAPS_BUDGET)

    def test_warning_text_names_google_maps(self):
        self.assertIn("Google Haritalar", MAPS_BUDGET_WARNING)

    def test_budget_reaches_a_wrapped_ground_provider_that_forwards_kwargs(self):
        # A wrapper (timing, logging) declares only *args/**kwargs: the budget must still be passed on, or
        # every find_ground call would get a fresh budget and the per-search limit would not hold.
        inner = FakeGroundMaps({"*": [segment("X", "Y", "10:00", 60, 300)]}, limit=1)
        wrapped = ForwardingGround(inner)
        result = self.engine(MILAN_TABLE, wrapped).search("Milano", "Barcelona", [DAY], max_stops=2,
                                                          flight_search_budget=30)
        self.assertGreater(inner.refused, 0)
        self.assertIn(MAPS_BUDGET_WARNING, result["warnings"])

    def test_supports_kwarg_sees_keywords_taken_by_a_var_keyword_function(self):
        from travel_engine.route_engine import _supports_kwarg

        def forwarding(*args, **kwargs):
            return None

        def explicit(a, maps_budget=None):
            return None

        def other(a, b=None):
            return None

        self.assertTrue(_supports_kwarg(forwarding, "maps_budget"))
        self.assertTrue(_supports_kwarg(explicit, "maps_budget"))
        self.assertFalse(_supports_kwarg(other, "maps_budget"))


MILAN_TABLE = {
    ("Milano", "Barcelona", DAY): [flight("Iberia", "08:00 – 10:10", 9000, "MXP ➔ BCN")],
    ("BGY", "GRO", DAY): [flight("Ryanair", "07:00 – 08:20", 1200, "BGY ➔ GRO")],
}


class UnknownGroundPriceTests(RealOnlyTestCase):
    def test_unknown_price_stays_none_and_marks_the_total_as_a_lower_bound(self):
        ground = FakeGround({("Girona", "Barcelona"): [segment("Girona", "Barcelona-Sants", "10:00", 60, None,
                                                               price_from=250)]})
        result = self.engine(MILAN_TABLE, ground).search("Milano", "Barcelona", [DAY], max_stops=2,
                                                         flight_search_budget=30)
        side = [o for o in result["options"] if self.ground_legs(o)]
        self.assertTrue(side, "a side route with the unpriced train must be offered")
        option = side[0]
        train = self.ground_legs(option)[0]
        self.assertIsNone(train["price_try"])
        self.assertFalse(train["price_known"])
        self.assertEqual(train["price_from_try"], 250.0)
        self.assertIs(train["price_is_estimated"], False)
        self.assertFalse(option["price_complete"])
        self.assertEqual(option["unknown_price_legs"], 1)
        self.assertEqual(option["total_lower_bound_try"], option["true_total_try"])
        known_legs = sum(leg["price_try"] for leg in option["legs"]
                         if leg["price_try"] is not None and leg["type"] != "transfer")
        self.assertEqual(option["total_try"], round(known_legs + sum(
            leg["price_try"] for leg in self.transfer_legs(option) if leg["price_try"] is not None), 2))
        # The ranking value is never a shown price: it lives in rank_total_try and the score only
        self.assertEqual(option["rank_total_try"], round(option["true_total_try"] + 250.0, 2))
        self.assertNotIn(250.0, [leg["price_try"] for leg in option["legs"]])

    def test_unknown_price_without_price_from_ranks_by_the_known_ground_median(self):
        # (the first train must leave after the 08:20 landing plus the transfer and buffer)
        ground = FakeGround({("Girona", "Barcelona"): [
            segment("Girona", "Barcelona-Sants", "12:00", 60, None),
            segment("Girona", "Barcelona-Sants", "14:00", 60, 100),
            segment("Girona", "Barcelona-Sants", "16:00", 60, 300),
        ]})
        result = self.engine(MILAN_TABLE, ground).search("Milano", "Barcelona", [DAY], max_stops=2,
                                                         flight_search_budget=30)
        unknown = [o for o in result["options"] if self.ground_legs(o) and not o["price_complete"]]
        self.assertTrue(unknown)
        option = unknown[0]
        self.assertIsNone(self.ground_legs(option)[0]["price_try"])
        # Median of the known 60-minute ground prices (100, 300) is 200: a ranking value only
        self.assertEqual(option["rank_total_try"], round(option["true_total_try"] + 200.0, 2))

    def test_known_option_is_unchanged_and_complete(self):
        ground = FakeGround({("Girona", "Barcelona"): [segment("Girona", "Barcelona-Sants", "10:00", 60, 300)]})
        result = self.engine(MILAN_TABLE, ground).search("Milano", "Barcelona", [DAY], max_stops=2,
                                                         flight_search_budget=30)
        option = next(o for o in result["options"] if self.ground_legs(o))
        self.assertTrue(option["price_complete"])
        self.assertEqual(option["unknown_price_legs"], 0)
        self.assertEqual(option["unknown_price_parts"], [])
        self.assertEqual(option["rank_total_try"], option["true_total_try"])

    def test_rank_penalty_medians_per_band_then_overall(self):
        """The ranking value of an unknown leg: its band's known median, else the overall median, else 0."""
        run = _Run(engine=None, origin="a", destination="b", dates=[DAY], arrive_by=None, depart_after=None,
                   max_stops=0, via_tokens=None, budget=1, time_value=150.0, min_stay={}, cabin_bag=False,
                   checked_bag=False, costs=costs_module, db=self.db, ground=None)
        t0 = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)

        def leg(mins, price):
            return _Leg("train", "x", "a", "b", t0, t0 + timedelta(minutes=mins), price, False, "", True)

        run.ground_cache[("k1",)] = [segment("a", "b", "10:00", 60, 100), segment("a", "b", "10:00", 60, 300)]
        run.ground_cache[("k2",)] = [segment("a", "b", "10:00", 300, 1000), segment("a", "b", "10:00", 60, None)]
        self.assertEqual(run.rank_penalty(leg(60, None)), 200.0)   # band 1: median(100, 300)
        self.assertEqual(run.rank_penalty(leg(300, None)), 1000.0)  # band 5: only 1000
        self.assertEqual(run.rank_penalty(leg(600, None)), 300.0)   # no band: median(100, 300, 1000)
        self.assertEqual(run.rank_penalty(leg(60, 42.0)), 0.0)      # a known price adds nothing
        empty = _Run(engine=None, origin="a", destination="b", dates=[DAY], arrive_by=None, depart_after=None,
                     max_stops=0, via_tokens=None, budget=1, time_value=150.0, min_stay={}, cabin_bag=False,
                     checked_bag=False, costs=costs_module, db=self.db, ground=None)
        self.assertEqual(empty.rank_penalty(leg(60, None)), 0.0)    # nothing known in the search at all

    def test_zero_placeholder_price_of_an_estimate_is_unknown(self):
        ground = FakeGround({("Girona", "Barcelona"): [
            segment("Girona", "Barcelona-Sants", "10:00", 60, 0, price_is_estimated=True, est=False)]})
        result = self.engine(MILAN_TABLE, ground).search("Milano", "Barcelona", [DAY], max_stops=2,
                                                         flight_search_budget=30)
        option = next(o for o in result["options"] if self.ground_legs(o))
        self.assertIsNone(self.ground_legs(option)[0]["price_try"])


class RealOnlyRuleTests(RealOnlyTestCase):
    def test_estimated_ground_segments_are_dropped_and_the_estimate_is_not_requested(self):
        ground = FakeGround({("Girona", "Barcelona"): [segment("Girona", "Barcelona-Sants", "10:00", 60, 300, est=True)]})
        off = self.engine(MILAN_TABLE, ground).search("Milano", "Barcelona", [DAY], max_stops=2,
                                                      flight_search_budget=30)
        self.assertTrue(any(leg["is_estimated"] for o in off["options"] for leg in self.ground_legs(o)),
                        "without the rule the estimated train is offered, labelled")
        on = self.engine(MILAN_TABLE, FakeGround({("Girona", "Barcelona"): [
            segment("Girona", "Barcelona-Sants", "10:00", 60, 300, est=True)]})).search(
            "Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=30, real_data_only=True)
        self.assertTrue(on["query"]["real_data_only"])
        for option in on["options"]:
            self.assertEqual(self.ground_legs(option), [])
            self.assertFalse(any(leg["is_estimated"] for leg in option["legs"] if leg["type"] != "transfer"))
            self.assertEqual(option["cost_breakdown"]["estimated_parts"], [])

    def test_allow_estimate_is_passed_only_when_the_provider_takes_it(self):
        ground = FakeGroundAllow({("Girona", "Barcelona"): [segment("Girona", "Barcelona-Sants", "10:00", 60, 300)]})
        self.engine(MILAN_TABLE, ground).search("Milano", "Barcelona", [DAY], max_stops=2,
                                                flight_search_budget=30, real_data_only=True)
        self.assertTrue(ground.allow_calls)
        self.assertTrue(all(value is False for value in ground.allow_calls))

        plain = FakeGroundAllow({("Girona", "Barcelona"): [segment("Girona", "Barcelona-Sants", "10:00", 60, 300)]})
        self.engine(MILAN_TABLE, plain).search("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=30)
        self.assertTrue(plain.allow_calls)
        self.assertTrue(all(value is True for value in plain.allow_calls))  # not passed: the provider default applies

    def test_provider_without_the_argument_still_works_and_is_filtered(self):
        ground = FakeGround({"*": [segment("X", "Y", "10:00", 60, 300, est=True)]})
        result = self.engine({}, ground).search("Girona", "Barcelona", [DAY], max_stops=0,
                                                flight_search_budget=3, real_data_only=True)
        self.assertEqual(result["options"], [])
        self.assertIn(REAL_ONLY_EMPTY_WARNING, result["warnings"])

    def test_everything_filtered_gives_the_empty_warning_and_the_estimated_route_is_offered_without_the_rule(self):
        def ground():
            return FakeGround({"*": [segment("X", "Y", "10:00", 60, 300, est=True)]})

        off = self.engine({}, ground()).search("Girona", "Barcelona", [DAY], max_stops=0, flight_search_budget=3)
        self.assertTrue(off["options"], "the estimated ground route appears when the rule is off")
        self.assertNotIn(REAL_ONLY_EMPTY_WARNING, off["warnings"])
        on = self.engine({}, ground()).search("Girona", "Barcelona", [DAY], max_stops=0, flight_search_budget=3,
                                              real_data_only=True)
        self.assertEqual(on["options"], [])
        self.assertIsNone(on["best"])
        self.assertIn(REAL_ONLY_EMPTY_WARNING, on["warnings"])

    def test_transfer_price_is_unknown_not_dropped_under_the_rule(self):
        result = self.engine({("Milano", "Barcelona", DAY): [flight("Iberia", "08:00 – 10:10", 9000, "MXP ➔ BCN")]},
                             FakeGround()).search("Milano", "Barcelona", [DAY], max_stops=0, flight_search_budget=3,
                                                  real_data_only=True)
        direct = [o for o in result["options"] if o["kind"] == "direct"]
        self.assertTrue(direct, "the flight stays: its transfer price is unknown, not an estimate")
        option = direct[0]
        self.assertTrue(self.transfer_legs(option))
        for leg in self.transfer_legs(option):
            self.assertIsNone(leg["price_try"])
            self.assertFalse(leg["price_known"])
        self.assertFalse(option["price_complete"])
        self.assertIn("transfers", option["unknown_price_parts"])
        self.assertEqual(option["cost_breakdown"]["transfers"], 0.0)
        self.assertEqual(option["cost_breakdown"]["estimated_parts"], [])
        self.assertEqual(option["true_total_try"], option["cost_breakdown"]["total"])

    def test_transfer_price_is_known_and_labelled_without_the_rule(self):
        result = self.engine({("Milano", "Barcelona", DAY): [flight("Iberia", "08:00 – 10:10", 9000, "MXP ➔ BCN")]},
                             FakeGround()).search("Milano", "Barcelona", [DAY], max_stops=0, flight_search_budget=3)
        option = next(o for o in result["options"] if o["kind"] == "direct")
        for leg in self.transfer_legs(option):
            self.assertIsNotNone(leg["price_try"])
            self.assertTrue(leg["is_estimated"])
        self.assertIn("transfers", option["cost_breakdown"]["estimated_parts"])

    def test_overnight_hotel_excludes_the_option_under_the_rule(self):
        table = {("BGY", "GRO", DAY): [flight("Ryanair", "20:00 – 21:10", 1200, "BGY ➔ GRO")]}
        ground = {("Girona", "Barcelona"): [segment("Girona", "Barcelona-Sants", "06:30", 60, 300)]}
        off = self.engine(table, FakeGround(ground)).search("Milano", "Barcelona", [DAY], max_stops=1,
                                                            flight_search_budget=30)
        self.assertTrue(any(o["night_wait"] for o in off["options"]))
        on = self.engine(table, FakeGround(ground)).search("Milano", "Barcelona", [DAY], max_stops=1,
                                                           flight_search_budget=30, real_data_only=True)
        self.assertFalse(any(o["night_wait"] for o in on["options"]))

    def test_real_only_result_is_json_serialisable_with_nulls(self):
        ground = FakeGround({("Girona", "Barcelona"): [segment("Girona", "Barcelona-Sants", "10:00", 60, None)]})
        result = self.engine(MILAN_TABLE, ground).search("Milano", "Barcelona", [DAY], max_stops=2,
                                                         flight_search_budget=30, real_data_only=True)
        json.dumps(result, default=str)  # no NaN / inf, nulls allowed
        for option in result["options"]:
            self.assertIn("price_complete", option)
            self.assertIn("rank_total_try", option)


if __name__ == "__main__":
    unittest.main()
