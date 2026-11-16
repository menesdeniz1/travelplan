import dataclasses
import logging
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Dict, Tuple
from datetime import datetime, timedelta
from travel_engine import fx
from travel_engine import ground_cache as ground_cache_module
from travel_engine.airports import Airport, AirportDB, haversine_km
from travel_engine.geo_clusters import GeoClusterRegistry

from travel_engine.providers.obilet_fares import price_for_operator

if TYPE_CHECKING:
    from travel_engine.providers.flixbus_provider import FlixBusProvider
    from travel_engine.providers.google_maps_transit_provider import GoogleMapsTransitProvider
    from travel_engine.providers.live_rail_provider import LiveRailProvider
    from travel_engine.providers.obilet_fares import ObiletFromPrices
    from travel_engine.providers.trenitalia_provider import TrenitaliaProvider
    from travel_engine.providers.transitous_provider import TransitousProvider

logger = logging.getLogger(__name__)

# Google Maps page loads allowed per search (see MapsBudget). TRAVELPLAN_MAPS_BUDGET overrides it; 0 turns
# the Maps fallback off.
DEFAULT_MAPS_BUDGET = 4
MAPS_BUDGET_ENV = "TRAVELPLAN_MAPS_BUDGET"

# Distance estimate used by find_ground when no live or table source knows the pair.
ROAD_DISTANCE_FACTOR = 1.25        # road km = great-circle km x 1.25
ESTIMATE_SPEED_KMH = 75.0          # average intercity bus/train speed
ESTIMATE_OVERHEAD_MINS = 20        # boarding and stops
ESTIMATE_EUR_PER_KM = 0.07
ESTIMATE_MIN_EUR = 8.0
ESTIMATE_MIN_ROAD_KM = 15.0        # shorter trips are not worth a ground search
ESTIMATE_FIRST_HOUR = 6
ESTIMATE_LAST_HOUR = 20            # inclusive; departures every ESTIMATE_STEP_HOURS
ESTIMATE_STEP_HOURS = 2
ESTIMATE_OPERATOR = "Tahmini tren/otobüs"
# Two live results from different sources are the same service when they share a mode and their
# departures and arrivals are this close (minutes). See RailBusProvider._merge_ground.
DUPLICATE_DEP_MINS = 5
DUPLICATE_ARR_MINS = 10

# Whole-day fetches. A day is asked once per (source, origin, destination, date) with no earliest time;
# the departures before a caller's earliest time are dropped locally (see _merge_live).
DAY_MAX_RESULTS = 500              # segments one source call may return for a whole day
TRENITALIA_PAGE_SIZE = 12          # Trenitalia answers one page of solutions from a departure time on
DAY_MAX_PAGES = 8                  # pages per paged source (Trenitalia, Transitous) and day
DAY_LAST_DEPARTURE_MINS = 23 * 60  # a page that reaches this departure is the last one of the day
LIVE_WORKERS = 4                   # live sources asked at once for one pair (RegioJet, FlixBus, Trenitalia, Transitous)
MEMO_MAX_ENTRIES = 512             # in-memory answers per provider (the persistent cache is larger)
REGIOJET_SOURCE = "regiojet"       # cache key of the RegioJet whole-day answer (its segments are "live")


def _clock_minutes(hhmm: str) -> int:
    """Minutes after midnight of an "HH:MM" text; ValueError when it is malformed."""
    parts = str(hhmm).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"not HH:MM: {hhmm!r}")
    return int(parts[0]) * 60 + int(parts[1])


def _hhmm_text(total_mins: int) -> str:
    total_mins %= 24 * 60
    return f"{total_mins // 60:02d}:{total_mins % 60:02d}"


def segment_from_dict(data: Dict[str, Any]) -> "ScheduledSegment":
    """Inverse of ScheduledSegment.to_dict (unknown keys are ignored)."""
    names = {field.name for field in dataclasses.fields(ScheduledSegment)}
    return ScheduledSegment(**{key: value for key, value in data.items() if key in names})


def _tagged(segments, source: str) -> List["ScheduledSegment"]:
    """A fresh list of the segments with their ``source`` set."""
    out = [dataclasses.replace(seg) for seg in segments]
    for seg in out:
        seg.source = source
    return out


def _paged_day(
    fetch: Callable[[Optional[str]], Optional[List["ScheduledSegment"]]],
    max_pages: int = DAY_MAX_PAGES,
) -> Optional[Tuple[List["ScheduledSegment"], bool]]:
    """The whole day from a source that answers one page of departures from a given time on.

    ``fetch(earliest)`` returns the page (``None`` on a failure). Each next page starts a minute after
    the last departure seen. Returns ``(segments, complete)``, or None when the first page gave no
    answer. ``complete`` is False when a later page failed or the page limit was reached: such an
    answer is used for the call but is not cached.
    """
    collected: List[ScheduledSegment] = []
    seen = set()
    earliest: Optional[str] = None
    for page_no in range(max_pages):
        page = fetch(earliest)
        if page is None:
            return (collected, False) if page_no else None
        fresh = []
        for seg in page:
            signature = (seg.departure_time, seg.arrival_time, seg.operator, seg.mode)
            if signature not in seen:
                seen.add(signature)
                fresh.append(seg)
        if not fresh:
            return collected, True
        collected.extend(fresh)
        last = max(_clock_minutes(seg.departure_time) for seg in fresh)
        if last >= DAY_LAST_DEPARTURE_MINS:
            return collected, True
        earliest = _hhmm_text(last + 1)
    return collected, False


def estimate_price_eur(road_km: float) -> float:
    """Distance-based fare estimate in EUR, shared by the estimate and by timetable sources without fares."""
    return round(max(ESTIMATE_MIN_EUR, road_km * ESTIMATE_EUR_PER_KM), 2)


