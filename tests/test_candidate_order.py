"""Tests for candidate ordering by known price bound (PLN-4a, C03)."""

import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel_engine.airports import AirportDB
from travel_engine.observations import ObservationStore
from travel_engine.route_engine import RouteEngine, _Run


class _DummyFlightProvider:
    def __init__(self, calendar: Optional[Dict[Tuple[str, str], Dict[str, float]]] = None):
        self.calendar = calendar or {}
        self.searched: List[Tuple[str, str, str]] = []

    def get_calendar_prices(
        self, origin: str, destination: str, trip_type: str = "oneway", max_age_secs: Optional[float] = None
    ) -> Dict[str, float]:
        return dict(self.calendar.get((origin, destination), {}))

    def search_flights(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: Optional[str] = None,
        trip_type: str = "oneway",
        **kwargs: Any,
    ) -> List[Any]:
        self.searched.append((origin, destination, departure_date))
        return []


class _DummyFlightProviderNoMaxAge:
    def __init__(self, calendar: Optional[Dict[Tuple[str, str], Dict[str, float]]] = None):
        self.calendar = calendar or {}

    def get_calendar_prices(self, origin: str, destination: str, trip_type: str = "oneway") -> Dict[str, float]:
        return dict(self.calendar.get((origin, destination), {}))

    def search_flights(self, *args: Any, **kwargs: Any) -> List[Any]:
        return []


class _DummyGroundProvider:
    def find_ground(self, *args: Any, **kwargs: Any) -> List[Any]:
        return []

    def ground_distance_km(self, *args: Any, **kwargs: Any) -> float:
        return 100.0


