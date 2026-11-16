"""Acceptance regressions for the route engine (ROADMAP Faz 2 and Faz 3). No network: fake providers only.

(a) İstanbul -> Selanik by ground, then flight: fixed-mode segments consider ground legs next to flights,
    overnight ground legs can be followed by next-day flights, and automatic mode discovers a far
    origin candidate (ground-connected LCC or large airport) without it being named.
(c) Stays roll dates: segments after a stay are searched on the days the stay can push the traveller to,
    and every stay is reported (stopovers) with its hotel night.
"""

import unittest
from dataclasses import replace
from datetime import datetime

from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import RouteEngine

DAY = "2026-11-12"
NEXT = "2026-11-13"
NEXT2 = "2026-11-14"


# ---------------------------------------------------------------- fakes

def flight(airline, times, price, airports, *, direct=True):
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
        "layover_info": "",
        "total_price_try": float(price),
        "currency": "TRY",
        "trip_type": "Tek yön",
        "badge": None,
        "google_flights_url": "https://www.google.com/travel/flights?q=test",
    }


def ground(origin_station, dest_station, dep, mins, price, *, operator="FlixBus", mode="bus",
           source="estimate"):
    estimated = source == "estimate"
    return ScheduledSegment(
        origin_station=origin_station, dest_station=dest_station, departure_time=dep, arrival_time="",
        duration_mins=mins, price_try=float(price), operator=operator, mode=mode, date="",
        is_estimated=estimated, price_eur=None, price_is_estimated=estimated, source=source,
    )


