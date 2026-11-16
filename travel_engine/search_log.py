"""SQLite log of live flight searches (route, date, outcome, cheapest price).

Every live Google Flights search is written here so that later analysis (Faz 2) can answer
"which intermediate stop is cheap on which route" and "is the price falling or rising".

Keys: origin/destination are normalized with `normalize_place`: 3-letter codes are upper-cased,
anything else is ASCII-folded (Turkish letters included) and lower-cased. Timestamps are UTC ISO-8601.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# Retention: record() prunes at most once per PRUNE_INTERVAL; VACUUM follows when more rows than this went.
DEFAULT_MAX_AGE_DAYS = 180
DEFAULT_MAX_ROWS = 50000
PRUNE_INTERVAL = timedelta(days=1)
VACUUM_AFTER_DELETED = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
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
"""

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    origin TEXT,
    destination TEXT,
    depart_dates TEXT,
    total_secs REAL,
    flight_searches INTEGER,
    google_page_loads INTEGER,
    trace TEXT
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_searches_pair ON searches (origin, destination, source, searched_at)",
    "CREATE INDEX IF NOT EXISTS idx_searches_date ON searches (origin, destination, travel_date, source)",
    "CREATE INDEX IF NOT EXISTS idx_search_runs_started_at ON search_runs (started_at)",
)

_TR_TRANSLITERATION = str.maketrans({"ı": "i", "İ": "I"})


