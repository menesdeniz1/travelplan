"""Tests for search runs table, logging, and health endpoint integration (A05)."""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from travel_engine.search_log import SearchLog
import server_v2


class SearchRunsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "search_log.db")
        self.log = SearchLog(self.db_path)

    def tearDown(self):
        self.log.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_record_run_and_recent_runs_roundtrip(self):
        trace1 = {
            "total_secs": 1.25,
            "counters": {"flight_searches": 4, "google_page_loads": 1},
        }
        trace2 = {
            "total_secs": 0.85,
            "counters": {"flight_searches": 2, "google_page_loads": 0},
        }

        self.log.record_run("IST", "ATH", ["2026-10-25"], trace1)
        self.log.record_run("SAW", "PRG", ["2026-10-26", "2026-10-27"], trace2)

        runs = self.log.recent_runs(limit=10)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["origin"], "SAW")
        self.assertEqual(runs[0]["destination"], "PRG")
        self.assertEqual(runs[0]["depart_dates"], ["2026-10-26", "2026-10-27"])
        self.assertEqual(runs[0]["total_secs"], 0.85)
        self.assertEqual(runs[0]["flight_searches"], 2)
        self.assertEqual(runs[0]["google_page_loads"], 0)
        self.assertIsInstance(runs[0]["trace"], dict)
        self.assertEqual(runs[0]["trace"]["total_secs"], 0.85)

        self.assertEqual(runs[1]["origin"], "IST")
        self.assertEqual(runs[1]["destination"], "ATH")
        self.assertEqual(runs[1]["total_secs"], 1.25)
        self.assertEqual(runs[1]["flight_searches"], 4)
        self.assertEqual(runs[1]["google_page_loads"], 1)
        self.assertIsInstance(runs[1]["trace"], dict)

    def test_google_page_loads_since_filters_by_time(self):
        t0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        with mock.patch("travel_engine.search_log._utc_now", return_value=t0):
            self.log.record_run("IST", "ATH", ["2026-10-25"], {"counters": {"google_page_loads": 3}})

        t1 = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
        with mock.patch("travel_engine.search_log._utc_now", return_value=t1):
            self.log.record_run("IST", "ATH", ["2026-10-25"], {"counters": {"google_page_loads": 5}})

        since_iso = datetime(2026, 10, 21, 0, 0, 0, tzinfo=timezone.utc).isoformat(timespec="seconds")
        total_today = self.log.google_page_loads_since(since_iso)
        self.assertEqual(total_today, 5)

        since_earlier = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc).isoformat(timespec="seconds")
        total_all = self.log.google_page_loads_since(since_earlier)
        self.assertEqual(total_all, 8)

    def test_legacy_database_migration_adds_table_and_preserves_searches(self):
        legacy_path = os.path.join(self.tmp_dir, "legacy_search_log.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute("""
            CREATE TABLE searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                searched_at TEXT NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                travel_date TEXT NOT NULL,
                status TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                min_price REAL,
                source TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute(
            "INSERT INTO searches (searched_at, origin, destination, travel_date, status, result_count, min_price, source, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-10-20T10:00:00+00:00", "IST", "ATH", "2026-10-25", "ok", 3, 1500.0, "google_flights", ""),
        )
        conn.commit()
        conn.close()

        migrated_log = SearchLog(legacy_path)
        try:
            recent_searches = migrated_log.recent(limit=10)
            self.assertEqual(len(recent_searches), 1)
            self.assertEqual(recent_searches[0]["origin"], "IST")

            migrated_log.record_run("SAW", "PRG", ["2026-10-26"], {"counters": {"google_page_loads": 1}})
            runs = migrated_log.recent_runs(limit=10)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["origin"], "SAW")
        finally:
            migrated_log.close()

    def test_lazy_search_log_recent_runs_does_not_create_file_when_missing(self):
        missing_db_path = os.path.join(self.tmp_dir, "non_existent.db")
        lazy_log = server_v2._LazySearchLog(missing_db_path)
        self.assertFalse(os.path.exists(missing_db_path))

        runs = lazy_log.recent_runs()
        self.assertEqual(runs, [])
        self.assertFalse(os.path.exists(missing_db_path))

        loads = lazy_log.google_page_loads_since("2026-10-21T00:00:00+00:00")
        self.assertEqual(loads, 0)
        self.assertFalse(os.path.exists(missing_db_path))

    def test_health_payload_contains_search_runs(self):
        payload = server_v2._health_payload()
        self.assertIn("search_runs", payload)
        self.assertIn("recent", payload["search_runs"])
        self.assertIn("google_page_loads_today", payload["search_runs"])
        self.assertIsInstance(payload["search_runs"]["recent"], list)
        self.assertIsInstance(payload["search_runs"]["google_page_loads_today"], int)


if __name__ == "__main__":
    unittest.main()
