import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import travel_engine.search_log as search_log_module
from travel_engine.search_log import SearchLog, normalize_place


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


class NormalizePlaceTests(unittest.TestCase):
    def test_codes_are_uppercased_and_stripped(self):
        self.assertEqual(normalize_place("ath"), "ATH")
        self.assertEqual(normalize_place(" ATH "), "ATH")

    def test_names_are_folded_lowercase(self):
        self.assertEqual(normalize_place("İstanbul"), "istanbul")
        self.assertEqual(normalize_place("Istanbul"), "istanbul")
        self.assertEqual(normalize_place("  Sabiha   Gökçen "), "sabiha gokcen")
        self.assertEqual(normalize_place("Şanlıurfa"), "sanliurfa")


class SearchLogRecordTests(unittest.TestCase):
    def setUp(self):
        self.log = SearchLog(":memory:")

    def tearDown(self):
        self.log.close()

    def test_record_and_recent_newest_first(self):
        self.log.record("ath", "prg", "2026-12-15", "ok", 3, 4830.0)
        self.log.record("ATH", "PRG", "2026-12-16", "no_flights", 0, None, detail="cards=0")
        rows = self.log.recent(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], "2026-12-16")
        self.assertEqual(rows[0]["status"], "no_flights")
        self.assertIsNone(rows[0]["min_price"])
        self.assertEqual(rows[0]["detail"], "cards=0")
        self.assertEqual(rows[1]["origin"], "ATH")
        self.assertEqual(rows[1]["destination"], "PRG")
        self.assertEqual(rows[1]["count"], 3)
        self.assertEqual(rows[1]["min_price"], 4830.0)
        self.assertEqual(rows[1]["source"], "google_flights")
        self.assertTrue(rows[1]["searched_at"].endswith("+00:00"))

    def test_recent_respects_limit(self):
        for _ in range(5):
            self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
        self.assertEqual(len(self.log.recent(limit=2)), 2)

    def test_pair_stats_counts_and_prices(self):
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 5, 100.0)
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 4, 140.0)
        self.log.record("ATH", "PRG", "2026-12-15", "parse_error", 0, None)
        self.log.record("ATH", "PRG", "2026-12-15", "no_flights", 0, None)
        stats = self.log.pair_stats("ath", "prg")
        self.assertEqual(stats["searches"], 4)
        self.assertEqual(stats["ok"], 2)
        self.assertEqual(stats["min_price"], 100.0)
        self.assertEqual(stats["avg_min_price"], 120.0)
        self.assertEqual(stats["last_min_price"], 140.0)
        self.assertIsNotNone(stats["last_at"])

    def test_pair_stats_empty_pair(self):
        stats = self.log.pair_stats("ATH", "NUU")
        self.assertEqual(stats["searches"], 0)
        self.assertEqual(stats["ok"], 0)
        self.assertIsNone(stats["min_price"])
        self.assertIsNone(stats["avg_min_price"])
        self.assertIsNone(stats["last_min_price"])
        self.assertIsNone(stats["last_at"])

    def test_pair_stats_ignores_rows_outside_window_and_other_source(self):
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 50.0, source="google_flights_roundtrip")
        # Old row inserted directly so its timestamp can be backdated.
        self.log._conn.execute(
            "INSERT INTO searches (searched_at, origin, destination, travel_date, status, result_count, "
            "min_price, source, detail) VALUES (?, 'ATH', 'PRG', '2026-01-01', 'ok', 1, 10.0, 'google_flights', '')",
            (_iso_days_ago(200),),
        )
        stats = self.log.pair_stats("ATH", "PRG", days=90)
        self.assertEqual(stats["searches"], 1)
        self.assertEqual(stats["min_price"], 100.0)
        rt = self.log.pair_stats("ATH", "PRG", days=90, source="google_flights_roundtrip")
        self.assertEqual(rt["min_price"], 50.0)

    def test_cheapest_destinations_sorted_unknown_last(self):
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
        self.log.record("ATH", "PRG", "2026-12-16", "ok", 1, 300.0)  # avg 200
        self.log.record("ATH", "VIE", "2026-12-15", "ok", 1, 150.0)  # avg 150
        self.log.record("ATH", "BUD", "2026-12-15", "no_flights", 0, None)  # no priced history
        result = self.log.cheapest_destinations("ath", ["BUD", "prg", "VIE", "XYZ"])
        self.assertEqual([name for name, _ in result], ["VIE", "prg", "BUD", "XYZ"])
        self.assertEqual(result[0][1], 150.0)
        self.assertEqual(result[1][1], 200.0)
        self.assertEqual(result[2][1], float("inf"))
        self.assertEqual(result[3][1], float("inf"))

    def test_cheapest_destinations_dedupes_candidates(self):
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
        result = self.log.cheapest_destinations("ATH", ["PRG", "prg", " PRG "])
        self.assertEqual(len(result), 1)

    def test_price_series_orders_oldest_first_and_filters_date(self):
        self.log._conn.executemany(
            "INSERT INTO searches (searched_at, origin, destination, travel_date, status, result_count, "
            "min_price, source, detail) VALUES (?, 'ATH', 'PRG', ?, 'ok', 1, ?, 'google_flights', '')",
            [
                ("2026-10-19T09:00:00+00:00", "2026-12-15", 120.0),
                ("2026-10-19T08:00:00+00:00", "2026-12-15", 150.0),
                ("2026-10-19T10:00:00+00:00", "2026-12-16", 999.0),  # other date
                ("2026-10-19T11:00:00+00:00", "2026-12-15", None),  # no price
            ],
        )
        series = self.log.price_series("ATH", "PRG", "2026-12-15")
        self.assertEqual(series, [("2026-10-19T08:00:00+00:00", 150.0), ("2026-10-19T09:00:00+00:00", 120.0)])
        self.assertEqual(self.log.price_series("ATH", "PRG", "2030-01-01"), [])

    def test_concurrent_records_are_all_stored(self):
        def worker():
            for _ in range(25):
                self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.log.pair_stats("ATH", "PRG")["searches"], 200)

    def test_close_closes_connection(self):
        self.log.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.log.recent()
        # Recreate so tearDown's close() does not fail on a closed connection.
        self.log = SearchLog(":memory:")


