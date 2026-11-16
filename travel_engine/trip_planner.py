"""Trip planner: ranked travel options for one origin -> destination request.

Option kinds:
- "direct": each flight the provider returns for origin -> destination (single ticket, no via point).
- "side_route": MultiModalRouter journeys (flight + airport transfer + rail/bus), separate tickets.
- "via_single_ticket": a single-ticket flight whose layover is at a via airport.
- "via_separate": two separate tickets, origin -> V and V -> destination, with a self-transfer buffer.

When `via` is set, only the via options are produced (side routes are not combined with via).

Times: flight and journey times are "HH:MM" plus a day offset from the search date. Every option is
converted to absolute datetimes so that arrive_by / depart_after compare full timestamps.

Search counting (flight_searches): every search_flights call this planner makes is counted once per
(origin, destination, date) because results are cached within one plan_trip call. Catchment searches
made internally by MultiModalRouter are added as an upper-bound estimate: one per catchment airport of
the destination cluster, per side-route date. The estimate is exact when the router uses the same
provider and the provider does not cache.

Route engine mode (``route_engine=`` given): ``plan_trip`` delegates the chain search to
``travel_engine.route_engine.RouteEngine.search`` and maps its options to the same contract. Mapping:
- engine kind "direct" -> "direct" (category direct). With a via set it becomes "via_single_ticket".
- engine kind "side_route", "ground" -> category side, kind unchanged.
- engine kind "multi_flight" -> category side, kind "multi_flight"; with a via set, "via_separate".
- include_side_routes=False maps to max_stops=0 and also drops single-leg "ground" options.
Extra features, in both modes: ``flex_days`` (date window, "flex" key), ``return_dates`` (second search
destination -> origin, "roundtrip" key with combinations) and ``search_log`` (price advice, "advice" key).
Without ``route_engine`` the legacy search above is used; the new keyword arguments that only the
route engine understands (max_stops, flight_search_budget, min_stay_mins, bag flags) are then ignored.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from travel_engine import advisor
from travel_engine.flexdates import expand_flex_dates, flex_summary
from travel_engine.geo_clusters import GeoClusterRegistry
from travel_engine.multimodal_router import (
    DEFAULT_TIME_VALUE_TRY_PER_HOUR,
    NIGHT_WAIT_MIN_OVERLAP_MINS,
    NIGHT_WAIT_PENALTY_TRY,
    _night_overlap_mins,
    extract_flight_day_offset,
    extract_flight_times,
)
from travel_engine.roundtrip import combine_roundtrip
from travel_engine.route_engine import REAL_ONLY_EMPTY_WARNING, departure_date_warning

logger = logging.getLogger(__name__)

DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%dT%H:%M"
DEFAULT_SELF_TRANSFER_BUFFER_MINS = 180
# Route engine results are requested with this cap so category bests (cheapest direct / side) see every
# option; the public "options" list is trimmed to max_results afterwards.
ENGINE_MAX_RESULTS = 500
# Legacy results are ranked over everything; the same trimming is applied after the search.
LEGACY_MAX_RESULTS = 10 ** 6
FLEX_MAX_DATES = 7
ROUTE_DIRECT_KINDS = ("direct", "via_single_ticket")
ROUTE_SIDE_KINDS = ("side_route", "via_separate", "ground", "multi_flight")
# Separate tickets carry a risk premium: a delayed first leg means the second ticket is lost
SEPARATE_TICKET_RISK_TRY = 500.0
MAX_VIA_AIRPORTS_SEARCHED = 4
# Searched first, in this order, when several via airports are available
VIA_PRIORITY = ("ATH", "SKG")

BADGE_CHEAPEST = "En Ucuz"
BADGE_FASTEST = "En Hızlı"
BADGE_BEST = "Önerilen"

_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_AIRPORT_CODE_RE = re.compile(r"\b([A-Z]{3})\b")


@dataclass
class _Candidate:
    """Internal option with datetime values; serialised to the public option dict at the end."""
    search_date: str
    kind: str
    depart: datetime
    arrive: datetime
    legs: List[Dict[str, Any]]
    total_try: float
    true_total_try: float
    separate_tickets: bool
    via_stop: Optional[str]
    night_wait: bool
    has_estimated_segments: bool


@dataclass
class _SearchContext:
    cache: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = field(default_factory=dict)
    flight_searches: int = 0
    searched_dates: Set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- helpers

def _parse_date(value: Any, name: str) -> datetime:
    try:
        return datetime.strptime(str(value).strip(), DATE_FMT)
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def _parse_datetime(value: Any, name: str) -> datetime:
    try:
        return datetime.strptime(str(value).strip(), DATETIME_FMT)
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DDTHH:MM, got {value!r}") from exc


def _parse_hhmm(text: Any) -> Optional[Tuple[int, int]]:
    match = _HHMM_RE.match(str(text or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _fmt(moment: datetime) -> str:
    return moment.strftime(DATETIME_FMT)


def _place_at_or_after(base: datetime, hhmm: Tuple[int, int], not_before: Optional[datetime]) -> datetime:
    """Place a clock time on the base day, or on the next day(s) so it is not before `not_before`."""
    candidate = base.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
    while not_before is not None and candidate < not_before:
        candidate += timedelta(days=1)
    return candidate


def _flight_times(flight: Dict[str, Any], base: datetime) -> Optional[Tuple[datetime, datetime]]:
    """Absolute (departure, arrival) of a flight dict searched on `base`, or None when unparsable."""
    times = extract_flight_times(flight)
    if times is None:
        return None
    dep_hm = _parse_hhmm(times[0])
    arr_hm = _parse_hhmm(times[1])
    if dep_hm is None or arr_hm is None:
        return None
    # Landing day: the "+N" suffix, or an arrival clock time before departure means the next day
    arr_day = max(extract_flight_day_offset(flight), 1 if arr_hm < dep_hm else 0)
    dep = base.replace(hour=dep_hm[0], minute=dep_hm[1])
    arr = base.replace(hour=arr_hm[0], minute=arr_hm[1]) + timedelta(days=arr_day)
    return dep, arr


def _price(flight: Dict[str, Any]) -> float:
    try:
        return float(flight.get("total_price_try") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _layover_codes(flight: Dict[str, Any], origin: str, destination: str) -> List[str]:
    """Intermediate airport codes of a connecting flight.

    Taken from layover_info ("2 sa. 40 dk. ATH (Atina)"). Falls back to the airports field, where only
    intermediate codes survive once origin and destination are removed ("PRG ➔ ATH ➔ IST").
    """
    ends = {origin.upper(), destination.upper()}
    codes = _AIRPORT_CODE_RE.findall(str(flight.get("layover_info") or ""))
    if not codes:
        codes = _AIRPORT_CODE_RE.findall(str(flight.get("airports") or ""))
    unique: List[str] = []
    for code in codes:
        if code not in ends and code not in unique:
            unique.append(code)
    return unique


def _night_wait(start: datetime, end: datetime) -> bool:
    """True when the gap between start and end overlaps the 00:00-06:00 window long enough."""
    if end <= start:
        return False
    base = datetime.combine(start.date(), time())
    start_m = int((start - base).total_seconds() // 60)
    end_m = int((end - base).total_seconds() // 60)
    return _night_overlap_mins(start_m, end_m) >= NIGHT_WAIT_MIN_OVERLAP_MINS


def _score(true_total: float, duration_mins: int, night_wait: bool, separate: bool, time_value: float) -> float:
    score = true_total + (duration_mins / 60.0) * time_value
    if night_wait:
        score += NIGHT_WAIT_PENALTY_TRY * time_value / DEFAULT_TIME_VALUE_TRY_PER_HOUR
    if separate:
        score += SEPARATE_TICKET_RISK_TRY
    return round(score, 2)


def _flight_leg(flight: Dict[str, Any], frm: str, to: str, dep: datetime, arr: datetime) -> Dict[str, Any]:
    return {
        "type": "flight",
        "operator": str(flight.get("airline") or "Havayolu"),
        "from": frm,
        "to": to,
        "depart": dep,
        "arrive": arr,
        "price_try": _price(flight),
        "is_estimated": False,
        "booking_url": str(flight.get("google_flights_url") or flight.get("booking_url") or ""),
        # The card's stop wording: a connection stays a connection even when it is one ticket
        "stops": str(flight.get("stops") or "") or None,
    }


def _via_tokens(via: Any) -> Optional[List[str]]:
    if via is None:
        return None
    items = [via] if isinstance(via, str) else list(via)
    tokens = [str(item).strip() for item in items if item is not None and str(item).strip()]
    return tokens or None


def _prioritise(codes: Sequence[str]) -> List[str]:
    rank = {code: index for index, code in enumerate(VIA_PRIORITY)}
    return sorted(codes, key=lambda code: rank.get(code, len(VIA_PRIORITY)))


def _station_name(value: Any) -> Optional[str]:
    """A station name as text, or None when missing or blank."""
    text = str(value or "").strip()
    return text or None


def _leg_source(value: Any) -> Optional[str]:
    """A leg's price/time source (live, flixbus, table, estimate, google_flights) or None when missing."""
    text = str(value or "").strip()
    return text or None


