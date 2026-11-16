"""Regression tests for the live QA findings (P1-P4, P7). No network: fake flight/ground providers, real AirportDB.

P1  Prag -> Istanbul via Yunanistan returned no options: island waypoints were searched instead of ATH/SKG, and
    a city destination (IST + SAW) dropped flights whose card has no airport codes.
P2  Baggage: low-cost carriers with split or unknown names were priced 0 (estimated).
P3  Near-duplicate options (same flights, same total, different estimated ground timings).
P4  Milano -> Barselona with max_stops=2 stopped after 4 searches; side candidates were never searched.
P7  budget_exhausted warnings did not say which dates / categories were left unsearched.
"""

import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from travel_engine import costs as costs_module
from travel_engine.airports import AirportDB, haversine_km
from travel_engine.providers.rail_bus_provider import ScheduledSegment
from travel_engine.route_engine import LCC_HUBS, RouteEngine
from travel_engine.trip_planner import TripPlanner

DAY = "2026-11-15"
RATE = 50.0


# ---------------------------------------------------------------- fakes

def flight(airline, times, price, airports="", *, direct=True, layover=""):
    """A dict in the GoogleFlightsProvider.search_flights shape. ``airports`` is "" when the card has no codes."""
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
        date="", is_estimated=est, price_eur=None, price_is_estimated=est, source="table" if not est else "estimate",
    )


