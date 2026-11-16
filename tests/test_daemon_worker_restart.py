"""TKP-8: daemon worker remembers last scan time across restarts via Store."""
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from server_v2 import _run_daemon_worker
from travel_engine.storage import Store, set_meta


class DaemonWorkerRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="daemon_worker_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.store = Store(self.db_path)
        self.tracker = SimpleNamespace(watches={"w": SimpleNamespace(active=True)})
        self.daemon_mock = mock.MagicMock()
        self.t0 = datetime(2026, 11, 20, 12, 0)
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def test_restarts_within_scan_interval_skip_scan_and_rescan_after_interval(self):
        # (kırmızı) İlk başlatma tarar; 5 dk, 1 sa ve 5 sa sonraki üç "yeniden başlatma" taramaz; 6 sa 1 dk sonra tarar.
        # Run 1: Initial start at t0 -> should scan
        _run_daemon_worker(
            tracker_inst=self.tracker,
            daemon_inst=self.daemon_mock,
            now_fn=lambda: self.t0,
            initial_delay=0,
            tick_secs=0,
            max_ticks=1,
            sleep_fn=lambda _: None,
            state_store=self.store,
        )
        self.assertEqual(self.daemon_mock.scan_all_watches.call_count, 1)
        self.daemon_mock.scan_all_watches.reset_mock()

        # Run 2: 5 minutes later -> should NOT scan
        _run_daemon_worker(
            tracker_inst=self.tracker,
            daemon_inst=self.daemon_mock,
            now_fn=lambda: self.t0 + timedelta(minutes=5),
            initial_delay=0,
            tick_secs=0,
            max_ticks=1,
            sleep_fn=lambda _: None,
            state_store=self.store,
        )
        self.daemon_mock.scan_all_watches.assert_not_called()

        # Run 3: 1 hour later -> should NOT scan
        _run_daemon_worker(
            tracker_inst=self.tracker,
            daemon_inst=self.daemon_mock,
            now_fn=lambda: self.t0 + timedelta(hours=1),
            initial_delay=0,
            tick_secs=0,
            max_ticks=1,
            sleep_fn=lambda _: None,
            state_store=self.store,
        )
        self.daemon_mock.scan_all_watches.assert_not_called()

        # Run 4: 5 hours later -> should NOT scan
        _run_daemon_worker(
            tracker_inst=self.tracker,
            daemon_inst=self.daemon_mock,
            now_fn=lambda: self.t0 + timedelta(hours=5),
            initial_delay=0,
            tick_secs=0,
            max_ticks=1,
            sleep_fn=lambda _: None,
            state_store=self.store,
        )
        self.daemon_mock.scan_all_watches.assert_not_called()

        # Run 5: 6 hours 1 min later -> should scan
        _run_daemon_worker(
            tracker_inst=self.tracker,
            daemon_inst=self.daemon_mock,
            now_fn=lambda: self.t0 + timedelta(hours=6, minutes=1),
            initial_delay=0,
            tick_secs=0,
            max_ticks=1,
            sleep_fn=lambda _: None,
            state_store=self.store,
        )
        self.assertEqual(self.daemon_mock.scan_all_watches.call_count, 1)

    def test_without_state_store_every_restart_scans(self):
        # (koruma) state_store verilmezse her başlatma tarar.
        _run_daemon_worker(
            tracker_inst=self.tracker,
            daemon_inst=self.daemon_mock,
            now_fn=lambda: self.t0,
            initial_delay=0,
            tick_secs=0,
            max_ticks=1,
            sleep_fn=lambda _: None,
            state_store=None,
        )
        self.assertEqual(self.daemon_mock.scan_all_watches.call_count, 1)
        self.daemon_mock.scan_all_watches.reset_mock()

        _run_daemon_worker(
            tracker_inst=self.tracker,
            daemon_inst=self.daemon_mock,
            now_fn=lambda: self.t0 + timedelta(minutes=5),
            initial_delay=0,
            tick_secs=0,
            max_ticks=1,
            sleep_fn=lambda _: None,
            state_store=None,
        )
        self.assertEqual(self.daemon_mock.scan_all_watches.call_count, 1)

    def test_corrupt_meta_value_falls_back_to_scan_without_error(self):
        # (kırmızı) Meta değeri bozuksa ("abc") tarar, hata yükselmez.
        with self.store.transaction() as conn:
            set_meta(conn, "daemon:last_scan_at", "abc")

        _run_daemon_worker(
            tracker_inst=self.tracker,
            daemon_inst=self.daemon_mock,
            now_fn=lambda: self.t0,
            initial_delay=0,
            tick_secs=0,
            max_ticks=1,
            sleep_fn=lambda _: None,
            state_store=self.store,
        )
        self.assertEqual(self.daemon_mock.scan_all_watches.call_count, 1)


if __name__ == "__main__":
    unittest.main()
