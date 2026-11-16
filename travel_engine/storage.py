"""SQLite storage for watches, price history and alerts.

One database file per data directory: ``<dir of the legacy JSON path>/travelplan.db``.
The ``TRAVELPLAN_DB`` environment variable overrides that path. The three classes
(PriceTracker, PriceHistoryManager, AlertNotifier) keep their JSON path arguments;
the path only decides which directory the database lives in.

Concurrency:
- Threads in one process are serialised by a per-database RLock.
- Processes (server, daemon, bot) are serialised by SQLite itself: WAL journal mode,
  ``busy_timeout`` and ``BEGIN IMMEDIATE`` write transactions.
- On top of that, ``BEGIN``, ``COMMIT``, reads and schema setup are retried with jittered
  backoff while SQLite still reports "database is locked" or "database is busy" (see
  ``retry_on_busy``), bounded by ``RETRY_TOTAL_S``. A transaction is never re-run: only its
  BEGIN and COMMIT are retried, so a read-modify-write inside ``Store.transaction`` stays atomic.

A connection is opened for each operation and closed afterwards. A handle held open
keeps the database file locked on Windows, which would stop temporary directories
from being removed.

Legacy JSON files are imported once per table (see ``import_legacy_json``). The JSON
file is renamed to ``<name>.migrated.bak`` afterwards, never deleted.

Test isolation: when the process is a test run and ``TRAVELPLAN_DB`` is not set, the
database goes to a per-process temporary directory instead of the repo root, the search
log does too (see ``server_v2``), and legacy JSON migration is disabled. This makes a
plain ``python -m unittest discover tests`` safe even though ``tests/conftest.py`` (which
only pytest loads) is skipped. A test run is detected by ``is_test_run``.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

logger = logging.getLogger(__name__)

DB_FILENAME = "travelplan.db"
DB_ENV_VAR = "TRAVELPLAN_DB"
# SQLite waits this long inside one attempt before reporting "database is locked".
BUSY_TIMEOUT_MS = 10_000
# Total time one storage call may spend retrying a lock, across all attempts.
RETRY_TOTAL_S = 10.0
RETRY_BASE_DELAY_S = 0.02
RETRY_MAX_DELAY_S = 0.5
SCHEMA_VERSION = "1"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS watches ("
    "  id TEXT PRIMARY KEY,"
    "  data TEXT NOT NULL,"
    "  updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS price_history ("
    "  key TEXT NOT NULL,"
    "  ts TEXT,"
    "  price REAL,"
    "  airline TEXT,"
    "  data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS price_history_key ON price_history (key)",
    "CREATE TABLE IF NOT EXISTS alerts ("
    "  id TEXT PRIMARY KEY,"
    "  ts TEXT,"
    "  data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta ("
    "  key TEXT PRIMARY KEY,"
    "  value TEXT)",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


NO_MIGRATE_ENV_VAR = "TRAVELPLAN_NO_MIGRATE"


TEST_RUNNER_NAMES = ("unittest", "pytest")


def is_test_run() -> bool:
    """True when this process was started as a test runner (pytest or unittest).

    pytest is recognised by being imported (nothing in the app imports it). unittest is
    recognised from the entry point: ``python -m unittest ...`` runs
    ``.../unittest/__main__.py`` (backslashes on Windows), so a path segment or file
    stem named ``unittest`` is in ``sys.argv[0]``. Also recognises ``-m unittest`` in
    ``sys.argv``, whitespace-separated ``unittest`` in ``sys.argv[0]`` (e.g.
    ``python -m unittest``), and ``sys.argv[0]`` without extension named ``unittest``
    when ``unittest.main`` or ``unittest.runner`` is loaded.
    """
    if "pytest" in sys.modules:
        return True

    argv = sys.argv or []
    argv0 = argv[0] if argv else ""

    # 1. sys.argv contains "-m" followed by "unittest"
    for i in range(len(argv) - 1):
        if argv[i] == "-m" and argv[i + 1] == "unittest":
            return True

    # 2. sys.argv[0] text contains "unittest" as a whitespace-separated token (e.g. "python -m unittest")
    if any(token.lower() == "unittest" for token in argv0.split()):
        return True

    # 3. unittest.main or unittest.runner module is loaded while sys.argv[0] without extension is "unittest"
    stem0 = os.path.splitext(os.path.basename(argv0))[0].lower()
    if stem0 == "unittest" and ("unittest.main" in sys.modules or "unittest.runner" in sys.modules):
        return True

    # 4. Existing path-segment / filename detection (pytest, unittest/__main__.py etc.)
    names = set()
    for part in re.split(r"[\\/]", argv0):
        if part:
            names.add(os.path.splitext(part)[0].lower())
    return any(name in TEST_RUNNER_NAMES for name in names)



_test_run_dir: Optional[str] = None
_test_run_db_paths: Dict[str, str] = {}
_test_run_lock = threading.Lock()


def _test_run_dir_locked() -> str:
    global _test_run_dir
    if _test_run_dir is None or not os.path.isdir(_test_run_dir):
        _test_run_dir = tempfile.mkdtemp(prefix=f"travelplan_test_{os.getpid()}_")
        atexit.register(shutil.rmtree, _test_run_dir, True)
    return _test_run_dir


def test_run_temp_dir() -> str:
    """Per-process temporary directory for an isolated test run (created once, removed at exit)."""
    with _test_run_lock:
        return _test_run_dir_locked()


def _isolated_db_path(legacy_json_path: str) -> str:
    """Temp database for one legacy path in an isolated test run.

    Each distinct legacy path gets its own file, as each data directory does in
    production. A test that works in its own temporary directory therefore starts
    with an empty database, and the same path always returns the same file within
    the run, so restart-style tests still see their data.
    """
    key = os.path.abspath(legacy_json_path)
    with _test_run_lock:
        path = _test_run_db_paths.get(key)
        if path is None:
            path = os.path.join(_test_run_dir_locked(), f"db_{len(_test_run_db_paths) + 1}.db")
            _test_run_db_paths[key] = path
        return path


def isolate_for_tests() -> bool:
    """True when this test run must not touch the data directory: a test run with no ``TRAVELPLAN_DB``.

    An explicit ``TRAVELPLAN_DB`` always wins, so a developer who points a test run at a
    database on purpose gets exactly that file.
    """
    return is_test_run() and not os.environ.get(DB_ENV_VAR, "").strip()


def migration_disabled() -> bool:
    """True when legacy JSON must neither be read nor renamed.

    That is ``TRAVELPLAN_NO_MIGRATE=1`` (or true/yes), and also any isolated test run.
    """
    if os.environ.get(NO_MIGRATE_ENV_VAR, "").strip().lower() in ("1", "true", "yes"):
        return True
    return isolate_for_tests()


def resolve_db_path(legacy_json_path: str) -> str:
    """Database path for a store whose legacy JSON file is ``legacy_json_path``.

    Order: ``TRAVELPLAN_DB``; then, in an isolated test run, a file in the per-process
    temp directory (one per legacy path); otherwise the directory of the legacy JSON path.
    """
    override = os.environ.get(DB_ENV_VAR, "").strip()
    if override:
        return os.path.abspath(override)
    if isolate_for_tests():
        return _isolated_db_path(legacy_json_path)
    return os.path.join(os.path.dirname(os.path.abspath(legacy_json_path)), DB_FILENAME)


_BUSY_MESSAGES = ("database is locked", "database is busy", "database table is locked")


def is_busy_error(exc: BaseException) -> bool:
    """True for the SQLite errors that mean "another connection holds the lock, try again"."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return any(text in message for text in _BUSY_MESSAGES)


