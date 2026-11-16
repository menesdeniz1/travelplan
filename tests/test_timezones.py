"""Time zones: airport/country zone table, UTC-based connection risk, local night windows.

No network: AirportDB is the bundled data, costs is called with pure inputs only.
"""

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from travel_engine import costs
from travel_engine.airports import (
    COUNTRY_TIMEZONES,
    UTC_FALLBACK,
    AirportDB,
    airport_zone,
)
from travel_engine.route_engine import _ground_arrival, _night_wait, _parse_flight, _wall_to_utc

UTC = timezone.utc


def utc(text):
    """'2026-10-26T12:00' -> aware UTC datetime."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)


def offset_hours(zone, text):
    """UTC offset in hours of ``zone`` at the UTC instant ``text``."""
    moment = utc(text).astimezone(ZoneInfo(zone))
    return moment.utcoffset().total_seconds() / 3600


class AirportZoneTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = AirportDB.load_default()

    def test_every_bundled_airport_has_a_real_zone(self):
        """No airport falls back to UTC: every country in the data has a zone, and every zone exists."""
        missing = []
        for ap in self.db._airports:
            zone = airport_zone(ap)
            if zone == UTC_FALLBACK or ap.country not in COUNTRY_TIMEZONES:
                missing.append((ap.iata, ap.country))
                continue
            ZoneInfo(zone)  # raises if the IANA name is unknown
        self.assertEqual(missing, [], "airports without a zone in COUNTRY_TIMEZONES")

    def test_country_table_covers_every_country_in_the_data(self):
        countries = {ap.country for ap in self.db._airports}
        self.assertEqual(sorted(countries - set(COUNTRY_TIMEZONES)), [])

    def test_city_airports(self):
        expected = {
            "IST": "Europe/Istanbul", "SAW": "Europe/Istanbul", "SKG": "Europe/Athens",
            "LHR": "Europe/London", "CDG": "Europe/Paris", "PRG": "Europe/Prague",
            "LIS": "Europe/Lisbon", "MAD": "Europe/Madrid", "BCN": "Europe/Madrid",
            "KGD": "Europe/Kaliningrad",
        }
        for code, zone in expected.items():
            self.assertEqual(self.db.timezone(code), zone, code)

    def test_portugal_and_spain_islands(self):
        self.assertEqual(self.db.timezone("PDL"), "Atlantic/Azores")
        self.assertEqual(self.db.timezone("TER"), "Atlantic/Azores")
        self.assertEqual(self.db.timezone("FNC"), "Atlantic/Madeira")
        for code in ("TFS", "TFN", "LPA", "ACE", "FUE", "SPC", "VDE"):
            self.assertEqual(self.db.timezone(code), "Atlantic/Canary", code)

    def test_russia_by_longitude_bands_and_exceptions(self):
        self.assertEqual(self.db.timezone("KGD"), "Europe/Kaliningrad")
        self.assertEqual(self.db.timezone("SVO"), "Europe/Moscow")
        self.assertEqual(self.db.timezone("KZN"), "Europe/Moscow")
        self.assertEqual(self.db.timezone("KUF"), "Europe/Samara")
        self.assertEqual(self.db.timezone("REN"), "Asia/Yekaterinburg")
        self.assertEqual(self.db.timezone("SVX"), "Asia/Yekaterinburg")

    def test_city_and_country_names(self):
        cases = {
            "İstanbul": "Europe/Istanbul",
            "Selanik": "Europe/Athens",
            "Londra": "Europe/London",
            "Lizbon": "Europe/Lisbon",
            "Prag": "Europe/Prague",
            "Spain": "Europe/Madrid",
            "ES": "Europe/Madrid",
            "Portugal": "Europe/Lisbon",
            "Russia": "Europe/Moscow",
            "Yunanistan": "Europe/Athens",
        }
        for text, zone in cases.items():
            self.assertEqual(self.db.timezone(text), zone, text)

    def test_unknown_place_is_utc(self):
        self.assertEqual(self.db.timezone("Atlantis"), UTC_FALLBACK)
        self.assertEqual(self.db.timezone(""), UTC_FALLBACK)

    def test_offsets_on_an_october_day_after_the_eu_change(self):
        """26 Oct 2026, 12:00 UTC: EU clocks are already on winter time."""
        day = "2026-10-26T12:00"
        self.assertEqual(offset_hours("Europe/Istanbul", day), 3)
        self.assertEqual(offset_hours("Europe/Athens", day), 2)
        self.assertEqual(offset_hours("Europe/London", day), 0)
        self.assertEqual(offset_hours("Europe/Paris", day), 1)
        self.assertEqual(offset_hours("Europe/Lisbon", day), 0)
        self.assertEqual(offset_hours("Europe/Madrid", day), 1)
        self.assertEqual(offset_hours("Atlantic/Canary", day), 0)
        self.assertEqual(offset_hours("Atlantic/Azores", day), -1)
        self.assertEqual(offset_hours("Atlantic/Madeira", day), 0)
        self.assertEqual(offset_hours("Europe/Moscow", day), 3)
        self.assertEqual(offset_hours("Europe/Kaliningrad", day), 2)
        self.assertEqual(offset_hours("Europe/Samara", day), 4)
        self.assertEqual(offset_hours("Asia/Yekaterinburg", day), 5)

    def test_eu_change_happens_at_01_utc_on_25_october_2026(self):
        self.assertEqual(offset_hours("Europe/Athens", "2026-10-25T00:30"), 3)
        self.assertEqual(offset_hours("Europe/Athens", "2026-10-25T01:30"), 2)
        self.assertEqual(offset_hours("Europe/Prague", "2026-10-25T00:30"), 2)
        self.assertEqual(offset_hours("Europe/Prague", "2026-10-25T01:30"), 1)


class WallClockConversionTests(unittest.TestCase):
    def test_wall_to_utc_uses_the_zone_of_the_day(self):
        self.assertEqual(_wall_to_utc(datetime(2026, 10, 26, 23, 0), "Europe/Istanbul"),
                         utc("2026-10-26T20:00"))
        self.assertEqual(_wall_to_utc(datetime(2026, 10, 26, 12, 25), "Europe/Athens"),
                         utc("2026-10-26T10:25"))
        # 04:00 on 25 Oct is after the change (01:00 UTC), so it is CET (UTC+1)
        self.assertEqual(_wall_to_utc(datetime(2026, 10, 25, 4, 0), "Europe/Prague"),
                         utc("2026-10-25T03:00"))

    def test_night_window_is_read_in_the_waiting_place_zone(self):
        # 03:00-09:00 UTC on 26 Oct is 05:00-11:00 in Athens (EET): one hour of night there, not 180 minutes.
        start, end = utc("2026-10-26T03:00"), utc("2026-10-26T09:00")
        self.assertFalse(_night_wait(start, end, "Europe/Athens"))
        # The same instants read in UTC are a full 180 minutes of night.
        self.assertTrue(_night_wait(start, end, "UTC"))

    def test_night_wait_in_istanbul_is_local_night(self):
        # 20:30-02:30 UTC is 23:30-05:30 in Istanbul (UTC+3): a night wait there, not in UTC.
        start, end = utc("2026-10-26T20:30"), utc("2026-10-27T02:30")
        self.assertTrue(_night_wait(start, end, "Europe/Istanbul"))
        self.assertFalse(_night_wait(start, end, "UTC"))

    def test_night_wait_keeps_its_real_length_across_the_dst_change(self):
        # Prague, 25 Oct: the clocks go back at 01:00 UTC. A wait from 22:30 UTC (00:30 CEST) to 04:30 UTC
        # (05:30 CET) is six real hours; the night window 00:00-06:00 overlaps it for 5 h 30 min.
        start, end = utc("2026-10-24T22:30"), utc("2026-10-25T04:30")
        self.assertTrue(_night_wait(start, end, "Europe/Prague"))
        # 01:30-03:30 UTC is 02:30-04:30 CET: two hours of night, below the 180 minute threshold.
        self.assertFalse(_night_wait(utc("2026-10-25T01:30"), utc("2026-10-25T03:30"), "Europe/Prague"))

    def test_arrival_of_a_ground_segment_uses_its_destination_clock(self):
        class Seg:
            duration_mins = 865
            arrival_time = "12:25"

        dep = utc("2026-10-26T20:00")  # 23:00 Istanbul
        self.assertEqual(_ground_arrival(Seg(), dep, "Europe/Istanbul", "Europe/Athens"),
                         utc("2026-10-27T10:25"))

    def test_arrival_falls_back_to_duration_without_arrival_time(self):
        class Seg:
            duration_mins = 90
            arrival_time = ""

        dep = utc("2026-10-26T20:00")
        self.assertEqual(_ground_arrival(Seg(), dep, "Europe/Istanbul", "Europe/Athens"),
                         utc("2026-10-26T21:30"))

    def test_arrival_cross_check_prefers_the_day_matching_the_duration(self):
        class Seg:
            duration_mins = 60
            arrival_time = "23:30"  # 23:30 on the departure day, 60 min after a 22:30 departure

        dep = utc("2026-10-26T19:30")  # 22:30 Istanbul
        self.assertEqual(_ground_arrival(Seg(), dep, "Europe/Istanbul", "Europe/Athens"),
                         utc("2026-10-26T21:30"))  # 23:30 Athens on 26 Oct is 21:30 UTC


class FlightCardTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = AirportDB.load_default()

    def parse(self, times, airports, day="2026-10-26"):
        raw = {"times": times, "airports": airports, "airline": "Test", "total_price_try": 100}
        base = datetime.strptime(day, "%Y-%m-%d")
        return _parse_flight(raw, base, None, None, self.db.timezone)

    def test_card_times_are_local_to_each_airport(self):
        flight = self.parse("09:00 – 11:20", "LHR ➔ CDG")
        self.assertEqual(flight.dep, utc("2026-10-26T09:00"))  # London is on GMT
        self.assertEqual(flight.arr, utc("2026-10-26T10:20"))  # Paris is on CET
        self.assertEqual((flight.dep_tz, flight.arr_tz), ("Europe/London", "Europe/Paris"))

    def test_next_day_arrival_without_suffix_is_detected_in_utc(self):
        # Istanbul 23:30 (20:30 UTC) to London 01:00 (01:00 UTC): 4 h 30 on the next day
        flight = self.parse("23:30 – 01:00", "IST ➔ LHR")
        self.assertEqual(flight.dep, utc("2026-10-26T20:30"))
        self.assertEqual(flight.arr, utc("2026-10-27T01:00"))

    def test_plus_one_suffix_counts_from_the_departure_date(self):
        # Istanbul 23:00 (20:00 UTC, 26th) to London 01:30 on the 27th (01:30 UTC): 5 h 30
        flight = self.parse("23:00 – 01:30+1", "IST ➔ LHR")
        self.assertEqual(flight.dep, utc("2026-10-26T20:00"))
        self.assertEqual(flight.arr, utc("2026-10-27T01:30"))

    def test_same_day_arrival_is_not_moved_by_a_wrong_clock_order(self):
        # Istanbul 06:00 (03:00 UTC) to Lisbon 07:00 (07:00 UTC): 4 h, the arrival clock is later than the start
        flight = self.parse("06:00 – 07:00", "IST ➔ LIS")
        self.assertEqual(flight.arr, utc("2026-10-26T07:00"))


class ConnectionRiskTimeZoneTests(unittest.TestCase):
    """costs.journey_risk reads the UTC fields when a leg carries them."""

    def bus_then_flight(self, arrive_local, arrive_utc, depart_local, depart_utc, depart_tz="Europe/Istanbul"):
        """A bus that reaches Thessaloniki (Athens time) and a flight from an airport in ``depart_tz``."""
        return [
            {"type": "bus", "operator": "Arda Tur", "from": "İstanbul", "to": "Selanik",
             "depart_at": "2026-10-26T07:00", "arrive_at": arrive_local,
             "depart_tz": "Europe/Istanbul", "arrive_tz": "Europe/Athens",
             "depart_utc": "2026-10-26T04:00:00Z", "arrive_utc": arrive_utc,
             "price_try": 100, "is_estimated": True},
            {"type": "flight", "operator": "Aegean", "from": "SAW", "to": "PRG",
             "depart_at": depart_local, "arrive_at": "2026-10-26T18:00",
             "depart_tz": depart_tz, "arrive_tz": "Europe/Prague",
             "depart_utc": depart_utc, "arrive_utc": "2026-10-26T16:00:00Z",
             "price_try": 900, "is_estimated": False},
        ]

    def test_gap_is_computed_on_the_utc_instants(self):
        # Bus reaches Thessaloniki at 12:25 Athens time (10:25 UTC); the flight leaves Istanbul at 15:20
        # local (12:20 UTC). The real gap is 115 minutes. Naive clock times would read 175 minutes.
        legs = self.bus_then_flight("2026-10-26T12:25", "2026-10-26T10:25:00Z",
                                    "2026-10-26T15:20", "2026-10-26T12:20:00Z")
        risk = costs.journey_risk(legs, separate_tickets=True)
        connection = risk["connections"][0]
        self.assertEqual(connection["buffer_mins"], 150)
        self.assertIn("bekleme 115 dk", " ".join(connection["reasons"]))
        self.assertEqual(connection["level"], "high")

    def test_legacy_legs_without_utc_fields_keep_the_naive_reading(self):
        legs = self.bus_then_flight("2026-10-26T12:25", "2026-10-26T10:25:00Z",
                                    "2026-10-26T15:20", "2026-10-26T12:20:00Z")
        for leg in legs:
            for key in ("depart_utc", "arrive_utc", "depart_tz", "arrive_tz"):
                leg.pop(key, None)
        risk = costs.journey_risk(legs, separate_tickets=True)
        self.assertIn("aktarma payı yalnızca 25 dk", " ".join(risk["connections"][0]["reasons"]))

    def test_night_window_is_the_local_one_of_the_waiting_place(self):
        # Waiting in Thessaloniki from 04:30 UTC (06:30 local) to 06:30 UTC (08:30 local): no night there,
        # although 04:30-06:30 UTC would be night. Same zone for the flight, so the gap is 120 minutes.
        legs = self.bus_then_flight("2026-10-26T06:30", "2026-10-26T04:30:00Z",
                                    "2026-10-26T08:30", "2026-10-26T06:30:00Z", depart_tz="Europe/Athens")
        risk = costs.journey_risk(legs, separate_tickets=True)
        self.assertNotIn("gece geçişi", " ".join(risk["connections"][0]["reasons"]))


if __name__ == "__main__":
    unittest.main()
