"""Tests for travel_engine.costs (no network: the EUR/TRY rate is patched to 50.0)."""

import unittest
from unittest.mock import patch

from travel_engine.airports import AirportDB, haversine_km
from travel_engine import costs as costs_module
from travel_engine.costs import (
    airport_transfer,
    baggage_fee_try,
    connection_risk,
    cost_breakdown,
    hotel_estimate_try,
    is_night_transport_time,
    journey_risk,
)

RATE = 50.0


class _RatePatched(unittest.TestCase):
    def setUp(self):
        patcher = patch("travel_engine.fx.get_eur_try_rate", return_value=(RATE, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)


class BaggageFeeTests(_RatePatched):
    def test_ryanair_cabin_trolley_is_estimated_low_cost_fee(self):
        self.assertEqual(baggage_fee_try("Ryanair"), (35.0 * RATE, True))

    def test_ryanair_checked_bag(self):
        self.assertEqual(baggage_fee_try("Ryanair", cabin_bag=False, checked_bag=True), (40.0 * RATE, True))

    def test_matching_is_case_insensitive(self):
        self.assertEqual(baggage_fee_try("RYANAIR"), baggage_fee_try("Ryanair"))

    def test_full_service_codeshare_is_free_and_known(self):
        self.assertEqual(baggage_fee_try("Austrian / Aegean"), (0.0, False))

    def test_low_cost_in_codeshare_uses_max_fee(self):
        # Wizz Air cabin 30 EUR; Lufthansa is full-service and adds nothing.
        self.assertEqual(baggage_fee_try("Wizz Air, Lufthansa"), (30.0 * RATE, True))

    def test_ryanair_with_unknown_partner_keeps_low_cost_fee(self):
        self.assertEqual(baggage_fee_try("Ryanair, Lauda"), (35.0 * RATE, True))

    def test_unknown_carrier_gets_default_estimate_not_zero(self):
        # Unknown carriers use the default cabin fee (20 EUR), always an estimate; never 0 for a bag asked for
        self.assertEqual(baggage_fee_try("Lauda"), (20.0 * RATE, True))
        self.assertEqual(baggage_fee_try(""), (20.0 * RATE, True))
        self.assertEqual(baggage_fee_try("Lauda", cabin_bag=False, checked_bag=True), (30.0 * RATE, True))

    def test_word_boundary_does_not_match_inside_other_words(self):
        # "lot" must not match inside "Pilot": the carrier is unknown, so the default estimate applies
        self.assertEqual(baggage_fee_try("Pilot Air"), (20.0 * RATE, True))

    def test_no_bags_requested_costs_nothing_exactly(self):
        self.assertEqual(baggage_fee_try("Ryanair", cabin_bag=False, checked_bag=False), (0.0, False))

    def test_pegasus_cabin_trolley_is_an_estimated_fee_not_included(self):
        # Basic fare includes only an under-seat item; a cabin trolley is an extra (estimate, 25 EUR)
        fee, is_estimated = baggage_fee_try("Pegasus", cabin_bag=True, checked_bag=False)
        self.assertEqual((fee, is_estimated), (25.0 * RATE, True))
        self.assertGreater(fee, 0.0)

    def test_ajet_cabin_trolley_is_an_estimated_fee_not_included(self):
        fee, is_estimated = baggage_fee_try("AJet", cabin_bag=True, checked_bag=False)
        self.assertEqual((fee, is_estimated), (25.0 * RATE, True))
        self.assertGreater(fee, 0.0)

    def test_pegasus_and_ajet_checked_bag_fees_unchanged(self):
        self.assertEqual(baggage_fee_try("Pegasus", cabin_bag=False, checked_bag=True), (30.0 * RATE, True))
        self.assertEqual(baggage_fee_try("AJet", cabin_bag=False, checked_bag=True), (35.0 * RATE, True))

    def test_pegasus_and_ajet_cabin_and_checked_sum(self):
        self.assertEqual(baggage_fee_try("Pegasus", cabin_bag=True, checked_bag=True), (55.0 * RATE, True))
        self.assertEqual(baggage_fee_try("AJet", cabin_bag=True, checked_bag=True), (60.0 * RATE, True))

    def test_pegasus_codeshare_with_full_service_keeps_estimate(self):
        self.assertEqual(baggage_fee_try("Pegasus, Turkish Airlines"), (25.0 * RATE, True))


class NightTaxiTests(_RatePatched):
    """00:30-05:30: public transport is assumed unavailable, so the airport transfer is a taxi (estimate)."""

    def test_night_transfer_is_a_taxi_with_estimated_fare_and_shorter_duration(self):
        db = AirportDB.load_default()
        km = costs_module._distance_to_centre_km(db.get("ATH"))
        day = airport_transfer("ATH")
        night = airport_transfer("ATH", at_time="01:50")
        self.assertEqual(night["mode"], "taxi")
        self.assertEqual(night["label"], "taksi")
        self.assertTrue(night["is_estimated"])
        self.assertEqual(night["duration_mins"], round(day["duration_mins"] * 0.8))
        self.assertAlmostEqual(night["price_try"], round(max(25.0, km * 1.75) * RATE, 2), places=2)
        self.assertGreater(night["price_try"], day["price_try"])
        self.assertIn("taksi", night["note"].lower())

    def test_daytime_transfer_keeps_the_public_mode(self):
        self.assertEqual(airport_transfer("ATH", at_time="12:00")["mode"], "metro")
        self.assertEqual(airport_transfer("ATH", at_time="00:29")["mode"], "metro")
        self.assertEqual(airport_transfer("ATH", at_time="05:31")["mode"], "metro")

    def test_window_bounds_are_inclusive(self):
        self.assertTrue(is_night_transport_time("00:30"))
        self.assertTrue(is_night_transport_time("05:30"))
        self.assertFalse(is_night_transport_time("00:29"))
        self.assertFalse(is_night_transport_time("05:31"))
        self.assertFalse(is_night_transport_time(None))
        self.assertFalse(is_night_transport_time(""))

    def test_no_time_given_keeps_the_table_value(self):
        self.assertEqual(airport_transfer("ATH")["mode"], "metro")

    def test_minimum_fare_applies_to_short_distances(self):
        result = costs_module._night_taxi_result(4.0, 30, RATE)
        self.assertEqual(result["price_try"], 25.0 * RATE)
        self.assertEqual(result["duration_mins"], 24)

    def test_unknown_airport_is_not_turned_into_a_taxi(self):
        result = airport_transfer("ZZZ", at_time="02:00")
        self.assertEqual(result["mode"], "unknown")
        self.assertEqual(result["duration_mins"], 45)


class TransferLabelTests(_RatePatched):
    def test_human_labels_never_say_estimated(self):
        self.assertEqual(airport_transfer("BVA")["label"], "otobüs")
        self.assertEqual(airport_transfer("ATH")["label"], "metro")
        self.assertEqual(airport_transfer("CDG")["label"], "tren")
        self.assertEqual(airport_transfer("LCY")["label"], "Tahmini transfer")
        self.assertEqual(airport_transfer("ZZZ")["label"], "Tahmini transfer")
        for code in ("BVA", "ATH", "CDG", "LCY", "ZZZ"):
            self.assertNotIn("estimated", airport_transfer(code)["label"].lower())


class AirportTransferTests(_RatePatched):
    def test_beauvais_from_table(self):
        result = airport_transfer("BVA")
        self.assertEqual(result["duration_mins"], 75)
        self.assertEqual(result["price_try"], 17.0 * RATE)
        self.assertEqual(result["mode"], "bus")
        self.assertTrue(result["is_estimated"])
        self.assertTrue(result["note"])

    def test_unlisted_airport_uses_distance_rule(self):
        # LCY is not in the table. Its reference point is the London metro centre.
        db = AirportDB.load_default()
        airport = db.get("LCY")
        centre = db.city_center("London")
        km = haversine_km(airport.lat, airport.lon, centre[0], centre[1])
        self.assertGreaterEqual(km, 3.0)

        result = airport_transfer("LCY")
        self.assertEqual(result["mode"], "estimated")
        self.assertTrue(result["is_estimated"])
        self.assertEqual(result["duration_mins"], round(25 + km * 1.5))
        self.assertEqual(result["price_try"], round(max(3.0, km * 0.15) * RATE, 2))

    def test_unknown_code_falls_back_to_default(self):
        result = airport_transfer("ZZZ")
        self.assertEqual(result["duration_mins"], 45)
        self.assertEqual(result["price_try"], 8.0 * RATE)
        self.assertEqual(result["mode"], "unknown")
        self.assertTrue(result["is_estimated"])


class HotelEstimateTests(_RatePatched):
    def test_country_from_city_name(self):
        price, is_estimated = hotel_estimate_try("Prague")
        self.assertEqual(price, 60.0 * RATE)  # CZ
        self.assertTrue(is_estimated)

    def test_country_from_iata(self):
        self.assertEqual(hotel_estimate_try("ATH"), (70.0 * RATE, True))

    def test_unknown_place_uses_default(self):
        self.assertEqual(hotel_estimate_try("Nowhereville"), (60.0 * RATE, True))


class ConnectionRiskTests(_RatePatched):
    def test_tight_separate_flight_to_flight_is_high(self):
        result = connection_risk(
            "2026-11-02T10:00", "2026-11-02T11:00",
            separate_tickets=True, next_mode="flight", min_buffer_mins=180,
        )
        self.assertEqual(result["level"], "high")
        self.assertGreaterEqual(result["score"], 60)
        self.assertTrue(result["reasons"])

    def test_comfortable_same_ticket_is_low(self):
        result = connection_risk(
            "2026-11-02T10:00", "2026-11-02T12:30",
            separate_tickets=False, next_mode="flight", min_buffer_mins=60,
        )
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["score"], 0)

    def test_last_train_is_medium_and_mentions_last_service(self):
        result = connection_risk(
            "2026-11-02T22:00", "2026-11-02T23:00",
            separate_tickets=False, next_mode="ground", min_buffer_mins=20,
            is_last_service=True,
        )
        self.assertEqual(result["level"], "medium")
        self.assertTrue(any("günün son seferi" in reason for reason in result["reasons"]))

    def test_separate_tickets_with_slack_under_90_is_at_least_medium(self):
        result = connection_risk(
            "2026-11-02T10:00", "2026-11-02T12:00",  # 120 min gap, slack 30 with buffer 90
            separate_tickets=True, next_mode="ground", min_buffer_mins=90,
        )
        self.assertEqual(result["level"], "medium")

    def test_result_keys(self):
        result = connection_risk("2026-11-02T10:00", "2026-11-02T12:30", False, "ground", 20)
        self.assertEqual(set(result), {"level", "score", "reasons"})
        self.assertTrue(0 <= result["score"] <= 100)


