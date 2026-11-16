"""Tests for DOG-8: no cross-border distance estimates (same country and landmass only)."""
import unittest
from unittest import mock

from travel_engine.airports import AirportDB
from travel_engine.providers.rail_bus_provider import RailBusProvider

DAY = "2026-11-12"


class EstimateBorderTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("travel_engine.fx.get_eur_try_rate", return_value=(50.0, "test", False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = RailBusProvider(airport_db=AirportDB.load_default())

    def test_cross_border_greece_turkey_gives_no_estimate(self):
        segs = self.provider.find_ground("Atina", "Izmir", DAY)
        self.assertEqual(segs, [])

    def test_cross_border_turkey_bulgaria_gives_no_estimate(self):
        segs = self.provider.find_ground("Istanbul", "Sofya", DAY)
        self.assertEqual(segs, [])

    def test_different_landmass_island_gives_no_estimate(self):
        segs = self.provider.find_ground("Atina", "Heraklion", DAY)
        self.assertEqual(segs, [])

    def test_same_country_turkey_keeps_estimate(self):
        segs = self.provider.find_ground("Istanbul", "Izmir", DAY)
        self.assertTrue(segs)
        self.assertEqual({s.source for s in segs}, {"estimate"})

    def test_same_country_france_keeps_estimate(self):
        segs = self.provider.find_ground("Lyon", "Marseille", DAY)
        self.assertTrue(segs)
        self.assertEqual({s.source for s in segs}, {"estimate"})


if __name__ == "__main__":
    unittest.main()