class CandidateOrderTests(unittest.TestCase):
    def setUp(self):
        self.db = AirportDB.load_default()

    def _make_run(
        self,
        flight_provider: Optional[Any] = None,
        observations: Optional[Any] = None,
        budget: int = 20,
        dates: Optional[List[str]] = None,
    ) -> _Run:
        fp = flight_provider or _DummyFlightProvider()
        gp = _DummyGroundProvider()
        engine = RouteEngine(
            fp,
            gp,
            airport_db=self.db,
            max_parallel_searches=1,
            observations=observations,
        )
        dates = dates or ["2026-11-12"]
        return _Run(
            engine=engine,
            origin="Atina",
            destination="Prag",
            dates=dates,
            arrive_by=None,
            depart_after=None,
            max_stops=2,
            via_tokens=None,
            budget=budget,
            time_value=150.0,
            min_stay={},
            cabin_bag=True,
            checked_bag=False,
            costs=None,
            db=self.db,
            ground=gp,
        )

    def test_reorder_known_bounds_preserves_unknown_positions(self):
        """(kırmızı) Sınırları [—, 900, —, 500, 700] olan beş öğe → sıra [—, 500, —, 700, 900] (bilinmeyenler yerinde)."""
        run = self._make_run()

        items = [
            (("UNK1", "UNK1"), ("DEST", "DEST"), "2026-11-12"),  # bound: None
            (("BGY", "BGY"), ("DEST", "DEST"), "2026-11-12"),    # bound: 900
            (("UNK2", "UNK2"), ("DEST", "DEST"), "2026-11-12"),  # bound: None
            (("ATH", "ATH"), ("DEST", "DEST"), "2026-11-12"),    # bound: 500
            (("SKG", "SKG"), ("DEST", "DEST"), "2026-11-12"),    # bound: 700
        ]

        bounds_map = {
            (("UNK1", "UNK1"), ("DEST", "DEST"), "2026-11-12"): None,
            (("BGY", "BGY"), ("DEST", "DEST"), "2026-11-12"): 900.0,
            (("UNK2", "UNK2"), ("DEST", "DEST"), "2026-11-12"): None,
            (("ATH", "ATH"), ("DEST", "DEST"), "2026-11-12"): 500.0,
            (("SKG", "SKG"), ("DEST", "DEST"), "2026-11-12"): 700.0,
        }

        run.known_bound = lambda frm, to, date: bounds_map.get((frm, to, date))

        reordered = run.order_by_known_bound(items)

        expected = [
            (("UNK1", "UNK1"), ("DEST", "DEST"), "2026-11-12"),  # index 0: None
            (("ATH", "ATH"), ("DEST", "DEST"), "2026-11-12"),    # index 1: 500
            (("UNK2", "UNK2"), ("DEST", "DEST"), "2026-11-12"),  # index 2: None
            (("SKG", "SKG"), ("DEST", "DEST"), "2026-11-12"),    # index 3: 700
            (("BGY", "BGY"), ("DEST", "DEST"), "2026-11-12"),    # index 4: 900
        ]
        self.assertEqual(reordered, expected)

    def test_observation_cheap_pair_searched_before_budget_exhaustion(self):
        """(kırmızı) Gözlem deposundaki ucuz bir çift, bütçe dolmadan önce aranır (sahte sağlayıcı, sahte depo, küçük bütçe)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "obs.db")
            store = ObservationStore(db_path)
            try:
                # Record a very cheap observation for SKG -> PRG
                store.record("SKG", "PRG", "2026-11-12", min_price=350.0, card_count=5)
                # Record an expensive observation for ATH -> PRG
                store.record("ATH", "PRG", "2026-11-12", min_price=1200.0, card_count=5)

                provider = _DummyFlightProvider()
                run = self._make_run(flight_provider=provider, observations=store, budget=1)

                # Queue with expensive first, cheap second
                queue = [
                    (("ATH", "ATH"), ("PRG", "PRG"), "2026-11-12"),
                    (("SKG", "SKG"), ("PRG", "PRG"), "2026-11-12"),
                ]
                reordered = run.order_by_known_bound(queue)
                self.assertEqual(reordered[0][0][0], "SKG")
                self.assertEqual(reordered[1][0][0], "ATH")

                # Run with budget 1: only the first search should run
                run.run_plan([("test_cat", None, queue)], next_day=False)
                searched_origins = [s[0] for s in provider.searched]
                self.assertIn("SKG", searched_origins)
                self.assertNotIn("ATH", searched_origins)
            finally:
                store.close()

    def test_without_store_and_calendar_order_is_identical(self):
        """(koruma) Depo ve takvim yokken arama sırası bugünküyle aynı."""
        provider = _DummyFlightProvider(calendar={})
        run = self._make_run(flight_provider=provider, observations=None, budget=10)

        items = [
            (("A", "A"), ("B", "B"), "2026-11-12"),
            (("C", "C"), ("D", "D"), "2026-11-12"),
            (("E", "E"), ("F", "F"), "2026-11-12"),
        ]

        reordered = run.order_by_known_bound(items)
        self.assertEqual(reordered, items)

    def test_known_bound_uses_min_of_calendar_and_observation(self):
        """known_bound selects min(calendar, observation) and handles missing sources."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "obs.db")
            store = ObservationStore(db_path)
            try:
                store.record("ATH", "PRG", "2026-11-12", min_price=600.0, card_count=2)

                calendar = {("ATH", "PRG"): {"2026-11-12": 800.0}}
                provider = _DummyFlightProvider(calendar=calendar)
                run = self._make_run(flight_provider=provider, observations=store)

                # Both known: 600 < 800 -> 600
                bound = run.known_bound(("ATH", "ATH"), ("PRG", "PRG"), "2026-11-12")
                self.assertEqual(bound, 600.0)

                # Calendar cheaper than observation
                store.record("SKG", "PRG", "2026-11-12", min_price=950.0, card_count=2)
                provider.calendar[("SKG", "PRG")] = {"2026-11-12": 550.0}
                bound2 = run.known_bound(("SKG", "SKG"), ("PRG", "PRG"), "2026-11-12")
                self.assertEqual(bound2, 550.0)

                # Multi-airport origin (frm[1] is None) skips observation and uses calendar
                provider.calendar[("Atina", "PRG")] = {"2026-11-12": 720.0}
                bound3 = run.known_bound(("Atina", None), ("PRG", "PRG"), "2026-11-12")
                self.assertEqual(bound3, 720.0)
            finally:
                store.close()

    def test_known_bound_handles_provider_without_max_age_secs(self):
        """known_bound works cleanly with flight providers that do not declare max_age_secs."""
        calendar = {("ATH", "PRG"): {"2026-11-12": 850.0}}
        provider = _DummyFlightProviderNoMaxAge(calendar=calendar)
        run = self._make_run(flight_provider=provider, observations=None)
        bound = run.known_bound(("ATH", "ATH"), ("PRG", "PRG"), "2026-11-12")
        self.assertEqual(bound, 850.0)


if __name__ == "__main__":
    unittest.main()
