"""Record and replay infrastructure for flight and ground providers (TST-1).

Allows recording live or mock provider calls into canonical JSON records,
and replaying them deterministically without touching the network or launching browsers.

Environment variables:
- TRAVELPLAN_RECORD_DIR: If set, providers record all calls to this directory.
- TRAVELPLAN_REPLAY_DIR: If set, providers replay recorded calls from this directory.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from travel_engine.providers.rail_bus_provider import (
    ScheduledSegment,
    segment_from_dict,
)

logger = logging.getLogger(__name__)

ENV_RECORD_DIR = "TRAVELPLAN_RECORD_DIR"
ENV_REPLAY_DIR = "TRAVELPLAN_REPLAY_DIR"


def get_replay_mode() -> Optional[str]:
    """Detect replay mode from environment variables.

    Returns:
        "record" if TRAVELPLAN_RECORD_DIR is set and non-empty.
        "replay" if TRAVELPLAN_REPLAY_DIR is set and non-empty.
        None otherwise.
    """
    if os.environ.get(ENV_RECORD_DIR, "").strip():
        return "record"
    if os.environ.get(ENV_REPLAY_DIR, "").strip():
        return "replay"
    return None


def get_storage_dir() -> Optional[str]:
    """Return the active storage directory path from environment variables."""
    rec = os.environ.get(ENV_RECORD_DIR, "").strip()
    if rec:
        return rec
    rep = os.environ.get(ENV_REPLAY_DIR, "").strip()
    if rep:
        return rep
    return None


def _normalize_value(val: Any) -> Any:
    """Recursively normalize parameter values for deterministic comparison."""
    if val is None or isinstance(val, (bool, int)):
        return val
    if isinstance(val, float):
        return round(val, 6)
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (list, tuple)):
        return [_normalize_value(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _normalize_value(v) for k, v in sorted(val.items())}
    if hasattr(val, "to_dict") and callable(val.to_dict):
        try:
            return _normalize_value(val.to_dict())
        except Exception:
            pass
    return str(val)


def make_call_key(category: str, method: str, params: Dict[str, Any]) -> str:
    """Generate a canonical deterministic lookup key for a call."""
    canonical_params = {str(k): _normalize_value(v) for k, v in sorted(params.items())}
    encoded = json.dumps(canonical_params, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{category}:{method}:{encoded}"


def make_filename(category: str, method: str, params: Dict[str, Any]) -> str:
    """Generate a human-readable and unique filename for a recorded call."""
    canonical_params = {str(k): _normalize_value(v) for k, v in sorted(params.items())}
    encoded = json.dumps(canonical_params, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]

    orig = params.get("origin") or params.get("from_place") or params.get("from_station") or ""
    dest = params.get("destination") or params.get("dest") or params.get("to_place") or params.get("to_station") or ""
    date = params.get("departure_date") or params.get("date_str") or params.get("date") or ""

    parts = [method]
    if orig and dest:
        parts.append(f"{orig}_{dest}")
    if date:
        parts.append(str(date))

    slug = "_".join(parts)
    slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", slug)[:50]
    return f"{slug}_{digest}.json"


def _serialize_response(category: str, method: str, response: Any) -> Any:
    """Convert response into JSON-serializable primitives."""
    if response is None or isinstance(response, (bool, int, float, str)):
        return response
    if isinstance(response, list):
        out = []
        for item in response:
            if hasattr(item, "to_dict") and callable(item.to_dict):
                out.append(item.to_dict())
            elif isinstance(item, dict):
                out.append(copy.deepcopy(item))
            else:
                out.append(item)
        return out
    if isinstance(response, dict):
        return copy.deepcopy(response)
    if hasattr(response, "to_dict") and callable(response.to_dict):
        return response.to_dict()
    return response


def _deserialize_response(category: str, method: str, data: Any) -> Any:
    """Reconstruct domain objects from stored JSON data."""
    if data is None:
        return None
    # Ground provider segments
    if category == "ground" and method in (
        "find_ground",
        "find_ground_live",
        "find_ground_next",
        "get_hub_connections",
        "get_schedules",
    ):
        if not isinstance(data, list):
            return []
        segments = []
        for item in data:
            if isinstance(item, dict):
                segments.append(segment_from_dict(item))
            elif isinstance(item, ScheduledSegment):
                segments.append(item)
        return segments

    # Flight searches or dictionary returns
    if isinstance(data, list):
        return copy.deepcopy(data)
    if isinstance(data, dict):
        return copy.deepcopy(data)
    return data


class BaseRecordReplayWrapper:
    """Base class for flight and ground provider record/replay wrappers."""

    def __init__(
        self,
        provider: Any = None,
        storage_dir: Optional[str] = None,
        mode: Optional[str] = None,
        category: str = "generic",
    ):
        self._provider = provider
        self.storage_dir = os.path.abspath(storage_dir) if storage_dir else get_storage_dir()
        if self.storage_dir and not os.path.isabs(self.storage_dir):
            self.storage_dir = os.path.abspath(self.storage_dir)
        self.mode = mode or get_replay_mode() or "passthrough"
        self.category = category

        self.replayed_calls = 0
        self.unrecorded_calls = 0
        self.recorded_calls = 0
        self.total_calls = 0

        self._records: Dict[str, Any] = {}
        self._lock = threading.Lock()

        if self.storage_dir:
            self._load_records(self.storage_dir)

    def _load_records(self, target_path: str) -> None:
        """Load records from a single file or a directory containing JSON records."""
        if not os.path.exists(target_path):
            return

        json_paths = []
        if os.path.isfile(target_path):
            json_paths.append(target_path)
        elif os.path.isdir(target_path):
            sub_dir = os.path.join(target_path, self.category)
            paths_to_scan = [target_path]
            if os.path.isdir(sub_dir):
                paths_to_scan.append(sub_dir)

            for scan_dir in paths_to_scan:
                for fname in os.listdir(scan_dir):
                    if fname.endswith(".json"):
                        fpath = os.path.join(scan_dir, fname)
                        if os.path.isfile(fpath):
                            json_paths.append(fpath)

        for path in json_paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = json.load(f)
                self._ingest_loaded_content(content)
            except Exception as e:
                logger.warning("Could not read replay record %s: %s", path, e)

    def _ingest_loaded_content(self, content: Any) -> None:
        """Parse loaded JSON dictionary or list into self._records."""
        if isinstance(content, dict):
            # Case 1: single record {provider, method, params, response}
            if "method" in content and "params" in content and "response" in content:
                prov = content.get("provider", self.category)
                if prov == self.category or self.category == "generic":
                    key = make_call_key(self.category, content["method"], content["params"])
                    self._records[key] = content["response"]
                return

            # Case 2: envelope {records: [...]}
            if "records" in content and isinstance(content["records"], list):
                for item in content["records"]:
                    self._ingest_loaded_content(item)
                return

            # Case 3: dictionary mapping call_key -> response
            for k, v in content.items():
                if isinstance(k, str) and ":" in k:
                    self._records[k] = v

        elif isinstance(content, list):
            for item in content:
                self._ingest_loaded_content(item)

    def _record_call(self, method: str, params: Dict[str, Any], response: Any) -> None:
        """Save a call result in memory and on disk when recording."""
        serialized = _serialize_response(self.category, method, response)
        key = make_call_key(self.category, method, params)

        with self._lock:
            self._records[key] = serialized
            self.recorded_calls += 1

        if self.storage_dir and self.mode == "record":
            try:
                target_dir = os.path.join(self.storage_dir, self.category)
                os.makedirs(target_dir, exist_ok=True)
                fname = make_filename(self.category, method, params)
                fpath = os.path.join(target_dir, fname)
                payload = {
                    "provider": self.category,
                    "method": method,
                    "params": {str(k): _normalize_value(v) for k, v in sorted(params.items())},
                    "response": serialized,
                }
                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error("Failed to save replay cassette to %s: %s", self.storage_dir, e)

    def _replay_call(
        self,
        method: str,
        params: Dict[str, Any],
        empty_factory: Callable[[], Any],
    ) -> Any:
        """Lookup a call result from records, or return empty and increment unrecorded count."""
        key = make_call_key(self.category, method, params)

        with self._lock:
            if key in self._records:
                self.replayed_calls += 1
                stored = self._records[key]
                return _deserialize_response(self.category, method, stored)

            self.unrecorded_calls += 1
            logger.info(
                "Replay miss for %s:%s with params %s (unrecorded_calls=%d)",
                self.category,
                method,
                params,
                self.unrecorded_calls,
            )
            return empty_factory()

    def stats(self) -> Dict[str, Any]:
        """Aggregate stats from the underlying provider and record/replay metrics."""
        base: Dict[str, Any] = {}
        if self._provider is not None:
            getter = getattr(self._provider, "stats", None)
            if callable(getter):
                try:
                    res = getter()
                    if isinstance(res, dict):
                        base = dict(res)
                except Exception:
                    base = {}

        with self._lock:
            base.update(
                {
                    "replay_mode": self.mode,
                    "storage_dir": self.storage_dir,
                    "total_calls": self.total_calls,
                    "replayed_calls": self.replayed_calls,
                    "unrecorded_calls": self.unrecorded_calls,
                    "recorded_calls": self.recorded_calls,
                }
            )
        return base

    def __getattr__(self, name: str) -> Any:
        """Transparently delegate missing attributes/methods to the underlying provider."""
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if self._provider is not None and hasattr(self._provider, name):
            return getattr(self._provider, name)
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")


class RecordReplayFlightProvider(BaseRecordReplayWrapper):
    """Flight provider wrapper implementing recording and replaying."""

    def __init__(
        self,
        provider: Any = None,
        storage_dir: Optional[str] = None,
        mode: Optional[str] = None,
    ):
        super().__init__(provider=provider, storage_dir=storage_dir, mode=mode, category="flights")

    @property
    def cache(self) -> Dict[str, Any]:
        if self._provider is not None and hasattr(self._provider, "cache"):
            return self._provider.cache
        return {}

    @property
    def calendar_cache(self) -> Dict[str, Any]:
        if self._provider is not None and hasattr(self._provider, "calendar_cache"):
            return self._provider.calendar_cache
        return {}

    def search_concurrency(self) -> int:
        if self._provider is not None and hasattr(self._provider, "search_concurrency"):
            getter = getattr(self._provider, "search_concurrency")
            if callable(getter):
                return getter()
        return 1

    def health_status(self) -> Dict[str, Any]:
        if self._provider is not None and hasattr(self._provider, "health_status"):
            getter = getattr(self._provider, "health_status")
            if callable(getter):
                return getter()
        return {"status": "ok", "mode": self.mode}

    def resource_status(self) -> Dict[str, Any]:
        if self._provider is not None and hasattr(self._provider, "resource_status"):
            getter = getattr(self._provider, "resource_status")
            if callable(getter):
                return getter()
        return {"browsers_running": 0, "mode": self.mode}

    def search_flights(
        self,
        origin: str = "Istanbul",
        destination: str = "Prague",
        departure_date: str = "2026-10-25",
        return_date: Optional[str] = None,
        trip_type: str = "roundtrip",
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            self.total_calls += 1

        params = {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "return_date": return_date,
            "trip_type": trip_type,
        }

        if self.mode == "record":
            if self._provider is None:
                return []
            res = self._provider.search_flights(
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=return_date,
                trip_type=trip_type,
                **kwargs,
            )
            self._record_call("search_flights", params, res)
            return res

        if self.mode == "replay":
            return self._replay_call("search_flights", params, empty_factory=list)

        # Passthrough
        if self._provider is not None:
            return self._provider.search_flights(
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=return_date,
                trip_type=trip_type,
                **kwargs,
            )
        return []

    def get_calendar_prices(
        self,
        origin: str,
        destination: str,
        trip_type: str = "oneway",
        **kwargs: Any,
    ) -> Dict[str, float]:
        with self._lock:
            self.total_calls += 1

        params = {
            "origin": origin,
            "destination": destination,
            "trip_type": trip_type,
        }

        if self.mode == "record":
            if self._provider is None:
                return {}
            res = self._provider.get_calendar_prices(origin=origin, destination=destination, trip_type=trip_type, **kwargs)
            self._record_call("get_calendar_prices", params, res)
            return res

        if self.mode == "replay":
            return self._replay_call("get_calendar_prices", params, empty_factory=dict)

        # Passthrough
        if self._provider is not None and hasattr(self._provider, "get_calendar_prices"):
            return self._provider.get_calendar_prices(origin=origin, destination=destination, trip_type=trip_type, **kwargs)
        return {}


class RecordReplayGroundProvider(BaseRecordReplayWrapper):
    """Ground provider wrapper implementing recording and replaying."""

    def __init__(
        self,
        provider: Any = None,
        storage_dir: Optional[str] = None,
        mode: Optional[str] = None,
    ):
        super().__init__(provider=provider, storage_dir=storage_dir, mode=mode, category="ground")

    def sources_health(self) -> Dict[str, Any]:
        if self._provider is not None and hasattr(self._provider, "sources_health"):
            getter = getattr(self._provider, "sources_health")
            if callable(getter):
                return getter()
        return {"mode": self.mode}

    def find_ground(
        self,
        origin: Optional[str] = None,
        dest: Optional[str] = None,
        date_str: Optional[str] = None,
        max_results: int = 10,
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
        allow_estimate: bool = True,
        maps_budget: Any = None,
        from_place: Optional[str] = None,
        to_place: Optional[str] = None,
        **kwargs: Any,
    ) -> List[ScheduledSegment]:
        with self._lock:
            self.total_calls += 1

        orig = origin or from_place or ""
        dst = dest or to_place or ""
        dt = date_str or kwargs.pop("date", "")

        params = {
            "origin": orig,
            "dest": dst,
            "date_str": dt,
            "max_results": max_results,
            "from_coord": from_coord,
            "to_coord": to_coord,
            "allow_estimate": allow_estimate,
        }

        if self.mode == "record":
            if self._provider is None:
                return []
            try:
                res = self._provider.find_ground(
                    orig,
                    dst,
                    dt,
                    max_results=max_results,
                    from_coord=from_coord,
                    to_coord=to_coord,
                    allow_estimate=allow_estimate,
                    maps_budget=maps_budget,
                    **kwargs,
                )
            except TypeError:
                res = self._provider.find_ground(orig, dst, dt, **kwargs)
            self._record_call("find_ground", params, res)
            return res

        if self.mode == "replay":
            return self._replay_call("find_ground", params, empty_factory=list)

        # Passthrough
        if self._provider is not None:
            try:
                return self._provider.find_ground(
                    orig,
                    dst,
                    dt,
                    max_results=max_results,
                    from_coord=from_coord,
                    to_coord=to_coord,
                    allow_estimate=allow_estimate,
                    maps_budget=maps_budget,
                    **kwargs,
                )
            except TypeError:
                return self._provider.find_ground(orig, dst, dt, **kwargs)
        return []

    def find_ground_live(
        self,
        origin: Optional[str] = None,
        dest: Optional[str] = None,
        date_str: Optional[str] = None,
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
        from_place: Optional[str] = None,
        to_place: Optional[str] = None,
        **kwargs: Any,
    ) -> List[ScheduledSegment]:
        with self._lock:
            self.total_calls += 1

        orig = origin or from_place or ""
        dst = dest or to_place or ""
        dt = date_str or kwargs.pop("date", "")

        params = {
            "origin": orig,
            "dest": dst,
            "date_str": dt,
            "from_coord": from_coord,
            "to_coord": to_coord,
        }

        if self.mode == "record":
            if self._provider is None:
                return []
            if hasattr(self._provider, "find_ground_live"):
                res = self._provider.find_ground_live(
                    orig, dst, dt, from_coord=from_coord, to_coord=to_coord, **kwargs
                )
            else:
                res = self.find_ground(orig, dst, dt, from_coord=from_coord, to_coord=to_coord, **kwargs)
            self._record_call("find_ground_live", params, res)
            return res

        if self.mode == "replay":
            return self._replay_call("find_ground_live", params, empty_factory=list)

        if self._provider is not None and hasattr(self._provider, "find_ground_live"):
            return self._provider.find_ground_live(
                orig, dst, dt, from_coord=from_coord, to_coord=to_coord, **kwargs
            )
        return []

    def find_ground_next(
        self,
        origin: Optional[str] = None,
        dest: Optional[str] = None,
        date_str: Optional[str] = None,
        anchor_time: Optional[str] = None,
        max_results: int = 5,
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
        from_place: Optional[str] = None,
        to_place: Optional[str] = None,
        **kwargs: Any,
    ) -> List[ScheduledSegment]:
        with self._lock:
            self.total_calls += 1

        orig = origin or from_place or ""
        dst = dest or to_place or ""
        dt = date_str or kwargs.pop("date", "")

        params = {
            "origin": orig,
            "dest": dst,
            "date_str": dt,
            "anchor_time": anchor_time,
            "max_results": max_results,
            "from_coord": from_coord,
            "to_coord": to_coord,
        }

        if self.mode == "record":
            if self._provider is None:
                return []
            if hasattr(self._provider, "find_ground_next"):
                res = self._provider.find_ground_next(
                    orig,
                    dst,
                    dt,
                    anchor_time=anchor_time,
                    max_results=max_results,
                    from_coord=from_coord,
                    to_coord=to_coord,
                    **kwargs,
                )
            else:
                res = self.find_ground(orig, dst, dt, from_coord=from_coord, to_coord=to_coord, **kwargs)
            self._record_call("find_ground_next", params, res)
            return res

        if self.mode == "replay":
            return self._replay_call("find_ground_next", params, empty_factory=list)

        if self._provider is not None and hasattr(self._provider, "find_ground_next"):
            return self._provider.find_ground_next(
                orig,
                dst,
                dt,
                anchor_time=anchor_time,
                max_results=max_results,
                from_coord=from_coord,
                to_coord=to_coord,
                **kwargs,
            )
        return []

    def get_hub_connections(
        self,
        origin: Optional[str] = None,
        dest: Optional[str] = None,
        date_str: Optional[str] = None,
        max_results: int = 5,
        from_place: Optional[str] = None,
        to_place: Optional[str] = None,
        **kwargs: Any,
    ) -> List[ScheduledSegment]:
        with self._lock:
            self.total_calls += 1

        orig = origin or from_place or ""
        dst = dest or to_place or ""
        dt = date_str or kwargs.pop("date", "")

        params = {
            "origin": orig,
            "dest": dst,
            "date_str": dt,
            "max_results": max_results,
        }

        if self.mode == "record":
            if self._provider is None:
                return []
            if hasattr(self._provider, "get_hub_connections"):
                res = self._provider.get_hub_connections(orig, dst, dt, max_results=max_results, **kwargs)
            else:
                res = self.find_ground(orig, dst, dt, max_results=max_results, **kwargs)
            self._record_call("get_hub_connections", params, res)
            return res

        if self.mode == "replay":
            return self._replay_call("get_hub_connections", params, empty_factory=list)

        if self._provider is not None and hasattr(self._provider, "get_hub_connections"):
            return self._provider.get_hub_connections(orig, dst, dt, max_results=max_results, **kwargs)
        return []

    def get_schedules(
        self,
        origin: Optional[str] = None,
        dest: Optional[str] = None,
        date_str: Optional[str] = None,
        from_place: Optional[str] = None,
        to_place: Optional[str] = None,
        **kwargs: Any,
    ) -> List[ScheduledSegment]:
        with self._lock:
            self.total_calls += 1

        orig = origin or from_place or ""
        dst = dest or to_place or ""
        dt = date_str or kwargs.pop("date", "")

        params = {
            "origin": orig,
            "dest": dst,
            "date_str": dt,
        }

        if self.mode == "record":
            if self._provider is None:
                return []
            if hasattr(self._provider, "get_schedules"):
                res = self._provider.get_schedules(orig, dst, dt, **kwargs)
            else:
                res = self.find_ground(orig, dst, dt, **kwargs)
            self._record_call("get_schedules", params, res)
            return res

        if self.mode == "replay":
            return self._replay_call("get_schedules", params, empty_factory=list)

        if self._provider is not None and hasattr(self._provider, "get_schedules"):
            return self._provider.get_schedules(orig, dst, dt, **kwargs)
        return []

    def ground_distance_km(
        self,
        from_place: Optional[str] = None,
        to_place: Optional[str] = None,
        origin: Optional[str] = None,
        dest: Optional[str] = None,
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
        **kwargs: Any,
    ) -> Optional[float]:
        with self._lock:
            self.total_calls += 1

        actual_from = from_place or origin or kwargs.pop("from_station", "")
        actual_to = to_place or dest or kwargs.pop("to_station", "")

        params = {
            "from_place": actual_from,
            "to_place": actual_to,
            "from_coord": from_coord,
            "to_coord": to_coord,
        }

        if self.mode == "record":
            if self._provider is None:
                return None
            if hasattr(self._provider, "ground_distance_km"):
                try:
                    res = self._provider.ground_distance_km(
                        actual_from, actual_to, from_coord=from_coord, to_coord=to_coord, **kwargs
                    )
                except TypeError:
                    res = self._provider.ground_distance_km(
                        from_place=actual_from, to_place=actual_to, from_coord=from_coord, to_coord=to_coord, **kwargs
                    )
            else:
                res = None
            self._record_call("ground_distance_km", params, res)
            return res

        if self.mode == "replay":
            return self._replay_call("ground_distance_km", params, empty_factory=lambda: None)

        if self._provider is not None and hasattr(self._provider, "ground_distance_km"):
            try:
                return self._provider.ground_distance_km(
                    actual_from, actual_to, from_coord=from_coord, to_coord=to_coord, **kwargs
                )
            except TypeError:
                return self._provider.ground_distance_km(
                    from_place=actual_from, to_place=actual_to, from_coord=from_coord, to_coord=to_coord, **kwargs
                )
        return None


def wrap_flight_provider(
    provider: Any = None,
    *,
    storage_dir: Optional[str] = None,
    mode: Optional[str] = None,
) -> Any:
    """Wrap flight provider with record/replay capability if configured."""
    active_mode = mode or get_replay_mode()
    active_dir = storage_dir or get_storage_dir()
    if active_mode is None and active_dir is None:
        return provider
    return RecordReplayFlightProvider(
        provider=provider,
        storage_dir=active_dir,
        mode=active_mode,
    )


def wrap_ground_provider(
    provider: Any = None,
    *,
    storage_dir: Optional[str] = None,
    mode: Optional[str] = None,
) -> Any:
    """Wrap ground provider with record/replay capability if configured."""
    active_mode = mode or get_replay_mode()
    active_dir = storage_dir or get_storage_dir()
    if active_mode is None and active_dir is None:
        return provider
    return RecordReplayGroundProvider(
        provider=provider,
        storage_dir=active_dir,
        mode=active_mode,
    )
