"""Route engine with real zones: cross-timezone chains, DST, arrive_by / depart_after and output fields.

No network: fake flight and ground providers, the bundled AirportDB, and fixed costs.
"""

import unittest
from datetime import datetime, timezone

from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import RouteEngine
from travel_engine.trip_planner import TripPlanner

DAY = "2026-10-26"       # the day after the EU clocks went back (25 Oct 2026); Greece and Turkey: UTC+2 / UTC+3
NEXT = "2026-10-27"
DST_DAY = "2026-10-25"   # the EU change happens at 01:00 UTC on this day


def flight(airline, times, price, airports, *, direct=True):
    return {
        "source": "test", "airline": airline, "times": times, "duration": "",
        "airports": airports, "stops": "Aktarmasız" if direct else "1 aktarma",
        "stops_count": 0 if direct else 1, "is_direct": direct, "layover_info": "",
        "total_price_try": float(price), "currency": "TRY", "trip_type": "Tek yön", "badge": None,
        "google_flights_url": "https://www.google.com/travel/flights?q=test",
    }


def bus(origin_station, dest_station, dep, arr, mins, price, operator="Arda Tur"):
    return ScheduledSegment(
        origin_station=origin_station, dest_station=dest_station, departure_time=dep, arrival_time=arr,
        duration_mins=mins, price_try=float(price), operator=operator, mode="bus", date="",
        is_estimated=True, price_eur=None, price_is_estimated=True, source="estimate",
    )


class Flights:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]


class Ground:
    def __init__(self, segments):
        self.segments = segments

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0):
        out = []
        for item in self.segments.get((from_place, to_place), []):
            if earliest_dep_time and item.departure_time < earliest_dep_time:
                continue
            out.append(item)
        out.sort(key=lambda item: item.departure_time)
        return out[:max_results]

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        if from_coord and to_coord:
            return haversine_km(from_coord[0], from_coord[1], to_coord[0], to_coord[1]) * 1.25
        return None


