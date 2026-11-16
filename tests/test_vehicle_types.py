"""Tests for mixed vehicle types support (KARA-8, T05)."""

import json
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel_engine.airports import AirportDB
from travel_engine.providers.live_rail_provider import (
    LiveRailProvider,
    LOCATIONS_URL,
    _reset_location_cache_for_tests,
)
from travel_engine.route_engine import RouteEngine, _Leg, _serialise_leg


class VehicleTypesTests(unittest.TestCase):
    def setUp(self):
        _reset_location_cache_for_tests()
        self.addCleanup(_reset_location_cache_for_tests)

    def test_live_rail_provider_carries_vehicle_types_lowercase_sorted(self):
        """(kırmızı) Sahte RegioJet yanıtında vehicleTypes: ['BUS','TRAIN'] olan bağlantı segmentte vehicle_types == ['bus','train'], mode == 'train'."""
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "rail", "regiojet_locations_subset.json")
        with open(fixture_path, "rb") as f:
            locations_data = f.read()

        provider = LiveRailProvider(eur_try_rate=50.0)
        fake_api_response = {
            "routes": [
                {
                    "id": "123",
                    "departureTime": "2026-10-28T08:00:00.000+02:00",
                    "arrivalTime": "2026-10-28T12:00:00.000+02:00",
                    "transfersCount": 1,
                    "freeSeatsCount": 10,
                    "priceFrom": 20.0,
                    "priceCurrency": "EUR",
                    "bookable": True,
                    "vehicleTypes": ["BUS", "TRAIN"],
                    "departureStationId": 10202000,
                    "arrivalStationId": 10202003,
                }
            ]
        }

        routes_body = json.dumps(fake_api_response).encode("utf-8")
        urlopen_path = "travel_engine.providers.live_rail_provider.urllib.request.urlopen"

        class FakeResponse:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def fake_urlopen(request, *args, **kwargs):
            url = getattr(request, "full_url", str(request))
            if url.startswith(LOCATIONS_URL):
                return FakeResponse(locations_data)
            return FakeResponse(routes_body)

        with mock.patch(urlopen_path, side_effect=fake_urlopen):
            segs = provider.search("Bratislava", "Praha", "2026-10-28")

        self.assertIsNotNone(segs)
        self.assertEqual(len(segs), 1)
        seg = segs[0]
        self.assertEqual(seg.mode, "train")  # mode value does NOT change
        self.assertTrue(hasattr(seg, "vehicle_types"))
        self.assertEqual(seg.vehicle_types, ["bus", "train"])

    def test_serialised_leg_contains_vehicle_types(self):
        """(kırmızı) Motor sonucundaki bacakta vehicle_types listesi var."""
        leg = _Leg(
            kind="train",
            operator="RegioJet",
            frm="Bratislava",
            to="Praha",
            dep=datetime(2026, 10, 28, 8, 0),
            arr=datetime(2026, 10, 28, 12, 0),
            price=1000.0,
            estimated=False,
            url="",
            main=True,
            vehicle_types=("bus", "train"),
        )
        serialised = _serialise_leg(leg)
        self.assertIn("vehicle_types", serialised)
        self.assertEqual(serialised["vehicle_types"], ["bus", "train"])

    def test_serialised_leg_default_empty_vehicle_types(self):
        """(koruma) vehicle_types belirtilmemişse boş liste döner."""
        leg = _Leg(
            kind="flight",
            operator="Wizz Air",
            frm="ATH",
            to="PRG",
            dep=datetime(2026, 10, 28, 8, 0),
            arr=datetime(2026, 10, 28, 10, 30),
            price=2000.0,
            estimated=False,
            url="",
            main=True,
        )
        serialised = _serialise_leg(leg)
        self.assertIn("vehicle_types", serialised)
        self.assertEqual(serialised["vehicle_types"], [])


if __name__ == "__main__":
    unittest.main()