class KeyedFlights:
    """Flights by exact (origin, destination, date) query, like the planner's search arguments."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [dict(item) for item in self.table.get((origin, destination, departure_date), [])]

    def destinations(self):
        return {to for _frm, to, _day in self.calls}


class AnyFlights:
    """The same list for every query (for option-level checks that do not depend on the query)."""

    def __init__(self, flights):
        self.flights = flights
        self.calls = []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        return [dict(item) for item in self.flights]


class FakeGround:
    def __init__(self, segments=None):
        self.segments = segments or {}

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                    to_coord=None, max_results=6, max_road_km=700.0):
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


class FlatCosts:
    """Free transfers and no hotel: totals are plain ticket sums, so tests read the prices directly."""

    def airport_transfer(self, iata, at_time=None):
        return {"duration_mins": 30, "price_try": 0.0, "mode": "bus", "label": "otobüs", "is_estimated": False,
                "note": ""}

    def cost_breakdown(self, legs, hotel_nights=0, hotel_place=None, cabin_bag=True, checked_bag=False):
        tickets = sum(leg["price_try"] for leg in legs if leg["type"] != "transfer")
        return {"tickets": tickets, "baggage": 0.0, "transfers": 0.0, "hotel": 0.0, "total": tickets,
                "estimated_parts": []}

    def journey_risk(self, legs, separate_tickets):
        return {"level": "medium" if separate_tickets else "low", "score": 0, "reasons": [], "connections": 0}


def make_engine(table=None, segments=None, costs=None, flights=None):
    flights = flights if flights is not None else KeyedFlights(table or {})
    engine = RouteEngine(flights, FakeGround(segments), airport_db=AirportDB.load_default(),
                         costs=costs or FlatCosts())
    return engine, flights


def minutes_between(start, end):
    fmt = "%Y-%m-%dT%H:%M"
    return int((datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds() // 60)


# ---------------------------------------------------------------- P1: via Greece

PRG_ATH = flight("Aegean", "06:45 – 10:55", 2900, "PRG ➔ ATH")
ATH_IST = flight("Turkish", "18:30 – 20:00", 2100, "ATH ➔ IST")
PRG_SKG = flight("Aegean", "13:10 – 16:30", 2600, "PRG ➔ SKG")
SKG_IST = flight("Turkish", "19:30 – 21:00", 1900, "SKG ➔ IST")


def greece_table(with_codes=True):
    def f(item):
        if with_codes:
            return item
        return {**item, "airports": ""}
    return {
        ("Prag", "ATH", DAY): [f(PRG_ATH)],
        ("ATH", "IST", DAY): [f(ATH_IST)],
        ("Prag", "SKG", DAY): [f(PRG_SKG)],
        ("SKG", "IST", DAY): [f(SKG_IST)],
    }


class ViaGreeceTests(unittest.TestCase):
    def search(self, table, budget=15, **kwargs):
        engine, flights = make_engine(table)
        result = engine.search("Prag", "İstanbul", [DAY], arrive_by=f"{DAY}T23:59", max_stops=2,
                               via_fixed=["Yunanistan"], flight_search_budget=budget, **kwargs)
        return result, flights

    def test_options_exist_when_cards_carry_airport_codes(self):
        result, _ = self.search(greece_table(with_codes=True))
        self.assertTrue(result["options"])
        self.assertTrue(all(o["via_stop"] in {"ATH", "SKG", "KVA"} for o in result["options"]))
        self.assertFalse(any("üzerinden uygun bağlantı bulunamadı" in w for w in result["warnings"]))

    def test_options_exist_when_cards_have_no_airport_codes(self):
        # Without codes the arrival airport comes from the search itself, so IST/SAW searches must be by code
        result, _ = self.search(greece_table(with_codes=False))
        self.assertTrue(result["options"], result["warnings"])
        chain = [o for o in result["options"] if o["separate_tickets"]]
        self.assertTrue(chain)
        flights_in_order = [leg for leg in chain[0]["legs"] if leg["type"] == "flight"]
        self.assertEqual([(f["from"], f["to"]) for f in flights_in_order][-1][1], "IST")

    def test_mainland_airports_are_searched_before_islands(self):
        _, flights = self.search(greece_table(), budget=15)
        origins_searched = {to for frm, to, _ in flights.calls if frm == "Prag"}
        self.assertIn("ATH", origins_searched)
        self.assertIn("SKG", origins_searched)
        for island in ("CFU", "CHQ", "HER", "ZTH", "JTR"):
            self.assertNotIn(island, origins_searched)

    def test_mainland_before_islands_with_a_small_budget(self):
        # Ranking does not depend on the table: with budget 6 (direct + 3 + 2 segments) the Greek
        # waypoints searched from Prague are the mainland ones, never islands.
        _, flights = self.search({}, budget=6)
        waypoints = {to for frm, to, _ in flights.calls if frm == "Prag" and to != "İstanbul"}
        self.assertTrue(waypoints)
        self.assertFalse(waypoints & {"CFU", "CHQ", "HER", "ZTH", "JTR", "EFL", "KGS"})

    def test_candidates_report_the_airports_tried_in_fixed_mode(self):
        result, _ = self.search(greece_table())
        candidates = result["candidates"]
        self.assertEqual(candidates["origin_side"], ["PRG"])
        self.assertEqual(candidates["dest_side"], ["IST", "SAW"])
        self.assertIn("ATH", candidates["hubs"])
        self.assertIn("SKG", candidates["hubs"])
        self.assertNotIn("CFU", candidates["hubs"])

    def test_no_option_warning_explains_timing_rule(self):
        late_skg_ist = flight("Turkish", "17:00 – 18:30", 1900, "SKG ➔ IST")  # leaves before the 180 min gap
        table = {("Prag", "SKG", DAY): [PRG_SKG], ("SKG", "IST", DAY): [late_skg_ist]}
        result, _ = self.search(table)
        self.assertEqual(result["options"], [])
        reason = [w for w in result["warnings"] if "üzerinden uygun bağlantı bulunamadı" in w]
        self.assertEqual(len(reason), 1, result["warnings"])
        self.assertEqual(reason[0], "Yunanistan üzerinden uygun bağlantı bulunamadı: 2 uçuş bulundu ama "
                                    "aktarma süresi/son varış saati kurallarına uymadı (aktarma süresi: 1)")

    def test_no_option_warning_names_the_missing_leg(self):
        table = {("Prag", "ATH", DAY): [PRG_ATH]}  # nothing leaves Athens for Istanbul
        result, _ = self.search(table)
        self.assertEqual(result["options"], [])
        self.assertTrue(any("Yunanistan ➔ İstanbul arasında uçuş bulunamadı" in w for w in result["warnings"]),
                        result["warnings"])

    def test_no_flights_at_all_says_so(self):
        result, _ = self.search({})
        self.assertEqual(result["options"], [])
        self.assertTrue(any(w.startswith("Yunanistan üzerinden uygun bağlantı bulunamadı: hiçbir uçuş bulunamadı")
                            for w in result["warnings"]), result["warnings"])


class UnreadableCardTests(unittest.TestCase):
    """A card without airport codes is dropped unless its endpoint is one airport. That must be visible (P1)."""

    def test_skipped_cards_are_reported_with_the_route(self):
        table = {("Prag", "İstanbul", DAY): [flight("Turkish", "09:00 – 11:00", 2500, "")]}
        engine, _ = make_engine(table)
        result = engine.search("Prag", "İstanbul", [DAY], max_stops=0, flight_search_budget=3)
        self.assertEqual(result["options"], [])
        self.assertTrue(any("1 uçuş okunamadı" in w and "Prag ➔ İstanbul" in w for w in result["warnings"]),
                        result["warnings"])

    def test_single_airport_endpoint_keeps_cards_without_codes(self):
        table = {("Prag", "Bratislava", DAY): [flight("Ryanair", "09:00 – 10:00", 900, "")]}
        engine, _ = make_engine(table)
        result = engine.search("Prag", "Bratislava", [DAY], max_stops=0, flight_search_budget=3)
        self.assertTrue(result["options"], result["warnings"])
        self.assertFalse(any("okunamadı" in w for w in result["warnings"]))


class TripPlannerViaEndToEndTests(unittest.TestCase):
    """The planner's mapping passes the fixed-mode options and candidates through unchanged (P1)."""

    def test_plan_trip_via_greece_returns_options_and_candidates(self):
        flights = KeyedFlights(greece_table(with_codes=False))
        engine = RouteEngine(flights, FakeGround(), airport_db=AirportDB.load_default(), costs=FlatCosts())
        planner = TripPlanner(flights, None, route_engine=engine)
        result = planner.plan_trip("Prag", "İstanbul", [DAY], arrive_by=f"{DAY}T23:59", via="Yunanistan",
                                   flight_search_budget=15)
        self.assertTrue(result["options"], result["warnings"])
        self.assertEqual(result["candidates"]["origin_side"], ["PRG"])
        self.assertEqual(result["candidates"]["dest_side"], ["IST", "SAW"])
        self.assertIn("SKG", result["candidates"]["hubs"])
        self.assertTrue(all(o["via_stop"] in {"ATH", "SKG", "KVA"} for o in result["options"]))


