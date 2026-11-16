"""SQLite storage: migration from the legacy JSON files, persistence, caps, concurrency."""

import glob
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest import mock

from travel_engine import notifier as notifier_module
from travel_engine import storage as storage_module
from travel_engine.notifier import AlertNotifier
from travel_engine.price_history import PriceHistoryManager
from travel_engine.storage import (
    DB_ENV_VAR, NO_MIGRATE_ENV_VAR, Store, get_store, resolve_db_path, retry_on_busy,
)
from travel_engine.tracker import PriceTracker

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stress_writer(db_path, legacy_json, origin, count):
    """Body of one spawned writer process: ``count`` real price-history writes on one route.

    The database path is set explicitly, so the child does not depend on how the test
    runner was started (is_test_run looks at argv, which a spawned child inherits).
    """
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    os.environ[DB_ENV_VAR] = db_path
    os.environ.pop(NO_MIGRATE_ENV_VAR, None)
    mgr = PriceHistoryManager(storage_path=legacy_json)
    for i in range(count):
        mgr.record_price(origin, "Prag", "2026-10-27", "oneway", 1000.0 + i, airline="X")

LEGACY_OLD_WATCH = {
    "id": "watch_old_1",
    "origin": "VIE",
    "destination": "ATH",
    "target_budget_try": 4000.0,
    "departure_date": "2026-10-25",
    "history": [{"timestamp": "2026-10-01T10:00:00+00:00", "price_try": 4500.0}],
}
LEGACY_TRIP_WATCH = {
    "id": "watch_trip_2",
    "origin": "ATH",
    "destination": "PRG",
    "target_budget_try": 7000.0,
    "departure_date": "2026-10-27",
    "trip_type": "roundtrip",
    "direct_budget_try": 10000.0,
    "trip_query": {"depart_dates": ["2026-10-27", "2026-10-28"], "arrive_by": "18:00",
                   "via": ["VIE"], "include_side_routes": True},
    "last_trip_alert": {"direct": 9500.0, "side": 6500.0},
    "history": [],
}


def _history_point(price, airline="Aegean", minute=0):
    return {
        "timestamp": datetime(2026, 10, 20, 9, minute).isoformat(),
        "time_label": f"09:{minute:02d}",
        "date_label": "20.10",
        "price": float(price),
        "airline": airline,
    }


class StorageTestBase(unittest.TestCase):
    def setUp(self):
        # Keep the real environment (a Telegram token, a DB override) out of these tests.
        # conftest.py sets TRAVELPLAN_DB and TRAVELPLAN_NO_MIGRATE for the whole session;
        # these tests exercise the real database path resolution and the JSON migration,
        # so both are cleared here for the duration of each test only.
        env = mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(DB_ENV_VAR, None)
        os.environ.pop(NO_MIGRATE_ENV_VAR, None)
        # Test-run isolation (temp DB, no migration) is covered by test_unittest_isolation.py.
        # These tests need the production path rules, so detection is switched off here.
        detect = mock.patch("travel_engine.storage.is_test_run", return_value=False)
        detect.start()
        self.addCleanup(detect.stop)

        tmp = tempfile.TemporaryDirectory(prefix="storage_test_")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.watches_json = os.path.join(self.dir, "watches.json")
        self.history_json = os.path.join(self.dir, "price_history.json")
        self.alerts_json = os.path.join(self.dir, "alerts.json")
        self.config_json = os.path.join(self.dir, "config.json")
        self.db = os.path.join(self.dir, "travelplan.db")

    def write_json(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            if isinstance(data, str):
                f.write(data)
            else:
                json.dump(data, f)

    def read_text(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def db_rows(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, params).fetchall()

    def make_alerts(self):
        return AlertNotifier(alerts_file=self.alerts_json, config_file=self.config_json)


class TestStoreBasics(StorageTestBase):
    def test_database_is_created_next_to_legacy_path_in_wal_mode(self):
        PriceTracker(storage_path=self.watches_json)

        self.assertTrue(os.path.exists(self.db))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"watches", "price_history", "alerts", "meta"} <= tables)

    def test_resolve_db_path_uses_directory_of_legacy_path(self):
        self.assertEqual(resolve_db_path(self.watches_json), self.db)

    def test_env_override_selects_database_file(self):
        other = os.path.join(self.dir, "custom", "override.db")
        with mock.patch.dict(os.environ, {DB_ENV_VAR: other}):
            tracker = PriceTracker(storage_path=self.watches_json)
            tracker.add_watch("ATH", "PRG", 5000.0, watch_id="w_env")

        self.assertEqual(tracker.db_path, os.path.abspath(other))
        self.assertTrue(os.path.exists(other))
        self.assertFalse(os.path.exists(self.db))

    def test_store_is_shared_per_path(self):
        self.assertIs(get_store(self.db), get_store(os.path.join(self.dir, ".", "travelplan.db")))

    def test_legacy_json_files_are_never_written(self):
        tracker = PriceTracker(storage_path=self.watches_json)
        tracker.add_watch("ATH", "PRG", 5000.0, watch_id="w1")
        PriceHistoryManager(storage_path=self.history_json).record_price(
            "Atina", "Prag", "2026-10-27", "oneway", 9000.0)
        self.make_alerts()._save_to_alerts_file({"id": "a1", "timestamp": "t"})

        self.assertFalse(os.path.exists(self.watches_json))
        self.assertFalse(os.path.exists(self.history_json))
        self.assertFalse(os.path.exists(self.alerts_json))


