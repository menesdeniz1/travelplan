"""Tests for travel_engine.route_engine. No network: fake flight/ground providers and costs, real AirportDB."""

import json
import unittest
from dataclasses import replace
from datetime import datetime

from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import (
    BADGE_BEST,
    BADGE_CHEAPEST,
    BADGE_FASTEST,
    BUDGET_WARNING,
    LCC_HUBS,
    RouteEngine,
)

DAY = "2026-10-28"


# ---------------------------------------------------------------- fakes

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


def seg(origin_station, dest_station, dep, mins, price, operator="RegioJet", mode="train", est=False):
    return ScheduledSegment(
        origin_station=origin_station, dest_station=dest_station, departure_time=dep,
        arrival_time="", duration_mins=mins, price_try=float(price), operator=operator, mode=mode,
        date="", is_estimated=est, price_eur=None, price_is_estimated=est, source="table",
    )


class FakeFlights:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]


class FakeGround:
    """Timetable keyed by (from_place, to_place) using the ground names the engine passes."""

    def __init__(self, segments=None):
        self.segments = segments or {}
        self.calls = []

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0):
        self.calls.append((from_place, to_place, date_str, earliest_dep_time))
        out = []
        for item in self.segments.get((from_place, to_place), []):
            if earliest_dep_time and item.departure_time < earliest_dep_time:
                continue
            out.append(replace(item, date=date_str))
        return out[:max_results]

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.2
        return None


class FakeCosts:
    """Deterministic: transfers are free, hotel 2000 TRY per night, separate tickets are 'medium' risk."""

    def __init__(self):
        self.breakdown_calls = []

    def airport_transfer(self, iata, at_time=None):
        return {"duration_mins": 30, "price_try": 0.0, "mode": "bus", "is_estimated": False, "note": ""}

    def cost_breakdown(self, legs, hotel_nights=0, hotel_place=None, cabin_bag=True, checked_bag=False):
        self.breakdown_calls.append({"hotel_nights": hotel_nights, "hotel_place": hotel_place})
        tickets = sum(leg["price_try"] for leg in legs if leg["type"] != "transfer")
        transfers = sum(leg["price_try"] for leg in legs if leg["type"] == "transfer")
        hotel = 2000.0 * hotel_nights
        return {"tickets": tickets, "baggage": 0.0, "transfers": transfers, "hotel": hotel,
                "total": tickets + transfers + hotel, "estimated_parts": []}

    def journey_risk(self, legs, separate_tickets):
        return {"level": "medium" if separate_tickets else "low", "score": 0, "reasons": [],
                "connections": 0}


def make_engine(table, segments=None, costs=None):
    flights = FakeFlights(table)
    ground = FakeGround(segments)
    costs = costs or FakeCosts()
    engine = RouteEngine(flights, ground, airport_db=AirportDB.load_default(), costs=costs)
    return engine, flights, ground, costs