class JourneyRiskTests(_RatePatched):
    def _flight(self, operator, origin, dest, dep, arr, price=1000.0):
        return {"type": "flight", "operator": operator, "from": origin, "to": dest,
                "depart_at": dep, "arrive_at": arr, "price_try": price, "is_estimated": False}

    def _train(self, origin, dest, dep, arr, price=500.0):
        return {"type": "train", "operator": "RegioJet", "from": origin, "to": dest,
                "depart_at": dep, "arrive_at": arr, "price_try": price, "is_estimated": False}

    def _transfer(self, origin, dest, dep, arr):
        return {"type": "transfer", "operator": "Airport Ground Transit", "from": origin, "to": dest,
                "depart_at": dep, "arrive_at": arr, "price_try": 0.0, "is_estimated": True}

    def test_worst_connection_decides_level(self):
        legs = [
            self._flight("Pegasus", "SAW", "PRG", "2026-11-02T06:00", "2026-11-02T09:00"),
            self._transfer("PRG", "Praha hl.n.", "2026-11-02T09:00", "2026-11-02T09:30"),
            self._train("Praha hl.n.", "Wien", "2026-11-02T11:00", "2026-11-02T13:00"),
            # train arrives 13:00, flight leaves 13:30: 30 min gap vs 150 min buffer
            self._flight("Wizz Air", "VIE", "BUD", "2026-11-02T13:30", "2026-11-02T14:30"),
        ]
        result = journey_risk(legs, separate_tickets=False)
        self.assertEqual(len(result["connections"]), 2)
        self.assertEqual([c["level"] for c in result["connections"]], ["low", "high"])
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["score"], max(c["score"] for c in result["connections"]))
        self.assertTrue(any("aktarması" in reason for reason in result["reasons"]))

    def test_comfortable_journey_is_low_without_reasons(self):
        legs = [
            self._flight("Pegasus", "SAW", "PRG", "2026-11-02T06:00", "2026-11-02T09:00"),
            self._flight("Pegasus", "PRG", "BUD", "2026-11-02T12:00", "2026-11-02T13:00"),
        ]
        result = journey_risk(legs, separate_tickets=False)
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(len(result["connections"]), 1)

    def test_single_leg_has_no_connections(self):
        result = journey_risk([self._flight("Ryanair", "BVA", "PRG", "2026-11-02T06:00", "2026-11-02T08:30")], True)
        self.assertEqual(result, {"level": "low", "score": 0, "reasons": [], "connections": []})


