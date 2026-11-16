import os
import unittest
from unittest import mock

from travel_engine.providers.google_hotels_provider import (
    GoogleHotelsProvider,
    HotelsBudget,
    parse_hotel_cards,
)
from travel_engine import costs

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "ghotels")


def _read_fixture(filename: str) -> str:
    path = os.path.join(FIXTURES_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class GoogleHotelsParsingTests(unittest.TestCase):
    def test_parse_prague_fixture(self):
        text = _read_fixture("prague_1adult_tr.txt")
        cards = parse_hotel_cards(text)
        self.assertTrue(len(cards) > 0)
        cheapest = min(cards, key=lambda c: c["price_try"])
        self.assertEqual(cheapest["hotel"], "Plus Prague Hostel")
        self.assertEqual(cheapest["price_try"], 1199.0)

    def test_parse_vienna_fixture(self):
        text = _read_fixture("vienna_1adult_tr.txt")
        cards = parse_hotel_cards(text)
        self.assertTrue(len(cards) > 0)
        cheapest = min(cards, key=lambda c: c["price_try"])
        self.assertEqual(cheapest["hotel"], "A&O Wien Hauptbahnhof")
        self.assertEqual(cheapest["price_try"], 1450.0)

    def test_parse_empty_fixture(self):
        text = _read_fixture("no_hotels_tr.txt")
        cards = parse_hotel_cards(text)
        self.assertEqual(cards, [])


class GoogleHotelsProviderTests(unittest.TestCase):
    def setUp(self):
        GoogleHotelsProvider.reset_shared_state()

    def test_opt_in_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = GoogleHotelsProvider()
            self.assertFalse(provider.enabled)
            res = provider.cheapest_night("Prague", "2026-11-10")
            self.assertIsNone(res)
            status = provider.health_status()
            self.assertEqual(status["status"], "disabled")
            self.assertFalse(status["enabled"])

    def test_cheapest_night_with_mock_fetch_when_enabled(self):
        prague_text = _read_fixture("prague_1adult_tr.txt")
        mock_fetch = mock.Mock(return_value=("https://www.google.com/travel/search?q=Prague", prague_text))

        with mock.patch.dict(os.environ, {"TRAVELPLAN_HOTELS": "1"}):
            provider = GoogleHotelsProvider(fetch_page=mock_fetch)
            self.assertTrue(provider.enabled)
            res = provider.cheapest_night("Prague", "2026-11-10")
            self.assertIsNotNone(res)
            self.assertEqual(res["hotel"], "Plus Prague Hostel")
            self.assertEqual(res["price_try"], 1199.0)
            self.assertEqual(res["source"], "google_hotels")
            mock_fetch.assert_called_once()

    def test_cheapest_night_caches_in_memory_and_sqlite(self):
        prague_text = _read_fixture("prague_1adult_tr.txt")
        mock_fetch = mock.Mock(return_value=("https://www.google.com/travel/search?q=Prague", prague_text))

        with mock.patch.dict(os.environ, {"TRAVELPLAN_HOTELS": "1"}):
            provider = GoogleHotelsProvider(fetch_page=mock_fetch)
            res1 = provider.cheapest_night("Prague", "2026-11-10")
            self.assertEqual(mock_fetch.call_count, 1)

            # Second call: memory cache hit
            res2 = provider.cheapest_night("Prague", "2026-11-10")
            self.assertEqual(mock_fetch.call_count, 1)
            self.assertEqual(res1, res2)

            # Reset memory cache: SQLite cache hit
            provider._mem_cache.clear()
            res3 = provider.cheapest_night("Prague", "2026-11-10")
            self.assertEqual(mock_fetch.call_count, 1)
            self.assertEqual(res1, res3)

    def test_cooldown_on_block_page(self):
        captcha_text = _read_fixture("sorry_captcha_tr.txt")
        mock_fetch = mock.Mock(return_value=("https://www.google.com/sorry/index", captcha_text))

        with mock.patch.dict(os.environ, {"TRAVELPLAN_HOTELS": "1"}):
            provider = GoogleHotelsProvider(fetch_page=mock_fetch)
            res = provider.cheapest_night("Prague", "2026-11-10")
            self.assertIsNone(res)

            status = provider.health_status()
            self.assertEqual(status["status"], "blocked")
            self.assertFalse(status["healthy"])
            self.assertGreater(status["blocked_cooldown_remaining_secs"], 0)

            # While in cooldown, next search does not even call fetch_page
            mock_fetch.reset_mock()
            res2 = provider.cheapest_night("Prague", "2026-11-10")
            self.assertIsNone(res2)
            mock_fetch.assert_not_called()

    def test_budget_exhausted_prevents_page_load(self):
        mock_fetch = mock.Mock()
        budget = HotelsBudget(limit=1)
        self.assertTrue(budget.take())
        self.assertFalse(budget.take())

        with mock.patch.dict(os.environ, {"TRAVELPLAN_HOTELS": "1"}):
            provider = GoogleHotelsProvider(fetch_page=mock_fetch)
            res = provider.cheapest_night("Prague", "2026-11-10", budget=budget)
            self.assertIsNone(res)
            mock_fetch.assert_not_called()

    def test_never_raises_exceptions(self):
        mock_fetch = mock.Mock(side_effect=RuntimeError("browser crashed"))
        with mock.patch.dict(os.environ, {"TRAVELPLAN_HOTELS": "1"}):
            provider = GoogleHotelsProvider(fetch_page=mock_fetch)
            res = provider.cheapest_night("Prague", "2026-11-10")
            self.assertIsNone(res)
            status = provider.health_status()
            self.assertEqual(status["status"], "error")


class CostsBreakdownHotelsTests(unittest.TestCase):
    def test_hotel_price_try_used_and_not_estimated(self):
        legs = [{
            "type": "flight",
            "operator": "Pegasus",
            "from": "IST",
            "to": "PRG",
            "depart_at": "2026-11-10T08:00",
            "arrive_at": "2026-11-10T10:00",
            "price_try": 2000.0,
            "is_estimated": False,
        }]

        # Without hotel_price_try (estimate used)
        bd_est = costs.cost_breakdown(legs, hotel_nights=1, hotel_place="PRG")
        self.assertIn("hotel", bd_est["estimated_parts"])
        self.assertGreater(bd_est["hotel"], 0.0)

        # Without hotel_price_try under real_data_only (hotel unknown)
        bd_est_real = costs.cost_breakdown(legs, hotel_nights=1, hotel_place="PRG", real_data_only=True)
        self.assertIn("hotel", bd_est_real["unknown_parts"])
        self.assertFalse(bd_est_real["price_complete"])

        # With hotel_price_try (real data used!)
        bd_real = costs.cost_breakdown(legs, hotel_nights=1, hotel_place="PRG", hotel_price_try=1199.0)
        self.assertEqual(bd_real["hotel"], 1199.0)
        self.assertNotIn("hotel", bd_real["estimated_parts"])

        # Under real_data_only: real hotel is counted, NOT in unknown_parts!
        bd_real_only = costs.cost_breakdown(
            legs, hotel_nights=1, hotel_place="PRG", cabin_bag=False, checked_bag=False,
            real_data_only=True, hotel_price_try=1199.0
        )
        self.assertEqual(bd_real_only["hotel"], 1199.0)
        self.assertNotIn("hotel", bd_real_only["unknown_parts"])
        self.assertNotIn("hotel", bd_real_only["estimated_parts"])


class RouteEngineHotelIntegrationTests(unittest.TestCase):
    def test_route_engine_passes_hotel_price_to_cost_breakdown(self):
        from travel_engine.route_engine import RouteEngine, _Run, _State, _Leg
        from datetime import datetime, timezone

        mock_hotels = mock.Mock()
        mock_hotels.cheapest_night.return_value = {
            "price_try": 1199.0,
            "hotel": "Plus Prague Hostel",
            "source": "google_hotels",
        }
        engine = RouteEngine(
            flight_provider=mock.Mock(),
            ground_provider=mock.Mock(),
            hotel_provider=mock_hotels,
        )

        dep1 = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)
        arr1 = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
        dep2 = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)
        arr2 = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)

        leg1 = _Leg(
            kind="flight", operator="AJet", frm="SAW", to="PRG", dep=dep1, arr=arr1,
            price=1000.0, estimated=False, url="", main=True, arr_tz="Europe/Prague"
        )
        leg2 = _Leg(
            kind="flight", operator="AJet", frm="PRG", to="BER", dep=dep2, arr=arr2,
            price=1000.0, estimated=False, url="", main=True, dep_tz="Europe/Prague"
        )

        run = _Run(
            engine=engine, origin="SAW", destination="BER", dates=["2026-11-10"],
            arrive_by=None, depart_after=None, max_stops=2, via_tokens=None,
            budget=10, time_value=50.0, min_stay={}, cabin_bag=False, checked_bag=False,
            costs=costs, db=engine.airport_db, ground=engine.ground_provider,
        )

        state = _State(node=("p", 1), time=arr2, legs=(leg1, leg2), mains=2, wp=0, done=True, stays=())
        option = engine._option(run, state)

        self.assertTrue(option["night_wait"])
        bd = option["cost_breakdown"]
        self.assertEqual(bd["hotel"], 1199.0)
        self.assertNotIn("hotel", bd["estimated_parts"])
        mock_hotels.cheapest_night.assert_called_once()


if __name__ == "__main__":
    unittest.main()
