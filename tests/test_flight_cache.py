"""Persistent flight cache (travel_engine/flight_cache.py): storage policy, TTLs, pruning, paths. No network."""

import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from travel_engine import flight_cache as fc
from travel_engine.flight_cache import CACHEABLE_STATUSES, FlightCache, default_path, route_key
from travel_engine import storage


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


ROUTE = route_key("Athens", "Prague", "oneway")
CARD = {"airline": "Test Air", "total_price_try": 4200.0, "is_direct": True}


class _TempDirCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fc_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "flight_cache.db")
        self.clock = FakeClock()

    def cache(self, **kwargs):
        kwargs.setdefault("clock", self.clock)
        return FlightCache(self.path, **kwargs)


class StoragePolicyTests(_TempDirCase):
    def test_database_file_is_created_on_first_use_not_on_construction(self):
        cache = self.cache()
        self.assertFalse(os.path.exists(self.path))
        self.assertIsNone(cache.get_results(ROUTE, "2026-12-15"))
        self.assertTrue(os.path.exists(self.path))

    def test_ok_result_round_trips_with_its_status(self):
        cache = self.cache()
        self.assertTrue(cache.put_results(ROUTE, "2026-12-15", "ok", [CARD]))
        hit = cache.get_results(ROUTE, "2026-12-15")
        self.assertEqual(hit["status"], "ok")
        self.assertEqual(hit["results"], [CARD])

    def test_no_flights_is_cached_with_an_empty_list(self):
        cache = self.cache()
        self.assertTrue(cache.put_results(ROUTE, "2026-12-15", "no_flights", []))
        hit = cache.get_results(ROUTE, "2026-12-15")
        self.assertEqual(hit, {"status": "no_flights", "results": [], "stored_at": self.clock.now})

    def test_failed_reads_are_never_stored(self):
        cache = self.cache()
        for status in ("blocked", "parse_error", "empty_page", "error", "unavailable", "blocked_cooldown", "busy"):
            self.assertNotIn(status, CACHEABLE_STATUSES)
            self.assertFalse(cache.put_results(ROUTE, "2026-12-15", status, [CARD]), status)
        self.assertIsNone(cache.get_results(ROUTE, "2026-12-15"))
        self.assertEqual(cache.row_count(), 0)

    def test_other_dates_and_routes_are_separate_entries(self):
        cache = self.cache()
        cache.put_results(ROUTE, "2026-12-15", "ok", [CARD])
        self.assertIsNone(cache.get_results(ROUTE, "2026-12-16"))
        self.assertIsNone(cache.get_results(route_key("Athens", "Prague", "roundtrip:2026-12-20"), "2026-12-15"))

    def test_route_key_ignores_case_and_surrounding_spaces(self):
        self.assertEqual(route_key(" Athens ", "PRAGUE", "oneway"), route_key("athens", "prague", "oneway"))
        self.assertNotEqual(route_key("Athens", "Prague", "oneway"), route_key("Athens", "Prague", "roundtrip:x"))

    def test_turkish_characters_survive_the_json_round_trip(self):
        cache = self.cache()
        card = dict(CARD, airline="Türk Hava Yolları / İzmir")
        cache.put_results(ROUTE, "2026-12-15", "ok", [card])
        self.assertEqual(cache.get_results(ROUTE, "2026-12-15")["results"][0]["airline"], "Türk Hava Yolları / İzmir")

    def test_same_key_is_replaced_by_the_newer_result(self):
        cache = self.cache()
        cache.put_results(ROUTE, "2026-12-15", "ok", [CARD])
        cache.put_results(ROUTE, "2026-12-15", "ok", [dict(CARD, total_price_try=3900.0)])
        self.assertEqual(cache.get_results(ROUTE, "2026-12-15")["results"][0]["total_price_try"], 3900.0)
        self.assertEqual(cache.row_count(), 1)


class TtlTests(_TempDirCase):
    def test_ok_entry_expires_after_the_flight_ttl(self):
        cache = self.cache(ttl_secs=45 * 60)
        cache.put_results(ROUTE, "2026-12-15", "ok", [CARD])
        self.clock.now += 45 * 60 - 1
        self.assertIsNotNone(cache.get_results(ROUTE, "2026-12-15"))
        self.clock.now += 2
        self.assertIsNone(cache.get_results(ROUTE, "2026-12-15"))

    def test_no_flights_entry_uses_the_short_ttl(self):
        cache = self.cache(ttl_secs=45 * 60, no_flights_ttl_secs=10 * 60)
        cache.put_results(ROUTE, "2026-12-15", "no_flights", [])
        self.clock.now += 9 * 60
        self.assertIsNotNone(cache.get_results(ROUTE, "2026-12-15"))
        self.clock.now += 61
        self.assertIsNone(cache.get_results(ROUTE, "2026-12-15"))

    def test_ttl_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {fc.TTL_ENV: "120"}):
            cache = self.cache(ttl_secs=None)
        self.assertEqual(cache.ttl_secs, 120.0)
        with mock.patch.dict(os.environ, {fc.TTL_ENV: "not-a-number"}):
            self.assertEqual(self.cache(ttl_secs=None).ttl_secs, float(fc.DEFAULT_TTL_SECS))

    def test_max_rows_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {fc.MAX_ROWS_ENV: "7"}):
            self.assertEqual(self.cache(max_rows=None).max_rows, 7)
        self.assertEqual(self.cache(max_rows=0).max_rows, 1)