# ---------------------------------------------------------------- P4: side candidates use the budget

class SideCandidateBudgetTests(unittest.TestCase):
    def test_side_candidates_are_searched_when_direct_and_lcc_searches_are_empty(self):
        engine, flights = make_engine({})
        result = engine.search("Milano", "Barselona", [DAY], max_stops=2, flight_search_budget=20)
        self.assertGreater(result["flight_searches"], 4)
        destinations_searched = {to for _frm, to, _ in flights.calls}
        self.assertIn("REU", destinations_searched)   # Reus: LCC base near Barcelona, now a dest-side candidate
        self.assertIn("GRO", destinations_searched)
        self.assertIn("REU", result["candidates"]["dest_side"])

    def test_lcc_dest_candidates_come_before_extra_near_airports(self):
        engine, flights = make_engine({})
        engine.search("Milano", "Barselona", [DAY], max_stops=2, flight_search_budget=4)
        searched = [to for _frm, to, _ in flights.calls]
        self.assertEqual(searched[0], "Barselona")
        self.assertTrue(all(code in {"GRO", "REU"} for code in searched[1:]), searched)

    def test_side_route_via_reus_is_found(self):
        table = {("BGY", "REU", DAY): [flight("Ryanair", "07:00 – 08:20", 1200, "BGY ➔ REU")]}
        segments = {("Reus", "Barselona"): [seg("Reus", "Barcelona-Sants", "10:00", 90, 300, "Bus")]}
        engine, _ = make_engine(table, segments)
        result = engine.search("Milano", "Barselona", [DAY], max_stops=2, flight_search_budget=20)
        side = [o for o in result["options"] if o["kind"] == "side_route"]
        self.assertTrue(side, result["warnings"])
        self.assertIn("REU", side[0]["stops"])

    def test_budget_warning_names_unsearched_categories_and_dates(self):
        engine, _ = make_engine({})
        result = engine.search("Milano", "Barselona", [DAY], max_stops=2, flight_search_budget=2)
        self.assertTrue(result["budget_exhausted"])
        self.assertIn("Arama bütçesi doldu", " ".join(result["warnings"]))
        note = [w for w in result["warnings"] if w.startswith("Bütçe nedeniyle tam taranamayan aramalar")]
        self.assertEqual(len(note), 1, result["warnings"])
        self.assertIn("ucuz havayolu varış havalimanları (2026-11-15)", note[0])

    def test_no_budget_note_when_everything_was_searched(self):
        engine, _ = make_engine({})
        result = engine.search("Milano", "Barselona", [DAY], max_stops=0, flight_search_budget=20)
        self.assertFalse(result["budget_exhausted"])
        self.assertFalse(any("tam taranamayan" in w for w in result["warnings"]))


