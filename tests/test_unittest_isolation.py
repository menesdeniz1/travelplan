"""Test-run isolation under plain unittest.

``tests/conftest.py`` is pytest-only, so ``python -m unittest discover tests`` skips it.
The guard in ``travel_engine/storage.py`` (and the search-log path in ``server_v2``) must
therefore protect the repo-root data on its own. These tests check that guard.

Safety: nothing here opens or writes the repo-root database, JSON files or search log.
Subprocess tests import ``server_v2`` with a fake unittest argv, a temporary working
directory and a temporary TMPDIR; they assert that every path they resolve lies inside
that temporary directory. The subprocess also runs with PYTHONDONTWRITEBYTECODE=1 so
the repo root gets no new __pycache__.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from travel_engine import storage
from travel_engine.storage import DB_ENV_VAR, NO_MIGRATE_ENV_VAR, get_store, import_legacy_json

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_ENV_VARS = (DB_ENV_VAR, NO_MIGRATE_ENV_VAR, "TRAVELPLAN_SEARCH_LOG")
UNITTEST_ARGV0 = r"C:\Python312\Lib\unittest\__main__.py"


def _is_inside(path, directory):
    path = os.path.normcase(os.path.abspath(path))
    directory = os.path.normcase(os.path.abspath(directory))
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:  # different drives on Windows: not inside
        return False


def _clean_env(temp_root):
    env = {k: v for k, v in os.environ.items() if k not in DATA_ENV_VARS}
    env.update({"TMPDIR": temp_root, "TEMP": temp_root, "TMP": temp_root,
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


class DetectionTests(unittest.TestCase):
    """is_test_run() reads the entry point, not whatever happens to be imported."""

    def _detect(self, argv, pytest_loaded=False):
        # patch.dict without clear restores sys.modules exactly on exit; pytest itself is hidden
        # for the check, because under pytest it is always imported
        with mock.patch.dict(sys.modules), mock.patch.object(sys, "argv", argv):
            sys.modules.pop("pytest", None)
            if pytest_loaded:
                sys.modules["pytest"] = mock.Mock()
            return storage.is_test_run()

    def test_unittest_entry_points_are_detected(self):
        self.assertTrue(self._detect([UNITTEST_ARGV0, "discover", "tests"]))
        self.assertTrue(self._detect(["/usr/lib/python3.12/unittest/__main__.py", "-v"]))
        self.assertTrue(self._detect(["unittest"]))

    def test_windows_dash_m_unittest_is_detected(self):
        self.assertTrue(self._detect(["-m", "unittest", "discover", "tests"]))
        self.assertTrue(self._detect(["python -m unittest", "discover", "tests"]))
        self.assertTrue(self._detect(["C:\\Python312\\python.exe -m unittest", "discover", "tests"]))

    def test_unittest_main_or_runner_loaded_with_unittest_stem(self):
        with mock.patch.dict(sys.modules), mock.patch.object(sys, "argv", ["unittest"]):
            sys.modules.pop("pytest", None)
            sys.modules["unittest.main"] = mock.Mock()
            self.assertTrue(storage.is_test_run())

        with mock.patch.dict(sys.modules), mock.patch.object(sys, "argv", ["unittest.exe"]):
            sys.modules.pop("pytest", None)
            sys.modules["unittest.runner"] = mock.Mock()
            self.assertTrue(storage.is_test_run())

    def test_pytest_is_detected_by_import(self):
        self.assertTrue(self._detect(["pytest"], pytest_loaded=True))

    def test_app_entry_points_are_not_test_runs(self):
        self.assertFalse(self._detect([r"C:\Users\user\Desktop\travelplan\server_v2.py"]))
        # A test-runner word in the arguments or in a folder name is not an entry point
        self.assertFalse(self._detect([r"C:\work\unittest_notes\server_v2.py", "--name", "pytest"]))
        self.assertFalse(self._detect([]))

    def test_explicit_db_path_turns_isolation_off(self):
        with mock.patch.object(sys, "argv", [UNITTEST_ARGV0]), \
                mock.patch.dict(os.environ, {DB_ENV_VAR: os.path.join(REPO_ROOT, "elsewhere.db")}):
            self.assertTrue(storage.is_test_run())
            self.assertFalse(storage.isolate_for_tests())
            self.assertEqual(storage.resolve_db_path(os.path.join(REPO_ROOT, "watches.json")),
                             os.path.abspath(os.path.join(REPO_ROOT, "elsewhere.db")))


class InProcessGuardTests(unittest.TestCase):
    """The guard applied inside this (already running) test process, with argv patched."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(DB_ENV_VAR, None)
        os.environ.pop(NO_MIGRATE_ENV_VAR, None)
        os.environ.pop("TRAVELPLAN_SEARCH_LOG", None)  # conftest.py sets it under pytest
        argv = mock.patch.object(sys, "argv", [UNITTEST_ARGV0, "discover", "tests"])
        argv.start()
        self.addCleanup(argv.stop)

    def test_isolated_run_resolves_db_outside_the_repo_root(self):
        legacy = os.path.join(REPO_ROOT, "watches.json")
        db_path = storage.resolve_db_path(legacy)
        self.assertTrue(storage.isolate_for_tests())
        self.assertTrue(_is_inside(db_path, storage.test_run_temp_dir()))
        self.assertFalse(_is_inside(db_path, REPO_ROOT))

    def test_each_legacy_path_gets_its_own_temp_db_and_it_is_stable(self):
        first = storage.resolve_db_path(os.path.join(REPO_ROOT, "watches.json"))
        other = storage.resolve_db_path(os.path.join(REPO_ROOT, "price_history.json"))
        self.assertNotEqual(first, other)
        self.assertEqual(first, storage.resolve_db_path(os.path.join(REPO_ROOT, "watches.json")))

    def test_isolated_run_disables_legacy_migration_and_keeps_the_json(self):
        self.assertTrue(storage.migration_disabled())
        tmp = tempfile.mkdtemp(prefix="isolation_legacy_")
        self.addCleanup(shutil.rmtree, tmp, True)
        legacy = os.path.join(tmp, "watches.json")
        with open(legacy, "w", encoding="utf-8") as f:
            json.dump([{"id": "w1"}], f)
        store = get_store(storage.resolve_db_path(legacy))
        imported = import_legacy_json(store, "watches", legacy, "watches",
                                      parse=lambda data: data, insert_rows=lambda conn, rows: len(rows))
        self.assertFalse(imported)
        self.assertTrue(os.path.exists(legacy))
        self.assertFalse(os.path.exists(legacy + ".migrated.bak"))

    def test_search_log_default_is_a_temp_file_in_an_isolated_run(self):
        import server_v2
        self.assertTrue(_is_inside(server_v2._default_search_log_path(), storage.test_run_temp_dir()))