class Costs:
    """Free transfers of 45 minutes; a hotel night costs 2000 TRY."""

    def airport_transfer(self, iata, at_time=None):
        return {"duration_mins": 45, "price_try": 0.0, "mode": "bus", "label": "otobüs",
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


def engine_for(table, segments=None):
    flights = Flights(table)
    return RouteEngine(flights, Ground(segments or {}), airport_db=AirportDB.load_default(), costs=Costs()), flights


def utc(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def main_legs(option):
    return [leg for leg in option["legs"] if leg["type"] != "transfer"]


def only_direct(result):
    return [o for o in result["options"] if o["kind"] == "direct"]


class IstanbulBusToSalonicaFlightTests(unittest.TestCase):
    """The live case of 19 Oct: İstanbul 23:00 -> Selanik 12:25 next day (Greek clock, UTC+2 after 25 Oct),
    then a flight from SKG. Times were compared as naive clocks and the connection was wrongly rejected."""

    def run_search(self, flight_rows):
        table = {("Selanik", "Bratislava", NEXT): flight_rows}
        segments = {("İstanbul", "Selanik"): [bus("İstanbul", "Selanik", "23:00", "12:25", 865, 2400)]}
        engine, flights = engine_for(table, segments)
        result = engine.search("İstanbul", "Bratislava", [DAY], via_fixed=["Selanik"], flight_search_budget=30)
        return result, flights

    def test_connection_with_15_20_flight_is_feasible(self):
        """Bus arrives 12:25 Athens = 10:25 UTC. Transfer 45 min + 120 min buffer: earliest flight 15:10 Athens."""
        result, _ = self.run_search([flight("Ryanair", "15:20 – 17:10", 1900, "SKG ➔ BTS")])
        chains = [o for o in result["options"] if [l["type"] for l in main_legs(o)] == ["bus", "flight"]]
        self.assertTrue(chains, f"the 15:20 connection must be feasible; warnings={result['warnings']}")

    def test_flight_before_the_buffer_is_still_rejected(self):
        """13:00 Athens (11:00 UTC) is only 35 minutes after the bus, so it must not connect."""
        result, _ = self.run_search([flight("Ryanair", "13:00 – 14:50", 1900, "SKG ➔ BTS")])
        self.assertFalse([o for o in result["options"] if [l["type"] for l in main_legs(o)] == ["bus", "flight"]])

    def test_bus_is_reported_in_local_times_with_zones(self):
        result, _ = self.run_search([flight("Ryanair", "15:20 – 17:10", 1900, "SKG ➔ BTS")])
        chain = [o for o in result["options"] if [l["type"] for l in main_legs(o)] == ["bus", "flight"]][0]
        bus_leg = main_legs(chain)[0]
        self.assertEqual(bus_leg["depart_at"], f"{DAY}T23:00")
        self.assertEqual(bus_leg["arrive_at"], f"{NEXT}T12:25")
        self.assertEqual(bus_leg["depart_tz"], "Europe/Istanbul")
        self.assertEqual(bus_leg["arrive_tz"], "Europe/Athens")
        self.assertEqual(utc(bus_leg["depart_utc"]), utc(f"{DAY}T20:00:00Z"))
        self.assertEqual(utc(bus_leg["arrive_utc"]), utc(f"{NEXT}T10:25:00Z"))
        flight_leg = main_legs(chain)[1]
        self.assertEqual(flight_leg["depart_at"], f"{NEXT}T15:20")
        self.assertEqual(flight_leg["depart_tz"], "Europe/Athens")
        self.assertEqual(utc(flight_leg["depart_utc"]), utc(f"{NEXT}T13:20:00Z"))


class TwoZonePairTests(unittest.TestCase):
    def test_london_to_paris_is_80_minutes_not_140(self):
        """London is on GMT and Paris on CET on 26 Oct: 09:00 London -> 11:20 Paris is 80 minutes."""
        engine, _ = engine_for({("LHR", "CDG", DAY): [flight("BA", "09:00 – 11:20", 2500, "LHR ➔ CDG")]})
        result = engine.search("LHR", "CDG", [DAY], max_stops=0)
        options = only_direct(result)
        self.assertTrue(options)
        leg = options[0]["legs"][0]
        self.assertEqual(leg["depart_at"], f"{DAY}T09:00")
        self.assertEqual(leg["arrive_at"], f"{DAY}T11:20")
        self.assertEqual((leg["depart_tz"], leg["arrive_tz"]), ("Europe/London", "Europe/Paris"))
        self.assertEqual(leg["arrive_utc"], f"{DAY}T10:20:00Z")
        self.assertEqual(options[0]["duration_mins"], 80)

    def test_lisbon_to_madrid_is_75_minutes(self):
        engine, _ = engine_for({("LIS", "MAD", DAY): [flight("TAP", "07:30 – 09:45", 2000, "LIS ➔ MAD")]})
        option = only_direct(engine.search("LIS", "MAD", [DAY], max_stops=0))[0]
        self.assertEqual(option["duration_mins"], 75)
        self.assertEqual(option["depart_tz"], "Europe/Lisbon")
        self.assertEqual(option["arrive_tz"], "Europe/Madrid")

    def test_canary_islands_to_madrid_is_two_hours(self):
        engine, _ = engine_for({("LPA", "MAD", DAY): [flight("Iberia", "10:00 – 13:00", 2100, "LPA ➔ MAD")]})
        option = only_direct(engine.search("LPA", "MAD", [DAY], max_stops=0))[0]
        self.assertEqual(option["duration_mins"], 120)
        self.assertEqual(option["depart_tz"], "Atlantic/Canary")

    def test_azores_are_one_hour_behind_lisbon(self):
        engine, _ = engine_for({("PDL", "LIS", DAY): [flight("SATA", "08:00 – 10:30", 1500, "PDL ➔ LIS")]})
        option = only_direct(engine.search("PDL", "LIS", [DAY], max_stops=0))[0]
        self.assertEqual(option["duration_mins"], 90)
        self.assertEqual(option["depart_tz"], "Atlantic/Azores")


class DstBoundaryTests(unittest.TestCase):
    def test_flight_across_the_eu_change_is_one_hour(self):
        """On 25 Oct 2026 Prague changes from CEST to CET at 01:00 UTC. 04:00 CET (03:00 UTC) to 06:00 EET
        (04:00 UTC) in Athens is one hour; the clocks alone suggest two."""
        engine, _ = engine_for({(("PRG", "ATH", DST_DAY)): [flight("Aegean", "04:00 – 06:00", 2300, "PRG ➔ ATH")]})
        option = only_direct(engine.search("PRG", "ATH", [DST_DAY], max_stops=0))[0]
        self.assertEqual(option["duration_mins"], 60)
        self.assertEqual(option["depart_utc"], f"{DST_DAY}T03:00:00Z")
        self.assertEqual(option["arrive_utc"], f"{DST_DAY}T04:00:00Z")
        self.assertEqual(option["depart_at"], f"{DST_DAY}T04:00")
        self.assertEqual(option["arrive_at"], f"{DST_DAY}T06:00")

    def test_flight_before_the_change_keeps_summer_offsets(self):
        engine, _ = engine_for({("PRG", "ATH", "2026-10-24"): [flight("Aegean", "10:00 – 12:00", 2300, "PRG ➔ ATH")]})
        option = only_direct(engine.search("PRG", "ATH", ["2026-10-24"], max_stops=0))[0]
        # 10:00 CEST (+2) is 08:00 UTC; 12:00 EEST (+3) is 09:00 UTC: one hour again
        self.assertEqual(option["depart_utc"], "2026-10-24T08:00:00Z")
        self.assertEqual(option["arrive_utc"], "2026-10-24T09:00:00Z")
        self.assertEqual(option["duration_mins"], 60)


class ArriveByDepartAfterTests(unittest.TestCase):
    """arrive_by is local time at the destination, depart_after local time at the origin."""

    def test_arrive_by_is_paris_time(self):
        # Flight arrives 11:20 Paris (10:20 UTC). 10:50 Paris is 09:50 UTC: too early.
        engine, _ = engine_for({("LHR", "CDG", DAY): [flight("BA", "09:00 – 11:20", 2500, "LHR ➔ CDG")]})
        result = engine.search("LHR", "CDG", [DAY], max_stops=0, arrive_by=f"{DAY}T10:50")
        self.assertFalse(only_direct(result))

    def test_arrive_by_later_in_paris_time_keeps_the_flight(self):
        # 11:30 Paris is 10:30 UTC: the 10:20 UTC arrival makes it.
        engine, _ = engine_for({("LHR", "CDG", DAY): [flight("BA", "09:00 – 11:20", 2500, "LHR ➔ CDG")]})
        result = engine.search("LHR", "CDG", [DAY], max_stops=0, arrive_by=f"{DAY}T11:30")
        self.assertTrue(only_direct(result))

    def test_depart_after_is_london_time(self):
        # Flight leaves 08:45 London (07:45 UTC). depart_after is London time: 08:50 drops it, 08:30 keeps it.
        engine, _ = engine_for({("LHR", "CDG", DAY): [flight("BA", "08:45 – 10:50", 2500, "LHR ➔ CDG")]})
        self.assertFalse(only_direct(engine.search("LHR", "CDG", [DAY], max_stops=0, depart_after=f"{DAY}T08:50")))
        self.assertTrue(only_direct(engine.search("LHR", "CDG", [DAY], max_stops=0, depart_after=f"{DAY}T08:30")))


class NightWaitTests(unittest.TestCase):
    def test_night_wait_is_read_on_the_clock_of_the_waiting_airport(self):
        """London 17:30 -> Istanbul 23:30 (20:30 UTC); the next morning Istanbul 05:30 -> Athens 07:30.
        The wait is 23:30-05:30 Istanbul time (night), but only 150 minutes of night in UTC."""
        table = {
            ("LHR", "IST", DAY): [flight("Turkish", "17:30 – 23:30", 2500, "LHR ➔ IST")],
            ("IST", "ATH", NEXT): [flight("Turkish", "05:30 – 07:30", 1800, "IST ➔ ATH")],
        }
        engine, _ = engine_for(table)
        # IST is a fixed stop, so the engine searches it (it is not one of the top automatic hubs)
        result = engine.search("LHR", "ATH", [DAY], via_fixed=["IST"], flight_search_budget=60)
        chains = [o for o in result["options"]
                  if [leg["from"] for leg in main_legs(o)] == ["LHR", "IST"] and len(main_legs(o)) == 2]
        self.assertTrue(chains, f"chain through IST missing; warnings={result['warnings']}")
        self.assertTrue(chains[0]["night_wait"])
        self.assertEqual(main_legs(chains[0])[0]["arrive_tz"], "Europe/Istanbul")


class OutputFieldTests(unittest.TestCase):
    def test_option_and_legs_carry_local_and_utc_fields(self):
        engine, _ = engine_for({("IST", "ATH", DAY): [flight("Turkish", "23:00 – 01:30+1", 2000, "IST ➔ ATH")]})
        option = only_direct(engine.search("IST", "ATH", [DAY], max_stops=0, flight_search_budget=5))
        self.assertTrue(option)
        option = option[0]
        self.assertEqual(option["depart_at"], f"{DAY}T23:00")
        self.assertEqual(option["arrive_at"], f"{NEXT}T01:30")
        self.assertEqual(option["depart_tz"], "Europe/Istanbul")
        self.assertEqual(option["arrive_tz"], "Europe/Athens")
        self.assertEqual(option["depart_utc"], f"{DAY}T20:00:00Z")
        self.assertEqual(option["arrive_utc"], f"{DAY}T23:30:00Z")  # 01:30 Athens on 27 Oct is 23:30 UTC on 26th
        self.assertEqual(option["duration_mins"], 210)
        leg = option["legs"][0]
        for key in ("depart_tz", "arrive_tz", "depart_utc", "arrive_utc"):
            self.assertIn(key, leg)

    def test_trip_planner_engine_mode_keeps_the_zone_fields(self):
        engine, _ = engine_for({("LHR", "CDG", DAY): [flight("BA", "09:00 – 11:20", 2500, "LHR ➔ CDG")]})
        planner = TripPlanner(flight_provider=None, router=None, route_engine=engine)
        result = planner.plan_trip("LHR", "CDG", [DAY], include_side_routes=False)
        options = [o for o in result["options"] if o["kind"] == "direct"]
        self.assertTrue(options)
        leg = options[0]["legs"][0]
        self.assertEqual(leg["depart_tz"], "Europe/London")
        self.assertEqual(leg["arrive_utc"], f"{DAY}T10:20:00Z")
        self.assertEqual(options[0]["depart_tz"], "Europe/London")


if __name__ == "__main__":
    unittest.main()