class CostBreakdownTests(_RatePatched):
    def _itinerary(self, transfer_price=0.0):
        return [
            {"type": "flight", "operator": "Ryanair", "from": "BVA", "to": "PRG",
             "depart_at": "2026-11-02T06:00", "arrive_at": "2026-11-02T08:30",
             "price_try": 2000.0, "is_estimated": False},
            {"type": "transfer", "operator": "Airport Ground Transit", "from": "PRG Airport Terminal",
             "to": "Praha hl.n.", "depart_at": "2026-11-02T08:30", "arrive_at": "2026-11-02T09:00",
             "price_try": transfer_price, "is_estimated": True},
            {"type": "train", "operator": "RegioJet", "from": "Praha hl.n.", "to": "Wien Hbf",
             "depart_at": "2026-11-02T10:00", "arrive_at": "2026-11-02T13:30",
             "price_try": 700.0, "is_estimated": False},
        ]

    def test_flight_plus_ground_counts_each_airport_once(self):
        result = cost_breakdown(self._itinerary(transfer_price=150.0), cabin_bag=True)
        # tickets exclude the transfer leg, so its price is not double-counted
        self.assertEqual(result["tickets"], 2700.0)
        self.assertEqual(result["baggage"], 35.0 * RATE)  # Ryanair cabin trolley
        # BVA (trip start, to airport) 17 EUR + PRG (arrival followed by train, to city) 3 EUR
        self.assertEqual(result["transfers"], 17.0 * RATE + 3.0 * RATE)
        self.assertEqual(result["hotel"], 0.0)
        self.assertEqual(result["total"], 2700.0 + 35.0 * RATE + 20.0 * RATE)
        self.assertEqual(result["estimated_parts"], ["baggage", "transfers"])

    def test_hotel_nights_use_country_estimate(self):
        result = cost_breakdown(self._itinerary(), hotel_nights=2, hotel_place="Prague")
        per_night, _ = hotel_estimate_try("Prague")
        self.assertEqual(result["hotel"], round(2 * per_night, 2))
        self.assertIn("hotel", result["estimated_parts"])
        self.assertEqual(result["total"], round(result["tickets"] + result["baggage"]
                                                + result["transfers"] + result["hotel"], 2))

    def test_flight_to_flight_at_airport_has_no_transfer_there(self):
        legs = [
            {"type": "flight", "operator": "Pegasus", "from": "SAW", "to": "ATH",
             "depart_at": "2026-11-02T06:00", "arrive_at": "2026-11-02T08:00", "price_try": 2000.0},
            {"type": "flight", "operator": "Ryanair", "from": "ATH", "to": "BVA",
             "depart_at": "2026-11-02T12:00", "arrive_at": "2026-11-02T14:00", "price_try": 1000.0},
        ]
        result = cost_breakdown(legs, cabin_bag=True)
        # SAW (trip start) 5 EUR + BVA (trip end) 17 EUR; no ATH transfer
        self.assertEqual(result["transfers"], 5.0 * RATE + 17.0 * RATE)
        self.assertEqual(result["tickets"], 3000.0)
        self.assertEqual(result["baggage"], 25.0 * RATE + 35.0 * RATE)  # Pegasus cabin estimate 25 + Ryanair 35
        self.assertIn("baggage", result["estimated_parts"])


