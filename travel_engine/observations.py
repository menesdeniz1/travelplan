"""Observations store: records observed flight route prices for smarter candidate ordering."""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple, Union

logger = logging.getLogger(__name__)

_DEFAULT_MAX_AGE_DAYS = 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_observations_path(path: Optional[str]) -> Tuple[str, bool]:
    """Resolve database path and disabled flag from explicit arg or env var."""
    if path is not None:
        p = str(path).strip()
    else:
        p = os.environ.get("TRAVELPLAN_OBSERVATIONS", "").strip()
        if not p:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            p = os.path.join(base_dir, "observations.db")

    disabled = (p.lower() == "off")
    return p, disabled


class ObservationStore:
    """Thread-safe SQLite store of observed minimum flight prices per route and date."""

    def __init__(self, path: Optional[str] = None):
        self._path, self._disabled = _resolve_observations_path(path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

        if not self._disabled:
            if self._path != ":memory:":
                parent = os.path.dirname(os.path.abspath(self._path))
                if parent:
                    os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS observations ("
                    "origin TEXT, "
                    "destination TEXT, "
                    "travel_date TEXT, "
                    "min_price REAL, "
                    "card_count INTEGER, "
                    "observed_at TEXT"
                    ")"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_observations_route_date "
                    "ON observations(origin, destination, travel_date)"
                )
                self._conn.commit()

    @property
    def is_enabled(self) -> bool:
        return not self._disabled and self._conn is not None

    def record(
        self,
        origin: str,
        destination: str,
        travel_date: str,
        min_price: float,
        card_count: int,
        observed_at: Optional[Union[str, datetime]] = None,
    ) -> None:
        """Record an observation for an origin-destination pair on a given travel date."""
        if not self.is_enabled:
            return

        origin_clean = (origin or "").strip().upper()
        dest_clean = (destination or "").strip().upper()
        date_clean = (travel_date or "").strip()
        min_p = float(min_price)
        count = int(card_count)

        if observed_at is None:
            obs_iso = _utc_iso(_utc_now())
        elif isinstance(observed_at, datetime):
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            obs_iso = _utc_iso(observed_at)
        else:
            obs_iso = str(observed_at).strip()

        with self._lock:
            if self._conn is not None:
                self._conn.execute(
                    "INSERT INTO observations (origin, destination, travel_date, min_price, card_count, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (origin_clean, dest_clean, date_clean, min_p, count, obs_iso),
                )
                self._conn.commit()

    def min_price(
        self,
        origin: str,
        destination: str,
        travel_date: str,
        max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
    ) -> Optional[float]:
        """Return the lowest recorded price within max_age_days, or None."""
        if not self.is_enabled:
            return None

        origin_clean = (origin or "").strip().upper()
        dest_clean = (destination or "").strip().upper()
        date_clean = (travel_date or "").strip()
        cutoff_dt = _utc_now() - timedelta(days=float(max_age_days))
        cutoff_iso = _utc_iso(cutoff_dt)

        with self._lock:
            if self._conn is None:
                return None
            cur = self._conn.execute(
                "SELECT MIN(min_price) FROM observations "
                "WHERE origin = ? AND destination = ? AND travel_date = ? AND observed_at >= ?",
                (origin_clean, dest_clean, date_clean, cutoff_iso),
            )
            row = cur.fetchone()
            if row is not None and row[0] is not None:
                return float(row[0])
            return None

    def prune(self, max_age_days: int = _DEFAULT_MAX_AGE_DAYS) -> int:
        """Delete observations older than max_age_days. Returns the number of deleted rows."""
        if not self.is_enabled:
            return 0

        cutoff_dt = _utc_now() - timedelta(days=float(max_age_days))
        cutoff_iso = _utc_iso(cutoff_dt)

        with self._lock:
            if self._conn is None:
                return 0
            cur = self._conn.execute(
                "DELETE FROM observations WHERE observed_at < ?",
                (cutoff_iso,),
            )
            deleted = cur.rowcount
            self._conn.commit()
            return int(deleted)

    def close(self) -> None:
        """Close database connection."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