# ---------------------------------------------------------------- P3: near-duplicates

BGY_GRO = flight("Ryanair", "07:00 – 08:20", 1200, "BGY ➔ GRO")


class NearDuplicateTests(unittest.TestCase):
    def search(self, segments, table=None):
        engine, _ = make_engine(table or {("BGY", "GRO", DAY): [BGY_GRO]}, segments)
        return engine.search("Milano", "Barselona", [DAY], max_stops=2, flight_search_budget=30)

    def flight_set_count(self, result, origin, destination):
        return sum(1 for o in result["options"]
                   if any(l["type"] == "flight" and l["from"] == origin and l["to"] == destination
                          for l in o["legs"]))

    def test_same_flights_same_total_different_estimated_timings_collapse(self):
        segments = {("Girona", "Barselona"): [
            seg("Girona", "Barcelona-Sants", "10:00", 60, 300, "Tahmini tren/otobüs", "bus", est=True),
            seg("Girona", "Barcelona-Sants", "11:00", 60, 300, "Tahmini tren/otobüs", "bus", est=True),
        ]}
        result = self.search(segments)
        self.assertEqual(self.flight_set_count(result, "BGY", "GRO"), 1)

    def test_kept_option_is_the_earliest_arrival_among_equal_estimates(self):
        segments = {("Girona", "Barselona"): [
            seg("Girona", "Barcelona-Sants", "11:00", 60, 300, "Tahmini tren/otobüs", "bus", est=True),
            seg("Girona", "Barcelona-Sants", "10:00", 60, 300, "Tahmini tren/otobüs", "bus", est=True),
        ]}
        result = self.search(segments)
        kept = [o for o in result["options"] if any(l["from"] == "BGY" for l in o["legs"] if l["type"] == "flight")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["arrive_at"], "2026-11-15T11:00")

    def test_live_ground_beats_estimate_in_a_duplicate_group(self):
        segments = {("Girona", "Barselona"): [
            seg("Girona", "Barcelona-Sants", "10:00", 60, 300, "Tahmini tren/otobüs", "bus", est=True),
            seg("Girona", "Barcelona-Sants", "11:00", 60, 300, "RegioJet", "bus", est=False),
        ]}
        result = self.search(segments)
        kept = [o for o in result["options"] if any(l["from"] == "BGY" for l in o["legs"] if l["type"] == "flight")]
        self.assertEqual(len(kept), 1)
        ground = [l for l in kept[0]["legs"] if l["type"] not in ("flight", "transfer")]
        self.assertEqual(len(ground), 1)
        self.assertFalse(ground[0]["is_estimated"])
        self.assertEqual(ground[0]["operator"], "RegioJet")

    def test_at_most_two_options_per_flight_set_when_totals_differ(self):
        segments = {("Girona", "Barselona"): [
            seg("Girona", "Barcelona-Sants", "10:00", 60, 300, "A", "bus"),
            seg("Girona", "Barcelona-Sants", "12:00", 60, 600, "B", "bus"),
            seg("Girona", "Barcelona-Sants", "14:00", 60, 900, "C", "bus"),
        ]}
        result = self.search(segments)
        self.assertEqual(self.flight_set_count(result, "BGY", "GRO"), 2)