if __name__ == "__main__":
    unittest.main()


class UnknownPriceAndRealOnlyTests(_RatePatched):
    """Unknown ticket prices (None) and the "Sadece gerçek veriler" rule of cost_breakdown (no network)."""

    @staticmethod
    def _flight(frm, to, price, operator="Ryanair"):
        return {"type": "flight", "operator": operator, "from": frm, "to": to,
                "depart_at": "2026-10-28T07:00", "arrive_at": "2026-10-28T08:20",
                "price_try": price, "is_estimated": False}

    @staticmethod
    def _train(frm, to, price, estimated=False):
        return {"type": "train", "operator": "RegioJet", "from": frm, "to": to,
                "depart_at": "2026-10-28T10:00", "arrive_at": "2026-10-28T11:00",
                "price_try": price, "is_estimated": estimated}

    def test_unknown_ticket_price_is_not_counted_as_zero(self):
        cb = cost_breakdown([self._flight("BGY", "ATH", 1000.0), self._train("ATH", "Atina", None)])
        self.assertEqual(cb["unknown_ticket_legs"], 1)
        self.assertFalse(cb["price_complete"])
        self.assertEqual(cb["tickets"], 1000.0)

    def test_all_prices_known_and_estimated_transfers_is_complete(self):
        cb = cost_breakdown([self._flight("BGY", "ATH", 1000.0), self._train("ATH", "Atina", 50.0)])
        self.assertEqual(cb["unknown_ticket_legs"], 0)
        self.assertEqual(cb["unknown_parts"], [])
        self.assertTrue(cb["price_complete"])
        self.assertIn("transfers", cb["estimated_parts"])  # counted, and labelled as estimated

    def test_real_only_counts_no_estimated_transfer(self):
        legs = [self._flight("BGY", "ATH", 1000.0), self._train("ATH", "Atina", 50.0)]
        cb = cost_breakdown(legs, real_data_only=True)
        self.assertEqual(cb["transfers"], 0.0)
        self.assertIn("transfers", cb["unknown_parts"])
        self.assertNotIn("transfers", cb["estimated_parts"])
        self.assertFalse(cb["price_complete"])

    def test_real_only_counts_an_official_transfer_fare(self):
        legs = [self._flight("BGY", "ATH", 1000.0), self._train("ATH", "Atina", 50.0)]
        with patch.dict(costs_module.OFFICIAL_TRANSFER_PRICES_EUR, {"BGY": 4.0}):
            self.assertIs(airport_transfer("BGY")["price_is_estimated"], False)
            cb = cost_breakdown(legs, cabin_bag=False, real_data_only=True)
        # BGY is official (4 EUR x 50 = 200 TRY); ATH is still an estimate, so it is not counted
        self.assertEqual(cb["transfers"], 200.0)
        self.assertEqual(cb["unknown_parts"], ["transfers"])

    def test_real_only_baggage_estimate_is_unknown(self):
        legs = [self._flight("BGY", "ATH", 1000.0), self._train("ATH", "Atina", 50.0)]
        off = cost_breakdown(legs, cabin_bag=True, checked_bag=False)
        self.assertGreater(off["baggage"], 0)
        self.assertIn("baggage", off["estimated_parts"])
        on = cost_breakdown(legs, cabin_bag=True, checked_bag=False, real_data_only=True)
        self.assertEqual(on["baggage"], 0.0)
        self.assertIn("baggage", on["unknown_parts"])
        self.assertNotIn("baggage", on["estimated_parts"])

    def test_small_bag_only_is_known_zero_under_the_rule(self):
        legs = [self._flight("BGY", "ATH", 1000.0), self._train("ATH", "Atina", 50.0)]
        cb = cost_breakdown(legs, cabin_bag=False, checked_bag=False, real_data_only=True)
        self.assertEqual(cb["baggage"], 0.0)
        self.assertNotIn("baggage", cb["unknown_parts"])

    def test_hotel_is_listed_as_estimated_and_not_counted_under_the_rule(self):
        legs = [self._flight("BGY", "ATH", 1000.0)]
        off = cost_breakdown(legs, hotel_nights=1, hotel_place="Atina")
        self.assertGreater(off["hotel"], 0)
        self.assertIn("hotel", off["estimated_parts"])
        on = cost_breakdown(legs, hotel_nights=1, hotel_place="Atina", real_data_only=True)
        self.assertEqual(on["hotel"], 0.0)
        self.assertIn("hotel", on["estimated_parts"])
        self.assertIn("hotel", on["unknown_parts"])
        self.assertFalse(on["price_complete"])