class KeyedFlights:
    """Flights by exact (origin, destination, date) query, as the engine sends them."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]


class CappedGround:
    """Like RailBusProvider.find_ground: departures filtered by the earliest time, sorted, capped at max_results."""

    def __init__(self, segments):
        self.segments = segments
        self.calls = []

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0):
        self.calls.append((from_place, to_place, date_str, earliest_dep_time))
        out = []
        for item in self.segments.get((from_place, to_place), []):
            if earliest_dep_time and item.departure_time < earliest_dep_time:
                continue
            out.append(replace(item, date=date_str))
        out.sort(key=lambda item: item.departure_time)
        return out[:max_results]

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        # Same road factor as RailBusProvider.ground_distance_km
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.25
        return None


class FixedCosts:
    """Transfers free and 30 minutes; a hotel costs 2000 TRY per night."""

    def airport_transfer(self, iata, at_time=None):
        return {"duration_mins": 30, "price_try": 0.0, "mode": "bus", "label": "otobüs",
                "is_estimated": False, "note": ""}

    def cost_breakdown(self, legs, hotel_nights=0, hotel_place=None, cabin_bag=True, checked_bag=False):
        tickets = sum(leg["price_try"] for leg in legs if leg["type"] != "transfer")
        transfers = sum(leg["price_try"] for leg in legs if leg["type"] == "transfer")
        hotel = 2000.0 * hotel_nights
        return {"tickets": tickets, "baggage": 0.0, "transfers": transfers, "hotel": hotel,
                "total": tickets + transfers + hotel, "estimated_parts": []}

    def journey_risk(self, legs, separate_tickets):
        return {"level": "medium" if separate_tickets else "low", "score": 0, "reasons": [],
                "connections": 0}


def make_engine(flights_table, segments):
    flights = KeyedFlights(flights_table)
    ground_fake = CappedGround(segments)
    engine = RouteEngine(flights, ground_fake, airport_db=AirportDB.load_default(), costs=FixedCosts())
    return engine, flights, ground_fake


def ts(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M")


def main_legs(option):
    """The legs a traveller boards (airport transfers are not main legs)."""
    return [leg for leg in option["legs"] if leg["type"] != "transfer"]


def leg_types(option):
    return [leg["type"] for leg in main_legs(option)]


# ---------------------------------------------------------------- (a) fixed mode: ground next to flights

class FixedSegmentGroundTests(unittest.TestCase):
    """via = Selanik, Bratislava; İstanbul -> Prag."""

    VIA = ["Selanik", "Bratislava"]
    # Overnight bus: leaves 22:30 Istanbul time (UTC+3), arrives 06:00 Thessaloniki time (UTC+2 in November).
    # The clocks show 7 h 30 min, but Thessaloniki is one hour behind, so the real travel time is 8 h 30 min
    # (510 min). The old fixture said 450 min: the naive arithmetic that the timezone fix removed.
    OVERNIGHT_BUS = ground("İstanbul", "Selanik", "22:30", 510, 2400, operator="Arda Tur")
    BTS_TRAIN = ground("Bratislava", "Prag", "19:00", 230, 650, operator="RegioJet", mode="train",
                       source="table")

    def table(self):
        return {
            # Selanik -> Bratislava by flight, next day after the bus (the only feasible departure)
            ("Selanik", "Bratislava", NEXT): [flight("Ryanair", "15:50 – 17:40", 1900, "SKG ➔ BTS")],
        }

    def segments(self):
        return {
            ("İstanbul", "Selanik"): [self.OVERNIGHT_BUS],
            ("Bratislava", "Prag"): [self.BTS_TRAIN],
        }

    def test_overnight_bus_is_followed_by_next_day_flight(self):
        """İstanbul bus (22:30 -> 06:00 +1) -> SKG -> BTS flight on the next day -> train to Prag."""
        engine, flights, _ = make_engine(self.table(), self.segments())
        result = engine.search("İstanbul", "Prag", [DAY], via_fixed=self.VIA, flight_search_budget=30)
        self.assertIn(("Selanik", "Bratislava", NEXT), flights.calls,
                      "the segment after the overnight bus must be searched on the next day")
        chains = [o for o in result["options"] if leg_types(o) == ["bus", "flight", "train"]]
        self.assertTrue(chains, f"no ground+flight chain; warnings={result['warnings']}")
        chain = chains[0]
        bus, air = main_legs(chain)[0], main_legs(chain)[1]
        self.assertEqual(bus["from"], "İstanbul")
        self.assertEqual(bus["to"], "Selanik")
        self.assertEqual(bus["depart_at"], f"{DAY}T22:30")
        self.assertEqual(bus["arrive_at"], f"{NEXT}T06:00")
        self.assertTrue(bus["is_estimated"], "an estimated bus must be flagged")
        self.assertEqual(air["depart_at"], f"{NEXT}T15:50")
        self.assertTrue(chain["separate_tickets"])

    def test_origin_ground_is_also_asked_for_the_evening_departures(self):
        """The day's first departures can fill the result cap; the evening ones are asked for separately."""
        engine, _, ground_fake = make_engine(self.table(), self.segments())
        engine.search("İstanbul", "Prag", [DAY], via_fixed=self.VIA, flight_search_budget=30)
        self.assertIn(("İstanbul", "Selanik", DAY, "18:00"), ground_fake.calls)

    def test_overnight_chain_respects_arrive_by(self):
        """The same chain reaches Prague at 22:50 on the next day; arrive_by before that drops it."""
        engine, _, _ = make_engine(self.table(), self.segments())
        result = engine.search("İstanbul", "Prag", [DAY], via_fixed=self.VIA, flight_search_budget=30,
                               arrive_by=f"{NEXT}T12:00")
        self.assertFalse([o for o in result["options"] if leg_types(o) == ["bus", "flight", "train"]])
        for option in result["options"]:
            self.assertLessEqual(ts(option["arrive_at"]), ts(f"{NEXT}T12:00"))

    def test_bus_and_flight_are_both_considered_for_the_first_segment(self):
        """IST -> Selanik can be a flight (IST ✈ SKG) or the ground leg; both kinds must reach the result."""
        table = self.table()
        table[("IST", "Selanik", DAY)] = [flight("Turkish", "08:15 – 08:40", 3500, "IST ➔ SKG")]
        engine, _, _ = make_engine(table, self.segments())
        result = engine.search("İstanbul", "Prag", [DAY], via_fixed=self.VIA, flight_search_budget=30)
        first_legs = {leg_types(o)[0] for o in result["options"]}
        self.assertIn("flight", first_legs, "the flight option for the first segment is missing")
        self.assertIn("bus", first_legs, "the ground option for the first segment is missing")

    def test_same_day_bus_keeps_working(self):
        """A daytime bus that reaches Selanik in time for a same-day flight is still an option."""
        table = {("Selanik", "Bratislava", DAY): [flight("Ryanair", "19:30 – 21:20", 1900, "SKG ➔ BTS")]}
        segments = {
            ("İstanbul", "Selanik"): [ground("İstanbul", "Selanik", "07:00", 500, 2400, operator="Arda Tur")],
            ("Bratislava", "Prag"): [self.BTS_TRAIN],
        }
        engine, _, _ = make_engine(table, segments)
        result = engine.search("İstanbul", "Prag", [DAY], via_fixed=self.VIA, flight_search_budget=30)
        self.assertTrue([o for o in result["options"] if leg_types(o)[:2] == ["bus", "flight"]])


# ---------------------------------------------------------------- (c) stays roll the dates

