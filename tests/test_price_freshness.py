"""Tests for flight price freshness timestamp (DOG-9 / B08)."""

import datetime
import os
import tempfile
import unittest
from unittest import mock

from travel_engine.flight_cache import FlightCache
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider
from travel_engine.route_engine import RouteEngine, _Flight, _Leg, _serialise_leg


class PriceFreshnessTests(unittest.TestCase):
    def setUp(self):
        GoogleFlightsProvider.reset_shared_state()
        self.addCleanup(GoogleFlightsProvider.reset_shared_state)

    def test_parsed_flight_item_has_observed_at(self):
        provider = GoogleFlightsProvider()
        card_text = "10:00 – 12:00\nTürk Hava Yolları\n2 sa. 0 dk.\nIST - PRG\nAktarmasız\n₺3.500"
        parsed = provider._parse_flight_item(card_text, "https://google.com", "Tek Yön")
        self.assertIsNotNone(parsed)
        self.assertIn("observed_at", parsed)
        self.assertIsInstance(parsed["observed_at"], str)
        # Check it parses as ISO datetime
        dt = datetime.datetime.fromisoformat(parsed["observed_at"])
        self.assertIsNotNone(dt.tzinfo)

    def test_cached_flight_preserves_first_observed_at(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "cache.db")
            cache = FlightCache(db_path, ttl_secs=600)
            ts_original = "2026-10-21T12:00:00+00:00"
            flights = [{
                "airline": "Pegasus",
                "times": "10:00 – 12:00",
                "duration": "2 sa.",
                "total_price_try": 2500.0,
                "is_direct": True,
                "observed_at": ts_original,
            }]
            cache.put_results("IST-PRG-oneway", "2026-11-15", "ok", flights)

            cached_entry = cache.get_results("IST-PRG-oneway", "2026-11-15")
            self.assertIsNotNone(cached_entry)
            self.assertEqual(cached_entry["results"][0]["observed_at"], ts_original)

    def test_route_engine_serialise_leg_includes_observed_at(self):
        now_iso = "2026-10-21T14:30:00+00:00"
        flight_leg = _Leg(
            kind="flight",
            operator="Pegasus",
            frm="SAW",
            to="PRG",
            dep=datetime.datetime(2026, 11, 15, 10, 0, tzinfo=datetime.timezone.utc),
            arr=datetime.datetime(2026, 11, 15, 12, 30, tzinfo=datetime.timezone.utc),
            price=2500.0,
            estimated=False,
            url="https://flypgs.com",
            main=True,
            observed_at=now_iso,
        )
        serialised = _serialise_leg(flight_leg)
        self.assertIn("observed_at", serialised)
        self.assertEqual(serialised["observed_at"], now_iso)

        # Ground leg has None
        ground_leg = _Leg(
            kind="train",
            operator="RegioJet",
            frm="PRG",
            to="VIE",
            dep=datetime.datetime(2026, 11, 15, 13, 0, tzinfo=datetime.timezone.utc),
            arr=datetime.datetime(2026, 11, 15, 17, 0, tzinfo=datetime.timezone.utc),
            price=400.0,
            estimated=False,
            url="",
            main=True,
        )
        serialised_ground = _serialise_leg(ground_leg)
        self.assertIn("observed_at", serialised_ground)
        self.assertIsNone(serialised_ground["observed_at"])

    def test_route_engine_transfers_flight_observed_at_to_leg(self):
        """When RouteEngine takes flights, the resulting flight legs retain observed_at."""
        ts = "2026-10-21T15:00:00+00:00"
        flight = _Flight(
            frm="SAW",
            to="PRG",
            dep=datetime.datetime(2026, 11, 15, 10, 0, tzinfo=datetime.timezone.utc),
            arr=datetime.datetime(2026, 11, 15, 12, 30, tzinfo=datetime.timezone.utc),
            price=2500.0,
            operator="Pegasus",
            url="",
            layovers=(),
            is_direct=True,
            observed_at=ts,
        )
        self.assertEqual(flight.observed_at, ts)


if __name__ == "__main__":
    unittest.main()