class TestWatchesStore(StorageTestBase):
    def test_watch_round_trip_keeps_trip_query_fields(self):
        tracker = PriceTracker(storage_path=self.watches_json)
        watch = tracker.add_watch(
            "ATH", "PRG", 7000.0,
            trip_type="roundtrip",
            direct_budget_try=10000.0,
            current_price=8000.0,
            watch_id="watch_trip_1",
            trip_query=LEGACY_TRIP_WATCH["trip_query"],
        )
        watch.last_trip_alert = {"direct": 9500.0, "side": 6500.0}
        tracker.save()

        reloaded = PriceTracker(storage_path=self.watches_json).get_watch("watch_trip_1")

        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.trip_query, LEGACY_TRIP_WATCH["trip_query"])
        self.assertEqual(reloaded.direct_budget_try, 10000.0)
        self.assertEqual(reloaded.target_budget_try, 7000.0)
        self.assertEqual(reloaded.trip_type, "roundtrip")
        self.assertEqual(reloaded.departure_date, "2026-10-27")
        self.assertEqual(reloaded.last_trip_alert, {"direct": 9500.0, "side": 6500.0})
        self.assertEqual([r.price_try for r in reloaded.history], [8000.0])

    def test_delete_is_persisted(self):
        tracker = PriceTracker(storage_path=self.watches_json)
        tracker.add_watch("ATH", "PRG", 5000.0, watch_id="w_keep")
        tracker.add_watch("ATH", "VIE", 5000.0, watch_id="w_drop")

        self.assertTrue(tracker.delete_watch("w_drop"))

        self.assertEqual(sorted(PriceTracker(storage_path=self.watches_json).watches), ["w_keep"])

    def test_concurrent_add_watch_from_threads_loses_nothing(self):
        tracker = PriceTracker(storage_path=self.watches_json)
        errors = []

        def worker(n):
            try:
                for i in range(10):
                    tracker.add_watch("ATH", "PRG", 5000.0, watch_id=f"w_{n}_{i}")
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(tracker.watches), 80)
        self.assertEqual(len(PriceTracker(storage_path=self.watches_json).watches), 80)

    def test_unreadable_stored_row_is_skipped_and_others_load(self):
        tracker = PriceTracker(storage_path=self.watches_json)
        tracker.add_watch("ATH", "PRG", 5000.0, watch_id="w_good")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO watches (id, data, updated_at) VALUES (?, ?, ?)",
                         ("w_bad", "{not json", "t"))
            conn.commit()

        with self.assertLogs("travel_engine.tracker", level="WARNING"):
            fresh = PriceTracker(storage_path=self.watches_json)

        self.assertEqual(sorted(fresh.watches), ["w_good"])


