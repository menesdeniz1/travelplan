"""Tests for 24-hour calendar retention in FlightCache and GoogleFlightsProvider (C02)."""

import os
import tempfile
import unittest

from travel_engine.flight_cache import CALENDAR_RETENTION_SECS, FlightCache, route_key
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider


class CalendarRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "fc_test.db")
        self.current_time = 1_000_000.0

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _clock(self) -> float:
        return self.current_time

    def test_calendar_two_hours_old_returns_empty_by_default_and_prices_with_max_age(self):
        cache = FlightCache(self.db_path, clock=self._clock)
        rkey = route_key("ATH", "PRG", "oneway")
        cache.put_calendar(rkey, {"2026-11-12": 1500.0})

        # Saat 2 saat ileri alınır (2 * 3600 sn)
        self.current_time += 2 * 3600

        # Varsayılan okuma (max_age_secs=None): 45 dk TTL dolduğu için {}
        self.assertEqual(cache.get_calendar(rkey), {})

        # max_age_secs=24*3600 ile okuma: 24 saat içinde olduğu için fiyatlar gelir
        self.assertEqual(cache.get_calendar(rkey, max_age_secs=24 * 3600), {"2026-11-12": 1500.0})

        # GoogleFlightsProvider üzerinden kontrol
        provider = GoogleFlightsProvider(flight_cache=cache)
        self.assertEqual(provider.get_calendar_prices("ATH", "PRG"), {})
        self.assertEqual(
            provider.get_calendar_prices("ATH", "PRG", max_age_secs=24 * 3600),
            {"2026-11-12": 1500.0},
        )

    def test_calendar_25_hours_old_expired_and_pruned_keeps_2h_old(self):
        cache = FlightCache(self.db_path, clock=self._clock)
        rkey_old = route_key("ATH", "PRG", "oneway")
        cache.put_calendar(rkey_old, {"2026-11-12": 1500.0})

        # 23 saat sonra yeni bir rota takvimi yazılır
        self.current_time += 23 * 3600
        rkey_new = route_key("IST", "PRG", "oneway")
        cache.put_calendar(rkey_new, {"2026-11-13": 2100.0})

        # 2 saat daha geçer: rkey_old 25 saatlik, rkey_new 2 saatlik olur
        self.current_time += 2 * 3600

        # 25 saatlik takvim hem varsayılan hem max_age=24*3600 okumada {} döner
        self.assertEqual(cache.get_calendar(rkey_old), {})
        self.assertEqual(cache.get_calendar(rkey_old, max_age_secs=24 * 3600), {})

        provider = GoogleFlightsProvider(flight_cache=cache)
        self.assertEqual(provider.get_calendar_prices("ATH", "PRG", max_age_secs=24 * 3600), {})

        # Silme (prune) işi 25 saatlik satırı kaldırır, 2 saatlik satırı bırakır
        cache.prune()

        # Doğrudan DB tablosunu sorgula: ATH_PRG kalmadı, IST_PRG duruyor
        rows_old = cache._read("SELECT travel_date FROM calendar_prices WHERE route = ?", (rkey_old,))
        self.assertEqual(len(rows_old), 0)

        rows_new = cache._read("SELECT travel_date FROM calendar_prices WHERE route = ?", (rkey_new,))
        self.assertEqual(len(rows_new), 1)

        # 2 saatlik takvim max_age_secs=24*3600 ile hâlâ okunabilir
        self.assertEqual(cache.get_calendar(rkey_new, max_age_secs=24 * 3600), {"2026-11-13": 2100.0})