class TransfersFromLegsTests(_RatePatched):
    """cost_breakdown(transfers_from_legs=True): the transfer legs' own prices, nothing else (DOG-1)."""

    @staticmethod
    def _legs(transfer_price, price_is_estimated=True):
        return [
            {"type": "flight", "operator": "Wizz Air", "from": "ATH", "to": "BTS",
             "depart_at": "2026-10-28T23:50", "arrive_at": "2026-10-29T01:05",
             "price_try": 4447.0, "is_estimated": False},
            {"type": "transfer", "operator": "taksi", "from": "BTS", "to": "Bratislava",
             "depart_at": "2026-10-29T01:05", "arrive_at": "2026-10-29T01:30", "price_try": transfer_price,
             "is_estimated": True, "price_is_estimated": price_is_estimated},
            {"type": "train", "operator": "RegioJet", "from": "Bratislava", "to": "Prag",
             "depart_at": "2026-10-29T06:00", "arrive_at": "2026-10-29T10:20",
             "price_try": 900.0, "is_estimated": False},
        ]

    def test_sums_the_transfer_legs_only(self):
        result = cost_breakdown(self._legs(1250.0), transfers_from_legs=True)
        self.assertEqual(result["transfers"], 1250.0)  # no ATH "trip start" transfer: no leg shows one
        self.assertEqual(result["tickets"], 5347.0)
        self.assertIn("transfers", result["estimated_parts"])

    def test_unknown_transfer_leg_is_unknown_not_free(self):
        result = cost_breakdown(self._legs(None), transfers_from_legs=True)
        self.assertEqual(result["transfers"], 0.0)
        self.assertIn("transfers", result["unknown_parts"])
        self.assertFalse(result["price_complete"])

    def test_real_data_only_drops_an_estimated_transfer_price(self):
        result = cost_breakdown(self._legs(1250.0), transfers_from_legs=True, real_data_only=True)
        self.assertEqual(result["transfers"], 0.0)
        self.assertIn("transfers", result["unknown_parts"])

    def test_known_transfer_price_counts_under_real_data_only(self):
        result = cost_breakdown(self._legs(300.0, price_is_estimated=False), transfers_from_legs=True,
                                real_data_only=True)
        self.assertEqual(result["transfers"], 300.0)
        self.assertNotIn("transfers", result["unknown_parts"])
        self.assertNotIn("transfers", result["estimated_parts"])
