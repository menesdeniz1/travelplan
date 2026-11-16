"""Tests for ObservationStore and RouteEngine observations integration (C01)."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from travel_engine.airports import AirportDB
from travel_engine.observations import ObservationStore
from travel_engine.route_engine import RouteEngine

DAY = "2026-11-12"


def _flight(airline, times, price, route="ATH ➔ PRG"):
    dep, arr = [t.strip() for t in times.split("–")]
    return {
        "airline": airline,
        "times": times,
        "departure_time": dep,
        "arrival_time": arr,
        "duration": "2h",
        "stops": "Aktarmasız",
        "route": route,
        "airports": route.replace("➔", "-"),
        "is_direct": True,
        "layover_info": None,
        "total_price_try": float(price),
        "currency": "TRY",
        "trip_type": "Tek yön",
        "badge": None,
        "google_flights_url": "https://www.google.com/travel/flights?q=test",
    }


class FakeFlights:
    def __init__(self, flights=None):
        self.flights = flights or []

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        return [dict(item) for item in self.flights]


class FakeGround:
    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        return 300.0

    def find_ground(self, *args, **kwargs):
        return []

    def query(self, *args, **kwargs):
        return []


class ObservationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "obs_test.db")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_record_and_min_price(self):
        store = ObservationStore(self.db_path)
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(days=61)

        # 61 gün önceki kayıt (pencere dışı)
        store.record("ATH", "PRG", DAY, 800.0, 1, observed_at=old_time)
        # Pencere içindeki iki kayıt
        store.record("ATH", "PRG", DAY, 1500.0, 2, observed_at=now - timedelta(days=2))
        store.record("ATH", "PRG", DAY, 1200.0, 3, observed_at=now - timedelta(days=1))

        # min_price: aynı rota ve tarih için iki kayıt -> en düşüğü (1200.0); 61 gün önceki (800.0) pencere dışında
        min_p = store.min_price("ATH", "PRG", DAY, max_age_days=60)
        self.assertEqual(min_p, 1200.0)

        # 90 günlük pencere verildiğinde 61 gün önceki de dahil olmalı
        min_p_90 = store.min_price("ATH", "PRG", DAY, max_age_days=90)
        self.assertEqual(min_p_90, 800.0)

        # Kayıt olmayan rota None dönmeli
        self.assertIsNone(store.min_price("IST", "PRG", DAY))
        store.close()

    def test_prune_removes_old_and_keeps_new(self):
        store = ObservationStore(self.db_path)
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(days=65)

        store.record("ATH", "PRG", DAY, 800.0, 1, observed_at=old_time)
        store.record("ATH", "PRG", DAY, 1200.0, 2, observed_at=now)

        deleted = store.prune(max_age_days=60)
        self.assertEqual(deleted, 1)

        # Eski silindiği için 90 günlük pencerede dahi 800.0 artık yok
        min_p = store.min_price("ATH", "PRG", DAY, max_age_days=100)
        self.assertEqual(min_p, 1200.0)
        store.close()

    def test_observations_disabled_when_off(self):
        old_env = os.environ.get("TRAVELPLAN_OBSERVATIONS")
        try:
            os.environ["TRAVELPLAN_OBSERVATIONS"] = "off"
            store = ObservationStore()
            store.record("ATH", "PRG", DAY, 1000.0, 1)
            self.assertIsNone(store.min_price("ATH", "PRG", DAY))
            self.assertEqual(store.prune(max_age_days=60), 0)
        finally:
            if old_env is not None:
                os.environ["TRAVELPLAN_OBSERVATIONS"] = old_env
            else:
                os.environ.pop("TRAVELPLAN_OBSERVATIONS", None)


class RouteEngineObservationsTests(unittest.TestCase):
    def setUp(self):
        self.airport_db = AirportDB.load_default()

    def test_engine_ingest_records_multiple_airport_pairs(self):
        mock_store = MagicMock()
        # İki farklı havalimanı çifti: ATH-PRG (2 kart) ve SKG-PRG (1 kart)
        flight_cards = [
            _flight("Aegean", "10:00 – 12:00", 1500.0, "ATH ➔ PRG"),
            _flight("Ryanair", "14:00 – 16:00", 1100.0, "ATH ➔ PRG"),
            _flight("Wizz", "09:00 – 11:00", 950.0, "SKG ➔ PRG"),
        ]
        flights = FakeFlights(flight_cards)
        engine = RouteEngine(
            flights,
            FakeGround(),
            airport_db=self.airport_db,
            observations=mock_store,
        )

        res = engine.search("Atina", "Prag", [DAY], max_stops=1)
        self.assertIn("options", res)

        # mock_store.record çağrılarını kontrol et
        calls = mock_store.record.call_args_list
        self.assertGreaterEqual(len(calls), 2)

        # ATH -> PRG: min_price 1100.0, card_count 2
        ath_prg_call = next(
            (c for c in calls if (c.kwargs.get("origin") == "ATH" or (c.args and c.args[0] == "ATH"))
             and (c.kwargs.get("destination") == "PRG" or (len(c.args) > 1 and c.args[1] == "PRG"))),
            None,
        )
        self.assertIsNotNone(ath_prg_call, f"ATH->PRG not found in {calls}")
        if ath_prg_call.kwargs:
            self.assertEqual(ath_prg_call.kwargs["min_price"], 1100.0)
            self.assertEqual(ath_prg_call.kwargs["card_count"], 2)
            self.assertEqual(ath_prg_call.kwargs["travel_date"], DAY)
        else:
            self.assertEqual(ath_prg_call.args[3], 1100.0)
            self.assertEqual(ath_prg_call.args[4], 2)
            self.assertEqual(ath_prg_call.args[2], DAY)

        # SKG -> PRG: min_price 950.0, card_count 1
        skg_prg_call = next(
            (c for c in calls if (c.kwargs.get("origin") == "SKG" or (c.args and c.args[0] == "SKG"))
             and (c.kwargs.get("destination") == "PRG" or (len(c.args) > 1 and c.args[1] == "PRG"))),
            None,
        )
        self.assertIsNotNone(skg_prg_call, f"SKG->PRG not found in {calls}")
        if skg_prg_call.kwargs:
            self.assertEqual(skg_prg_call.kwargs["min_price"], 950.0)
            self.assertEqual(skg_prg_call.kwargs["card_count"], 1)
            self.assertEqual(skg_prg_call.kwargs["travel_date"], DAY)
        else:
            self.assertEqual(skg_prg_call.args[3], 950.0)
            self.assertEqual(skg_prg_call.args[4], 1)
            self.assertEqual(skg_prg_call.args[2], DAY)

    def test_engine_ingest_fault_tolerant_on_store_error(self):
        failing_store = MagicMock()
        failing_store.record.side_effect = RuntimeError("database disk image is malformed")

        flight_cards = [
            _flight("Aegean", "10:00 – 12:00", 1500.0, "ATH ➔ PRG"),
        ]
        flights = FakeFlights(flight_cards)
        engine = RouteEngine(
            flights,
            FakeGround(),
            airport_db=self.airport_db,
            observations=failing_store,
        )

        # Depo hata fırlatsa bile arama düşmemeli ve sonuç dönmeli
        res = engine.search("Atina", "Prag", [DAY], max_stops=0)
        self.assertIn("options", res)
        self.assertGreaterEqual(len(res["options"]), 1)