class CalendarTests(_TempDirCase):
    def test_calendar_round_trip_and_replacement(self):
        cache = self.cache()
        self.assertEqual(cache.get_calendar(ROUTE), {})
        self.assertTrue(cache.put_calendar(ROUTE, {"2026-12-15": 4200.0, "2026-12-16": 3900.0}))
        self.assertEqual(cache.get_calendar(ROUTE), {"2026-12-15": 4200.0, "2026-12-16": 3900.0})
        cache.put_calendar(ROUTE, {"2026-12-20": 5000.0})
        self.assertEqual(cache.get_calendar(ROUTE), {"2026-12-20": 5000.0})

    def test_empty_calendar_is_not_stored(self):
        cache = self.cache()
        self.assertFalse(cache.put_calendar(ROUTE, {}))
        self.assertEqual(cache.get_calendar(ROUTE), {})

    def test_calendar_expires_with_the_flight_ttl(self):
        cache = self.cache(ttl_secs=600)
        cache.put_calendar(ROUTE, {"2026-12-15": 4200.0})
        self.clock.now += 599
        self.assertEqual(cache.get_calendar(ROUTE), {"2026-12-15": 4200.0})
        self.clock.now += 2
        self.assertEqual(cache.get_calendar(ROUTE), {})


class PruneTests(_TempDirCase):
    def test_expired_rows_are_deleted_on_prune(self):
        cache = self.cache(ttl_secs=600, no_flights_ttl_secs=60)
        cache.put_results(ROUTE, "2026-12-15", "no_flights", [])
        cache.put_results(ROUTE, "2026-12-16", "ok", [CARD])
        cache.put_calendar(ROUTE, {"2026-12-15": 4200.0})
        self.clock.now += 120
        cache.prune()
        self.assertEqual(cache.row_count(), 1)
        self.assertIsNotNone(cache.get_results(ROUTE, "2026-12-16"))
        self.clock.now += 600
        cache.prune()
        self.assertEqual(cache.row_count(), 0)
        self.assertEqual(cache.get_calendar(ROUTE), {})

    def test_oldest_rows_go_first_when_over_max_rows(self):
        cache = self.cache(max_rows=3)
        for day in range(1, 6):
            self.clock.now += 1
            cache.put_results(ROUTE, f"2026-12-{day:02d}", "ok", [CARD])
        cache.prune()
        self.assertEqual(cache.row_count(), 3)
        self.assertIsNone(cache.get_results(ROUTE, "2026-12-01"))
        self.assertIsNone(cache.get_results(ROUTE, "2026-12-02"))
        self.assertIsNotNone(cache.get_results(ROUTE, "2026-12-05"))

    def test_prune_runs_every_fifty_writes(self):
        cache = self.cache(max_rows=10_000)
        with mock.patch.object(cache, "prune", wraps=cache.prune) as spy:
            for day in range(fc.PRUNE_EVERY_WRITES):
                cache.put_results(ROUTE, f"d{day}", "no_flights", [])
        self.assertEqual(spy.call_count, 1)


class FailureTests(_TempDirCase):
    def test_unusable_database_path_is_a_miss_not_an_error(self):
        os.makedirs(self.path)  # a directory where the database file should be
        cache = self.cache()
        self.assertIsNone(cache.get_results(ROUTE, "2026-12-15"))
        self.assertFalse(cache.put_results(ROUTE, "2026-12-15", "ok", [CARD]))
        self.assertEqual(cache.get_calendar(ROUTE), {})
        self.assertFalse(cache.put_calendar(ROUTE, {"2026-12-15": 1.0}))

    def test_non_json_values_are_stored_as_text(self):
        cache = self.cache()
        self.assertTrue(cache.put_results(ROUTE, "2026-12-15", "ok", [{"price": object()}]))  # default=str
        self.assertIsNotNone(cache.get_results(ROUTE, "2026-12-15"))

    def test_concurrent_writers_do_not_lose_rows(self):
        cache = self.cache(max_rows=10_000)
        errors = []

        def writer(start):
            try:
                for day in range(start, start + 20):
                    cache.put_results(ROUTE, f"w{day}", "no_flights", [])
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n * 20,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(cache.row_count(), 80)


class PathTests(unittest.TestCase):
    def test_explicit_environment_path_wins(self):
        with mock.patch.dict(os.environ, {fc.PATH_ENV: r"D:\somewhere\cache.db"}):
            self.assertEqual(default_path(), r"D:\somewhere\cache.db")

    def test_disabled_values_return_no_path(self):
        for value in ("off", "OFF", "0", "none", "false"):
            with mock.patch.dict(os.environ, {fc.PATH_ENV: value}):
                self.assertEqual(default_path(), "", value)

    def test_test_runs_default_to_a_temporary_directory(self):
        env = {k: v for k, v in os.environ.items() if k != fc.PATH_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            path = default_path()
        self.assertEqual(os.path.dirname(path), storage.test_run_temp_dir())
        repo = os.path.dirname(os.path.dirname(os.path.abspath(fc.__file__)))
        self.assertFalse(os.path.abspath(path).startswith(repo))

    def test_normal_runs_default_to_the_repo_root(self):
        env = {k: v for k, v in os.environ.items() if k != fc.PATH_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fc, "is_test_run", return_value=False):
            path = default_path()
        repo = os.path.dirname(os.path.dirname(os.path.abspath(fc.__file__)))
        self.assertEqual(path, os.path.join(repo, "flight_cache.db"))


if __name__ == "__main__":
    unittest.main()