def _ascii_fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.translate(_TR_TRANSLITERATION))
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def normalize_place(value: str) -> str:
    """Canonical key for an origin/destination: 'ath' and ' ATH ' -> 'ATH', 'İstanbul' -> 'istanbul'."""
    text = " ".join(_ascii_fold(value or "").split())
    if len(text) == 3 and text.isalpha():
        return text.upper()
    return text.lower()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class SearchLog:
    """Thread-safe append-only log backed by sqlite3."""

    def __init__(self, path: str):
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        # Re-entrant: record() runs retention while holding the lock, and retention calls vacuum().
        self._lock = threading.RLock()
        self._last_prune_at: Optional[datetime] = None
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.execute(_RUNS_SCHEMA)
            for statement in _INDEXES:
                self._conn.execute(statement)
            self._conn.commit()

    def record(
        self,
        origin: str,
        destination: str,
        date: str,
        status: str,
        count: int,
        min_price: Optional[float],
        source: str = "google_flights",
        detail: str = "",
    ) -> None:
        row = (
            _utc_iso(_utc_now()),
            normalize_place(origin),
            normalize_place(destination),
            (date or "").strip(),
            status,
            int(count),
            None if min_price is None else float(min_price),
            source,
            detail or "",
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO searches (searched_at, origin, destination, travel_date, status, "
                "result_count, min_price, source, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            self._conn.commit()
            self._maybe_prune_locked()

    def prune(self, max_age_days: int = DEFAULT_MAX_AGE_DAYS, max_rows: int = DEFAULT_MAX_ROWS) -> int:
        """Delete rows older than max_age_days, then keep only the newest max_rows. Returns the number deleted."""
        with self._lock:
            return self._prune_locked(max_age_days, max_rows, _utc_now())

    def vacuum(self) -> None:
        """Rebuild the database file so space freed by pruning goes back to the disk."""
        with self._lock:
            self._conn.execute("VACUUM")

    def _maybe_prune_locked(self) -> None:
        """Automatic retention, at most once a day. Failures are logged; the write that triggered it still succeeds."""
        now = _utc_now()
        if self._last_prune_at is not None and now - self._last_prune_at < PRUNE_INTERVAL:
            return
        self._last_prune_at = now
        try:
            deleted = self._prune_locked(DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_ROWS, now)
            if deleted > VACUUM_AFTER_DELETED:
                self.vacuum()
        except Exception:
            logger.warning("Search log retention failed", exc_info=True)

    def _prune_locked(self, max_age_days: int, max_rows: int, now: datetime) -> int:
        cutoff = _utc_iso(now - timedelta(days=max_age_days))
        deleted = self._conn.execute("DELETE FROM searches WHERE searched_at < ?", (cutoff,)).rowcount
        deleted += self._conn.execute(
            "DELETE FROM searches WHERE id NOT IN (SELECT id FROM searches ORDER BY id DESC LIMIT ?)",
            (max(0, int(max_rows)),),
        ).rowcount
        self._conn.commit()
        return max(0, deleted)

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM searches ORDER BY id DESC LIMIT ?", (max(0, int(limit)),)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def pair_stats(
        self, origin: str, destination: str, days: int = 90, source: str = "google_flights"
    ) -> Dict[str, Any]:
        since = _utc_iso(_utc_now() - timedelta(days=days))
        o, d = normalize_place(origin), normalize_place(destination)
        with self._lock:
            agg = self._conn.execute(
                "SELECT COUNT(*) AS searches, "
                "SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok, "
                "MIN(min_price) AS min_price, AVG(min_price) AS avg_min_price "
                "FROM searches WHERE origin = ? AND destination = ? AND source = ? AND searched_at >= ?",
                (o, d, source, since),
            ).fetchone()
            last = self._conn.execute(
                "SELECT searched_at FROM searches WHERE origin = ? AND destination = ? AND source = ? "
                "AND searched_at >= ? ORDER BY id DESC LIMIT 1",
                (o, d, source, since),
            ).fetchone()
            last_priced = self._conn.execute(
                "SELECT min_price FROM searches WHERE origin = ? AND destination = ? AND source = ? "
                "AND searched_at >= ? AND min_price IS NOT NULL ORDER BY id DESC LIMIT 1",
                (o, d, source, since),
            ).fetchone()
        avg = agg["avg_min_price"]
        return {
            "searches": int(agg["searches"] or 0),
            "ok": int(agg["ok"] or 0),
            "min_price": None if agg["min_price"] is None else float(agg["min_price"]),
            "avg_min_price": None if avg is None else round(float(avg), 2),
            "last_min_price": None if last_priced is None else float(last_priced["min_price"]),
            "last_at": None if last is None else last["searched_at"],
        }

    def codes_with_history(
        self,
        codes: Sequence[str],
        role: str,
        days: int = 90,
        source: str = "google_flights",
    ) -> Set[str]:
        """Codes among ``codes`` that have a priced search record within the last ``days`` days.

        ``role`` determines the column checked: "origin" matches the origin column,
        "destination" matches the destination column. Matching is done against uppercase IATA codes.
        """
        if not codes:
            return set()
        col = "origin" if role == "origin" else "destination" if role == "destination" else None
        if col is None:
            return set()

        norm_codes = {str(c).strip().upper() for c in codes if c and str(c).strip()}
        if not norm_codes:
            return set()

        since = _utc_iso(_utc_now() - timedelta(days=days))
        placeholders = ",".join("?" for _ in norm_codes)
        query = (
            f"SELECT DISTINCT {col} FROM searches "
            f"WHERE {col} IN ({placeholders}) AND source = ? AND searched_at >= ? AND min_price IS NOT NULL"
        )
        params = list(norm_codes) + [source, since]
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return {r[col] for r in rows}

    def cheapest_destinations(
        self, origin: str, candidates: List[str], days: int = 90, source: str = "google_flights"
    ) -> List[Tuple[str, float]]:
        """Candidates (as given, de-duplicated) sorted by average historical min price.

        Candidates with no priced history are returned last with price float('inf').
        """
        since = _utc_iso(_utc_now() - timedelta(days=days))
        o = normalize_place(origin)
        with self._lock:
            rows = self._conn.execute(
                "SELECT destination, AVG(min_price) AS avg_price FROM searches "
                "WHERE origin = ? AND source = ? AND searched_at >= ? AND min_price IS NOT NULL "
                "GROUP BY destination",
                (o, source, since),
            ).fetchall()
        avg_by_dest = {r["destination"]: float(r["avg_price"]) for r in rows}
        seen = set()
        scored: List[Tuple[str, float]] = []
        for candidate in candidates:
            key = normalize_place(candidate)
            if key in seen:
                continue
            seen.add(key)
            scored.append((candidate, avg_by_dest.get(key, float("inf"))))
        scored.sort(key=lambda item: item[1])
        return scored

    def price_series(
        self, origin: str, destination: str, date: str, source: str = "google_flights"
    ) -> List[Tuple[str, float]]:
        """(searched_at, min_price) for one route and travel date, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT searched_at, min_price FROM searches WHERE origin = ? AND destination = ? "
                "AND travel_date = ? AND source = ? AND min_price IS NOT NULL ORDER BY searched_at, id",
                (normalize_place(origin), normalize_place(destination), (date or "").strip(), source),
            ).fetchall()
        return [(r["searched_at"], float(r["min_price"])) for r in rows]

    def record_run(
        self,
        origin: Optional[str],
        destination: Optional[str],
        depart_dates: Any,
        trace: Optional[Dict[str, Any]],
    ) -> None:
        """Record an end-to-end trip search run."""
        trace_dict = trace if isinstance(trace, dict) else {}

        total_secs = None
        if "total_secs" in trace_dict and trace_dict["total_secs"] is not None:
            try:
                total_secs = float(trace_dict["total_secs"])
            except (ValueError, TypeError):
                total_secs = None

        counters = trace_dict.get("counters") if isinstance(trace_dict.get("counters"), dict) else {}

        flight_searches = 0
        try:
            flight_searches = int(counters.get("flight_searches", 0) or 0)
        except (ValueError, TypeError):
            flight_searches = 0

        google_page_loads = 0
        try:
            google_page_loads = int(counters.get("google_page_loads", 0) or 0)
        except (ValueError, TypeError):
            google_page_loads = 0

        depart_dates_str = None
        if depart_dates is not None:
            try:
                depart_dates_str = json.dumps(depart_dates)
            except Exception:
                depart_dates_str = str(depart_dates)

        trace_json = None
        if trace is not None:
            try:
                trace_json = json.dumps(trace, default=str)
            except Exception:
                trace_json = str(trace)

        row = (
            _utc_iso(_utc_now()),
            None if origin is None else str(origin),
            None if destination is None else str(destination),
            depart_dates_str,
            total_secs,
            flight_searches,
            google_page_loads,
            trace_json,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO search_runs (started_at, origin, destination, depart_dates, "
                "total_secs, flight_searches, google_page_loads, trace) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            self._conn.commit()

    def recent_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM search_runs ORDER BY id DESC LIMIT ?", (max(0, int(limit)),)
            ).fetchall()
        return [self._run_row_to_dict(r) for r in rows]

    def google_page_loads_since(self, since_iso: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT SUM(google_page_loads) AS total FROM search_runs WHERE started_at >= ?",
                ((since_iso or "").strip(),),
            ).fetchone()
        if row and row["total"] is not None:
            return int(row["total"])
        return 0

    @staticmethod
    def _run_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        trace_raw = row["trace"]
        trace_obj = None
        if trace_raw:
            try:
                trace_obj = json.loads(trace_raw)
            except Exception:
                trace_obj = trace_raw

        depart_dates_raw = row["depart_dates"]
        depart_dates_obj = depart_dates_raw
        if depart_dates_raw:
            try:
                depart_dates_obj = json.loads(depart_dates_raw)
            except Exception:
                depart_dates_obj = depart_dates_raw

        return {
            "id": row["id"],
            "started_at": row["started_at"],
            "origin": row["origin"],
            "destination": row["destination"],
            "depart_dates": depart_dates_obj,
            "total_secs": row["total_secs"],
            "flight_searches": row["flight_searches"],
            "google_page_loads": row["google_page_loads"],
            "trace": trace_obj,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "searched_at": row["searched_at"],
            "origin": row["origin"],
            "destination": row["destination"],
            "date": row["travel_date"],
            "status": row["status"],
            "count": row["result_count"],
            "min_price": row["min_price"],
            "source": row["source"],
            "detail": row["detail"],
        }