class TestWatchesMigration(StorageTestBase):
    def legacy_watches(self):
        return {w["id"]: w for w in (LEGACY_OLD_WATCH, LEGACY_TRIP_WATCH)}

    def test_legacy_watches_json_is_imported_and_renamed(self):
        self.write_json(self.watches_json, self.legacy_watches())
        original = self.read_text(self.watches_json)

        tracker = PriceTracker(storage_path=self.watches_json)

        self.assertEqual(sorted(tracker.watches), ["watch_old_1", "watch_trip_2"])
        old = tracker.get_watch("watch_old_1")
        self.assertIsNone(old.trip_query)
        self.assertEqual([r.price_try for r in old.history], [4500.0])
        trip = tracker.get_watch("watch_trip_2")
        self.assertEqual(trip.trip_query, LEGACY_TRIP_WATCH["trip_query"])
        self.assertEqual(trip.last_trip_alert, {"direct": 9500.0, "side": 6500.0})

        self.assertFalse(os.path.exists(self.watches_json))
        backup = self.watches_json + ".migrated.bak"
        self.assertEqual(self.read_text(backup), original)

    def test_no_migrate_switch_leaves_legacy_file_untouched(self):
        self.write_json(self.watches_json, self.legacy_watches())
        original = self.read_text(self.watches_json)

        with mock.patch.dict(os.environ, {NO_MIGRATE_ENV_VAR: "1"}):
            tracker = PriceTracker(storage_path=self.watches_json)

        self.assertEqual(tracker.watches, {})
        self.assertEqual(self.read_text(self.watches_json), original)
        self.assertEqual(glob.glob(os.path.join(self.dir, "watches.json*")), [self.watches_json])

    def test_second_start_does_nothing(self):
        self.write_json(self.watches_json, self.legacy_watches())
        PriceTracker(storage_path=self.watches_json)
        rows_after_first = self.db_rows("SELECT id, data FROM watches ORDER BY id")

        second = PriceTracker(storage_path=self.watches_json)

        self.assertEqual(sorted(second.watches), ["watch_old_1", "watch_trip_2"])
        self.assertEqual(self.db_rows("SELECT id, data FROM watches ORDER BY id"), rows_after_first)
        self.assertEqual(glob.glob(os.path.join(self.dir, "watches.json*")),
                         [self.watches_json + ".migrated.bak"])

    def test_legacy_file_appearing_after_migration_is_ignored(self):
        self.write_json(self.watches_json, self.legacy_watches())
        PriceTracker(storage_path=self.watches_json)

        self.write_json(self.watches_json, {"watch_stale": dict(LEGACY_OLD_WATCH, id="watch_stale")})
        again = PriceTracker(storage_path=self.watches_json)

        self.assertNotIn("watch_stale", again.watches)
        self.assertTrue(os.path.exists(self.watches_json))

    def test_existing_rows_block_import_and_legacy_file_is_kept(self):
        tracker = PriceTracker(storage_path=self.watches_json)
        tracker.add_watch("ATH", "PRG", 5000.0, watch_id="w_db")
        self.write_json(self.watches_json, self.legacy_watches())

        with self.assertLogs("travel_engine.storage", level="WARNING"):
            fresh = PriceTracker(storage_path=self.watches_json)

        self.assertEqual(sorted(fresh.watches), ["w_db"])
        self.assertTrue(os.path.exists(self.watches_json))

        # Emptying the table later must not bring the old file back.
        fresh.delete_watch("w_db")
        later = PriceTracker(storage_path=self.watches_json)
        self.assertEqual(later.watches, {})

    def test_existing_backup_is_never_overwritten(self):
        backup = self.watches_json + ".migrated.bak"
        self.write_json(backup, "PREVIOUS BACKUP")
        self.write_json(self.watches_json, self.legacy_watches())

        PriceTracker(storage_path=self.watches_json)

        self.assertEqual(self.read_text(backup), "PREVIOUS BACKUP")
        self.assertEqual(len(glob.glob(os.path.join(self.dir, "watches.json.migrated*"))), 2)

    def test_corrupt_legacy_watches_is_kept_and_retried_later(self):
        corrupt = "[1, 2"
        self.write_json(self.watches_json, corrupt)

        with self.assertLogs("travel_engine.storage", level="WARNING"):
            tracker = PriceTracker(storage_path=self.watches_json)

        self.assertEqual(tracker.watches, {})
        self.assertEqual(self.read_text(self.watches_json), corrupt)
        self.assertEqual(glob.glob(os.path.join(self.dir, "watches.json*.bak")), [])

        self.write_json(self.watches_json, self.legacy_watches())
        repaired = PriceTracker(storage_path=self.watches_json)
        self.assertEqual(sorted(repaired.watches), ["watch_old_1", "watch_trip_2"])