class SubprocessImportTests(unittest.TestCase):
    """Fresh interpreters, so module-level path setup in server_v2 really runs."""

    @classmethod
    def setUpClass(cls):
        cls.temp_root = tempfile.mkdtemp(prefix="isolation_child_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_root, ignore_errors=True)

    def _run_child(self, code):
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=self.temp_root,
            env=_clean_env(self.temp_root),
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr[-2000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_import_server_v2_under_unittest_uses_temp_paths(self):
        code = "\n".join([
            "import sys, json",
            f"sys.argv = [{UNITTEST_ARGV0!r}, 'discover', 'tests']",
            f"sys.path.insert(0, {REPO_ROOT!r})",
            "import server_v2",
            "from travel_engine import storage",
            "print(json.dumps({",
            "  'db': server_v2.tracker.db_path,",
            "  'alerts_db': server_v2.notifier._store.db_path,",
            "  'search_log': server_v2.SEARCH_LOG_PATH,",
            "  'no_migrate': storage.migration_disabled(),",
            "}))",
        ])
        paths = self._run_child(code)
        self.assertTrue(paths["no_migrate"])
        for key in ("db", "alerts_db", "search_log"):
            self.assertTrue(_is_inside(paths[key], self.temp_root), msg=f"{key}={paths[key]}")
            self.assertFalse(_is_inside(paths[key], REPO_ROOT), msg=f"{key}={paths[key]}")
        self.assertEqual(os.listdir(self.temp_root), [], "child should remove its temp dir at exit")

    def test_app_entry_point_still_uses_repo_root_paths(self):
        # Only the pure path rule runs here (no database is opened)
        code = "\n".join([
            "import sys, json",
            f"sys.argv = [{os.path.join(REPO_ROOT, 'server_v2.py')!r}]",
            f"sys.path.insert(0, {REPO_ROOT!r})",
            "from travel_engine import storage",
            f"legacy = {os.path.join(REPO_ROOT, 'watches.json')!r}",
            "print(json.dumps({'db': storage.resolve_db_path(legacy), 'no_migrate': storage.migration_disabled()}))",
        ])
        paths = self._run_child(code)
        self.assertEqual(paths["db"], os.path.join(REPO_ROOT, "travelplan.db"))
        self.assertFalse(paths["no_migrate"])

    def test_subprocess_server_v2_not_mistaken_for_test_run(self):
        code = "\n".join([
            "import sys, json",
            "sys.argv = ['server_v2.py']",
            f"sys.path.insert(0, {REPO_ROOT!r})",
            "from travel_engine import storage",
            "print(json.dumps({",
            "  'is_test_run': storage.is_test_run(),",
            "  'no_migrate': storage.migration_disabled(),",
            "}))",
        ])
        res = self._run_child(code)
        self.assertFalse(res["is_test_run"])
        self.assertFalse(res["no_migrate"])

    def test_dash_c_script_still_uses_repo_root_db(self):
        # A non-test process running via python -c must resolve to repo root travelplan.db
        code = "\n".join([
            "import sys, json",
            "sys.argv = ['-c']",
            f"sys.path.insert(0, {REPO_ROOT!r})",
            "from travel_engine import storage",
            f"legacy = {os.path.join(REPO_ROOT, 'watches.json')!r}",
            "print(json.dumps({",
            "  'db': storage.resolve_db_path(legacy),",
            "  'is_test': storage.is_test_run(),",
            "}))",
        ])
        res = self._run_child(code)
        self.assertFalse(res["is_test"])
        self.assertEqual(res["db"], os.path.join(REPO_ROOT, "travelplan.db"))


if __name__ == "__main__":
    unittest.main()