def retry_on_busy(operation: Callable[[], Any], label: str = "database operation") -> Any:
    """Run ``operation``; while SQLite reports a lock, retry with jittered exponential backoff.

    Every attempt already waits up to ``BUSY_TIMEOUT_MS`` inside SQLite. This loop is the
    backstop that keeps the whole call within ``RETRY_TOTAL_S``: after that the last error
    is raised. Other errors are raised at once. ``operation`` must be safe to repeat, which
    holds for BEGIN, COMMIT, reads and the idempotent schema setup.
    """
    deadline = time.monotonic() + RETRY_TOTAL_S
    delay = RETRY_BASE_DELAY_S
    attempts = 0
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_busy_error(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Giving up on %s after %d retries: %s", label, attempts, exc)
                raise
            attempts += 1
            pause = min(delay, RETRY_MAX_DELAY_S) * random.uniform(0.5, 1.5)
            logger.debug("%s is locked (%s); retry %d in %.3fs", label, exc, attempts, pause)
            time.sleep(min(pause, remaining))
            delay = min(delay * 2, RETRY_MAX_DELAY_S)


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """Roll back an open transaction without masking the error that caused it."""
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error as exc:  # pragma: no cover - only if the connection is already broken
        logger.debug("ROLLBACK failed (%s); closing the connection discards the transaction", exc)


class Store:
    """Access to one SQLite database file. Safe to share between threads."""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        retry_on_busy(self._create_schema, "schema setup")

    def _create_schema(self) -> None:
        """Set WAL and create the tables in one transaction. Idempotent, so a retry is safe."""
        conn = self._open()
        try:
            # WAL is persistent for the file, so the switch is only needed once. Checking
            # first means a process that finds WAL already set takes no extra lock.
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
                conn.execute("COMMIT")
            except BaseException:
                _rollback_quietly(conn)
                raise
        finally:
            conn.close()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
            isolation_level=None,  # transactions are managed explicitly below
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run the block in one write transaction. Rolls back if the block raises.

        BEGIN and COMMIT are retried while the database is locked. The block itself runs
        once, so reads and writes inside it are atomic with respect to other processes.
        The connection is closed on every path.
        """
        with self._lock:
            conn = self._open()
            try:
                retry_on_busy(lambda: conn.execute("BEGIN IMMEDIATE"), "write transaction")
                try:
                    yield conn
                except BaseException:
                    _rollback_quietly(conn)
                    raise
                try:
                    # A COMMIT that reports "locked" leaves the transaction open, so it can be retried.
                    retry_on_busy(lambda: conn.execute("COMMIT"), "commit")
                except BaseException:
                    _rollback_quietly(conn)
                    raise
            finally:
                conn.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[tuple]:
        with self._lock:
            conn = self._open()
            try:
                return retry_on_busy(lambda: conn.execute(sql, params).fetchall(), "read")
            finally:
                conn.close()

    def get_meta(self, key: str) -> Optional[str]:
        rows = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None


_stores: Dict[str, Store] = {}
_stores_lock = threading.Lock()


def get_store(db_path: str) -> Store:
    """Shared Store for a database path (one per path per process)."""
    key = os.path.abspath(db_path)
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = Store(key)
            _stores[key] = store
        return store


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def _retire_legacy_file(path: str) -> None:
    """Rename an imported JSON file to ``<path>.migrated.bak``. Never overwrites a backup."""
    target = f"{path}.migrated.bak"
    if os.path.exists(target):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = f"{path}.migrated-{stamp}.bak"
    try:
        os.replace(path, target)
        logger.info("Imported %s; original kept as %s", path, target)
    except OSError as exc:
        logger.warning(
            "Imported %s but could not rename it (%s). It is no longer read, "
            "but please move or delete it by hand.",
            path, exc,
        )


def import_legacy_json(
    store: Store,
    name: str,
    json_path: str,
    table: str,
    parse: Callable[[Any], Any],
    insert_rows: Callable[[sqlite3.Connection, Any], int],
) -> bool:
    """Import a legacy JSON file into ``table``, at most once per ``name``.

    The import runs only when the table has no rows yet. It is recorded in ``meta``
    as ``migrated:<name>`` in the same transaction as the inserts, so a second start
    does nothing. If the table already has rows, the file is left in place and the
    import is recorded as skipped, so an old file cannot overwrite newer data.

    A file that cannot be parsed is logged, left in place and not recorded, so the
    import is retried on the next start. Returns True when rows were imported.

    With ``TRAVELPLAN_NO_MIGRATE=1`` this does nothing at all: the file is not read
    and not renamed.
    """
    if migration_disabled():
        return False
    meta_key = f"migrated:{name}"
    if not os.path.exists(json_path):
        return False
    if store.get_meta(meta_key) is not None:
        return False

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            parsed = parse(json.load(f))
    except Exception as exc:
        logger.warning(
            "Legacy %s file %s could not be read (%s). Leaving it in place; starting empty.",
            name, json_path, exc,
        )
        return False

    try:
        with store.transaction() as conn:
            if get_meta(conn, meta_key) is not None:
                return False
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                set_meta(conn, meta_key, f"skipped: {table} already had rows at {utc_now_iso()}")
                imported = None
            else:
                count = insert_rows(conn, parsed)
                set_meta(conn, meta_key, f"imported {count} rows at {utc_now_iso()}")
                imported = count
    except Exception:
        logger.exception("Importing legacy %s file %s failed; it stays in place", name, json_path)
        return False

    if imported is None:
        logger.warning(
            "%s already has rows in %s, so legacy file %s was not imported and is left in place.",
            table, store.db_path, json_path,
        )
        return False
    logger.info("Imported %d %s record(s) from %s", imported, name, json_path)
    _retire_legacy_file(json_path)
    return True
