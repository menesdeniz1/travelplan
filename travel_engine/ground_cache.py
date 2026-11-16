"""Persistent cache of ground (train and bus) answers, one whole day per entry (SQLite).

Why: a route search asks find_ground for many places and earliest departure times, and the same
pair of places on the same day comes back in the next search and in the next process. The live
sources answer per day, so the cache keeps the whole day of one source for one (origin, destination,
date) and the caller filters the departures by its earliest time locally.

What is stored
- ``ground_results``: the segments one source returned for one (source, origin, destination, travel
  date, anchor, coordinates). ``anchor`` is "" for a whole-day entry. Google Maps answers depend on
  the time they are asked from, so their entries carry that time (HH:MM) as the anchor.
  Segments are stored as ``ScheduledSegment.to_dict()`` dictionaries.
- Places are compared case-insensitively with the whitespace collapsed (``norm_place``).

Policy
- Only answers are stored: a list, possibly empty. A failed, unconfigured or unresolvable source is
  never stored (``put(..., None)`` is refused), so an error is retried by the next call.
- A non-empty answer lives TRAVELPLAN_GROUND_CACHE_TTL_SECS (default 6 h). An empty answer lives
  EMPTY_TTL_SECS (1 h).
- Retention: expired rows are deleted on write (at most every PRUNE_EVERY_WRITES writes), then the
  oldest rows beyond TRAVELPLAN_GROUND_CACHE_MAX_ROWS (default 5000).

Location
- TRAVELPLAN_GROUND_CACHE: path of the database; "off" (or 0 / none / false / no) disables the cache.
- Default: <repo root>/ground_cache.db. In a test run each RailBusProvider gets its own file in the
  per-process temporary directory, so tests never share ground data and never write into the repo.
- The file is created on first use, not at import time.

Concurrency: one lock per GroundCache object; each operation opens and closes its own connection
(as flight_cache.py does), with WAL, a busy timeout and storage.retry_on_busy. Any database error is
logged and treated as a miss: the cache never fails a search.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Sequence

from travel_engine.storage import BUSY_TIMEOUT_MS, is_test_run, retry_on_busy, test_run_temp_dir

logger = logging.getLogger(__name__)

PATH_ENV = "TRAVELPLAN_GROUND_CACHE"
TTL_ENV = "TRAVELPLAN_GROUND_CACHE_TTL_SECS"
MAX_ROWS_ENV = "TRAVELPLAN_GROUND_CACHE_MAX_ROWS"
DISABLED_VALUES = ("off", "0", "none", "false", "no")

DEFAULT_TTL_SECS = 6 * 3600
EMPTY_TTL_SECS = 3600
DEFAULT_MAX_ROWS = 5000
PRUNE_EVERY_WRITES = 50

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS ground_results (
        source TEXT NOT NULL,
        origin TEXT NOT NULL,
        dest TEXT NOT NULL,
        travel_date TEXT NOT NULL,
        anchor TEXT NOT NULL,
        coords TEXT NOT NULL,
        payload TEXT NOT NULL,
        count INTEGER NOT NULL,
        stored_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        PRIMARY KEY (source, origin, dest, travel_date, anchor, coords)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ground_results_expiry ON ground_results (expires_at)",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def norm_place(name: object) -> str:
    """Cache form of a place name: trimmed, inner whitespace collapsed, case-folded."""
    return " ".join(str(name or "").split()).casefold()


def coords_text(from_coord: Optional[Sequence[float]], to_coord: Optional[Sequence[float]]) -> str:
    """Cache form of the coordinates given with a query ("" when neither side has one)."""
    if from_coord is None and to_coord is None:
        return ""

    def one(coord: Optional[Sequence[float]]) -> str:
        return "-" if coord is None else f"{float(coord[0]):.4f},{float(coord[1]):.4f}"

    return f"{one(from_coord)}|{one(to_coord)}"


def ttl_for(count: int, ttl_secs: float = DEFAULT_TTL_SECS, empty_ttl_secs: float = EMPTY_TTL_SECS) -> float:
    """Lifetime in seconds of an answer with ``count`` segments."""
    return float(empty_ttl_secs if count <= 0 else ttl_secs)


def default_ttl_for(count: int) -> float:
    """``ttl_for`` with the TRAVELPLAN_GROUND_CACHE_TTL_SECS override."""
    return ttl_for(count, ttl_secs=_env_int(TTL_ENV, DEFAULT_TTL_SECS))


def from_env() -> Optional["GroundCache"]:
    """The default cache for a new provider, or None when TRAVELPLAN_GROUND_CACHE disables it.

    In a test run each call returns a new file in the per-process temporary directory.
    """
    explicit = os.environ.get(PATH_ENV, "").strip()
    if explicit:
        if explicit.lower() in DISABLED_VALUES:
            return None
        return GroundCache(explicit)
    if is_test_run():
        return GroundCache(os.path.join(test_run_temp_dir(), f"ground_cache_{uuid.uuid4().hex}.db"))
    return GroundCache(os.path.join(_REPO_ROOT, "ground_cache.db"))


class GroundCache:
    """SQLite-backed store of ground answers per source and day. Thread-safe."""

    def __init__(
        self,
        path: str,
        ttl_secs: Optional[float] = None,
        empty_ttl_secs: float = EMPTY_TTL_SECS,
        max_rows: Optional[int] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.path = os.path.abspath(path)
        self.ttl_secs = float(ttl_secs if ttl_secs is not None else _env_int(TTL_ENV, DEFAULT_TTL_SECS))
        self.empty_ttl_secs = float(empty_ttl_secs)
        self.max_rows = max(1, int(max_rows if max_rows is not None else _env_int(MAX_ROWS_ENV, DEFAULT_MAX_ROWS)))
        self._clock = clock
        self._lock = threading.RLock()
        self._writes_since_prune = 0
        self._ready = False

    def ttl_for(self, count: int) -> float:
        return ttl_for(count, self.ttl_secs, self.empty_ttl_secs)

    # -- connection and schema ----------------------------------------------

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        retry_on_busy(self._create_schema, "ground cache schema")
        self._ready = True

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
            isolation_level=None,  # explicit transactions below
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        except BaseException:
            conn.close()
            raise
        return conn

    def _create_schema(self) -> None:
        conn = self._open()
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _read(self, sql: str, params: tuple = ()) -> List[tuple]:
        with self._lock:
            self._ensure_ready()
            conn = self._open()
            try:
                return retry_on_busy(lambda: conn.execute(sql, params).fetchall(), "ground cache read")
            finally:
                conn.close()

    def _write(self, statements: List[tuple]) -> None:
        """Run (sql, params) statements in one write transaction."""
        with self._lock:
            self._ensure_ready()
            conn = self._open()
            try:
                retry_on_busy(lambda: conn.execute("BEGIN IMMEDIATE"), "ground cache write")
                try:
                    for sql, params in statements:
                        conn.execute(sql, params)
                    retry_on_busy(lambda: conn.execute("COMMIT"), "ground cache commit")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

    # -- public API -----------------------------------------------------------

    def get(
        self, source: str, origin: str, dest: str, travel_date: str, anchor: str = "", coords: str = "",
    ) -> Optional[List[Dict]]:
        """The stored segment dictionaries (possibly an empty list), or None on a miss or an error."""
        try:
            rows = self._read(
                "SELECT payload FROM ground_results WHERE source = ? AND origin = ? AND dest = ? "
                "AND travel_date = ? AND anchor = ? AND coords = ? AND expires_at > ?",
                (source, norm_place(origin), norm_place(dest), travel_date, anchor, coords, self._clock()),
            )
            if not rows:
                return None
            payload = json.loads(rows[0][0])
            return payload if isinstance(payload, list) else None
        except Exception:
            logger.warning("Ground cache read failed; treating as a miss", exc_info=True)
            return None

    def put(
        self,
        source: str,
        origin: str,
        dest: str,
        travel_date: str,
        segments: Optional[List[Dict]],
        anchor: str = "",
        coords: str = "",
    ) -> bool:
        """Store one answer. ``None`` (a failed or unavailable source) is refused and stores nothing."""
        if segments is None:
            return False
        now = self._clock()
        count = len(segments)
        try:
            payload = json.dumps(segments, ensure_ascii=False, default=str)
            self._write([(
                "INSERT OR REPLACE INTO ground_results (source, origin, dest, travel_date, anchor, coords, "
                "payload, count, stored_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, norm_place(origin), norm_place(dest), travel_date, anchor, coords, payload, count,
                 now, now + self.ttl_for(count)),
            )])
        except Exception:
            logger.warning("Ground cache write failed; answer not cached", exc_info=True)
            return False
        self._after_write()
        return True

    def row_count(self) -> int:
        rows = self._read("SELECT COUNT(*) FROM ground_results")
        return int(rows[0][0]) if rows else 0

    def prune(self) -> None:
        """Delete expired rows, then the oldest rows beyond max_rows."""
        now = self._clock()
        statements = [
            ("DELETE FROM ground_results WHERE expires_at <= ?", (now,)),
            ("DELETE FROM ground_results WHERE rowid IN ("
             "SELECT rowid FROM ground_results ORDER BY stored_at DESC LIMIT -1 OFFSET ?)",
             (self.max_rows,)),
        ]
        try:
            self._write(statements)
        except Exception:
            logger.warning("Ground cache prune failed", exc_info=True)
        with self._lock:
            self._writes_since_prune = 0

    def _after_write(self) -> None:
        with self._lock:
            self._writes_since_prune += 1
            due = self._writes_since_prune >= PRUNE_EVERY_WRITES
        if due:
            self.prune()