class StayRollTests(unittest.TestCase):
    """via = Selanik, Bratislava, Viyana; min_stay Selanik 480, Viyana 600; depart 2026-11-12."""

    VIA = ["Selanik", "Bratislava", "Viyana"]
    STAYS = {"Selanik": 480, "Viyana": 600}

    def setUp(self):
        self.table = {
            # Selanik -> Bratislava on the day after the stay (14:50 arrival + 8 h stay = 22:50)
            ("Selanik", "Bratislava", NEXT): [flight("Ryanair", "09:00 – 10:50", 1900, "SKG ➔ BTS")],
        }
        self.segments = {
            ("İstanbul", "Selanik"): [ground("İstanbul", "Selanik", "06:00", 530, 2400, operator="Arda Tur")],
            ("Bratislava", "Viyana"): [ground("Bratislava", "Viyana", "13:00", 60, 300, operator="RegioJet",
                                              mode="train", source="table")],
            ("Viyana", "Prag"): [ground("Viyana", "Prag", "06:00", 230, 500, operator="RegioJet",
                                        mode="train", source="table")],
        }

    def test_later_segments_are_searched_on_the_days_the_stays_push_them(self):
        engine, flights, _ = make_engine(self.table, self.segments)
        engine.search("İstanbul", "Prag", [DAY], via_fixed=self.VIA, min_stay_mins=self.STAYS,
                      flight_search_budget=30)
        self.assertIn(("Selanik", "Bratislava", NEXT), flights.calls)
        self.assertIn(("Selanik", "Bratislava", NEXT2), flights.calls,
                      "a stay of 8 hours plus the night must also try the day after")

    def test_chain_with_both_stays_is_found_and_reports_each_stop(self):
        engine, _, _ = make_engine(self.table, self.segments)
        result = engine.search("İstanbul", "Prag", [DAY], via_fixed=self.VIA, min_stay_mins=self.STAYS,
                               flight_search_budget=30)
        self.assertTrue(result["options"], f"no option; warnings={result['warnings']}")
        chain = next(o for o in result["options"] if leg_types(o)[:2] == ["bus", "flight"])
        self.assertEqual(chain["stopover"], {"place": "Selanik", "mins": 480})
        self.assertEqual(chain["stopovers"], [{"place": "Selanik", "mins": 480},
                                              {"place": "Viyana", "mins": 600}])
        self.assertEqual(leg_types(chain)[-1], "train")
        self.assertGreaterEqual(chain["cost_breakdown"]["hotel"], 2000.0, "overnight stop must cost a hotel night")
        bus, air = main_legs(chain)[0], main_legs(chain)[1]
        self.assertGreaterEqual(minutes_between(bus["arrive_at"], air["depart_at"]), 480)

    def test_option_without_stops_has_no_stopover(self):
        engine, _, _ = make_engine(self.table, self.segments)
        result = engine.search("İstanbul", "Prag", [DAY], via_fixed=self.VIA, flight_search_budget=30)
        for option in result["options"]:
            if not option["stopovers"]:
                self.assertIsNone(option["stopover"])


