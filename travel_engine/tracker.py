"""TravelPlan Price Watching & Alert Trigger System.

Monitors route prices, records historical fluctuations, and detects
when a travel combination drops below a user's target budget.

Watches are stored in the SQLite database next to the given path (see
``travel_engine.storage``). A legacy ``watches.json`` next to it is imported once.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, Optional, Any, List
from .models import TripWatch
from .storage import get_store, import_legacy_json, resolve_db_path, utc_now_iso


logger = logging.getLogger(__name__)


def _watch_from_item(item: Any) -> TripWatch:
    """Build one TripWatch from its stored dict. Raises on malformed input."""
    w = TripWatch(
        id=item["id"],
        origin=item["origin"],
        destination=item["destination"],
        target_budget_try=item["target_budget_try"],
        departure_date=item.get("departure_date"),
        return_date=item.get("return_date"),
        trip_type=item.get("trip_type", "oneway"),
        max_transfers=item.get("max_transfers", 3),
        departure_time_slot=item.get("departure_time_slot", "all"),
        time_window_start=item.get("time_window_start"),
        time_window_end=item.get("time_window_end"),
        include_multimodal=item.get("include_multimodal", True),
        active=item.get("active", True),
        last_alert_price=item.get("last_alert_price"),
        last_alert_at=item.get("last_alert_at"),
        last_mm_alert_price=item.get("last_mm_alert_price"),
        last_mm_alert_at=item.get("last_mm_alert_at"),
        trip_query=item.get("trip_query"),
        direct_budget_try=item.get("direct_budget_try"),
        last_trip_alert=item.get("last_trip_alert"),
    )
    for r in item.get("history", []):
        w.add_reading(r["price_try"], datetime.fromisoformat(r["timestamp"]))
    return w


def _parse_watches(data: Any) -> Dict[str, TripWatch]:
    """Build TripWatch objects from a {id: watch dict} mapping. Raises on any malformed entry."""
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object at top level, got {type(data).__name__}")
    return {wid: _watch_from_item(item) for wid, item in data.items()}


def _validate_legacy_watches(data: Any) -> Dict[str, Any]:
    """Check a legacy watches.json payload and return its raw items for import."""
    _parse_watches(data)
    return data


def _insert_watch_rows(conn: sqlite3.Connection, items: Dict[str, Any]) -> int:
    now = utc_now_iso()
    rows = [(wid, json.dumps(item, ensure_ascii=False), now) for wid, item in items.items()]
    conn.executemany("INSERT OR REPLACE INTO watches (id, data, updated_at) VALUES (?, ?, ?)", rows)
    return len(rows)


class PriceTracker:
    def __init__(self, storage_path: str = "watches.json"):
        self.storage_path = storage_path
        self._lock = threading.RLock()
        self._store = get_store(resolve_db_path(storage_path))
        self.db_path = self._store.db_path
        self.watches: Dict[str, TripWatch] = {}
        self.load()

    def add_watch(
        self,
        origin: str,
        destination: str,
        target_budget_try: float,
        departure_date: Optional[str] = None,
        return_date: Optional[str] = None,
        trip_type: str = "oneway",
        max_transfers: int = 3,
        departure_time_slot: str = "all",
        time_window_start: Optional[str] = None,
        time_window_end: Optional[str] = None,
        current_price: Optional[float] = None,
        watch_id: Optional[str] = None,
        include_multimodal: bool = True,
        trip_query: Optional[Dict[str, Any]] = None,
        direct_budget_try: Optional[float] = None,
    ) -> TripWatch:
        wid = watch_id or f"watch_{origin.lower()}_{destination.lower()}_{int(datetime.now(timezone.utc).timestamp())}"
        trip_query = dict(trip_query) if trip_query else None
        if trip_query and trip_query.get("depart_dates"):
            # Trip watches are keyed by their first departure date for display and listings
            departure_date = trip_query["depart_dates"][0]
        watch = TripWatch(
            id=wid,
            origin=origin,
            destination=destination,
            target_budget_try=target_budget_try,
            departure_date=departure_date,
            return_date=return_date,
            trip_type=trip_type,
            max_transfers=max_transfers,
            departure_time_slot=departure_time_slot,
            time_window_start=time_window_start,
            time_window_end=time_window_end,
            include_multimodal=include_multimodal,
            trip_query=trip_query,
            direct_budget_try=direct_budget_try,
        )
        if current_price is not None:
            watch.add_reading(current_price)
        with self._lock:
            self.watches[wid] = watch
            self.save()
        return watch

    def get_watch(self, watch_id: str) -> Optional[TripWatch]:
        with self._lock:
            return self.watches.get(watch_id)

    def delete_watch(self, watch_id: str) -> bool:
        with self._lock:
            if watch_id in self.watches:
                del self.watches[watch_id]
                self.save()
                return True
            return False

    def save(self) -> None:
        """Replace the stored watches with the in-memory ones, in one transaction."""
        with self._lock:
            now = utc_now_iso()
            rows = [
                (wid, json.dumps(w.to_dict(), ensure_ascii=False), now)
                for wid, w in self.watches.items()
            ]
            try:
                with self._store.transaction() as conn:
                    conn.execute("DELETE FROM watches")
                    conn.executemany(
                        "INSERT INTO watches (id, data, updated_at) VALUES (?, ?, ?)", rows,
                    )
            except Exception:
                logger.exception("Failed to save watches to %s", self.db_path)

    def load(self, prune: bool = False) -> None:
        """Merge watches from the store into memory.

        TripWatch objects already in memory are kept as they are, so readings and
        alert state recorded by concurrent scans are never discarded. Ids that are
        only in the store are added. Ids missing from the store are removed only when
        prune=True. A stored row that cannot be parsed is logged and skipped, and an
        in-memory watch with that id is not pruned.
        """
        with self._lock:
            import_legacy_json(
                self._store, "watches", self.storage_path, "watches",
                _validate_legacy_watches, _insert_watch_rows,
            )
            try:
                rows = self._store.query("SELECT id, data FROM watches ORDER BY rowid")
            except sqlite3.Error as e:
                logger.warning(
                    "Could not read watches from %s, keeping %d in-memory watches: %s",
                    self.db_path, len(self.watches), e,
                )
                return

            parsed: Dict[str, TripWatch] = {}
            unreadable: List[str] = []
            for wid, data in rows:
                try:
                    parsed[wid] = _watch_from_item(json.loads(data))
                except Exception as e:
                    unreadable.append(wid)
                    logger.warning("Skipping unreadable watch %s in %s: %s", wid, self.db_path, e)

            for wid, w in parsed.items():
                if wid not in self.watches:
                    self.watches[wid] = w
            if prune:
                for wid in [wid for wid in self.watches if wid not in parsed and wid not in unreadable]:
                    del self.watches[wid]