def _optional_flag(value: Any) -> Optional[bool]:
    """A leg's price_is_estimated as a bool, or None when the leg does not say."""
    return None if value is None else bool(value)


def _serialise_leg(leg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": leg["type"],
        "operator": leg["operator"],
        "from": leg["from"],
        "to": leg["to"],
        "depart_at": _fmt(leg["depart"]),
        "arrive_at": _fmt(leg["arrive"]),
        # Time zone fields come from the route engine only; the legacy path keeps naive times (null)
        "depart_tz": None,
        "arrive_tz": None,
        "depart_utc": None,
        "arrive_utc": None,
        "price_try": round(float(leg["price_try"]), 2),
        "is_estimated": bool(leg["is_estimated"]),
        "booking_url": leg["booking_url"],
        # Real station names of a ground leg (null when unknown, e.g. router segments)
        "origin_station_name": _station_name(leg.get("origin_station_name")),
        "dest_station_name": _station_name(leg.get("dest_station_name")),
        "source": _leg_source(leg.get("source")),
        "price_is_estimated": _optional_flag(leg.get("price_is_estimated")),
        "from_coord": leg.get("from_coord"),
        "to_coord": leg.get("to_coord"),
    }


def _to_option(candidate: _Candidate, time_value: float) -> Dict[str, Any]:
    duration = int((candidate.arrive - candidate.depart).total_seconds() // 60)
    return {
        "kind": candidate.kind,
        "depart_at": _fmt(candidate.depart),
        "arrive_at": _fmt(candidate.arrive),
        "depart_tz": None,
        "arrive_tz": None,
        "depart_utc": None,
        "arrive_utc": None,
        "total_try": round(candidate.total_try, 2),
        "true_total_try": round(candidate.true_total_try, 2),
        "duration_mins": duration,
        "separate_tickets": candidate.separate_tickets,
        "via_stop": candidate.via_stop,
        "night_wait": candidate.night_wait,
        "has_estimated_segments": candidate.has_estimated_segments,
        "score": _score(candidate.true_total_try, duration, candidate.night_wait,
                        candidate.separate_tickets, time_value),
        "badges": [],
        "legs": [_serialise_leg(leg) for leg in candidate.legs],
    }


def _leg_key(option: Dict[str, Any]) -> Tuple[Tuple[str, str, str, str, str, str], ...]:
    return tuple(
        (leg["type"], leg["operator"], leg["from"], leg["to"], leg["depart_at"], leg["arrive_at"])
        for leg in option["legs"]
    )


def _with_station_names(leg: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of an engine leg that always carries origin_station_name / dest_station_name, source and
    price_is_estimated (null when the engine did not set them), and the engine's zone fields (depart_tz,
    arrive_tz, depart_utc, arrive_utc) unchanged."""
    return {
        **leg,
        "depart_tz": leg.get("depart_tz"),
        "arrive_tz": leg.get("arrive_tz"),
        "depart_utc": leg.get("depart_utc"),
        "arrive_utc": leg.get("arrive_utc"),
        "origin_station_name": _station_name(leg.get("origin_station_name")),
        "dest_station_name": _station_name(leg.get("dest_station_name")),
        "source": _leg_source(leg.get("source")),
        "price_is_estimated": _optional_flag(leg.get("price_is_estimated")),
        "from_coord": leg.get("from_coord"),
        "to_coord": leg.get("to_coord"),
    }


def _rank_total(option: Dict[str, Any]) -> float:
    """Ranking value of an option for "cheapest" picks: the known total plus the ranking-only value of its
    unknown prices (see route_engine). Equal to true_total_try when every price is known."""
    value = option.get("rank_total_try", option.get("true_total_try"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def _legacy_real_option(option: Dict[str, Any]) -> bool:
    """real_data_only for the legacy planner (no route engine): no estimated leg, no estimated transfer and no
    night wait (its hotel is an estimate). The legacy planner cannot leave a price unknown, so an estimated
    transfer excludes the option here (the route engine keeps it with an unknown transfer price)."""
    if option.get("night_wait") or option.get("has_estimated_segments"):
        return False
    for leg in option.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        if leg.get("is_estimated") or leg.get("price_is_estimated"):
            return False
    return True


def _legacy_real_only(options: List[Dict[str, Any]], raw: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Filters legacy options for real_data_only and recomputes the category picks the result carries."""
    kept = [o for o in options if _legacy_real_option(o)]
    if not kept and options:
        raw.setdefault("warnings", []).append(REAL_ONLY_EMPTY_WARNING)
    direct_kinds = ROUTE_DIRECT_KINDS
    side_kinds = ROUTE_SIDE_KINDS
    direct = [o for o in kept if o.get("kind") in direct_kinds]
    side = [o for o in kept if o.get("kind") in side_kinds]
    picks = {
        "best_direct": direct[0] if direct else None,
        "best_side_route": side[0] if side else None,
        "cheapest_direct": min(direct, key=_rank_total, default=None),
        "cheapest_side_route": min(side, key=_rank_total, default=None),
    }
    for key, value in picks.items():
        if key in raw:
            raw[key] = value
    if "query" in raw:
        raw["query"]["real_data_only"] = True
    return kept, raw


def _map_engine_option(option: Dict[str, Any], via_on: bool) -> Dict[str, Any]:
    """Copy of a RouteEngine option with the planner's kind names (see the module docstring)."""
    mapped = dict(option)
    mapped["badges"] = list(option.get("badges") or [])
    if isinstance(option.get("legs"), list):
        mapped["legs"] = [_with_station_names(leg) for leg in option["legs"] if isinstance(leg, dict)]
    kind = option.get("kind")
    if via_on and kind == "direct":
        mapped["kind"] = "via_single_ticket"
    elif via_on and kind == "multi_flight":
        mapped["kind"] = "via_separate"
    return mapped


# ---------------------------------------------------------------- planner

class TripPlanner:
    """Builds ranked options for origin -> destination on one or more dates.

    flight_provider: object with search_flights(origin=, destination=, departure_date=, trip_type=).
    router: MultiModalRouter (or None to skip side routes).
    route_engine: optional RouteEngine; when set, plan_trip uses it (see the module docstring).
    search_log: optional SearchLog; when set, results carry an "advice" buy-or-wait suggestion.
    """

    def __init__(
        self,
        flight_provider: Any,
        router: Any,
        geo_registry: Optional[GeoClusterRegistry] = None,
        self_transfer_buffer_mins: int = DEFAULT_SELF_TRANSFER_BUFFER_MINS,
        route_engine: Any = None,
        search_log: Any = None,
    ):
        self.flight_provider = flight_provider
        self.router = router
        if geo_registry is None:
            geo_registry = getattr(router, "geo_registry", None) or GeoClusterRegistry()
        self.geo_registry = geo_registry
        self.self_transfer_buffer_mins = int(self_transfer_buffer_mins)
        self.route_engine = route_engine
        self.search_log = search_log

    # ------------------------------------------------------------ public entry point

    def plan_trip(
        self,
        origin: str,
        destination: str,
        depart_dates: List[str],
        arrive_by: Optional[str] = None,
        depart_after: Optional[str] = None,
        via: Optional[Any] = None,
        include_side_routes: bool = True,
        time_value_try_per_hour: float = DEFAULT_TIME_VALUE_TRY_PER_HOUR,
        max_results: int = 10,
        max_stops: int = 2,
        flight_search_budget: int = 20,
        min_stay_mins: Optional[Dict[str, int]] = None,
        cabin_bag: bool = False,
        checked_bag: bool = False,
        flex_days: int = 0,
        return_dates: Optional[Any] = None,
        return_arrive_by: Optional[str] = None,
        return_depart_after: Optional[str] = None,
        return_via: Optional[Any] = None,
        min_stay_hours: float = 0.0,
        real_data_only: bool = False,
        via_on_date: Optional[Any] = None,
        return_via_on_date: Optional[Any] = None,
        schengen_single_entry: bool = False,
    ) -> Dict[str, Any]:
        """Ranked options for origin -> destination, optionally with flexible dates and a return leg.

        Old keyword arguments keep their meaning. The new ones:
        - max_stops, flight_search_budget, min_stay_mins, cabin_bag, checked_bag: route engine only.
        - flex_days: each depart date is widened to +-flex_days (at most 7 dates in total); the result
          gets "flex" = flex_summary(...) against the first requested date. Outbound only.
        - return_dates: a second search destination -> origin using return_arrive_by, return_depart_after,
          return_via (same search rules otherwise). The result gets "roundtrip" = {"return": <result for
          the return>, "combos": combine_roundtrip(outbound, return, min_stay_hours)}.
        - search_log is set: the result gets "advice" (advisor.advice_for_route for the first date and the
          best option's price); the value is None if the advice could not be computed.
        """
        requested = self._normalize_dates(depart_dates)
        cap = max(0, int(max_results))
        time_value = max(0.0, float(time_value_try_per_hour))
        flex = max(0, int(flex_days or 0))
        search_dates = self._expand_flex_dates(requested, flex) if flex else requested

        real = bool(real_data_only)
        all_options, result = self._search_one(
            origin, destination, search_dates,
            arrive_by=arrive_by, depart_after=depart_after, via=via,
            include_side_routes=include_side_routes, time_value=time_value, cap=cap,
            max_stops=max_stops, flight_search_budget=flight_search_budget,
            min_stay_mins=min_stay_mins, cabin_bag=cabin_bag, checked_bag=checked_bag,
            real_data_only=real,
            via_on_date=via_on_date,
            schengen_single_entry=schengen_single_entry,
        )
        result["query"]["depart_dates"] = requested
        result["query"]["real_data_only"] = real
        result["query"]["schengen_single_entry"] = bool(schengen_single_entry)
        if flex:
            result["query"]["flex_dates"] = search_dates
            result["flex"] = flex_summary(all_options, requested[0])

        if self.route_engine is None and (via_on_date or return_via_on_date):
            warn = "Tarih kuralı (via_on_date) bu arama kipinde uygulanmadı."
            if "warnings" not in result:
                result["warnings"] = []
            if warn not in result["warnings"]:
                result["warnings"].append(warn)
        if self.route_engine is None and schengen_single_entry:
            warn = "Schengen kuralı bu arama kipinde uygulanmadı"
            if "warnings" not in result:
                result["warnings"] = []
            if warn not in result["warnings"]:
                result["warnings"].append(warn)
        if return_dates:
            return_day_list = self._normalize_dates(return_dates)
            return_all, return_result = self._search_one(
                destination, origin, return_day_list,
                arrive_by=return_arrive_by, depart_after=return_depart_after, via=return_via,
                include_side_routes=include_side_routes, time_value=time_value, cap=cap,
                max_stops=max_stops, flight_search_budget=flight_search_budget,
                min_stay_mins=min_stay_mins, cabin_bag=cabin_bag, checked_bag=checked_bag,
                real_data_only=real,
                via_on_date=return_via_on_date,
                schengen_single_entry=schengen_single_entry,
            )
            result["roundtrip"] = {
                "return": return_result,
                "combos": combine_roundtrip(
                    all_options, return_all, min_stay_hours=min_stay_hours,
                    time_value_try_per_hour=time_value),
            }

        if self.search_log is not None:
            best = result.get("best") or (result["options"][0] if result["options"] else None)
            price = best.get("true_total_try") if isinstance(best, dict) else None
            try:
                result["advice"] = advisor.advice_for_route(
                    self.search_log, origin, destination, requested[0], price)
            except Exception as exc:  # advice is optional; never fail the search over it
                logger.warning("Price advice for %s->%s failed: %s", origin, destination, exc)
                result["advice"] = None
        return result

    # ------------------------------------------------------------ routing between engines

    @staticmethod
    def _expand_flex_dates(requested: List[str], flex_days: int) -> List[str]:
        """Dates within +-flex_days of each requested date, at most FLEX_MAX_DATES in total."""
        out: List[str] = []
        for day in requested:
            for candidate in expand_flex_dates(day, flex_days, max_count=FLEX_MAX_DATES):
                if candidate not in out:
                    out.append(candidate)
                if len(out) >= FLEX_MAX_DATES:
                    break
            if len(out) >= FLEX_MAX_DATES:
                break
        # Dates in the past are dropped by expand_flex_dates; keep the request if nothing is left
        return sorted(out) if out else list(requested)

    def _search_one(
        self,
        origin: str,
        destination: str,
        dates: List[str],
        *,
        arrive_by: Optional[str],
        depart_after: Optional[str],
        via: Optional[Any],
        include_side_routes: bool,
        time_value: float,
        cap: int,
        max_stops: int,
        flight_search_budget: int,
        min_stay_mins: Optional[Dict[str, int]],
        cabin_bag: bool,
        checked_bag: bool,
        real_data_only: bool = False,
        via_on_date: Optional[Any] = None,
        schengen_single_entry: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """One search in the active mode. Returns (all ranked options, public result trimmed to cap)."""
        if self.route_engine is None:
            raw = self._plan_legacy(
                origin, destination, dates,
                arrive_by=arrive_by, depart_after=depart_after, via=via,
                include_side_routes=include_side_routes, time_value_try_per_hour=time_value,
                max_results=LEGACY_MAX_RESULTS,
                via_on_date=via_on_date,
            )
            all_options = list(raw["options"])
            if real_data_only:
                all_options, raw = _legacy_real_only(all_options, raw)
            raw["options"] = all_options[:cap]
            raw["query"]["max_results"] = cap
            return all_options, raw
        return self._search_with_engine(
            origin, destination, dates,
            arrive_by=arrive_by, depart_after=depart_after, via=via,
            include_side_routes=include_side_routes, time_value=time_value, cap=cap,
            max_stops=max_stops, flight_search_budget=flight_search_budget,
            min_stay_mins=min_stay_mins, cabin_bag=cabin_bag, checked_bag=checked_bag,
            real_data_only=real_data_only,
            via_on_date=via_on_date,
            schengen_single_entry=schengen_single_entry,
        )

    def _search_with_engine(
        self,
        origin: str,
        destination: str,
        dates: List[str],
        *,
        arrive_by: Optional[str],
        depart_after: Optional[str],
        via: Optional[Any],
        include_side_routes: bool,
        time_value: float,
        cap: int,
        max_stops: int,
        flight_search_budget: int,
        min_stay_mins: Optional[Dict[str, int]],
        cabin_bag: bool,
        checked_bag: bool,
        real_data_only: bool = False,
        via_on_date: Optional[Any] = None,
        schengen_single_entry: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        via_fixed = _via_tokens(via)
        via_on = via_fixed is not None
        effective_stops = int(max_stops) if include_side_routes else 0
        # The flag is passed only when set, so route engines without it keep working unchanged
        real_kwargs = {"real_data_only": True} if real_data_only else {}
        via_date_kwargs = {"via_on_date": dict(via_on_date)} if via_on_date else {}
        schengen_kwargs = {"schengen_single_entry": True} if schengen_single_entry else {}
        raw = self.route_engine.search(
            origin, destination, dates,
            arrive_by=arrive_by,
            depart_after=depart_after,
            max_stops=effective_stops,
            via_fixed=via_fixed,
            flight_search_budget=int(flight_search_budget),
            time_value_try_per_hour=time_value,
            min_stay_mins=dict(min_stay_mins) if min_stay_mins else None,
            cabin_bag=bool(cabin_bag),
            checked_bag=bool(checked_bag),
            max_results=ENGINE_MAX_RESULTS,
            **via_date_kwargs,
            **real_kwargs,
            **schengen_kwargs,
        ) or {}

        options = [_map_engine_option(o, via_on) for o in raw.get("options") or [] if isinstance(o, dict)]
        near_misses = [_map_engine_option(o, via_on) for o in raw.get("near_misses") or [] if isinstance(o, dict)]
        if not include_side_routes:
            # Without side routes a single-leg ground trip is not a flight option either: drop it and
            # recompute the badges, since the engine's best/cheapest/fastest may have been ground legs.
            kept = [o for o in options if o["kind"] != "ground"]
            if len(kept) != len(options):
                options = kept
                for option in options:
                    option["badges"] = []
                self._assign_badges(options)
            near_misses = [o for o in near_misses if o["kind"] != "ground"]
            best = options[0] if options else None
            cheapest = min(options, key=_rank_total, default=None)
            fastest = min(options, key=lambda o: o["duration_mins"], default=None)
        else:
            best = _map_engine_option(raw["best"], via_on) if raw.get("best") else None
            cheapest = _map_engine_option(raw["cheapest"], via_on) if raw.get("cheapest") else None
            fastest = _map_engine_option(raw["fastest"], via_on) if raw.get("fastest") else None

        direct = [o for o in options if o["kind"] in ROUTE_DIRECT_KINDS]
        side = [o for o in options if o["kind"] in ROUTE_SIDE_KINDS]
        engine_query = raw.get("query") or {}
        engine_resolved = engine_query.get("resolved") if isinstance(engine_query.get("resolved"), dict) else {}
        resolved_via: Optional[List[str]] = None
        if via_on:
            # Resolved IATA codes of the via waypoints, in route order (each waypoint's airports)
            resolved_via = list(dict.fromkeys(
                str(code) for group in (engine_resolved.get("via") or []) for code in (group or [])
            ))
        result = {
            "query": {
                "origin": origin,
                "destination": destination,
                "depart_dates": list(dates),
                "arrive_by": arrive_by,
                "depart_after": depart_after,
                "via": via,
                "via_fixed": via_fixed,
                "include_side_routes": bool(include_side_routes),
                "time_value_try_per_hour": time_value,
                "max_results": cap,
                "max_stops": effective_stops,
                "flight_search_budget": int(flight_search_budget),
                "min_stay_mins": dict(min_stay_mins) if min_stay_mins else None,
                "via_on_date": dict(via_on_date) if via_on_date else None,
                "cabin_bag": bool(cabin_bag),
                "checked_bag": bool(checked_bag),
                "resolved": engine_resolved,
                "resolved_via": resolved_via,
                "engine": "route_engine",
            },
            "options": options[:cap],
            "near_misses": near_misses,
            "best_direct": direct[0] if direct else None,
            "best_side_route": side[0] if side else None,
            "cheapest_direct": min(direct, key=_rank_total, default=None),
            "cheapest_side_route": min(side, key=_rank_total, default=None),
            "cheapest_direct_priced": min(
                [o for o in direct if o.get("unknown_price_legs") == 0 and o.get("true_total_try") is not None],
                key=lambda o: float(o.get("true_total_try", float("inf"))), default=None,
            ),
            "cheapest_side_route_priced": min(
                [o for o in side if o.get("unknown_price_legs") == 0 and o.get("true_total_try") is not None],
                key=lambda o: float(o.get("true_total_try", float("inf"))), default=None,
            ),
            "searched_dates": sorted(set(dates)),
            "flight_searches": int(raw.get("flight_searches") or 0),
            "warnings": list(raw.get("warnings") or []),
            "budget_exhausted": bool(raw.get("budget_exhausted")),
            "candidates": dict(raw.get("candidates") or {}),
            "best": best,
            "cheapest": cheapest,
            "fastest": fastest,
            "trace": raw.get("trace"),
        }
        return options, result

    # ------------------------------------------------------------ legacy search

    def _plan_legacy(
        self,
        origin: str,
        destination: str,
        depart_dates: List[str],
        arrive_by: Optional[str] = None,
        depart_after: Optional[str] = None,
        via: Optional[Any] = None,
        include_side_routes: bool = True,
        time_value_try_per_hour: float = DEFAULT_TIME_VALUE_TRY_PER_HOUR,
        max_results: int = 10,
        via_on_date: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """The original planner (flight search + MultiModalRouter). Unchanged behaviour."""
        dates = self._normalize_dates(depart_dates)
        arrive_by_dt = _parse_datetime(arrive_by, "arrive_by") if arrive_by else None
        depart_after_dt = _parse_datetime(depart_after, "depart_after") if depart_after else None
        time_value = max(0.0, float(time_value_try_per_hour))
        cap = max(0, int(max_results))
        buffer_mins = self.self_transfer_buffer_mins

        ctx = _SearchContext()
        if via_on_date:
            ctx.warnings.append("Tarih kuralı (via_on_date) bu arama kipinde uygulanmadı.")
        via_tokens = _via_tokens(via)
        via_codes: Optional[List[str]] = None
        if via_tokens is not None:
            via_codes = self._resolve_via_tokens(via_tokens, ctx.warnings)

        candidates: List[_Candidate] = []
        for day in dates:
            if via_codes is None:
                candidates += self._direct_and_side_candidates(
                    origin, destination, day, include_side_routes, time_value, ctx)
            else:
                candidates += self._via_candidates(
                    origin, destination, day, via_codes, arrive_by_dt, buffer_mins, ctx)

        # Filter by the caller's deadlines, using full timestamps
        kept: List[_Candidate] = []
        for candidate in candidates:
            if depart_after_dt is not None and candidate.depart < depart_after_dt:
                continue
            if arrive_by_dt is not None and candidate.arrive > arrive_by_dt:
                continue
            kept.append(candidate)

        # Rank, then drop identical itineraries (keeping the best-ranked copy)
        ranked = sorted(
            ((_to_option(c, time_value), c) for c in kept),
            key=lambda pair: (pair[0]["score"], pair[0]["true_total_try"],
                              pair[0]["duration_mins"], pair[0]["depart_at"]),
        )
        unique: List[Dict[str, Any]] = []
        unique_dates: Set[str] = set()
        seen: Set[Tuple] = set()
        for option, candidate in ranked:
            key = _leg_key(option)
            if key in seen:
                continue
            seen.add(key)
            unique.append(option)
            unique_dates.add(candidate.search_date)

        if unique:
            # Per-date notes only when other dates produced options (the warning is about departures)
            for day in dates:
                if day not in unique_dates:
                    ctx.warnings.append(departure_date_warning(day))
        else:
            ctx.warnings.append("Seçilen tarihler için kurallara uyan seçenek bulunamadı")

        self._assign_badges(unique)

        direct_kind = "via_single_ticket" if via_codes is not None else "direct"
        side_kinds = ("side_route", "via_separate")
        best_direct = next((o for o in unique if o["kind"] == direct_kind), None)
        best_side = next((o for o in unique if o["kind"] in side_kinds), None)
        # Budget alerts need the cheapest option, which may not be the best-scored (e.g. a longer flight)
        cheapest_direct = min((o for o in unique if o["kind"] == direct_kind), key=lambda o: o["true_total_try"], default=None)
        cheapest_side = min((o for o in unique if o["kind"] in side_kinds), key=lambda o: o["true_total_try"], default=None)

        return {
            "query": {
                "origin": origin,
                "destination": destination,
                "depart_dates": dates,
                "arrive_by": arrive_by,
                "depart_after": depart_after,
                "via": via,
                "resolved_via": via_codes,
                "include_side_routes": bool(include_side_routes),
                "time_value_try_per_hour": time_value,
                "max_results": cap,
            },
            "options": unique[:cap],
            "best_direct": best_direct,
            "best_side_route": best_side,
            "cheapest_direct": cheapest_direct,
            "cheapest_side_route": cheapest_side,
            "searched_dates": sorted(ctx.searched_dates),
            "flight_searches": ctx.flight_searches,
            "warnings": list(dict.fromkeys(ctx.warnings)),
        }

    # ------------------------------------------------------------ searching

    def _search(self, origin: str, destination: str, date_str: str, ctx: _SearchContext) -> List[Dict[str, Any]]:
        key = (origin.upper(), destination.upper(), date_str)
        if key in ctx.cache:
            return ctx.cache[key]
        ctx.flight_searches += 1
        ctx.searched_dates.add(date_str)
        try:
            results = self.flight_provider.search_flights(
                origin=origin,
                destination=destination,
                departure_date=date_str,
                trip_type="oneway",
            ) or []
        except Exception as exc:  # a failing search must not abort the whole plan
            logger.warning("Flight search %s->%s on %s failed: %s", origin, destination, date_str, exc)
            ctx.warnings.append(f"Uçuş araması başarısız: {origin} ➔ {destination} ({date_str})")
            results = []
        ctx.cache[key] = list(results)
        return ctx.cache[key]

    def _resolve_via_tokens(self, tokens: List[str], warnings: List[str]) -> List[str]:
        codes: List[str] = []
        for token in tokens:
            resolved = self.geo_registry.resolve_via(token)
            if not resolved:
                warnings.append(f"'{token}' hiçbir havalimanıyla eşleşmedi (zorunlu geçiş)")
            for code in resolved:
                if code not in codes:
                    codes.append(code)
        return codes

    @staticmethod
    def _normalize_dates(depart_dates: Any) -> List[str]:
        items = [depart_dates] if isinstance(depart_dates, str) else list(depart_dates or [])
        dates: List[str] = []
        for item in items:
            day = _parse_date(item, "depart_dates").strftime(DATE_FMT)
            if day not in dates:
                dates.append(day)
        if not dates:
            raise ValueError("depart_dates must contain at least one date")
        return sorted(dates)

    # ------------------------------------------------------------ candidates

    def _direct_and_side_candidates(
        self,
        origin: str,
        destination: str,
        day: str,
        include_side_routes: bool,
        time_value: float,
        ctx: _SearchContext,
    ) -> List[_Candidate]:
        base = datetime.strptime(day, DATE_FMT)
        flights = self._search(origin, destination, day, ctx)
        out: List[_Candidate] = []
        for flight in flights:
            times = _flight_times(flight, base)
            if times is None:
                continue
            dep, arr = times
            price = _price(flight)
            out.append(_Candidate(
                search_date=day,
                kind="direct",
                depart=dep,
                arrive=arr,
                legs=[_flight_leg(flight, origin, destination, dep, arr)],
                total_try=price,
                true_total_try=price,
                separate_tickets=False,
                via_stop=None,
                night_wait=False,
                has_estimated_segments=False,
            ))
        if include_side_routes and self.router is not None:
            out += self._side_route_candidates(origin, destination, day, flights, time_value, ctx)
        return out

    def _side_route_candidates(
        self,
        origin: str,
        destination: str,
        day: str,
        direct_flights: List[Dict[str, Any]],
        time_value: float,
        ctx: _SearchContext,
    ) -> List[_Candidate]:
        # Router makes one catchment search per catchment airport of the destination cluster (not counted by us)
        registry = getattr(self.router, "geo_registry", None) or self.geo_registry
        cluster = registry.get_cluster(destination)
        if cluster:
            ctx.flight_searches += len(cluster.catchment_airports)
        try:
            result = self.router.search_multimodal(
                origin,
                destination,
                day,
                prefetched_direct_flights=direct_flights,
                time_value_try_per_hour=time_value,
            ) or {}
        except Exception as exc:
            logger.warning("Side-route search %s->%s on %s failed: %s", origin, destination, day, exc)
            ctx.warnings.append(f"Yan yol araması başarısız: {origin} ➔ {destination} ({day})")
            return []
        out: List[_Candidate] = []
        for journey in result.get("multimodal_routes") or []:
            candidate = self._journey_candidate(journey, day)
            if candidate is not None:
                out.append(candidate)
        return out

    def _journey_candidate(self, journey: Dict[str, Any], day: str) -> Optional[_Candidate]:
        """Convert a router journey to absolute legs. Segment durations are exact; only start times need a day."""
        base = datetime.strptime(day, DATE_FMT)
        legs: List[Dict[str, Any]] = []
        prev_end: Optional[datetime] = None
        for seg in journey.get("segments") or []:
            hhmm = _parse_hhmm(seg.get("departure_time"))
            if hhmm is None:
                return None
            dep = _place_at_or_after(base, hhmm, prev_end)
            arr = dep + timedelta(minutes=int(seg.get("duration_mins") or 0))
            legs.append({
                "type": str(seg.get("type") or "transfer"),
                "operator": str(seg.get("operator") or ""),
                "from": str(seg.get("from") or ""),
                "to": str(seg.get("to") or ""),
                "depart": dep,
                "arrive": arr,
                "price_try": float(seg.get("price_try") or 0.0),
                "is_estimated": bool(seg.get("is_estimated")),
                "booking_url": str(seg.get("booking_url") or ""),
                "origin_station_name": _station_name(seg.get("origin_station_name")),
                "dest_station_name": _station_name(seg.get("dest_station_name")),
                "source": _leg_source(seg.get("source")),
                "price_is_estimated": _optional_flag(seg.get("price_is_estimated")),
            })
            prev_end = arr
        if not legs:
            return None
        return _Candidate(
            search_date=day,
            kind="side_route",
            depart=legs[0]["depart"],
            arrive=legs[-1]["arrive"],
            legs=legs,
            total_try=float(journey.get("total_price_try") or 0.0),
            true_total_try=float(journey.get("true_total_try") or journey.get("total_price_try") or 0.0),
            separate_tickets=True,
            via_stop=None,
            night_wait=bool(journey.get("night_wait")),
            has_estimated_segments=bool(journey.get("has_estimated_segments")),
        )

    def _via_candidates(
        self,
        origin: str,
        destination: str,
        day: str,
        via_codes: List[str],
        arrive_by_dt: Optional[datetime],
        buffer_mins: int,
        ctx: _SearchContext,
    ) -> List[_Candidate]:
        if not via_codes:
            return []
        base = datetime.strptime(day, DATE_FMT)
        origin_u, dest_u = origin.upper(), destination.upper()
        out: List[_Candidate] = []

        # (a) single-ticket flights with a via layover
        via_set = set(via_codes)
        for flight in self._search(origin, destination, day, ctx):
            if flight.get("is_direct"):
                continue
            stops = [code for code in _layover_codes(flight, origin, destination) if code in via_set]
            if not stops:
                continue
            times = _flight_times(flight, base)
            if times is None:
                continue
            dep, arr = times
            price = _price(flight)
            out.append(_Candidate(
                search_date=day,
                kind="via_single_ticket",
                depart=dep,
                arrive=arr,
                legs=[_flight_leg(flight, origin, destination, dep, arr)],
                total_try=price,
                true_total_try=price,
                separate_tickets=False,
                via_stop=stops[0],
                night_wait=False,
                has_estimated_segments=False,
            ))

        # (b) two separate tickets through a via airport
        targets = [code for code in _prioritise(via_codes) if code not in (origin_u, dest_u)]
        for via_code in targets[:MAX_VIA_AIRPORTS_SEARCHED]:
            first_parsed = self._parsed_flights(origin, via_code, day, base, ctx)
            if not first_parsed:
                continue
            next_day = base + timedelta(days=1)
            second_days = [day]
            # The second leg may run the next morning only if the arrival deadline allows it
            if arrive_by_dt is None or next_day < arrive_by_dt:
                second_days.append(next_day.strftime(DATE_FMT))
            for second_day in second_days:
                second_base = datetime.strptime(second_day, DATE_FMT)
                second_parsed = self._parsed_flights(via_code, destination, second_day, second_base, ctx)
                for f1, dep1, arr1 in first_parsed:
                    for f2, dep2, arr2 in second_parsed:
                        if dep2 < arr1 + timedelta(minutes=buffer_mins):
                            continue
                        price1, price2 = _price(f1), _price(f2)
                        total = price1 + price2
                        out.append(_Candidate(
                            search_date=day,
                            kind="via_separate",
                            depart=dep1,
                            arrive=arr2,
                            legs=[
                                _flight_leg(f1, origin, via_code, dep1, arr1),
                                _flight_leg(f2, via_code, destination, dep2, arr2),
                            ],
                            total_try=total,
                            true_total_try=total,
                            separate_tickets=True,
                            via_stop=via_code,
                            night_wait=_night_wait(arr1, dep2),
                            has_estimated_segments=False,
                        ))
        return out

    def _parsed_flights(
        self,
        origin: str,
        destination: str,
        day: str,
        base: datetime,
        ctx: _SearchContext,
    ) -> List[Tuple[Dict[str, Any], datetime, datetime]]:
        parsed = []
        for flight in self._search(origin, destination, day, ctx):
            times = _flight_times(flight, base)
            if times is not None:
                parsed.append((flight, times[0], times[1]))
        return parsed

    # ------------------------------------------------------------ badges

    @staticmethod
    def _assign_badges(options: List[Dict[str, Any]]) -> None:
        if not options:
            return
        cheapest = min(options, key=lambda o: (_rank_total(o), o["score"]))
        fastest = min(options, key=lambda o: (o["duration_mins"], o["score"]))
        best = options[0]  # already sorted by score
        cheapest["badges"].append(BADGE_CHEAPEST)
        fastest["badges"].append(BADGE_FASTEST)
        best["badges"].append(BADGE_BEST)