class TestPriceHistoryStore(StorageTestBase):
    def test_recorded_points_survive_restart(self):
        mgr = PriceHistoryManager(storage_path=self.history_json)
        mgr.record_price("Atina", "Prag", "2026-10-27", "oneway", 10984.0, airline="SKY express")
        mgr.record_price("Atina", "Prag", "2026-10-27", "oneway", 9800.0, airline="KLM")

        reloaded = PriceHistoryManager(storage_path=self.history_json)

        self.assertEqual(reloaded.history, mgr.history)
        found = reloaded.find_recorded_price("ATH", "PRG", "2026-10-27", "oneway")
        self.assertEqual(found, (9800.0, "KLM"))

    def test_only_newest_50_points_per_route_are_kept(self):
        mgr = PriceHistoryManager(storage_path=self.history_json)
        for i in range(60):
            mgr.record_price("Atina", "Prag", "2026-10-27", "oneway", 1000.0 + i, airline="X")

        key = next(iter(mgr.history))
        self.assertEqual(len(mgr.history[key]), 50)
        self.assertEqual(mgr.history[key][-1]["price"], 1059.0)
        self.assertEqual(self.db_rows("SELECT COUNT(*) FROM price_history")[0][0], 50)
        reloaded = PriceHistoryManager(storage_path=self.history_json)
        self.assertEqual([p["price"] for p in reloaded.history[key]], [1000.0 + i for i in range(10, 60)])

    def test_legacy_price_history_json_is_imported_and_renamed(self):
        key = "atina_prag_2026-10-27_oneway"
        legacy = {key: [_history_point(12000.0, "Aegean", 0), _history_point(11500.0, "Aegean", 5)]}
        self.write_json(self.history_json, legacy)
        original = self.read_text(self.history_json)

        mgr = PriceHistoryManager(storage_path=self.history_json)

        self.assertEqual(mgr.history, legacy)
        self.assertEqual(
            mgr.find_recorded_price("ATH", "PRG", "2026-10-27", "oneway"), (11500.0, "Aegean"))
        self.assertFalse(os.path.exists(self.history_json))
        self.assertEqual(self.read_text(self.history_json + ".migrated.bak"), original)

    def test_second_start_after_price_history_import_is_idempotent(self):
        key = "atina_prag_2026-10-27_oneway"
        self.write_json(self.history_json, {key: [_history_point(12000.0), _history_point(11500.0, minute=5)]})
        PriceHistoryManager(storage_path=self.history_json)

        second = PriceHistoryManager(storage_path=self.history_json)

        self.assertEqual(len(second.history[key]), 2)
        self.assertEqual(self.db_rows("SELECT COUNT(*) FROM price_history")[0][0], 2)

    def test_corrupt_legacy_price_history_starts_empty_and_is_kept(self):
        self.write_json(self.history_json, "{broken")

        with self.assertLogs("travel_engine.storage", level="WARNING"):
            mgr = PriceHistoryManager(storage_path=self.history_json)

        self.assertEqual(mgr.history, {})
        self.assertEqual(self.read_text(self.history_json), "{broken")

    def test_points_from_separate_processes_are_all_kept(self):
        # Each child process records 20 points for its own route; SQLite must serialise them.
        script = (
            "import sys\n"
            f"sys.path.insert(0, {REPO_ROOT!r})\n"
            "from travel_engine.price_history import PriceHistoryManager\n"
            "n = int(sys.argv[1])\n"
            f"mgr = PriceHistoryManager(storage_path={self.history_json!r})\n"
            "for i in range(20):\n"
            "    mgr.record_price(f'Origin{n}', 'Prag', '2026-10-27', 'oneway', 1000.0 + i, airline='X')\n"
        )
        env = dict(os.environ)
        env.pop(DB_ENV_VAR, None)
        env.pop(NO_MIGRATE_ENV_VAR, None)
        procs = [
            subprocess.Popen([sys.executable, "-c", script, str(n)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for n in range(3)
        ]
        for p in procs:
            _, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace"))

        counts = dict(self.db_rows("SELECT key, COUNT(*) FROM price_history GROUP BY key"))
        self.assertEqual(sorted(counts.values()), [20, 20, 20])


class TestConcurrentWrites(StorageTestBase):
    def test_four_processes_writing_fifty_points_each_lose_nothing(self):
        # spawn, not fork: each child starts clean, as the real server and daemon do.
        ctx = multiprocessing.get_context("spawn")
        processes, writes = 4, 50
        with mock.patch.dict(os.environ):
            os.environ.pop(DB_ENV_VAR, None)
            os.environ.pop(NO_MIGRATE_ENV_VAR, None)
            procs = [
                ctx.Process(target=_stress_writer,
                            args=(self.db, self.history_json, f"Origin{n}", writes))
                for n in range(processes)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=60)
        for p in procs:
            self.assertFalse(p.is_alive(), "writer process did not finish")
        self.assertEqual([p.exitcode for p in procs], [0] * processes)

        counts = dict(self.db_rows("SELECT key, COUNT(*) FROM price_history GROUP BY key"))
        self.assertEqual(sorted(counts.values()), [writes] * processes)
        self.assertEqual(self.db_rows("SELECT COUNT(*) FROM price_history")[0][0], processes * writes)

    def test_write_waits_out_a_lock_held_longer_than_one_busy_timeout(self):
        store = Store(self.db)
        holder = sqlite3.connect(self.db, timeout=0, check_same_thread=False, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        # Each attempt gives up after 50 ms, so the write only succeeds through the retry loop.
        release = threading.Timer(0.5, lambda: holder.execute("COMMIT"))
        try:
            with mock.patch.object(storage_module, "BUSY_TIMEOUT_MS", 50):
                release.start()
                with store.transaction() as conn:
                    conn.execute("INSERT INTO meta (key, value) VALUES ('waited', 'yes')")
        finally:
            release.join()
            holder.close()

        self.assertEqual(store.get_meta("waited"), "yes")

    def test_lock_that_never_clears_fails_within_the_retry_budget(self):
        store = Store(self.db)
        holder = sqlite3.connect(self.db, timeout=0, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(storage_module, "BUSY_TIMEOUT_MS", 20), \
                    mock.patch.object(storage_module, "RETRY_TOTAL_S", 0.5):
                started = time.monotonic()
                with self.assertLogs("travel_engine.storage", level="WARNING"):
                    with self.assertRaises(sqlite3.OperationalError):
                        with store.transaction() as conn:
                            conn.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
                elapsed = time.monotonic() - started
        finally:
            holder.execute("ROLLBACK")
            holder.close()

        self.assertLess(elapsed, 5.0)
        # The failed write left nothing behind, and the store works again once the lock is gone.
        self.assertIsNone(store.get_meta("x"))
        with store.transaction() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
        self.assertEqual(store.get_meta("x"), "y")


class TestRetryHelper(StorageTestBase):
    def test_busy_error_is_retried_until_it_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with mock.patch.object(storage_module.time, "sleep"):
            self.assertEqual(retry_on_busy(flaky, "test"), "ok")
        self.assertEqual(len(calls), 3)

    def test_busy_message_variant_is_retried(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise sqlite3.OperationalError("database is busy")
            return 1

        with mock.patch.object(storage_module.time, "sleep"):
            self.assertEqual(retry_on_busy(flaky, "test"), 1)

    def test_other_errors_are_raised_at_once(self):
        calls = []

        def broken():
            calls.append(1)
            raise sqlite3.OperationalError("no such table: nope")

        with self.assertRaises(sqlite3.OperationalError):
            retry_on_busy(broken, "test")
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_the_total_budget(self):
        calls = []

        def always_locked():
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(storage_module, "RETRY_TOTAL_S", 0.2), \
                self.assertLogs("travel_engine.storage", level="WARNING"):
            with self.assertRaises(sqlite3.OperationalError):
                retry_on_busy(always_locked, "test")
        self.assertGreater(len(calls), 1)

    def test_failed_transaction_rolls_back_and_releases_the_lock(self):
        store = Store(self.db)
        with self.assertRaises(ValueError):
            with store.transaction() as conn:
                conn.execute("INSERT INTO meta (key, value) VALUES ('half', 'written')")
                raise ValueError("boom")

        self.assertIsNone(store.get_meta("half"))
        # A second writer gets the lock straight away, so the first one closed its connection.
        other = sqlite3.connect(self.db, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
        finally:
            other.close()


class TestAlertsStore(StorageTestBase):
    def test_alerts_are_newest_first_and_capped_at_50(self):
        n = self.make_alerts()
        for i in range(60):
            n._save_to_alerts_file({"id": f"a{i}", "timestamp": f"t{i}", "seq": i})

        alerts = n.get_alerts()
        self.assertEqual(len(alerts), 50)
        self.assertEqual(alerts[0]["seq"], 59)
        self.assertEqual(alerts[-1]["seq"], 10)
        self.assertEqual(self.make_alerts().get_alerts(), alerts)
        self.assertEqual(self.db_rows("SELECT COUNT(*) FROM alerts")[0][0], 50)

    def test_clear_alerts_empties_store_and_later_saves_work(self):
        n = self.make_alerts()
        n._save_to_alerts_file({"id": "a1", "timestamp": "t"})

        n.clear_alerts()
        self.assertEqual(n.get_alerts(), [])
        self.assertEqual(self.make_alerts().get_alerts(), [])

        n._save_to_alerts_file({"id": "a2", "timestamp": "t"})
        self.assertEqual([a["id"] for a in n.get_alerts()], ["a2"])

    def test_system_message_is_stored_as_alert(self):
        n = self.make_alerts()

        self.assertFalse(n.send_system_message("scraper back online"))

        stored = self.make_alerts().get_alerts()
        self.assertEqual(stored[0]["type"], "system")
        self.assertEqual(stored[0]["message"], "scraper back online")

    def test_concurrent_alert_saves_from_threads_lose_nothing(self):
        n = self.make_alerts()

        def worker(k):
            for i in range(5):
                n._save_to_alerts_file({"id": f"t{k}_{i}", "timestamp": "t"})

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(n.get_alerts()), 40)

    def test_legacy_alerts_json_is_imported_in_order_and_renamed(self):
        legacy = [{"id": f"alert_{i}", "timestamp": f"t{i}", "read": False} for i in (3, 2, 1)]
        self.write_json(self.alerts_json, legacy)
        original = self.read_text(self.alerts_json)

        n = self.make_alerts()

        self.assertEqual(n.get_alerts(), legacy)
        self.assertFalse(os.path.exists(self.alerts_json))
        self.assertEqual(self.read_text(self.alerts_json + ".migrated.bak"), original)

        # Second start is a no-op, and new alerts go on top.
        second = self.make_alerts()
        self.assertEqual(second.get_alerts(), legacy)
        second._save_to_alerts_file({"id": "alert_new", "timestamp": "t9"})
        self.assertEqual(self.make_alerts().get_alerts()[0]["id"], "alert_new")

    def test_legacy_alerts_over_limit_keep_the_newest_50(self):
        legacy = [{"id": f"alert_{i}", "timestamp": "t"} for i in range(55)]
        self.write_json(self.alerts_json, legacy)

        alerts = self.make_alerts().get_alerts()

        self.assertEqual([a["id"] for a in alerts], [f"alert_{i}" for i in range(50)])
        self.assertEqual(len(json.loads(self.read_text(self.alerts_json + ".migrated.bak"))), 55)

    def test_legacy_alerts_without_unique_ids_get_new_ids(self):
        legacy = [{"timestamp": "t1"}, {"id": "dup", "timestamp": "t2"}, {"id": "dup", "timestamp": "t3"}]
        self.write_json(self.alerts_json, legacy)

        alerts = self.make_alerts().get_alerts()

        ids = [a["id"] for a in alerts]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        self.assertIn("dup", ids)

    def test_corrupt_legacy_alerts_is_kept_and_starts_empty(self):
        for corrupt in ("{broken", json.dumps({"not": "a list"})):
            with self.subTest(corrupt=corrupt):
                self.write_json(self.alerts_json, corrupt)
                with self.assertLogs("travel_engine.storage", level="WARNING"):
                    n = self.make_alerts()
                self.assertEqual(n.get_alerts(), [])
                self.assertEqual(self.read_text(self.alerts_json), corrupt)
                os.remove(self.alerts_json)

    def test_default_alerts_path_is_module_constant_directory(self):
        with mock.patch.object(notifier_module, "ALERTS_FILE", self.alerts_json):
            n = AlertNotifier(config_file=self.config_json)

        self.assertEqual(n.db_path, self.db)


if __name__ == "__main__":
    unittest.main()