def maps_budget_from_env() -> int:
    """Google Maps page loads per search from TRAVELPLAN_MAPS_BUDGET (0 or more); the default when unset or invalid."""
    raw = os.environ.get(MAPS_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_MAPS_BUDGET
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s: ignored %r (expected a whole number)", MAPS_BUDGET_ENV, raw)
        return DEFAULT_MAPS_BUDGET
    if value < 0:
        logger.warning("%s: ignored %r (must be 0 or more)", MAPS_BUDGET_ENV, raw)
        return DEFAULT_MAPS_BUDGET
    return value


class MapsBudget:
    """Google Maps page loads allowed for one search, shared by every find_ground call of that search.

    Counts page loads, not lookups: one lookup loads one page, or two when the first page is sparse.
    ``take()`` reserves one page load and returns False once the limit is used up; each refusal is
    counted in ``denied``. Answers from the Maps cache cost nothing. Thread-safe.
    """

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.used = 0
        self.denied = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.used < self.limit:
                self.used += 1
                return True
            self.denied += 1
            return False

    @property
    def exhausted(self) -> bool:
        """True when at least one page load was refused, i.e. some Maps lookup was skipped."""
        return self.denied > 0


@dataclass
class ScheduledSegment:
    origin_station: str
    dest_station: str
    departure_time: str   # "HH:MM"
    arrival_time: str     # "HH:MM"
    duration_mins: int
    # None when no fare is known (e.g. a Google Maps timetable without a shown price). Never invented.
    price_try: Optional[float]
    operator: str
    mode: str             # "train" or "bus"
    date: str             # "YYYY-MM-DD"
    # False only when the times come from a live source (see live_rail_provider).
    # Otherwise the times come from the static timetable table.
    is_estimated: bool = True
    # Original fare in EUR when it came from a live source; price_try is then the converted amount.
    price_eur: Optional[float] = None
    # True when price_try is a table estimate (no live fare), even if the times are live.
    price_is_estimated: bool = True
    # Where the segment came from: "live" (RegioJet API), "flixbus", "trenitalia", "transitous",
    # "google_maps", "table" (static corridor table) or "estimate" (distance-based, see RailBusProvider.find_ground).
    source: str = "table"
    # Real station names where the boarding and alighting happen, for the traveller to look for.
    # table: the corridor station names; live: RegioJet's station names (e.g. "Vienna - Hbf");
    # estimate: None (no station is known). origin_station/dest_station keep the places as given.
    origin_station_name: Optional[str] = None
    dest_station_name: Optional[str] = None
    # Indicative "from" fare in TRY ("560 TL'den başlayan" on obilet): the cheapest fare the operator
    # shows for the route on another day. Only a hint; price_try stays None when the fare is unknown.
    price_from_try: Optional[float] = None
    vehicle_types: List[str] = field(default_factory=list)
    arrival_day_offset: int = 0
    note: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "origin_station": self.origin_station,
            "dest_station": self.dest_station,
            "departure_time": self.departure_time,
            "arrival_time": self.arrival_time,
            "duration_mins": self.duration_mins,
            "price_try": self.price_try,
            "operator": self.operator,
            "mode": self.mode,
            "date": self.date,
            "is_estimated": self.is_estimated,
            "price_eur": self.price_eur,
            "price_is_estimated": self.price_is_estimated,
            "source": self.source,
            "origin_station_name": self.origin_station_name,
            "dest_station_name": self.dest_station_name,
            "price_from_try": self.price_from_try,
            "vehicle_types": list(self.vehicle_types),
            "arrival_day_offset": self.arrival_day_offset,
            "note": self.note,
        }