def ts(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M")


def minutes_between(start, end):
    return int((ts(end) - ts(start)).total_seconds() // 60)


# ---------------------------------------------------------------- Milano -> Barcelona

class MilanBarcelonaTests(unittest.TestCase):
    TABLE = {
        ("Milano", "Barcelona", DAY): [flight("Iberia", "08:00 – 10:10", 9000, "MXP ➔ BCN")],
        ("BGY", "GRO", DAY): [flight("Ryanair", "07:00 – 08:20", 1200, "BGY ➔ GRO")],
    }
    SEGMENTS = {
        ("Girona", "Barcelona"): [seg("Girona", "Barcelona-Sants", "10:00", 60, 300)],
    }

    def test_side_route_beats_expensive_direct_with_buffers(self):
        engine, flights, _, _ = make_engine(self.TABLE, self.SEGMENTS)
        result = engine.search("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=30)
        best = result["best"]
        self.assertIsNotNone(best)
        self.assertEqual(best["kind"], "side_route")
        self.assertEqual([leg["type"] for leg in best["legs"]], ["transfer", "flight", "transfer", "train"])
        transfer_in, fly, transfer_out, train = best["legs"]
        self.assertEqual(transfer_in["to"], "BGY")
        self.assertEqual((fly["from"], fly["to"]), ("BGY", "GRO"))
        self.assertEqual(train["from"], "Girona")
        self.assertGreaterEqual(minutes_between(transfer_in["arrive_at"], fly["depart_at"]), 120)
        self.assertGreaterEqual(minutes_between(transfer_out["arrive_at"], train["depart_at"]), 30)
        self.assertTrue(best["separate_tickets"])
        self.assertEqual(best["stops"], ["GRO"])
        self.assertIn("direct", [o["kind"] for o in result["options"]])
        self.assertIn("BGY", result["candidates"]["origin_side"])
        self.assertIn("GRO", result["candidates"]["dest_side"])
        self.assertLessEqual(result["flight_searches"], 30)
        self.assertFalse(result["budget_exhausted"])

    def test_budget_is_never_exceeded_and_is_reported(self):
        engine, flights, _, _ = make_engine(self.TABLE, self.SEGMENTS)
        result = engine.search("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=3)
        self.assertLessEqual(len(flights.calls), 3)
        self.assertEqual(result["flight_searches"], len(flights.calls))
        self.assertTrue(result["budget_exhausted"])
        self.assertIn(BUDGET_WARNING, result["warnings"])

    def test_badges_and_best_cheapest_fastest(self):
        engine, _, _, _ = make_engine(self.TABLE, self.SEGMENTS)
        result = engine.search("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=30)
        options = result["options"]
        self.assertIs(result["best"], options[0])
        self.assertIn(BADGE_BEST, options[0]["badges"])
        cheapest = min(options, key=lambda o: o["true_total_try"])
        self.assertIn(BADGE_CHEAPEST, cheapest["badges"])
        fastest = min(options, key=lambda o: o["duration_mins"])
        self.assertIn(BADGE_FASTEST, fastest["badges"])
        self.assertEqual(result["fastest"]["duration_mins"], min(o["duration_mins"] for o in options))
        self.assertEqual(result["cheapest"]["true_total_try"], min(o["true_total_try"] for o in options))

    def test_determinism(self):
        first = make_engine(self.TABLE, self.SEGMENTS)[0].search("Milano", "Barcelona", [DAY], max_stops=2,
                                                                 flight_search_budget=30)
        second = make_engine(self.TABLE, self.SEGMENTS)[0].search("Milano", "Barcelona", [DAY], max_stops=2,
                                                                  flight_search_budget=30)
        # "trace" holds wall-clock times, which differ on every run
        first.pop("trace", None)
        second.pop("trace", None)
        self.assertEqual(json.dumps(first, sort_keys=True, default=str),
                         json.dumps(second, sort_keys=True, default=str))

    def test_overnight_wait_flags_night_and_adds_hotel(self):
        table = {
            ("BGY", "GRO", DAY): [flight("Ryanair", "20:00 – 21:10", 1200, "BGY ➔ GRO")],
        }
        segments = {("Girona", "Barcelona"): [seg("Girona", "Barcelona-Sants", "06:30", 60, 300)]}
        engine, _, _, costs = make_engine(table, segments)
        result = engine.search("Milano", "Barcelona", [DAY], max_stops=1, flight_search_budget=30)
        chains = [o for o in result["options"] if any(l["type"] == "flight" for l in o["legs"])]
        self.assertTrue(chains)
        option = chains[0]
        self.assertTrue(option["night_wait"])
        self.assertTrue(any(call["hotel_nights"] == 1 and call["hotel_place"] == "Girona"
                            for call in costs.breakdown_calls))
        train = option["legs"][-1]
        self.assertEqual(train["type"], "train")
        self.assertEqual(train["depart_at"], "2026-10-29T06:30")


# ---------------------------------------------------------------- origin side: Bratislava -> London

class OriginSideTests(unittest.TestCase):
    TABLE = {
        ("Bratislava", "Londra", DAY): [flight("Ryanair", "07:55 – 10:20", 9000, "BTS ➔ STN")],
        ("VIE", "STN", DAY): [flight("Wizz Air", "13:00 – 15:10", 2500, "VIE ➔ STN")],
    }
    SEGMENTS = {
        ("Bratislava", "Vienna"): [seg("Bratislava hl.st.", "Wien Hbf", "08:00", 70, 350, "ÖBB / RegioJet")],
    }

    def test_ground_to_cheap_airport_first_leg(self):
        engine, _, _, _ = make_engine(self.TABLE, self.SEGMENTS)
        result = engine.search("Bratislava", "Londra", [DAY], max_stops=2, flight_search_budget=30)
        best = result["best"]
        self.assertIsNotNone(best)
        self.assertEqual(best["kind"], "side_route")
        self.assertEqual(best["legs"][0]["type"], "train")
        self.assertEqual(best["legs"][0]["from"], "Bratislava hl.st.")
        self.assertEqual(best["legs"][1]["type"], "transfer")
        flight_leg = best["legs"][2]
        self.assertEqual((flight_leg["type"], flight_leg["from"], flight_leg["to"]), ("flight", "VIE", "STN"))
        self.assertGreaterEqual(minutes_between(best["legs"][1]["arrive_at"], flight_leg["depart_at"]), 120)
        self.assertIn("VIE", result["candidates"]["origin_side"])
        self.assertIn("BTS", result["candidates"]["origin_side"])


# ---------------------------------------------------------------- hub chains

class HubTests(unittest.TestCase):
    """Atina -> Londra through the best-ranked hub (computed by the engine, so the test does not depend on the ranking)."""

    ORIGIN = "Atina"
    DEST = "Londra"

    @classmethod
    def setUpClass(cls):
        probe = make_engine({})[0].search(cls.ORIGIN, cls.DEST, [DAY], max_stops=1, flight_search_budget=0)
        cls.hub = probe["candidates"]["hubs"][0]
        cls.base_table = {
            (cls.ORIGIN, cls.hub, DAY): [flight("Wizz Air", "06:00 – 08:00", 1000, f"ATH ➔ {cls.hub}")],
            (cls.hub, cls.DEST, DAY): [
                flight("Ryanair", "10:00 – 11:40", 800, f"{cls.hub} ➔ LTN"),
                flight("Ryanair", "12:30 – 14:10", 900, f"{cls.hub} ➔ LTN"),
            ],
        }

    def test_hub_chain_is_separate_tickets_and_respects_180_minutes(self):
        engine, _, _, _ = make_engine(self.base_table)
        result = engine.search(self.ORIGIN, self.DEST, [DAY], max_stops=1, flight_search_budget=50)
        self.assertIn(self.hub, result["candidates"]["hubs"])
        multi = [o for o in result["options"] if o["kind"] == "multi_flight"]
        self.assertTrue(multi)
        for option in multi:
            self.assertTrue(option["separate_tickets"])
            self.assertEqual(option["stops"], [self.hub])
            first, second = [leg for leg in option["legs"] if leg["type"] == "flight"]
            self.assertGreaterEqual(minutes_between(first["arrive_at"], second["depart_at"]), 180)
        second_departures = [leg["depart_at"][11:16] for o in multi for leg in o["legs"]
                             if leg["type"] == "flight" and leg["from"] == self.hub]
        self.assertIn("12:30", second_departures)
        self.assertNotIn("10:00", second_departures, "a 120 minute connection must be rejected")

    def test_min_stay_at_named_stop_is_enforced_and_marked(self):
        table = dict(self.base_table)
        table[(self.hub, self.DEST, DAY)] = self.base_table[(self.hub, self.DEST, DAY)] + [
            flight("Ryanair", "17:00 – 18:40", 1000, f"{self.hub} ➔ LTN"),
        ]
        engine, _, _, _ = make_engine(table)
        result = engine.search(self.ORIGIN, self.DEST, [DAY], max_stops=1, flight_search_budget=50,
                               min_stay_mins={self.hub: 480})
        multi = [o for o in result["options"] if o["kind"] == "multi_flight"]
        self.assertTrue(multi)
        for option in multi:
            self.assertEqual(option["stopover"], {"place": self.hub, "mins": 480})
            first, second = [leg for leg in option["legs"] if leg["type"] == "flight"]
            self.assertGreaterEqual(minutes_between(first["arrive_at"], second["depart_at"]), 480)
        second_departures = [leg["depart_at"][11:16] for o in multi for leg in o["legs"]
                             if leg["type"] == "flight" and leg["from"] == self.hub]
        self.assertEqual(second_departures, ["17:00"] * len(second_departures))


# ---------------------------------------------------------------- fixed waypoint: Prag -> Yunanistan -> Istanbul

class ViaFixedTests(unittest.TestCase):
    GREEK = {"ATH", "SKG", "HER", "CFU", "CHQ", "EFL", "JTR", "KGS", "KVA", "IOA", "JMK", "JSI"}
    TABLE = {
        ("Prag", "SKG", DAY): [flight("Aegean", "08:00 – 10:00", 1500, "PRG ➔ SKG")],
        # Multi-airport destinations are searched per airport code (IST, SAW), so the arrival airport is known
        ("SKG", "IST", DAY): [flight("Turkish", "14:00 – 15:45", 1200, "SKG ➔ IST")],
        ("Prag", "İstanbul", DAY): [
            flight("Turkish", "09:00 – 15:00", 2500, "PRG ➔ IST"),
            flight("Turkish", "07:00 – 14:30", 2000, "PRG ➔ IST", direct=False, layover="2 sa. 40 dk. SKG (Selanik)"),
        ],
    }

    def test_only_options_through_greece(self):
        engine, _, _, _ = make_engine(self.TABLE)
        result = engine.search("Prag", "İstanbul", [DAY], via_fixed=["Yunanistan"], flight_search_budget=30)
        options = result["options"]
        self.assertTrue(options)
        for option in options:
            self.assertTrue(option["stops"], "a non-stop option must be rejected")
            self.assertIn(option["via_stop"], self.GREEK)
        single = [o for o in options if not o["separate_tickets"]]
        self.assertTrue(single)
        self.assertEqual(single[0]["via_stop"], "SKG")
        self.assertEqual(single[0]["kind"], "direct")
        separate = [o for o in options if o["separate_tickets"]]
        self.assertTrue(separate)
        chain = separate[0]
        flights_in_order = [leg for leg in chain["legs"] if leg["type"] == "flight"]
        self.assertEqual([(f["from"], f["to"]) for f in flights_in_order], [("PRG", "SKG"), ("SKG", "IST")])
        self.assertGreaterEqual(minutes_between(flights_in_order[0]["arrive_at"],
                                                flights_in_order[1]["depart_at"]), 180)
        self.assertEqual(chain["kind"], "multi_flight")


# ---------------------------------------------------------------- arrival deadlines across days

class ArriveByTests(unittest.TestCase):
    TABLE = {
        ("Prag", "Barselona", DAY): [flight("Ryanair", "22:30 – 00:40+1", 900, "PRG ➔ BCN")],
    }

    def test_arrive_by_compares_full_timestamps(self):
        engine, _, _, _ = make_engine(self.TABLE)
        early = engine.search("Prag", "Barselona", [DAY], max_stops=0, arrive_by="2026-10-28T23:59")
        self.assertEqual(early["options"], [])
        # No option at all: the general reason is shown, not a per-date note
        self.assertFalse(any("kalkışlı uygun seçenek bulunamadı" in w for w in early["warnings"]), early["warnings"])
        self.assertTrue(any("kurallarına uymadı" in w for w in early["warnings"]), early["warnings"])
        tight = engine.search("Prag", "Barselona", [DAY], max_stops=0, arrive_by="2026-10-29T01:00")
        self.assertEqual(tight["options"], [])
        ok = engine.search("Prag", "Barselona", [DAY], max_stops=0, arrive_by="2026-10-29T06:00")
        self.assertEqual(len(ok["options"]), 1)
        self.assertEqual(ok["options"][0]["arrive_at"], "2026-10-29T01:10")
        self.assertEqual(ok["options"][0]["kind"], "direct")

    def test_depart_after_uses_the_time_the_traveller_leaves(self):
        engine, _, _, _ = make_engine(self.TABLE)
        # The flight leaves 22:30; the airport transfer (30 min) and the 120 min buffer put the
        # departure from Prague at 20:00, so a 23:00 earliest departure must reject it.
        late = engine.search("Prag", "Barselona", [DAY], max_stops=0, depart_after="2026-10-28T23:00")
        self.assertEqual(late["options"], [])
        early = engine.search("Prag", "Barselona", [DAY], max_stops=0, depart_after="2026-10-28T19:00")
        self.assertEqual(len(early["options"]), 1)
        self.assertGreaterEqual(early["options"][0]["depart_at"], "2026-10-28T19:00")


# ---------------------------------------------------------------- pure ground

class PureGroundTests(unittest.TestCase):
    SEGMENTS = {("Viyana", "Prag"): [seg("Wien Hbf", "Praha hl.n.", "08:00", 255, 750)]}

    def test_direct_ground_option_without_flights(self):
        engine, flights, _, _ = make_engine({}, self.SEGMENTS)
        result = engine.search("Viyana", "Prag", [DAY], max_stops=0)
        best = result["best"]
        self.assertIsNotNone(best)
        self.assertEqual(best["kind"], "ground")
        self.assertEqual([leg["type"] for leg in best["legs"]], ["train"])
        self.assertFalse(best["separate_tickets"])
        self.assertEqual(result["flight_searches"], len(flights.calls))
        self.assertIn("Düşük Risk", best["badges"])


# ---------------------------------------------------------------- real costs module

class RealCostsIntegrationTests(unittest.TestCase):
    def test_default_costs_module_fills_cost_fields(self):
        table = {
            ("BGY", "GRO", DAY): [flight("Ryanair", "07:00 – 08:20", 1200, "BGY ➔ GRO")],
        }
        flights = FakeFlights(table)
        ground = FakeGround({("Girona", "Barcelona"): [seg("Girona", "Barcelona-Sants", "10:00", 60, 300)]})
        engine = RouteEngine(flights, ground, airport_db=AirportDB.load_default())
        result = engine.search("Milano", "Barcelona", [DAY], max_stops=1, flight_search_budget=30)
        chains = [o for o in result["options"] if o["kind"] == "side_route"]
        self.assertTrue(chains)
        option = chains[0]
        self.assertGreaterEqual(option["true_total_try"], option["total_try"] - 0.01)
        self.assertIn("total", option["cost_breakdown"])
        self.assertIn(option["risk"]["level"], {"low", "medium", "high"})


# ---------------------------------------------------------------- input validation and unknown places

class ValidationTests(unittest.TestCase):
    def test_invalid_inputs_raise(self):
        engine, _, _, _ = make_engine({})
        with self.assertRaises(ValueError):
            engine.search("Prag", "Barselona", [DAY], max_stops=4)
        with self.assertRaises(ValueError):
            engine.search("Prag", "Barselona", ["28.10.2026"])
        with self.assertRaises(ValueError):
            engine.search("Prag", "Barselona", [DAY], arrive_by="2026-10-28 12:00")

    def test_unknown_origin_returns_empty_result(self):
        engine, flights, _, _ = make_engine({})
        result = engine.search("Xyzzyland", "Prag", [DAY], max_stops=2)
        self.assertEqual(result["options"], [])
        self.assertIsNone(result["best"])
        self.assertEqual(flights.calls, [])
        self.assertTrue(any("bulunamadı" in warning for warning in result["warnings"]))


# ---------------------------------------------------------------- candidate lists

class CandidateTests(unittest.TestCase):
    def test_hubs_are_lcc_airports_between_origin_and_destination(self):
        engine, _, _, _ = make_engine({})
        result = engine.search("Atina", "Londra", [DAY], max_stops=1, flight_search_budget=0)
        hubs = result["candidates"]["hubs"]
        self.assertTrue(hubs)
        self.assertLessEqual(len(hubs), 4)
        for code in hubs:
            self.assertIn(code, LCC_HUBS)
            self.assertNotIn(code, result["candidates"]["origin_side"])
            self.assertNotIn(code, result["candidates"]["dest_side"])
        self.assertTrue(result["budget_exhausted"])


# ---------------------------------------------------------------- station names on legs

def named(segment, origin_name, dest_name, source="table"):
    """A ScheduledSegment carrying the real station names, as RailBusProvider returns them."""
    return replace(segment, origin_station_name=origin_name, dest_station_name=dest_name, source=source)


class StationNameLegTests(unittest.TestCase):
    def test_table_ground_legs_carry_corridor_station_names(self):
        segments = {("Viyana", "Prag"): [named(seg("Wien Hbf", "Praha hl.n.", "08:00", 255, 750),
                                               "Wien Hbf", "Praha hl.n.")]}
        engine, _, _, _ = make_engine({}, segments)
        best = engine.search("Viyana", "Prag", [DAY], max_stops=0)["best"]
        train = best["legs"][0]
        self.assertEqual(train["type"], "train")
        self.assertEqual(train["origin_station_name"], "Wien Hbf")
        self.assertEqual(train["dest_station_name"], "Praha hl.n.")

    def test_live_ground_legs_carry_regiojet_station_names(self):
        segments = {("Viyana", "Prag"): [named(seg("Vienna - Hbf", "Prague - MS", "09:15", 250, 690),
                                               "Vienna - Hbf", "Prague - MS", source="live")]}
        engine, _, _, _ = make_engine({}, segments)
        best = engine.search("Viyana", "Prag", [DAY], max_stops=0)["best"]
        self.assertEqual(best["legs"][0]["origin_station_name"], "Vienna - Hbf")
        self.assertEqual(best["legs"][0]["dest_station_name"], "Prague - MS")

    def test_estimated_ground_segments_have_null_station_names(self):
        segments = {("Viyana", "Prag"): [seg("Viyana", "Prag", "08:00", 255, 750, est=True)]}
        engine, _, _, _ = make_engine({}, segments)
        best = engine.search("Viyana", "Prag", [DAY], max_stops=0)["best"]
        self.assertIsNone(best["legs"][0]["origin_station_name"])
        self.assertIsNone(best["legs"][0]["dest_station_name"])

    def test_flight_legs_carry_airport_labels_and_transfer_legs_carry_names(self):
        table = {
            ("Prag", "Atina", DAY): [flight("Aegean", "08:00 – 10:00", 1500, "PRG ➔ ATH")],
        }
        engine, _, _, _ = make_engine(table)
        result = engine.search("Prag", "Atina", [DAY], max_stops=0)
        self.assertTrue(result["options"])
        option = result["options"][0]
        flight_leg = next(leg for leg in option["legs"] if leg["type"] == "flight")
        self.assertEqual(flight_leg["origin_station_name"], "PRG - Václav Havel Airport Prague")
        self.assertEqual(flight_leg["dest_station_name"], "ATH - Athens Eleftherios Venizelos International Airport")
        transfers = [leg for leg in option["legs"] if leg["type"] == "transfer"]
        self.assertEqual(len(transfers), 2)
        # City -> airport, then airport -> city: city name on the city side, "<IATA> - <name>" on the airport side
        self.assertEqual((transfers[0]["origin_station_name"], transfers[0]["dest_station_name"]),
                         ("Prag", "PRG - Václav Havel Airport Prague"))
        self.assertEqual((transfers[1]["origin_station_name"], transfers[1]["dest_station_name"]),
                         ("ATH - Athens Eleftherios Venizelos International Airport", "Atina"))

    def test_every_leg_has_both_station_name_keys(self):
        segments = {("Viyana", "Prag"): [seg("Wien Hbf", "Praha hl.n.", "08:00", 255, 750)]}
        engine, _, _, _ = make_engine({}, segments)
        for option in engine.search("Viyana", "Prag", [DAY])["options"]:
            for leg in option["legs"]:
                self.assertIn("origin_station_name", leg)
                self.assertIn("dest_station_name", leg)


class LegSourceTests(unittest.TestCase):
    """Every leg says where its price and times come from (source) and whether the price is estimated."""

    def test_table_ground_leg_reports_table_source_and_exact_price(self):
        segments = {("Viyana", "Prag"): [seg("Wien Hbf", "Praha hl.n.", "08:00", 255, 750)]}
        engine, _, _, _ = make_engine({}, segments)
        train = engine.search("Viyana", "Prag", [DAY], max_stops=0)["best"]["legs"][0]
        self.assertEqual(train["source"], "table")
        self.assertIs(train["price_is_estimated"], False)

    def test_live_ground_leg_reports_live_source(self):
        segments = {("Viyana", "Prag"): [named(seg("Vienna - Hbf", "Prague - MS", "09:15", 250, 690),
                                               "Vienna - Hbf", "Prague - MS", source="live")]}
        engine, _, _, _ = make_engine({}, segments)
        train = engine.search("Viyana", "Prag", [DAY], max_stops=0)["best"]["legs"][0]
        self.assertEqual(train["source"], "live")
        self.assertIs(train["price_is_estimated"], False)

    def test_flixbus_ground_leg_reports_flixbus_source(self):
        segments = {("Viyana", "Prag"): [named(seg("Wien Hbf", "Praha hl.n.", "08:00", 255, 400, operator="FlixBus",
                                                   mode="bus"), "Wien Hbf", "Praha hl.n.", source="flixbus")]}
        engine, _, _, _ = make_engine({}, segments)
        bus = engine.search("Viyana", "Prag", [DAY], max_stops=0)["best"]["legs"][0]
        self.assertEqual(bus["source"], "flixbus")

    def test_estimated_ground_leg_reports_estimate_source_and_estimated_price(self):
        estimated = replace(seg("Viyana", "Prag", "08:00", 255, 750, est=True), source="estimate")
        engine, _, _, _ = make_engine({}, {("Viyana", "Prag"): [estimated]})
        train = engine.search("Viyana", "Prag", [DAY], max_stops=0)["best"]["legs"][0]
        self.assertEqual(train["source"], "estimate")
        self.assertIs(train["price_is_estimated"], True)

    def test_flight_and_transfer_legs_report_their_sources(self):
        table = {
            ("Prag", "Atina", DAY): [flight("Aegean", "08:00 – 10:00", 1500, "PRG ➔ ATH")],
        }
        engine, _, _, _ = make_engine(table)
        option = engine.search("Prag", "Atina", [DAY], max_stops=0)["options"][0]
        flight_leg = next(leg for leg in option["legs"] if leg["type"] == "flight")
        self.assertEqual(flight_leg["source"], "google_flights")
        self.assertIs(flight_leg["price_is_estimated"], False)
        transfers = [leg for leg in option["legs"] if leg["type"] == "transfer"]
        self.assertTrue(transfers)
        for leg in transfers:
            self.assertEqual(leg["source"], "estimate")
            self.assertIs(leg["price_is_estimated"], True)

    def test_every_leg_has_both_source_keys(self):
        segments = {("Viyana", "Prag"): [seg("Wien Hbf", "Praha hl.n.", "08:00", 255, 750)]}
        engine, _, _, _ = make_engine({}, segments)
        for option in engine.search("Viyana", "Prag", [DAY])["options"]:
            for leg in option["legs"]:
                self.assertIn("source", leg)
                self.assertIn("price_is_estimated", leg)


class ResolvedViaTests(unittest.TestCase):
    TABLE = {
        ("Prag", "SKG", DAY): [flight("Aegean", "08:00 – 10:00", 1500, "PRG ➔ SKG")],
        ("SKG", "İstanbul", DAY): [flight("Turkish", "14:00 – 15:45", 1200, "SKG ➔ IST")],
    }

    def test_fixed_waypoint_codes_are_reported_per_waypoint(self):
        engine, _, _, _ = make_engine(self.TABLE)
        result = engine.search("Prag", "İstanbul", [DAY], via_fixed=["Selanik"], flight_search_budget=30)
        resolved = result["query"]["resolved"]
        self.assertEqual(resolved["via"], [[ap.iata for ap in AirportDB.load_default().resolve("Selanik")]])
        self.assertIn("SKG", resolved["via"][0])

    def test_without_via_there_are_no_waypoint_codes(self):
        engine, _, _, _ = make_engine(self.TABLE)
        result = engine.search("Prag", "İstanbul", [DAY])
        self.assertEqual(result["query"]["resolved"]["via"], [])


class LegCoordTests(unittest.TestCase):
    TABLE = {
        ("Milano", "Barcelona", DAY): [flight("Iberia", "08:00 – 10:10", 9000, "MXP ➔ BCN")],
        ("BGY", "GRO", DAY): [flight("Ryanair", "07:00 – 08:20", 1200, "BGY ➔ GRO")],
    }
    SEGMENTS = {
        ("Girona", "Barcelona"): [seg("Girona", "Barcelona-Sants", "10:00", 60, 300)],
    }

    def test_legs_carry_from_coord_and_to_coord(self):
        engine, _, _, _ = make_engine(self.TABLE, self.SEGMENTS)
        result = engine.search("Milano", "Barcelona", [DAY], max_stops=2, flight_search_budget=30)
        self.assertTrue(result["options"])
        leg = result["options"][0]["legs"][0]
        self.assertIn("from_coord", leg)
        self.assertIn("to_coord", leg)
        self.assertIsInstance(leg["from_coord"], dict)
        self.assertIn("lat", leg["from_coord"])
        self.assertIn("lon", leg["from_coord"])
        self.assertIsInstance(leg["to_coord"], dict)
        self.assertIn("lat", leg["to_coord"])
        self.assertIn("lon", leg["to_coord"])

    def test_legs_unknown_place_has_none_coord(self):
        from travel_engine.route_engine import _serialise_leg, _Leg
        from datetime import datetime, timezone
        now = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        leg = _Leg(
            kind="flight", operator="UnknownAir", frm="XYZUNKNOWN", to="NOWHERE",
            dep=now, arr=now, dep_tz="UTC", arr_tz="UTC", price=100.0,
            covers=[], estimated=False, url="", origin_station_name=None,
            dest_station_name=None, source="test", price_is_estimated=False, stops=None, main=True,
        )
        serialised = _serialise_leg(leg)
        self.assertIn("from_coord", serialised)
        self.assertIn("to_coord", serialised)
        self.assertIsNone(serialised["from_coord"])
        self.assertIsNone(serialised["to_coord"])


if __name__ == "__main__":
    unittest.main()