def minutes_between(start, end):
    return int((ts(end) - ts(start)).total_seconds() // 60)


# ---------------------------------------------------------------- (a) automatic mode: far origin candidate

class FarOriginCandidateTests(unittest.TestCase):
    """origin İstanbul, destination Prag, automatic stops. Selanik (SKG, large, ~636 km road) is not an
    origin side candidate (300 km straight-line radius), only a far candidate."""

    SEGMENTS = {
        ("İstanbul", "Thessaloniki"): [ground("İstanbul", "Thessaloniki", "08:00", 390, 2400,
                                              operator="FlixBus", source="flixbus")],
    }

    def table(self):
        return {("SKG", "PRG", DAY): [flight("Aegean", "17:30 – 19:30", 2600, "SKG ➔ PRG")]}

    def test_far_origin_candidate_gives_ground_then_flight(self):
        engine, flights, _ = make_engine(self.table(), self.SEGMENTS)
        result = engine.search("İstanbul", "Prag", [DAY], max_stops=2, flight_search_budget=30)
        self.assertIn("SKG", result["candidates"]["origin_far"])
        self.assertIn(("SKG", "PRG", DAY), flights.calls)
        chains = [o for o in result["options"] if leg_types(o)[:2] == ["bus", "flight"]]
        self.assertTrue(chains, f"no bus+flight chain; warnings={result['warnings']}")
        self.assertEqual(main_legs(chains[0])[0]["to"], "Thessaloniki")
        self.assertEqual(main_legs(chains[0])[1]["from"], "SKG")

    def test_estimate_only_far_candidate_needs_budget(self):
        segments = {("İstanbul", "Thessaloniki"): [ground("İstanbul", "Thessaloniki", "08:00", 390, 2400,
                                                          operator="Tahmini", source="estimate")]}
        engine, _, _ = make_engine(self.table(), segments)
        small = engine.search("İstanbul", "Prag", [DAY], max_stops=2, flight_search_budget=10)
        self.assertNotIn("SKG", small["candidates"]["origin_far"])
        big = engine.search("İstanbul", "Prag", [DAY], max_stops=2, flight_search_budget=30)
        self.assertIn("SKG", big["candidates"]["origin_far"])


# ---------------------------------------------------------------- fixed mode: passing through a waypoint airport

class ViaAirportPassThroughTests(unittest.TestCase):
    """via = Selanik (SKG): a chain that lands at SKG and flies on from SKG must not go out to the city and back
    (no SKG -> Selanik -> SKG transfer pair). A stay or a ground leg from the city keeps the city transfer."""

    IST_TO_SKG = ("IST", "Selanik", DAY)

    def table(self, departures):
        return {
            self.IST_TO_SKG: [flight("Turkish", "08:00 – 09:00", 3000, "IST ➔ SKG")],
            ("Selanik", "Prag", DAY): departures,
        }

    def transfers_between(self, option, frm, to):
        return [leg for leg in option["legs"] if leg["type"] == "transfer" and leg["from"] == frm and leg["to"] == to]

    def test_pass_through_airport_connects_airport_to_airport(self):
        """Landing at SKG 09:00, the next flight leaves SKG 12:00 (gap 180 min): one direct connection."""
        departures = [flight("Aegean", "12:00 – 14:00", 2600, "SKG ➔ PRG")]
        engine, _, _ = make_engine(self.table(departures), {})
        result = engine.search("İstanbul", "Prag", [DAY], via_fixed=["Selanik"], max_stops=0,
                               flight_search_budget=30)
        direct = [o for o in result["options"] if leg_types(o) == ["flight", "flight"]]
        self.assertTrue(direct, f"no airport-to-airport connection; warnings={result['warnings']}")
        option = direct[0]
        self.assertEqual([leg["from"] for leg in main_legs(option)], ["IST", "SKG"])
        self.assertEqual(main_legs(option)[1]["depart_at"], f"{DAY}T12:00")
        self.assertTrue(option["separate_tickets"])
        for item in result["options"]:
            self.assertFalse(self.transfers_between(item, "SKG", "Selanik"),
                             "the SKG -> Selanik -> SKG round trip must not be used without a stay")

    def test_separate_ticket_gap_still_applies(self):
        """Landing 09:00, departure 11:00 (120 min) is too tight for a separate ticket and the city round trip
        needs more time than that: no chain may use it."""
        departures = [flight("Aegean", "11:00 – 13:00", 2600, "SKG ➔ PRG")]
        engine, _, _ = make_engine(self.table(departures), {})
        result = engine.search("İstanbul", "Prag", [DAY], via_fixed=["Selanik"], max_stops=0,
                               flight_search_budget=30)
        self.assertFalse([o for o in result["options"] if leg_types(o) == ["flight", "flight"]],
                         "a 120 minute separate-ticket connection must not be offered")

    def test_stay_at_waypoint_keeps_the_city_transfer(self):
        """With a 60 minute stay at Selanik the traveller goes into the city and back (stopover reported)."""
        departures = [flight("Aegean", "16:00 – 18:00", 2600, "SKG ➔ PRG")]
        engine, _, _ = make_engine(self.table(departures), {})
        result = engine.search("İstanbul", "Prag", [DAY], via_fixed=["Selanik"], max_stops=0,
                               min_stay_mins={"Selanik": 60}, flight_search_budget=30)
        stayed = [o for o in result["options"] if leg_types(o) == ["flight", "flight"]]
        self.assertTrue(stayed, f"no option with a stay; warnings={result['warnings']}")
        option = stayed[0]
        self.assertEqual(option["stopover"], {"place": "Selanik", "mins": 60})
        self.assertTrue(self.transfers_between(option, "SKG", "Selanik"))
        self.assertTrue(self.transfers_between(option, "Selanik", "SKG"))

    def test_ground_leg_from_the_city_keeps_the_city_transfer(self):
        """Selanik -> Sofya by bus leaves from the city: the chain must go into the city."""
        table = {
            self.IST_TO_SKG: [flight("Turkish", "08:00 – 09:00", 3000, "IST ➔ SKG")],
            ("Sofya", "Prag", DAY): [flight("Czech", "18:00 – 19:30", 2000, "SOF ➔ PRG")],
        }
        segments = {("Selanik", "Sofya"): [ground("Selanik", "Sofya", "10:00", 240, 800, operator="FlixBus")]}
        engine, _, _ = make_engine(table, segments)
        result = engine.search("İstanbul", "Prag", [DAY], via_fixed=["Selanik", "Sofya"], max_stops=0,
                               flight_search_budget=30)
        chains = [o for o in result["options"] if leg_types(o) == ["flight", "bus", "flight"]]
        self.assertTrue(chains, f"no flight-bus-flight chain; warnings={result['warnings']}")
        self.assertTrue(self.transfers_between(chains[0], "SKG", "Selanik"))
        self.assertEqual([leg["from"] for leg in main_legs(chains[0])], ["IST", "Selanik", "SOF"])


if __name__ == "__main__":
    unittest.main()