# ---------------------------------------------------------------- P2: baggage on every flight leg

class BaggageOnEveryLegTests(unittest.TestCase):
    """Real costs module with a fixed EUR rate, so the fee per leg is checked in TRY."""

    def setUp(self):
        patcher = patch("travel_engine.fx.get_eur_try_rate", return_value=(RATE, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def search_direct(self, airline, times="07:00 – 09:00", price=1500):
        engine = RouteEngine(AnyFlights([flight(airline, times, price, "ATH ➔ BER")]), None,
                             airport_db=AirportDB.load_default(), costs=costs_module)
        result = engine.search("Atina", "Berlin", [DAY], max_stops=0, flight_search_budget=3, cabin_bag=True)
        direct = [o for o in result["options"] if o["kind"] == "direct"]
        self.assertTrue(direct, result["warnings"])
        return direct[0]

    def test_codeshare_string_with_split_brand_has_a_real_fee(self):
        option = self.search_direct("easy / Jet")
        flight_legs = [l for l in option["legs"] if l["type"] == "flight"]
        self.assertEqual(flight_legs[0]["operator"], "easy / Jet")
        self.assertEqual(option["cost_breakdown"]["baggage"], 30.0 * RATE)  # easyJet cabin trolley, estimate
        self.assertIn("baggage", option["cost_breakdown"]["estimated_parts"])

    def test_unknown_carrier_is_not_priced_at_zero(self):
        option = self.search_direct("Air Serbia")
        self.assertEqual(option["cost_breakdown"]["baggage"], 20.0 * RATE)
        self.assertIn("baggage", option["cost_breakdown"]["estimated_parts"])

    def test_pegasus_cabin_trolley_is_an_estimated_extra_fee(self):
        # Basic fare includes only an under-seat item; the cabin trolley is an estimated 25 EUR add-on
        option = self.search_direct("Pegasus")
        self.assertEqual(option["cost_breakdown"]["baggage"], 25.0 * RATE)
        self.assertIn("baggage", option["cost_breakdown"]["estimated_parts"])

    def test_full_service_carrier_is_free(self):
        option = self.search_direct("Turkish / Airlines")
        self.assertEqual(option["cost_breakdown"]["baggage"], 0.0)
        self.assertNotIn("baggage", option["cost_breakdown"]["estimated_parts"])

    def test_multi_flight_option_sums_one_fee_per_flight_leg(self):
        # Two separately ticketed flights through Budapest (a hub on the Athens-Berlin line)
        flights = [
            flight("Pegasus", "06:00 – 08:00", 1000, "ATH ➔ BUD"),
            flight("Air Serbia", "11:00 – 13:00", 1000, "BUD ➔ BER"),
        ]
        engine = RouteEngine(AnyFlights(flights), None, airport_db=AirportDB.load_default(), costs=costs_module)
        result = engine.search("Atina", "Berlin", [DAY], max_stops=2, flight_search_budget=30, cabin_bag=True)
        chains = [o for o in result["options"] if o["separate_tickets"]]
        self.assertTrue(chains, result["warnings"])
        option = chains[0]
        flight_legs = [l for l in option["legs"] if l["type"] == "flight"]
        expected = sum(
            costs_module.baggage_fee_try(leg["operator"], True, False)[0] for leg in flight_legs)
        self.assertEqual(option["cost_breakdown"]["baggage"], round(expected, 2))
        self.assertEqual(option["cost_breakdown"]["baggage"], 45.0 * RATE)  # Pegasus cabin 25 + unknown carrier 20 EUR
        self.assertIn("baggage", option["cost_breakdown"]["estimated_parts"])


# ---------------------------------------------------------------- P5: night transfers, labels, date warnings

class NightTransferRouteTests(unittest.TestCase):
    """Real costs module (fixed EUR rate). A flight landing after midnight gets a taxi, not metro (live QA)."""

    def setUp(self):
        patcher = patch("travel_engine.fx.get_eur_try_rate", return_value=(RATE, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def transfer_legs(self, flights, origin="Prag", destination="Atina"):
        engine = RouteEngine(AnyFlights(flights), None, airport_db=AirportDB.load_default(), costs=costs_module)
        result = engine.search(origin, destination, [DAY], max_stops=0, flight_search_budget=6)
        return result, [leg for option in result["options"] for leg in option["legs"] if leg["type"] == "transfer"]

    def test_arrival_at_night_gets_a_taxi_transfer(self):
        flights = [flight("Aegean", "23:00 – 01:50", 1500, "PRG ➔ ATH")]
        result, transfers = self.transfer_legs(flights)
        self.assertTrue(transfers, result["warnings"])
        leg = next(l for l in transfers if l["from"] == "ATH")
        self.assertEqual(leg["operator"], "taksi")
        self.assertTrue(leg["is_estimated"])
        public = costs_module.airport_transfer("ATH")["duration_mins"]
        self.assertEqual(minutes_between(leg["depart_at"], leg["arrive_at"]), round(public * 0.8))
        self.assertEqual(leg["depart_at"], "2026-11-16T01:50")

    def test_daytime_arrival_keeps_the_public_transfer(self):
        flights = [flight("Aegean", "09:00 – 11:00", 1500, "PRG ➔ ATH")]
        _, transfers = self.transfer_legs(flights)
        leg = next(l for l in transfers if l["from"] == "ATH")
        self.assertEqual(leg["operator"], "metro")

    def test_transfer_operator_label_never_says_estimated(self):
        # LCY is not in the transfer table: its label must be the human "Tahmini transfer"
        flights = [flight("Aegean", "09:00 – 11:00", 1500, "PRG ➔ LCY")]
        result, transfers = self.transfer_legs(flights, origin="Prag", destination="Londra")
        self.assertTrue(transfers, result["warnings"])
        for leg in transfers:
            self.assertNotIn("estimated", leg["operator"].lower())
        lcy = next(l for l in transfers if l["from"] == "LCY")
        self.assertEqual(lcy["operator"], "Tahmini transfer")


class DepartureDateWarningTests(unittest.TestCase):
    """Per-date notes are about departures and appear only when other dates produced options (live QA)."""

    class OnlyOnDay:
        def __init__(self, day, flights):
            self.day, self.flights = day, flights

        def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
            return [dict(item) for item in self.flights] if departure_date == self.day else []

    def test_missing_date_gets_a_departure_note_when_another_date_has_options(self):
        flights = self.OnlyOnDay(DAY, [flight("Aegean", "07:00 – 09:00", 1500, "ATH ➔ BER")])
        engine = RouteEngine(flights, None, airport_db=AirportDB.load_default(), costs=FlatCosts())
        result = engine.search("Atina", "Berlin", [DAY, "2026-11-16"], max_stops=0, flight_search_budget=6)
        self.assertTrue(result["options"], result["warnings"])
        self.assertIn("16.11 kalkışlı uygun seçenek bulunamadı", result["warnings"])
        self.assertFalse(any("15.11" in w for w in result["warnings"]), result["warnings"])
        self.assertFalse(any("kurallara uyan seçenek bulunamadı" in w for w in result["warnings"]))

    def test_no_per_date_note_when_no_option_exists_at_all(self):
        engine = RouteEngine(AnyFlights([]), None, airport_db=AirportDB.load_default(), costs=FlatCosts())
        result = engine.search("Atina", "Berlin", [DAY, "2026-11-16"], max_stops=0, flight_search_budget=6)
        self.assertEqual(result["options"], [])
        self.assertFalse(any("kalkışlı" in w for w in result["warnings"]), result["warnings"])
        self.assertTrue(result["warnings"])


# ---------------------------------------------------------------- P6: connections are not non-stop flights

def card_flight(airline, times, price, airports, *, is_direct, stops):
    """A provider dict with explicit stop wording (live cards: "Aktarmasız", "1 aktarma", "bilinmiyor", ...)."""
    return {**flight(airline, times, price, airports, direct=is_direct), "stops": stops,
            "stops_count": 0 if is_direct else 1}


class SingleTicketConnectionTests(unittest.TestCase):
    """Live QA: a 7.5 h Pegasus ATH->BER labelled non-stop. A connection (or a card without stop text) is a
    single-ticket connection in the engine: never a non-stop, and its leg says what the card said."""

    def setUp(self):
        patcher = patch("travel_engine.fx.get_eur_try_rate", return_value=(RATE, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_search(self, flights, max_stops):
        engine = RouteEngine(AnyFlights(flights), None, airport_db=AirportDB.load_default(), costs=costs_module)
        return engine.search("Atina", "Berlin", [DAY], max_stops=max_stops, flight_search_budget=6)

    def test_non_stop_search_drops_connections_and_cards_without_stop_info(self):
        flights = [
            card_flight("Pegasus", "09:30 – 17:00", 1500, "ATH ➔ BER", is_direct=False, stops="Aktarmalı (tahmini)"),
            card_flight("Pegasus", "10:00 – 13:00", 1600, "ATH ➔ BER", is_direct=False, stops="bilinmiyor"),
            card_flight("Aegean", "07:00 – 10:00", 1700, "ATH ➔ BER", is_direct=True, stops="Aktarmasız"),
        ]
        result = self.run_search(flights, max_stops=0)
        self.assertTrue(result["options"], result["warnings"])
        for option in result["options"]:
            for leg in option["legs"]:
                if leg["type"] == "flight":
                    self.assertEqual(leg["stops"], "Aktarmasız")
                    self.assertEqual(leg["operator"], "Aegean")

    def test_connection_is_kept_with_its_stop_wording_when_stops_are_allowed(self):
        flights = [card_flight("Pegasus", "09:30 – 17:00", 1500, "ATH ➔ BER", is_direct=False,
                               stops="Aktarmalı (tahmini)")]
        result = self.run_search(flights, max_stops=1)
        flight_legs = [leg for option in result["options"] for leg in option["legs"] if leg["type"] == "flight"]
        self.assertTrue(flight_legs, result["warnings"])
        self.assertEqual(flight_legs[0]["stops"], "Aktarmalı (tahmini)")
        self.assertNotIn("Aktarmasız", [leg["stops"] for leg in flight_legs])

    def test_ground_and_transfer_legs_have_no_stop_wording(self):
        flights = [card_flight("Aegean", "07:00 – 10:00", 1700, "ATH ➔ BER", is_direct=True, stops="Aktarmasız")]
        result = self.run_search(flights, max_stops=0)
        for option in result["options"]:
            for leg in option["legs"]:
                if leg["type"] != "flight":
                    self.assertIsNone(leg["stops"])


# ---------------------------------------------------------------- P1 helper: the default airports

class AirportChoiceTests(unittest.TestCase):
    def test_greek_waypoint_ranking_puts_mainland_first(self):
        db = AirportDB.load_default()
        self.assertFalse(db.is_island("ATH"))
        self.assertTrue(db.is_island("CFU"))
        self.assertTrue(db.is_island("HER"))
        self.assertFalse(db.is_island("SKG"))
        self.assertFalse(db.is_island("KVA"))

    def test_reus_and_girona_are_lcc_candidates_for_barcelona(self):
        self.assertIn("REU", LCC_HUBS)
        self.assertIn("GRO", LCC_HUBS)


if __name__ == "__main__":
    unittest.main()
