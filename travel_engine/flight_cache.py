"""Persistent cache of parsed Google Flights results (SQLite), the L2 behind the in-memory cache.

Why: a live search opens one Chromium page (seconds to minutes per trip). The same one-way
route and date are asked again by the next search, the daemon and the UI, so their parsed
results and the calendar prices of the route are kept on disk between processes.

What is stored
- ``flight_results``: the ranked results of one live search, per (route, travel date). Route
  is "origin|destination|trip_type" with the normalised query names the provider searches with,
  and trip_type "oneway" or "roundtrip:<return date>".
- ``calendar_prices``: the Google calendar minimum per day of a route (one-way), refreshed by
  every live search of that route.

Policy
- Only "ok" (results found) and "no_flights" are stored. Every other status (blocked, parse
  error, empty page, error, busy) is a failed read and is never cached.
- "ok" lives TRAVELPLAN_FLIGHT_CACHE_TTL_SECS (default 45 min); "no_flights" lives
  NO_FLIGHTS_TTL_SECS (10 min). Calendar prices use the flight TTL.
- Retention: expired rows are deleted on write (at most every PRUNE_EVERY_WRITES writes), then
  the oldest rows beyond TRAVELPLAN_FLIGHT_CACHE_MAX_ROWS (default 5000).

Location
- TRAVELPLAN_FLIGHT_CACHE: path of the database; "off" (or 0 / none / false) disables the cache.
- Default: <repo root>/flight_cache.db. In a test run (storage.is_test_run) the default is a
  per-process temporary directory, so the tests never write into the repo.
- The file is created on first use, not at import time.

Concurrency: one lock per FlightCache object; each operation opens and closes its own connection
(as storage.Store does), with WAL, busy timeout and storage.retry_on_busy for lock contention.
Any database error is logged and treated as a miss: the cache never fails a search.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from travel_engine.storage import BUSY_TIMEOUT_MS, is_test_run, retry_on_busy, test_run_temp_dir

logger = logging.getLogger(__name__)

PATH_ENV = "TRAVELPLAN_FLIGHT_CACHE"
TTL_ENV = "TRAVELPLAN_FLIGHT_CACHE_TTL_SECS"
MAX_ROWS_ENV = "TRAVELPLAN_FLIGHT_CACHE_MAX_ROWS"
DISABLED_VALUES = ("off", "0", "none", "false", "no")

DEFAULT_TTL_SECS = 45 * 60
CALENDAR_RETENTION_SECS = 24 * 3600
NO_FLIGHTS_TTL_SECS = 10 * 60
DEFAULT_MAX_ROWS = 5000
PRUNE_EVERY_WRITES = 50

STATUS_OK = "ok"
STATUS_NO_FLIGHTS = "no_flights"
CACHEABLE_STATUSES = frozenset({STATUS_OK, STATUS_NO_FLIGHTS})

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS flight_results (
        route TEXT NOT NULL,
        travel_date TEXT NOT NULL,
        status TEXT NOT NULL,
        results TEXT NOT NULL,
        stored_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        PRIMARY KEY (route, travel_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_flight_results_expiry ON flight_results (expires_at)",
    """
    CREATE TABLE IF NOT EXISTS calendar_prices (
        route TEXT NOT NULL,
        travel_date TEXT NOT NULL,
        price REAL NOT NULL,
        stored_at REAL NOT NULL,
        PRIMARY KEY (route, travel_date)
    )
    """,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def route_key(origin: str, destination: str, trip_type: str) -> str:
    """Stable key of one route: case-insensitive and trimmed."""
    return "|".join(str(part or "").strip().casefold() for part in (origin, destination, trip_type))


def default_path() -> str:
    """Database path from the environment, else the test-run temp dir, else <repo>/flight_cache.db.

    Returns "" when the cache is disabled (TRAVELPLAN_FLIGHT_CACHE=off).
    """
    explicit = os.environ.get(PATH_ENV, "").strip()
    if explicit:
        return "" if explicit.lower() in DISABLED_VALUES else explicit
    if is_test_run():
        return os.path.join(test_run_temp_dir(), "flight_cache.db")
    return os.path.join(_REPO_ROOT, "flight_cache.db")


class FlightCache:
    """SQLite-backed store of live search results and calendar prices. Thread-safe."""

    def __init__(
        self,
        path: str,
        ttl_secs: Optional[float] = None,
        no_flights_ttl_secs: float = NO_FLIGHTS_TTL_SECS,
        max_rows: Optional[int] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.path = os.path.abspath(path)
        self.ttl_secs = float(ttl_secs if ttl_secs is not None else _env_int(TTL_ENV, DEFAULT_TTL_SECS))
        self.no_flights_ttl_secs = float(no_flights_ttl_secs)
        self.max_rows = max(1, int(max_rows if max_rows is not None else _env_int(MAX_ROWS_ENV, DEFAULT_MAX_ROWS)))
        self._clock = clock
        self._lock = threading.RLock()
        self._writes_since_prune = 0
        self._ready = False

    # -- connection and schema ----------------------------------------------

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        retry_on_busy(self._create_schema, "flight cache schema")
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
                return retry_on_busy(lambda: conn.execute(sql, params).fetchall(), "flight cache read")
            finally:
                conn.close()

    def _write(self, statements: List[tuple]) -> None:
        """Run (sql, params) statements in one write transaction."""
        with self._lock:
            self._ensure_ready()
            conn = self._open()
            try:
                retry_on_busy(lambda: conn.execute("BEGIN IMMEDIATE"), "flight cache write")
                try:
                    for sql, params in statements:
                        conn.execute(sql, params)
                    retry_on_busy(lambda: conn.execute("COMMIT"), "flight cache commit")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

    # -- public API -----------------------------------------------------------

    def get_results(self, route: str, travel_date: str) -> Optional[Dict[str, Any]]:
        """{"status": ..., "results": [...], "stored_at": ts} for a valid entry, else None."""
        now = self._clock()
        try:
            rows = self._read(
                "SELECT status, results, stored_at FROM flight_results "
                "WHERE route = ? AND travel_date = ? AND expires_at > ?",
                (route, travel_date, now),
            )
            if not rows:
                return None
            status, payload, stored_at = rows[0]
            parsed_results = json.loads(payload)
            if isinstance(parsed_results, list) and stored_at:
                try:
                    fallback_iso = datetime.fromtimestamp(stored_at, tz=timezone.utc).isoformat(timespec="seconds")
                    for r in parsed_results:
                        if isinstance(r, dict) and "observed_at" not in r:
                            r["observed_at"] = fallback_iso
                except Exception:
                    pass
            return {"status": status, "results": parsed_results, "stored_at": stored_at}
        except Exception:
            logger.warning("Flight cache read failed; treating as a miss", exc_info=True)
            return None

    def put_results(self, route: str, travel_date: str, status: str, results: List[Dict[str, Any]]) -> bool:
        """Store one live result. Returns False (and stores nothing) for any status but ok / no_flights."""
        if status not in CACHEABLE_STATUSES:
            return False
        now = self._clock()
        ttl = self.ttl_secs if status == STATUS_OK else self.no_flights_ttl_secs
        try:
            try:
                now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")
                for r in results:
                    if isinstance(r, dict) and "observed_at" not in r:
                        r["observed_at"] = now_iso
            except Exception:
                pass
            payload = json.dumps(results, ensure_ascii=False, default=str)
            self._write([(
                "INSERT OR REPLACE INTO flight_results (route, travel_date, status, results, stored_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (route, travel_date, status, payload, now, now + ttl),
            )])
        except Exception:
            logger.warning("Flight cache write failed; result not cached", exc_info=True)
            return False
        self._after_write()
        return True

    def get_calendar(self, route: str, max_age_secs: Optional[float] = None) -> Dict[str, float]:
        """Calendar prices of a route that are still within max_age_secs (defaults to flight TTL)."""
        if max_age_secs is None:
            effective_age = self.ttl_secs
        else:
            effective_age = min(float(max_age_secs), float(CALENDAR_RETENTION_SECS))
        cutoff = self._clock() - effective_age
        try:
            rows = self._read(
                "SELECT travel_date, price FROM calendar_prices WHERE route = ? AND stored_at > ?",
                (route, cutoff),
            )
        except Exception:
            logger.warning("Flight cache calendar read failed; treating as a miss", exc_info=True)
            return {}
        return {day: float(price) for day, price in rows}

    def put_calendar(self, route: str, prices: Dict[str, float]) -> bool:
        """Replace the calendar of a route with ``prices`` (ISO date -> TRY)."""
        if not prices:
            return False
        now = self._clock()
        statements: List[tuple] = [("DELETE FROM calendar_prices WHERE route = ?", (route,))]
        statements += [
            ("INSERT OR REPLACE INTO calendar_prices (route, travel_date, price, stored_at) VALUES (?, ?, ?, ?)",
             (route, day, float(price), now))
            for day, price in prices.items()
        ]
        try:
            self._write(statements)
        except Exception:
            logger.warning("Flight cache calendar write failed", exc_info=True)
            return False
        self._after_write()
        return True

    def row_count(self) -> int:
        rows = self._read("SELECT COUNT(*) FROM flight_results")
        return int(rows[0][0]) if rows else 0

    def prune(self) -> None:
        """Delete expired rows, then the oldest flight rows beyond max_rows."""
        now = self._clock()
        statements = [
            ("DELETE FROM flight_results WHERE expires_at <= ?", (now,)),
            ("DELETE FROM calendar_prices WHERE stored_at <= ?", (now - CALENDAR_RETENTION_SECS,)),
            ("DELETE FROM flight_results WHERE rowid IN ("
             "SELECT rowid FROM flight_results ORDER BY stored_at DESC LIMIT -1 OFFSET ?)",
             (self.max_rows,)),
        ]
        try:
            self._write(statements)
        except Exception:
            logger.warning("Flight cache prune failed", exc_info=True)
        with self._lock:
            self._writes_since_prune = 0

    def _after_write(self) -> None:
        with self._lock:
            self._writes_since_prune += 1
            due = self._writes_since_prune >= PRUNE_EVERY_WRITES
        if due:
            self.prune()