class RailBusProvider:
    """Provides schedule timetable and pricing for European high-speed rail and intercity bus routes."""

    def __init__(
        self,
        geo_registry: Optional[GeoClusterRegistry] = None,
        live_provider: Optional["LiveRailProvider"] = None,
        airport_db: Optional[AirportDB] = None,
        bus_provider: Optional["FlixBusProvider"] = None,
        timetable_provider: Optional["TransitousProvider"] = None,
        timetable_maps_provider: Optional["GoogleMapsTransitProvider"] = None,
        bus_from_price_provider: Optional["ObiletFromPrices"] = None,
        trenitalia_provider: Optional["TrenitaliaProvider"] = None,
        obilet_schedule_provider: Optional[Any] = None,
        maps_budget_limit: Optional[int] = None,
        ground_cache: Optional["ground_cache_module.GroundCache"] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.registry = geo_registry or GeoClusterRegistry()
        # Persistent whole-day answers (travel_engine/ground_cache.py). None: the default cache from the
        # environment (TRAVELPLAN_GROUND_CACHE), or no persistent cache when that is "off".
        self.ground_cache = ground_cache if ground_cache is not None else ground_cache_module.from_env()
        self._clock = clock
        # In-memory whole-day answers: key -> (expires_at, segments). Same lifetimes as the persistent cache.
        self._memo: Dict[tuple, Tuple[float, List["ScheduledSegment"]]] = {}
        self._memo_lock = threading.Lock()
        # One lock per key so that concurrent searches for the same pair and day ask a source once.
        self._key_locks: Dict[tuple, threading.Lock] = {}
        self._stats_lock = threading.Lock()
        self._stats_source_calls = Counter()
        self._stats_cache_hits = 0
        self._stats_maps_searches = 0
        # Optional live source tried first for the corridors it supports; the table is the fallback.
        self.live_provider = live_provider
        # Optional FlixBus source for find_ground (any object with can_resolve, search and health_status).
        self.bus_provider = bus_provider
        # Optional Transitous timetable source for find_ground: real times, estimated fares.
        self.timetable_provider = timetable_provider
        # Optional Google Maps timetable source (real times, fares only when Maps shows one).
        self.timetable_maps_provider = timetable_maps_provider
        # Optional obilet "from" fares per bus operator, attached to unpriced bus segments only.
        self.bus_from_price_provider = bus_from_price_provider
        # Optional Trenitalia source for find_ground: real Italian trains with the fare when shown (priced).
        self.trenitalia_provider = trenitalia_provider
        # Optional obilet route schedule source for TR <-> GR/BG bus routes (T16).
        self.obilet_schedule_provider = obilet_schedule_provider
        # Google Maps page loads per search (MapsBudget); 0 turns the Maps fallback off.
        self.maps_budget_limit = (
            maps_budget_from_env() if maps_budget_limit is None else max(0, int(maps_budget_limit))
        )
        # Airport coordinates for distance estimates; loaded lazily from the bundled CSV.
        self._airport_db = airport_db
        # Define baseline corridor parameters: (dep_hours, interval_hours, duration_mins, price_try, operator, mode)
        self._corridors = {
            ("Wien Hbf", "Praha hl.n."): (6, 2, 255, 750.0, "ÖBB Railjet / RegioJet", "train"),
            ("Praha hl.n.", "Wien Hbf"): (6, 2, 255, 750.0, "ÖBB Railjet / RegioJet", "train"),
            ("Bratislava hl.st.", "Praha hl.n."): (6, 2, 260, 650.0, "ČD EuroCity / RegioJet", "train"),
            ("Dresden Hbf", "Praha hl.n."): (7, 2, 140, 550.0, "EuroCity", "train"),
            ("Berlin Hbf", "Praha hl.n."): (7, 2, 270, 950.0, "EuroCity", "train"),
            ("Bratislava hl.st.", "Wien Hbf"): (6, 1, 50, 380.0, "ÖBB / Slovak Rail", "train"),
            ("Wien Hbf", "Bratislava hl.st."): (6, 1, 50, 380.0, "ÖBB / Slovak Rail", "train"),
            ("Budapest Keleti", "Wien Hbf"): (6, 2, 155, 650.0, "ÖBB Railjet", "train"),
            ("Wien Hbf", "Budapest Keleti"): (6, 2, 155, 650.0, "ÖBB Railjet", "train"),
            ("Budapest Keleti", "Bratislava hl.st."): (7, 2, 140, 550.0, "EuroCity", "train"),
            ("Bratislava hl.st.", "Budapest Keleti"): (7, 2, 140, 550.0, "EuroCity", "train"),
            ("Nürnberg Hbf", "München Hbf"): (6, 1, 65, 450.0, "DB ICE", "train"),
            ("Stuttgart Hbf", "München Hbf"): (6, 1, 125, 650.0, "DB ICE", "train"),
            ("Salzburg Hbf", "München Hbf"): (6, 1, 95, 520.0, "ÖBB Railjet", "train"),
            ("Dresden Hbf", "Berlin Hbf"): (6, 2, 120, 500.0, "DB EuroCity", "train"),
            ("Leipzig Hbf", "Berlin Hbf"): (6, 1, 75, 420.0, "DB ICE", "train"),
            ("Athens Larissis", "Thessaloniki"): (7, 3, 260, 850.0, "Hellenic Train", "train"),
            ("Thessaloniki", "Athens Larissis"): (7, 3, 260, 850.0, "Hellenic Train", "train"),
            ("Sofia Central", "Thessaloniki"): (8, 4, 300, 750.0, "Arda Tur / FlixBus", "bus"),
            # Estimates from a static table, like the other corridors
            ("Budapest Kelenföld", "Praha hl.n."): (8, 3, 420, 1100.0, "RegioJet", "train"),
            ("München ZOB", "Praha hl.n."): (7, 2, 300, 800.0, "FlixBus", "bus"),
        }

    def _parse_hhmm(self, time_str: str) -> int:
        parts = time_str.strip().split(":")
        return int(parts[0]) * 60 + int(parts[1])

    def _format_hhmm(self, total_mins: int) -> str:
        # Wrap within 24h
        total_mins = total_mins % (24 * 60)
        h = total_mins // 60
        m = total_mins % 60
        return f"{h:02d}:{m:02d}"

    def get_schedules(
        self,
        from_station: str,
        to_station: str,
        date_str: str,
        earliest_dep_time: Optional[str] = None
    ) -> List[ScheduledSegment]:
        key = (from_station.strip(), to_station.strip())
        if key not in self._corridors:
            return []

        if self.live_provider is not None and self.live_provider.supports(*key):
            live = self._live_schedules(key, date_str, earliest_dep_time)
            if live:
                return live

        return self._table_segments(key, from_station, to_station, date_str, earliest_dep_time)

    def _table_segments(
        self,
        key: tuple,
        origin_name: str,
        dest_name: str,
        date_str: str,
        earliest_dep_time: Optional[str],
    ) -> List[ScheduledSegment]:
        """Departures of one table corridor, labelled with the given origin and destination names.

        ``origin_name``/``dest_name`` are the places as given; the corridor's own station names
        are stored in ``origin_station_name``/``dest_station_name``.
        """
        start_h, step_h, dur_m, price, operator, mode = self._corridors[key]
        earliest_mins = self._parse_hhmm(earliest_dep_time) if earliest_dep_time else 0

        segments: List[ScheduledSegment] = []
        cur_h = start_h
        while cur_h < 22:
            dep_m = cur_h * 60
            if dep_m >= earliest_mins:
                arr_m = dep_m + dur_m
                segments.append(ScheduledSegment(
                    origin_station=origin_name,
                    dest_station=dest_name,
                    departure_time=self._format_hhmm(dep_m),
                    arrival_time=self._format_hhmm(arr_m),
                    duration_mins=dur_m,
                    price_try=price,
                    operator=operator,
                    mode=mode,
                    date=date_str,
                    origin_station_name=key[0],
                    dest_station_name=key[1],
                ))
            cur_h += step_h

        return segments

    def _live_schedules(
        self,
        key: tuple,
        date_str: str,
        earliest_dep_time: Optional[str],
    ) -> Optional[List[ScheduledSegment]]:
        """Live segments with table prices filled in where no live fare exists; None means use the table."""
        try:
            live = self.live_provider.get_schedules(key[0], key[1], date_str, earliest_dep_time)
        except Exception:  # the live source must never break the table path
            logger.exception("Live rail provider failed for %s -> %s", key[0], key[1])
            return None
        if not live:
            return None
        table_price = self._corridors[key][3]
        for seg in live:
            seg.source = "live"
            if seg.price_eur is None:
                seg.price_try = table_price
                seg.price_is_estimated = True
        return live

    def get_hub_connections(
        self,
        from_hub: str,
        to_city: str,
        date_str: str,
        earliest_dep_time: Optional[str] = None
    ) -> List[ScheduledSegment]:
        connections = self.registry.get_transit_connections(from_hub, to_city)
        results: List[ScheduledSegment] = []
        for conn in connections:
            schedules = self.get_schedules(
                from_station=conn.station_from,
                to_station=conn.station_to,
                date_str=date_str,
                earliest_dep_time=earliest_dep_time
            )
            results.extend(schedules)
        return results

    # -- ground options between any two places (route engine entry point) --------

    def stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            return {
                "source_calls": dict(self._stats_source_calls),
                "cache_hits": self._stats_cache_hits,
                "maps_searches": self._stats_maps_searches,
            }

    def find_ground(
        self,
        from_place: str,
        to_place: str,
        date_str: str,
        earliest_dep_time: Optional[str] = None,   # "HH:MM" local
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
        max_results: int = 6,
        max_road_km: float = 700.0,
        timetable_maps_provider: Optional["GoogleMapsTransitProvider"] = None,
        bus_from_price_provider: Optional["ObiletFromPrices"] = None,
        allow_estimate: bool = True,
        maps_budget: Optional[MapsBudget] = None,
    ) -> List[ScheduledSegment]:
        """Train/bus options between two European places, sorted by departure.

        Works for one date only: ``earliest_dep_time`` filters that day's departures, and the
        result is ``[]`` when nothing departs on ``date_str`` after that time. For the next-day
        rollover use ``find_ground_next``.

        Priced live sources first: RegioJet (``source="live"``), FlixBus (``source="flixbus"``, when
        ``bus_provider`` is set), Trenitalia (``source="trenitalia"``, when ``trenitalia_provider`` is
        set: real times and the fare when shown) and Transitous (``source="transitous"``, when
        ``timetable_provider`` is set and enabled: real times, estimated fares). Each one is skipped
        when it cannot resolve either place, and a failing source does not stop the others. Their
        results are merged (``_merge_ground``): cross-source duplicates are dropped, the segment with
        the better fare stays (a known fare, then an estimate, then no fare), the list is sorted by
        departure and capped at ``max_results``, with at least one segment of each source when several
        returned any.

        Google Maps timetables (``source="google_maps"``, when ``timetable_maps_provider`` is set) are a
        fallback: they are asked only when all the live sources above returned nothing for the pair.
        Their fare is shown only when Maps shows one. Every Maps page load is charged to
        ``maps_budget`` (a ``MapsBudget`` shared by one search; a new one of ``maps_budget_limit``
        page loads when None). A refused load is skipped, and ``maps_budget.exhausted`` tells the caller.

        Only when no live source (Maps included) returns anything are the static corridor table
        (``source="table"``) and then a distance estimate (``source="estimate"``, departures
        06:00-20:00 only) used. Table and estimate segments are always ``is_estimated=True``.
        ``allow_estimate=False`` skips both the table and the estimate: only live answers are returned.

        Bus segments without a fare get ``price_from_try`` from ``bus_from_price_provider`` (obilet)
        when its operator matches. ``price_try`` stays None: the "from" fare is only a hint.

        The provider arguments override the ones given to the constructor for this call.

        ``origin_station``/``dest_station`` are the places as given. ``origin_station_name``/
        ``dest_station_name`` are the real stations (None for estimates and most Maps options).

        A pure country token ("Greece", "Italy", "GR") is not a place, so it returns ``[]``
        (see ``_is_country_token``). A ``from_coord``/``to_coord`` given for a side overrides that check.
        """
        origin = from_place.strip()
        dest = to_place.strip()
        if not origin or not dest or origin.casefold() == dest.casefold():
            return []
        if self._is_unplaceable_country(origin, from_coord) or self._is_unplaceable_country(dest, to_coord):
            return []

        maps = timetable_maps_provider if timetable_maps_provider is not None else self.timetable_maps_provider
        obilet = bus_from_price_provider if bus_from_price_provider is not None else self.bus_from_price_provider
        found = self._live_found(origin, dest, date_str, earliest_dep_time, max_results, from_coord, to_coord)
        if not found and maps is not None:
            budget = maps_budget if maps_budget is not None else MapsBudget(self.maps_budget_limit)
            if budget.limit > 0:
                found = self._merge_ground(
                    [self._maps_ground(origin, dest, date_str, earliest_dep_time, max_results, maps, budget)],
                    max_results,
                )
        if found:
            self._attach_bus_from_prices(found, origin, dest, obilet)
            return found  # already merged, sorted and capped
        if not allow_estimate:
            return []
        found = self._table_ground(origin, dest, date_str, earliest_dep_time)
        if not found:
            found = self._estimate_ground(
                origin, dest, date_str, earliest_dep_time, from_coord, to_coord, max_road_km
            )
        found.sort(key=lambda seg: self._parse_hhmm(seg.departure_time))
        return found[:max(0, max_results)]

    def find_ground_live(
        self,
        from_place: str,
        to_place: str,
        date_str: str,
        earliest_dep_time: Optional[str] = None,
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
        max_results: int = 6,
        bus_from_price_provider: Optional["ObiletFromPrices"] = None,
    ) -> List[ScheduledSegment]:
        """The live part of ``find_ground`` only: RegioJet, FlixBus, Trenitalia and Transitous, merged.

        Same answer as ``find_ground`` whenever it returns live segments, but no Google Maps page load,
        no table and no estimate. ``[]`` means no live source answered, and then ``find_ground`` goes on
        to its fallbacks. The day's answers are shared with ``find_ground`` (see ``_live_days``).
        """
        origin = from_place.strip()
        dest = to_place.strip()
        if not origin or not dest or origin.casefold() == dest.casefold():
            return []
        if self._is_unplaceable_country(origin, from_coord) or self._is_unplaceable_country(dest, to_coord):
            return []
        obilet = bus_from_price_provider if bus_from_price_provider is not None else self.bus_from_price_provider
        found = self._live_found(origin, dest, date_str, earliest_dep_time, max_results, from_coord, to_coord)
        if found:
            self._attach_bus_from_prices(found, origin, dest, obilet)
        return found

    def find_ground_next(
        self,
        from_place: str,
        to_place: str,
        date_str: str,
        earliest_dep_time: Optional[str] = None,   # "HH:MM" local, applies to date_str only
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
        max_results: int = 6,
        max_road_km: float = 700.0,
        allow_estimate: bool = True,
        maps_budget: Optional[MapsBudget] = None,
    ) -> Tuple[str, List[ScheduledSegment]]:
        """``find_ground`` for ``date_str``, or for the next day from 00:00 when that day has none.

        Returns ``(date_used, segments)``. ``date_used`` is ``date_str`` when segments were found
        there, the following date when the rollover found them, and ``date_str`` with ``[]`` when
        neither day has a connection. Only one rollover is tried. An unparsable date gives ``[]``.
        Both calls share one ``maps_budget``.
        """
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return date_str, []

        if maps_budget is None:
            maps_budget = MapsBudget(self.maps_budget_limit)
        options = dict(from_coord=from_coord, to_coord=to_coord, max_results=max_results,
                       max_road_km=max_road_km, allow_estimate=allow_estimate, maps_budget=maps_budget)
        segments = self.find_ground(from_place, to_place, date_str, earliest_dep_time, **options)
        if segments:
            return date_str, segments
        next_date = (day + timedelta(days=1)).isoformat()
        segments = self.find_ground(from_place, to_place, next_date, None, **options)
        if segments:
            return next_date, segments
        return date_str, []

    def ground_distance_km(
        self,
        from_place: str,
        to_place: str,
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
    ) -> Optional[float]:
        """Estimated road distance in km (great-circle x 1.25), or None when a place has no coordinates.

        A pure country token without a given coordinate has none (None), see ``find_ground``.
        """
        if self._is_unplaceable_country(from_place, from_coord) or self._is_unplaceable_country(to_place, to_coord):
            return None
        a = self._place_coord(from_place, from_coord)
        b = self._place_coord(to_place, to_coord)
        if a is None or b is None:
            return None
        return haversine_km(a[0], a[1], b[0], b[1]) * ROAD_DISTANCE_FACTOR

    def _is_unplaceable_country(self, place: str, coord: Optional[Tuple[float, float]]) -> bool:
        return coord is None and self._is_country_token(place)

    def _is_country_token(self, place: str) -> bool:
        """True when ``place`` names a whole country rather than a city, station or airport.

        Heuristic, in order:
          1. The registry's country tables (English and Turkish names, 2-letter codes), see
             ``GeoClusterRegistry.is_country``. This works even with an empty airport database.
          2. Otherwise, the airport data: a token that is a city or airport (``AirportDB.find_city``
             or a 3-letter IATA code) is never a country.
          3. Otherwise, a token whose airports span more than one city is a country
             (e.g. "Italy" -> Rome, Milan, ...). A country with a single airport is not detected.
        """
        if self.registry.is_country(place):
            return True
        db = self._airports()
        airports = db.resolve(place)
        if len(airports) < 2 or db.find_city(place):
            return False
        return len({ap.city for ap in airports}) > 1

    def _airports(self) -> AirportDB:
        if self._airport_db is None:
            self._airport_db = AirportDB.load_default()
        return self._airport_db

    def _place_coord(self, place: str, coord: Optional[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
        if coord is not None:
            return float(coord[0]), float(coord[1])
        db = self._airports()
        centre = db.city_center(place)
        if centre is not None:
            return centre
        airports = db.resolve(place)
        if airports:
            return airports[0].lat, airports[0].lon
        return None

    def _place_airport(self, place: str, coord: Optional[Tuple[float, float]]) -> Optional["Airport"]:
        """An airport standing for ``place`` (its country and landmass): the place's own airports when they are
        all in one country, else the airport nearest to the place's point within 100 km. None when unknown."""
        db = self._airports()
        airports = db.resolve(place)
        if airports and len({ap.country for ap in airports}) == 1:
            return airports[0]
        point = self._place_coord(place, coord)
        if point is not None:
            near = db.nearby(point[0], point[1], 100, limit=1)
            if near:
                return near[0][0]
        return None

    def _estimate_allowed(self, origin: str, dest: str, from_coord, to_coord) -> bool:
        """The distance estimate is used only inside one country on one landmass (DOG-8)."""
        a = self._place_airport(origin, from_coord)
        b = self._place_airport(dest, to_coord)
        if a is None or b is None or a.country != b.country:
            return False
        return self._airports().ground_reachable(a.iata, b.iata)

    # -- whole-day live answers ------------------------------------------------

    def _live_found(
        self,
        origin: str,
        dest: str,
        date_str: str,
        earliest_dep_time: Optional[str],
        max_results: int,
        from_coord: Optional[Tuple[float, float]] = None,
        to_coord: Optional[Tuple[float, float]] = None,
    ) -> List[ScheduledSegment]:
        """RegioJet, FlixBus, Trenitalia and Transitous departures after ``earliest_dep_time``, merged.

        Empty when none returns anything. Google Maps is not part of it (a fallback, see find_ground).
        """
        return self._merge_live(self._live_days(origin, dest, date_str, from_coord, to_coord),
                                earliest_dep_time, max_results)

    def _live_days(
        self,
        origin: str,
        dest: str,
        date_str: str,
        from_coord: Optional[Tuple[float, float]],
        to_coord: Optional[Tuple[float, float]],
    ) -> List[Tuple[str, Optional[List[ScheduledSegment]]]]:
        """Whole-day answers of the priced live sources, in priority order, asked at the same time.

        One item per configured source: ``(source key, segments)``. ``segments`` is None when the source
        gave no answer (it cannot resolve a place, or it failed). Each source is asked once per
        (origin, destination, date) while its answer is fresh (see ``_day_answer``).
        """
        plan = []
        if self.live_provider is not None:
            plan.append((REGIOJET_SOURCE, self._call_regiojet))
        if self.bus_provider is not None:
            plan.append(("flixbus", self._call_flixbus))
        if self.trenitalia_provider is not None:
            plan.append(("trenitalia", self._call_trenitalia))
        if self.timetable_provider is not None:
            plan.append(("transitous", self._call_transitous))
        if self.obilet_schedule_provider is not None and self._is_tr_gr_or_bg_pair(origin, dest, from_coord, to_coord):
            plan.append(("obilet_page", self._call_obilet_schedule))

        def ask(item):
            source, call = item
            return source, self._day_answer(source, origin, dest, date_str, from_coord, to_coord, call)

        if len(plan) <= 1:
            return [ask(item) for item in plan]
        with ThreadPoolExecutor(max_workers=min(LIVE_WORKERS, len(plan))) as pool:
            return list(pool.map(ask, plan))  # pool.map keeps the priority order

    def _merge_live(
        self,
        answers: List[Tuple[str, Optional[List[ScheduledSegment]]]],
        earliest_dep_time: Optional[str],
        max_results: int,
    ) -> List[ScheduledSegment]:
        """The departures after ``earliest_dep_time`` of each source's whole-day answer, merged (``_merge_ground``).

        A source's list is cut to ``max_results`` after the earliest filter, as the source itself cut it
        before (RegioJet never did), so the result is the one a search with that earliest time gave.
        """
        try:
            earliest = _clock_minutes(earliest_dep_time) if earliest_dep_time else None
        except ValueError:
            return []  # a malformed time: the sources used to answer nothing for it
        groups: List[List[ScheduledSegment]] = []
        for source, segments in answers:
            if not segments:
                continue
            picked = [seg for seg in segments
                      if earliest is None or self._parse_hhmm(seg.departure_time) >= earliest]
            picked.sort(key=lambda seg: self._parse_hhmm(seg.departure_time))
            if source != REGIOJET_SOURCE:
                picked = picked[:max(0, max_results)]
            picked = [dataclasses.replace(seg) for seg in picked]
            if picked:
                groups.append(picked)
        if not groups:
            return []
        return self._merge_ground(groups, max_results)

    def _day_answer(
        self,
        source: str,
        origin: str,
        dest: str,
        date_str: str,
        from_coord: Optional[Tuple[float, float]],
        to_coord: Optional[Tuple[float, float]],
        call: Callable[..., Tuple[Optional[List[ScheduledSegment]], bool]],
    ) -> Optional[List[ScheduledSegment]]:
        """One source's whole-day answer: from memory or the persistent cache, else asked once and stored.

        Concurrent calls for the same key wait for the first one, so the source is asked once per TTL.
        ``None`` (no answer) and a partial answer are returned to this call only and never stored.
        """
        key = self._key(source, origin, dest, date_str, "", from_coord, to_coord)
        with self._key_lock(key):
            known = self._memo_get(key, origin, dest)
            if known is not None:
                with self._stats_lock:
                    self._stats_cache_hits += 1
                return known
            with self._stats_lock:
                self._stats_source_calls[source] += 1
            try:
                segments, cacheable = call(origin, dest, date_str, from_coord, to_coord)
            except Exception:  # a source problem must never break the route engine
                logger.exception("%s ground search failed for %s -> %s", source, origin, dest)
                return None
            if segments is None:
                return None
            if cacheable:
                self._store(key, segments)
            return [dataclasses.replace(seg) for seg in segments]

    def _call_regiojet(self, origin, dest, date_str, from_coord, to_coord):
        """(segments or None, cacheable) of RegioJet for the whole day (the coordinates are not used)."""
        live = self.live_provider
        try:
            if not (live.can_resolve(origin) and live.can_resolve(dest)):
                return None, False
            found = live.search(origin, dest, date_str, None)
        except Exception:  # a live problem must never break the route engine
            logger.exception("Live ground search failed for %s -> %s", origin, dest)
            return None, False
        if found is None:
            return None, False
        return _tagged(found, "live"), True

    def _call_flixbus(self, origin, dest, date_str, from_coord, to_coord):
        bus = self.bus_provider
        try:
            if not (bus.can_resolve(origin) and bus.can_resolve(dest)):
                return None, False
            found = bus.search(origin, dest, date_str, None, max_results=DAY_MAX_RESULTS)
        except Exception:  # a bus problem must never break the route engine
            logger.exception("FlixBus ground search failed for %s -> %s", origin, dest)
            return None, False
        if found is None:
            return None, False
        return _tagged(found, "flixbus"), True

    def _call_trenitalia(self, origin, dest, date_str, from_coord, to_coord):
        trenitalia = self.trenitalia_provider
        try:
            if not (trenitalia.can_resolve(origin) and trenitalia.can_resolve(dest)):
                return None, False
            paged = _paged_day(lambda earliest: trenitalia.search(
                origin, dest, date_str, earliest, max_results=TRENITALIA_PAGE_SIZE))
        except Exception:  # a Trenitalia problem must never break the route engine
            logger.exception("Trenitalia ground search failed for %s -> %s", origin, dest)
            return None, False
        if paged is None:
            return None, False
        segments, complete = paged
        return _tagged(segments, "trenitalia"), complete

    def _call_transitous(self, origin, dest, date_str, from_coord, to_coord):
        timetable = self.timetable_provider
        try:
            # A given coordinate needs no name lookup; a place without one must resolve by name.
            if from_coord is None and not timetable.can_resolve(origin):
                return None, False
            if to_coord is None and not timetable.can_resolve(dest):
                return None, False
            paged = _paged_day(lambda earliest: timetable.search(
                origin, dest, date_str, earliest, from_coord=from_coord, to_coord=to_coord,
                max_results=DAY_MAX_RESULTS))
        except Exception:  # a timetable problem must never break the route engine
            logger.exception("Transitous ground search failed for %s -> %s", origin, dest)
            return None, False
        if paged is None:
            return None, False
        segments, complete = paged
        return _tagged(segments, "transitous"), complete

    def _call_obilet_schedule(self, origin, dest, date_str, from_coord, to_coord):
        provider = self.obilet_schedule_provider
        if provider is None:
            return None, False
        try:
            segments = provider.departures(origin, dest, date_str=date_str)
        except Exception:
            logger.exception("obilet schedule ground search failed for %s -> %s", origin, dest)
            return None, False
        if not segments:
            return [], True
        return _tagged(segments, "obilet_page"), True

    def _country_of(self, place: str, coord: Optional[Tuple[float, float]]) -> Optional[str]:
        ap = self._place_airport(place, coord)
        if ap is not None and getattr(ap, "country", None):
            return ap.country

        name = (place or "").strip().lower()
        # Direct fold removal of diacritics
        import unicodedata
        folded = "".join(c for c in unicodedata.normalize("NFKD", name.replace("ı", "i").replace("İ", "i"))
                         if not unicodedata.combining(c))

        if folded in {
            "dedeagac", "alexandroupoli", "alexandroupolis", "selanik", "thessaloniki",
            "atina", "athens", "gumulcine", "komotini", "iskece", "xanthi", "kavala"
        }:
            return "GR"
        if folded in {
            "sofya", "sofia", "filibe", "plovdiv", "burgaz", "burgas", "varna"
        }:
            return "BG"
        if folded in {"istanbul", "esenler", "ankara", "izmir", "edirne", "canakkale", "bursa", "kesan"}:
            return "TR"

        try:
            from travel_engine.providers.obilet_fares import route_slug
            slug = route_slug(place)
            if slug == "istanbul":
                return "TR"
            if slug in {"selanik", "dedeagac"}:
                return "GR"
            if slug in {"sofya", "filibe"}:
                return "BG"
            if slug is not None:
                return "TR"
        except Exception:
            pass
        return None

    def _is_tr_gr_or_bg_pair(self, origin: str, dest: str, from_coord=None, to_coord=None) -> bool:
        c1 = self._country_of(origin, from_coord)
        c2 = self._country_of(dest, to_coord)
        if not c1 or not c2:
            return False
        return (c1 == "TR" and c2 in ("GR", "BG")) or (c2 == "TR" and c1 in ("GR", "BG"))

    # -- memory and persistent cache of whole-day answers -----------------------

    def _key(self, source, origin, dest, date_str, anchor, from_coord, to_coord) -> tuple:
        return (source, ground_cache_module.norm_place(origin), ground_cache_module.norm_place(dest),
                date_str, anchor, ground_cache_module.coords_text(from_coord, to_coord))

    def _key_lock(self, key: tuple) -> threading.Lock:
        with self._memo_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = self._key_locks[key] = threading.Lock()
            return lock

    def _ttl_for(self, count: int) -> float:
        if self.ground_cache is not None:
            return self.ground_cache.ttl_for(count)
        return ground_cache_module.default_ttl_for(count)

    def _memo_get(self, key: tuple, origin: str, dest: str) -> Optional[List[ScheduledSegment]]:
        """Fresh copies of the stored answer for ``key``, with the places named as given now; None on a miss."""
        now = self._clock()
        with self._memo_lock:
            entry = self._memo.get(key)
        segments = entry[1] if entry is not None and entry[0] > now else None
        if segments is None and self.ground_cache is not None:
            segments = self._persistent_get(key)
            if segments is not None:
                self._memo_put(key, segments, now)
        if segments is None:
            return None
        return [dataclasses.replace(seg, origin_station=origin, dest_station=dest) for seg in segments]

    def _persistent_get(self, key: tuple) -> Optional[List[ScheduledSegment]]:
        source, origin, dest, date_str, anchor, coords = key
        rows = self.ground_cache.get(source, origin, dest, date_str, anchor=anchor, coords=coords)
        if rows is None:
            return None
        try:
            return [segment_from_dict(row) for row in rows]
        except (TypeError, ValueError):
            logger.warning("Ground cache entry unreadable for %s; treating as a miss", source, exc_info=True)
            return None

    def _store(self, key: tuple, segments: List[ScheduledSegment]) -> None:
        self._memo_put(key, segments, self._clock())
        if self.ground_cache is not None:
            source, origin, dest, date_str, anchor, coords = key
            self.ground_cache.put(source, origin, dest, date_str, [seg.to_dict() for seg in segments],
                                  anchor=anchor, coords=coords)

    def _memo_put(self, key: tuple, segments: List[ScheduledSegment], now: float) -> None:
        with self._memo_lock:
            if key not in self._memo and len(self._memo) >= MEMO_MAX_ENTRIES:
                for stale in [k for k, (until, _) in self._memo.items() if until <= now]:
                    del self._memo[stale]
                if len(self._memo) >= MEMO_MAX_ENTRIES:
                    del self._memo[min(self._memo, key=lambda k: self._memo[k][0])]
            self._memo[key] = (now + self._ttl_for(len(segments)), [dataclasses.replace(seg) for seg in segments])

    def _maps_ground(
        self,
        origin: str,
        dest: str,
        date_str: str,
        earliest_dep_time: Optional[str],
        max_results: int,
        maps: Optional["GoogleMapsTransitProvider"],
        budget: MapsBudget,
    ) -> List[ScheduledSegment]:
        """Google Maps options from ``earliest_dep_time`` on (a fallback, see find_ground).

        Maps answers from the time it is asked from, so its entry is kept per anchor (``earliest_dep_time``,
        "00:00" when None). A cached answer costs no page load; a refused or failed one is not cached.
        """
        if maps is None:
            return []
        try:
            if not (maps.can_resolve(origin) and maps.can_resolve(dest)):
                return []
        except Exception:  # a timetable problem must never break the route engine
            logger.exception("Google Maps ground search failed for %s -> %s", origin, dest)
            return []
        key = self._key("google_maps", origin, dest, date_str, earliest_dep_time or "00:00", None, None)
        with self._key_lock(key):
            segments = self._memo_get(key, origin, dest)
            if segments is None:
                with self._stats_lock:
                    self._stats_maps_searches += 1
                try:
                    found = maps.search(origin, dest, date_str, earliest_dep_time,
                                        max_results=DAY_MAX_RESULTS, budget=budget)
                except Exception:  # a timetable problem must never break the route engine
                    logger.exception("Google Maps ground search failed for %s -> %s", origin, dest)
                    return []
                if found is None:  # failed, blocked, or no page load left in the budget
                    return []
                segments = _tagged(found, "google_maps")
                self._store(key, segments)
        if not segments:
            return []
        earliest = _clock_minutes(earliest_dep_time) if earliest_dep_time else None
        picked = [seg for seg in segments if earliest is None or self._parse_hhmm(seg.departure_time) >= earliest]
        return picked[:max(0, max_results)]

    def _attach_bus_from_prices(
        self,
        segments: List[ScheduledSegment],
        origin: str,
        dest: str,
        obilet: Optional["ObiletFromPrices"],
    ) -> None:
        """Set ``price_from_try`` on bus segments without a fare, from obilet's operator table (in place)."""
        unpriced = [seg for seg in segments if seg.mode == "bus" and seg.price_try is None]
        if obilet is None or not unpriced:
            return
        try:
            prices = obilet.from_prices(origin, dest)
        except Exception:  # an indicative price must never break the search
            logger.exception("obilet from-prices failed for %s -> %s", origin, dest)
            return
        if not prices:
            return
        for seg in unpriced:
            seg.price_from_try = price_for_operator(prices, seg.operator)

    def _merge_ground(self, groups: List[List[ScheduledSegment]], max_results: int) -> List[ScheduledSegment]:
        """Merge live results, one list per source in priority order.

        A segment is dropped when another source already has the same service (``_same_service``);
        of the two, ``_prefers`` decides which one stays. Duplicates within one source are kept.
        The result is sorted by departure and capped at ``max_results`` with source diversity
        (``_with_source_diversity``).
        """
        kept: List[ScheduledSegment] = []
        for group in groups:
            for seg in group:
                twin_at = next(
                    (i for i, other in enumerate(kept)
                     if other.source != seg.source and self._same_service(other, seg)),
                    None,
                )
                if twin_at is None:
                    kept.append(seg)
                elif self._prefers(seg, kept[twin_at]):
                    kept[twin_at] = seg
        kept.sort(key=lambda seg: self._parse_hhmm(seg.departure_time))
        return self._with_source_diversity(kept, max(0, max_results))

    def _same_service(self, a: ScheduledSegment, b: ScheduledSegment) -> bool:
        return (
            a.mode == b.mode
            and self._clock_gap(a.departure_time, b.departure_time) <= DUPLICATE_DEP_MINS
            and self._clock_gap(a.arrival_time, b.arrival_time) <= DUPLICATE_ARR_MINS
        )

    def _clock_gap(self, a: str, b: str) -> int:
        """Minutes between two HH:MM times of day, the short way round midnight."""
        diff = abs(self._parse_hhmm(a) - self._parse_hhmm(b)) % (24 * 60)
        return min(diff, 24 * 60 - diff)

    @staticmethod
    def _price_rank(seg: ScheduledSegment) -> int:
        """2 for a known fare, 1 for an estimate, 0 when no fare is known (price_try None)."""
        if seg.price_try is None:
            return 0
        return 1 if seg.price_is_estimated else 2

    @classmethod
    def _prefers(cls, new: ScheduledSegment, old: ScheduledSegment) -> bool:
        """True when ``new`` should replace the duplicate ``old``.

        A known fare beats an estimate, and an estimate beats no fare. Between two fares of the same
        kind the cheaper one wins. Two segments without a fare keep ``old`` (the earlier source).
        """
        new_rank, old_rank = cls._price_rank(new), cls._price_rank(old)
        if new_rank != old_rank:
            return new_rank > old_rank
        return new_rank > 0 and new.price_try < old.price_try

    def _with_source_diversity(self, ordered: List[ScheduledSegment], limit: int) -> List[ScheduledSegment]:
        """The first ``limit`` segments of a departure-sorted list, with one of each source when possible."""
        head = ordered[:limit]
        sources = list(dict.fromkeys(seg.source for seg in ordered))
        if limit < 2 or len(sources) < 2:
            return head
        for source in sources:
            if any(seg.source == source for seg in head):
                continue
            counts = Counter(seg.source for seg in head)
            # Make room by replacing the latest segment of a source that has more than one in the head.
            victim = next((i for i in range(len(head) - 1, -1, -1) if counts[head[i].source] > 1), None)
            if victim is None:
                break
            head[victim] = next(seg for seg in ordered if seg.source == source)
            head.sort(key=lambda seg: self._parse_hhmm(seg.departure_time))
        return head

    def sources_health(self) -> dict:
        """Health of the ground sources used by find_ground: ``regiojet``, ``flixbus``, ``trenitalia``,
        ``transitous``, ``google_maps`` and ``obilet`` (fare hints).

        Each value is the provider's own ``health_status()`` when it has one, ``{"available": True}``
        when it does not, and ``{"available": False}`` when the source is not configured.
        """
        return {
            "regiojet": self._source_health(self.live_provider),
            "flixbus": self._source_health(self.bus_provider),
            "trenitalia": self._source_health(self.trenitalia_provider),
            "transitous": self._source_health(self.timetable_provider),
            "google_maps": self._source_health(self.timetable_maps_provider),
            "obilet": self._source_health(self.bus_from_price_provider),
        }

    @staticmethod
    def _source_health(provider) -> dict:
        if provider is None:
            return {"available": False}
        health = getattr(provider, "health_status", None)
        if not callable(health):
            return {"available": True}
        try:
            return dict(health())
        except Exception as exc:  # a health report must never fail
            logger.exception("Health check failed for %s", type(provider).__name__)
            return {"available": True, "error": str(exc)}

    def _table_ground(
        self, origin: str, dest: str, date_str: str, earliest_dep_time: Optional[str],
    ) -> List[ScheduledSegment]:
        pairs: List[Tuple[str, str]] = []
        # Airport-code form ("DRS" -> Prague): the registry's transit links into the cluster city.
        for conn in self.registry.get_transit_connections(origin, dest):
            pairs.append((conn.station_from, conn.station_to))
        # Station names or cluster cities on either side.
        for from_station in self._table_stations(origin):
            for to_station in self._table_stations(dest):
                pairs.append((from_station, to_station))

        segments: List[ScheduledSegment] = []
        seen_pairs = set()
        seen_departures = set()
        for pair in pairs:
            if pair not in self._corridors or pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            for seg in self._table_segments(pair, origin, dest, date_str, earliest_dep_time):
                # Two station pairs may produce the same departure under the given names.
                signature = (seg.departure_time, seg.arrival_time, seg.operator, seg.mode)
                if signature in seen_departures:
                    continue
                seen_departures.add(signature)
                segments.append(seg)
        return segments

    def _table_stations(self, place: str) -> List[str]:
        """Table station names that a place (station name or cluster city/airport) stands for."""
        name = place.strip()
        if any(name in pair for pair in self._corridors):
            return [name]
        # Cluster city or airport: main station, transit-connection stations, and stations
        # that other clusters' links leave from this cluster's airport (e.g. Budapest Kelenföld for BUD).
        return self.registry.stations_for(name)

    def _estimate_ground(
        self,
        origin: str,
        dest: str,
        date_str: str,
        earliest_dep_time: Optional[str],
        from_coord: Optional[Tuple[float, float]],
        to_coord: Optional[Tuple[float, float]],
        max_road_km: float,
    ) -> List[ScheduledSegment]:
        """The distance estimate is used only inside one country on one landmass (DOG-8)."""
        road_km = self.ground_distance_km(origin, dest, from_coord, to_coord)
        if road_km is None or road_km > max_road_km or road_km < ESTIMATE_MIN_ROAD_KM:
            return []
        if not self._estimate_allowed(origin, dest, from_coord, to_coord):
            return []

        duration = round(road_km / ESTIMATE_SPEED_KMH * 60 + ESTIMATE_OVERHEAD_MINS)
        price_eur = estimate_price_eur(road_km)
        price_try = round(price_eur * fx.get_eur_try_rate()[0], 2)
        earliest_mins = self._parse_hhmm(earliest_dep_time) if earliest_dep_time else 0

        segments: List[ScheduledSegment] = []
        for hour in range(ESTIMATE_FIRST_HOUR, ESTIMATE_LAST_HOUR + 1, ESTIMATE_STEP_HOURS):
            dep_m = hour * 60
            if dep_m < earliest_mins:
                continue
            segments.append(ScheduledSegment(
                origin_station=origin,
                dest_station=dest,
                departure_time=self._format_hhmm(dep_m),
                arrival_time=self._format_hhmm(dep_m + duration),  # may cross midnight
                duration_mins=duration,
                price_try=price_try,
                operator=ESTIMATE_OPERATOR,
                mode="bus",
                date=date_str,
                is_estimated=True,
                price_eur=price_eur,
                price_is_estimated=True,
                source="estimate",
            ))
        return segments