class SearchLogFileTests(unittest.TestCase):
    def test_file_database_persists_and_creates_schema_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "nested" / "searches.sqlite")
            first = SearchLog(path)
            first.record("ATH", "PRG", "2026-12-15", "ok", 2, 3902.0)
            first.close()
            second = SearchLog(path)  # CREATE TABLE IF NOT EXISTS must not wipe data
            try:
                self.assertEqual(second.pair_stats("ATH", "PRG")["min_price"], 3902.0)
            finally:
                second.close()


def _insert_old_rows(log, count, days_ago):
    log._conn.executemany(
        "INSERT INTO searches (searched_at, origin, destination, travel_date, status, result_count, "
        "min_price, source, detail) VALUES (?, 'ATH', 'PRG', '2026-12-15', 'ok', 1, 100.0, 'google_flights', '')",
        [(_iso_days_ago(days_ago),)] * count,
    )
    log._conn.commit()


def _row_count(log):
    return log._conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0]


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.log = SearchLog(":memory:")

    def tearDown(self):
        self.log.close()

    def test_prune_deletes_rows_older_than_max_age(self):
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)  # the automatic prune for today has run
        _insert_old_rows(self.log, 3, days_ago=200)
        deleted = self.log.prune(max_age_days=180, max_rows=50000)
        self.assertEqual(deleted, 3)
        self.assertEqual(_row_count(self.log), 1)

    def test_prune_keeps_only_newest_max_rows(self):
        for _ in range(10):
            self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
        newest_id = self.log.recent(limit=1)[0]["id"]
        deleted = self.log.prune(max_age_days=180, max_rows=4)
        self.assertEqual(deleted, 6)
        rows = self.log.recent(limit=100)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["id"], newest_id)

    def test_prune_with_nothing_to_delete_returns_zero(self):
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
        self.assertEqual(self.log.prune(), 0)

    def test_record_prunes_automatically_at_most_once_a_day(self):
        _insert_old_rows(self.log, 2, days_ago=200)
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)  # first write of the process: prunes
        self.assertEqual(_row_count(self.log), 1)

        _insert_old_rows(self.log, 2, days_ago=200)
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)  # same day: no prune yet
        self.assertEqual(_row_count(self.log), 4)

        later = search_log_module._utc_now() + timedelta(days=1, minutes=1)
        with mock.patch.object(search_log_module, "_utc_now", return_value=later):
            self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
        # The two backdated rows are gone; the two fresh rows and the new one remain.
        self.assertEqual(_row_count(self.log), 3)

    def test_vacuum_runs_after_pruning_more_than_a_thousand_rows(self):
        _insert_old_rows(self.log, 1500, days_ago=200)
        with mock.patch.object(self.log, "vacuum") as vacuum:
            self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
        vacuum.assert_called_once_with()
        self.assertEqual(_row_count(self.log), 1)

    def test_no_vacuum_for_small_prunes(self):
        _insert_old_rows(self.log, 20, days_ago=200)
        with mock.patch.object(self.log, "vacuum") as vacuum:
            self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
        vacuum.assert_not_called()

    def test_explicit_vacuum_is_safe_on_memory_and_file_databases(self):
        self.log.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
        self.log.vacuum()
        self.assertEqual(_row_count(self.log), 1)
        with tempfile.TemporaryDirectory() as tmp:
            disk = SearchLog(str(Path(tmp) / "search_log.db"))
            try:
                disk.record("ATH", "PRG", "2026-12-15", "ok", 1, 100.0)
                disk.prune(max_age_days=1, max_rows=10)
                disk.vacuum()
                self.assertEqual(_row_count(disk), 1)
            finally:
                disk.close()


if __name__ == "__main__":
    unittest.main()
