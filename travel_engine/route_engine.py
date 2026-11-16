"""Route engine: cheapest / fastest chains from A to B through ground and air legs (Europe + Turkey).

Chain kinds
-----------
A flight A->B, a chain A ✈ C 🚆 B, A 🚌 C ✈ B, A 🚌 C ✈ D 🚆 B, A ✈ H ✈ B (separate tickets through
a low-cost hub) and their combinations. Automatic stops are chosen by the engine; fixed stops
(``via_fixed``) are visited in the given order.

Option kinds: "direct" (one flight, possibly single-ticket with layovers), "ground" (one train/bus
leg only), "side_route" (any chain with at least one ground leg), "multi_flight" (two or more
separately ticketed flights). A leg of type "transfer" is an airport <-> city transfer.

Algorithm
---------
1. Resolve. Origin and destination are resolved with AirportDB (airports, city, metro groups,
   country). A country place lists its airports by score (size, LCC status, then distance to the
   other end), not alphabetically.
   Side candidates are airports reached by ground from the place's own airports:
   - a candidate must be ground_reachable from one of the place's own airports (AirportDB). When
     AirportDB has no ground_reachable, islands are excluded unless the place itself is on an
     island (FALLBACK_ISLAND_AIRPORTS);
   - a non-own candidate must be an LCC_HUBS airport or have SearchLog history. Size alone is not
     used: the bundled data marks small regional airports (KLV, PED) as "large";
   - straight-line distance within origin_radius_km / dest_radius_km and road distance
     (ground_distance_km) within radius * 1.3. LCC_HUBS may also be used up to LCC_SIDE_KM (450 km
     road), with a small score instead of the strong one.
   Score: own airport +10, large +3 / medium +1, LCC_HUBS +4 when near (+1 when far), SearchLog
   history +1, minus road km / 100. Own airports are kept up to OWN_SIDE_KEEP; non-own up to
   ORIGIN_SIDE_KEEP (4) / DEST_SIDE_KEEP (5).
   Extra candidates: near (same road limit) non-LCC airports without history. They fail the volume
   gate, so they are searched only in the LATE category (side_extra), with budget left over.
   Hubs: LCC_HUBS airports that are not own airports or side candidates, whose detour
   (d(A,H)+d(H,B))/d(A,B) is at most 1.6; ranked by detour, top 4 kept.

2. Budget. Every new flight_provider.search_flights call counts; cached (from, to, date) lookups
   do not. Searches are planned in categories, in this order:
   C1 direct: origin -> destination, city-level, one search per depart date;
   C2 origin own airport -> LCC dest-side candidate (max_stops >= 1);
   C3 origin-side LCC candidate -> destination own airport (max_stops >= 1);
   C4 other side pairs: exactly one end is own (and not C2/C3); both non-own (max_stops >= 2);
   C4b far side (max_stops >= 1): LCC or large airports up to FAR_ROAD_KM by road from an end, with a
      known ground connection (live, FlixBus or table; an estimate only when the flight budget is at
      least FAR_ESTIMATE_MIN_BUDGET). Far origin -> LCC/own destination side, own origin -> far destination.
   C5 hub chains: origin -> H and H -> destination (max_stops >= 1); ground + hub chains and
      hub -> hub pairs (max_stops >= 2 / >= 3).
   Policy (no category starves): C1 is never capped. In a first pass C2, C3 and C4 may use at most
   BUDGET_SHARE_LCC_DEST (40%), BUDGET_SHARE_LCC_ORIGIN (15%) and BUDGET_SHARE_SIDE_PAIRS (15%) of
   the budget, C4b at most BUDGET_SHARE_FAR_SIDE (10%); C5 then takes the rest, so hub chains get at
   least 20%. A second pass runs the
   searches left over from C2-C4 in the same order. Within a category, pairs with a known cheapest
   historical price (SearchLog.cheapest_destinations; unknown prices are ignored) come first, and
   the (pair, date) queue is round-robin over the depart dates, so each pair gets a date in every
   round; inside one round, pairs with a known calendar price (see below) come cheapest first. When the
   budget runs out the plan stops, budget_exhausted=True and BUDGET_WARNING is added.
   Parallel searches: the searches of a category run in batches of up to the provider's capacity
   (search_concurrency(): one browser page per browser, TRAVELPLAN_MAX_BROWSERS). Budget is reserved in
   queue order before a batch runs, and the results are ingested in queue order, so the result does not
   depend on which search finishes first.
   Calendar pruning (conservative, never for the direct category): the Google calendar of a route gives the
   cheapest price of that route per day, known after any live search of the route (kept in the flight cache
   for TRAVELPLAN_FLIGHT_CACHE_TTL_SECS). A flight search is skipped when that price is above
   CALENDAR_PRUNE_MARGIN (1.3) x the best option score found so far: every chain using one of its flights
   costs at least that price, so it cannot be the best or the cheapest option. Skipped searches are counted
   in pruned_searches and are not reported as unsearched. Limitation: a skipped connection could be the
   fastest option; the bound says nothing about duration.
   A third pass, only when budget remains, searches the next-day second flight (step 4). The
   side_extra category (C6, extra near airports) runs last, after the next-day pass, so it never takes
   budget from C2-C5. When the budget runs out, the warning names the categories and dates not searched.

3. Ground. Ground legs come from find_ground. Pure ground origin -> destination is allowed when
   ground_distance_km <= 700.

4. Chains. A depth-first search over states (city / airport location, time, legs). Rules:
   - A flight leaving a city is preceded by a transfer city -> airport. When the chain starts at the
     origin, the transfer is scheduled backwards (leave as late as possible). Otherwise the flight
     departs at least transfer + 120 min after arrival in the city.
   - A flight landing at an airport is followed by a transfer airport -> city, then the next ground
     leg departs at least transfer + 30 min after landing.
   - Flight -> flight on separate tickets needs at least 180 min between landing and departure.
     A connection airport may also depart on the next day; those flights exist only where the
     next-day searches of step 2 found them.
   - Ground -> ground at a city needs at least 30 min.
   - min_stay_mins (per named stop) raises the minimum gap at that stop; the option carries
     "stopover": {"place", "mins"}.
   - Automatic stops = number of main legs minus one; max_stops bounds the main legs.
   - Fixed stops (via_fixed): each waypoint (city, airport or country, resolved with AirportDB) must be
     reached in order. Between two waypoints exactly one main leg is used (max_stops 0). A single
     flight whose layover airport belongs to a waypoint passes through it (single ticket).
     Waypoint search endpoints: a country takes its 3 best airports, a multi-airport city its 2. Best
     means mainland before islands, then LCC bases, then large airports, then nearest to the neighbouring
     waypoints. Each endpoint is searched by its own IATA code, so arrivals are never ambiguous.
     A waypoint passed through at its airport with no minimum stay is connected airport -> airport
     (separate-ticket gap) when the next flight leaves the same airport: no city round trip and no transfer
     costs. A stay, or a ground leg from the city, keeps the airport -> city transfer.
   - Times are UTC instants (aware datetimes); each leg endpoint keeps its IANA zone. A flight card's
     "HH:MM" times are local: the departure in the origin airport's zone, the arrival in the destination's,
     "+N" counting days from the departure date (without it, an arrival that is not after the departure
     is the next day). A ground segment's arrival_time is local to the destination place (duration_mins is the
     cross-check). Buffers, stays and deadlines compare instants; arrive_by is local time at the destination,
     depart_after local time at the origin, and the night window (00:00-06:00) is local to the waiting place.
     A chain takes the flights of the day it stands on and the
     next day (feasibility is checked on the times). Fixed mode also searches every segment after a
     waypoint on the next day, and on the day after when a stay before it can push the traveller on
     (roll_dates; MAX_ROLL_DAYS). Ground legs may roll to later days; from the origin, an evening query
     (OVERNIGHT_FROM_HM) gives the overnight departures.
   - Fixed-stop segments take ground legs up to VIA_ROAD_LIMIT_KM (800 km road) next to the flights.

5. Filter by depart_after / arrive_by on full timestamps (see step 4 for the zones). If the DFS state or chain limit is hit,
   the result carries SEARCH_LIMIT_WARNING ("Arama sınırına ulaşıldı; sonuçlar eksik olabilir"). Options with the same leg sequence are
   merged. Near-duplicates (same flights, same kind, true totals within 1%) collapse to one option:
   the one with the fewest estimated ground legs, then the best score, then the earliest arrival.
   At most 2 options per flight set are kept. Each option gets:
   - night_wait when a gap overlaps 00:00-06:00 by at least 180 min; the cost includes a hotel night;
   - cost_breakdown (travel_engine.costs) and journey_risk (travel_engine.costs);
   - score = true_total + hours * time_value + (1500 * time_value / 150 if night_wait)
     + risk penalty (low 0, medium 400, high 1200).

6. Badges: "Önerilen" (best score), "En Ucuz" (lowest true total), "En Hızlı" (shortest),
   "Düşük Risk" (low risk and score within 15% of the best).

Output: each leg and option has depart_at / arrive_at (local wall-clock times of its endpoints, "YYYY-MM-DDTHH:MM"),
depart_tz / arrive_tz (IANA zones) and depart_utc / arrive_utc (ISO with Z).

Costs are imported lazily (``from travel_engine import costs``) unless an object is injected.

Unknown prices and real data only
---------------------------------
- A leg's ``price_try`` is None when the price is unknown (a real timetable without a fare, or an
  estimated transfer under real_data_only). It is never replaced by an estimate. ``price_known`` says
  whether a number is shown; ``price_from_try`` is an indicative "from" price (real, not date-specific).
- Option totals: ``true_total_try`` is the sum of the known prices (a lower bound). ``price_complete`` is
  False when a leg price or a cost part (baggage, transfers, hotel) is unknown; ``unknown_price_legs``
  counts the non-transfer legs with unknown prices and ``unknown_price_parts`` names the other unknown
  parts. ``total_lower_bound_try`` equals ``true_total_try``.
- Ranking (``rank_total_try`` and ``score``): an unknown leg adds a ranking-only value that is never shown
  and never part of a total: its ``price_from_try`` when given, else the median known ground price of the
  same travel-time band (RANK_BAND_MINS, a proxy for distance) among the ground segments of this search,
  else the median of all known ground prices, else UNKNOWN_RANK_TRY (0). Unknown transfers and bags add 0
  (no real price exists to compare with). Options with complete prices are ranked exactly as before.
- ``real_data_only=True`` ("Sadece gerçek veriler"): estimated ground segments (estimated times or
  fares) are dropped when the segments are read, and the distance-estimate fallback is not requested
  (the ground provider gets ``allow_estimate=False`` when it accepts that argument). Transfers and bags
  are not estimated in this mode: their price is unknown. A hotel night (always an estimate) excludes the
  option. An option that still has an estimated ground leg or an estimated cost part is dropped. If that
  drops every option, the result carries REAL_ONLY_EMPTY_WARNING.
"""

from __future__ import annotations

import contextlib
import heapq
import inspect
import logging
import math
import re
import statistics
import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from travel_engine import airports as _airports
from travel_engine.airports import Airport, AirportDB, _city_table_country, haversine_km
from travel_engine.providers.rail_bus_provider import DEFAULT_MAPS_BUDGET, MapsBudget
from travel_engine.trace import SearchTrace
from travel_engine.multimodal_router import (
    DEFAULT_TIME_VALUE_TRY_PER_HOUR,
    NIGHT_WAIT_MIN_OVERLAP_MINS,
    NIGHT_WAIT_PENALTY_TRY,
    _night_overlap_mins,
    extract_flight_day_offset,
    extract_flight_times,
    get_operator_booking_url,
)

logger = logging.getLogger(__name__)

LCC_HUBS: Tuple[str, ...] = (
    "BGY", "STN", "BVA", "CRL", "BUD", "WAW", "KRK", "VIE", "BTS", "OTP", "SOF", "BCN", "MAD",
    "DUB", "LTN", "BER", "CPH", "SAW", "IST", "ATH", "MXP", "CIA", "GRO", "REU", "KTW", "WMI", "RIX", "VNO",
)

ROAD_LIMIT_KM = 700.0
SEPARATE_TICKET_GAP_MINS = 180
AIRPORT_BUFFER_MINS = 120
CITY_STOP_MINS = 30
ORIGIN_SIDE_KEEP = 4
DEST_SIDE_KEEP = 5
OWN_SIDE_KEEP = 6
EXTRA_SIDE_KEEP = 2          # near non-LCC airports without flight volume: searched only with spare budget
VIA_COUNTRY_ENDS = 3         # airports searched per fixed country waypoint
VIA_CITY_ENDS = 2            # airports searched per multi-airport city (origin, waypoint or destination)
VIA_SIDE_KEEP = 2            # side airports kept per waypoint end in via mode (T12)
VIA_SIDE_MAX_SEARCHES = 4    # maximum flight searches spent on via_side category (T12)
DEDUPE_TOTAL_TOLERANCE = 0.01  # options with the same flights and a true total within 1% are near-duplicates
LCC_SIDE_KM = 450.0
# Calendar pruning (see _Run.should_prune): a flight search is skipped only when the Google calendar minimum
# of its route on that date is above this multiple of the best option's score found so far. The score of
# any chain is at least the price of each of its flights, so the bound alone already needs a factor of 1.0.
CALENDAR_PRUNE_MARGIN = 1.3
SIDE_NEAR_FACTOR = 1.3
BUDGET_SHARE_LCC_DEST = 0.40
BUDGET_SHARE_LCC_ORIGIN = 0.15
BUDGET_SHARE_SIDE_PAIRS = 0.15
BUDGET_SHARE_FAR_SIDE = 0.10     # far road-side candidates (auto mode), low priority
FAR_ROAD_KM = 650.0              # far candidates: LCC or large airports up to this road distance from an end
FAR_SIDE_KEEP = 12               # far candidates kept per end (the budget share limits the searches)
FAR_PROBE_LIMIT = 40             # far candidates whose ground connection is looked up (no flight budget used)
FAR_ESTIMATE_MIN_BUDGET = 20     # an estimate-only far candidate is used only when the flight budget is this big
KNOWN_GROUND_SOURCES = frozenset({"live", "flixbus", "table"})
VIA_ROAD_LIMIT_KM = 800.0        # fixed-stop segments; also the max_road_km of every ground query
GROUND_MAX_RESULTS = 12          # ground segments per query (6 cut off the evening departures)
OVERNIGHT_FROM_HM = "18:00"      # extra ground query from this time for a chain that starts at the origin
MAX_ROLL_DAYS = 2                # fixed mode: a segment after a stay is searched up to this many days later
NEAR_MISS_HOURS = 12
NEAR_MISS_SAVING = 0.15
NEAR_MISS_MAX = 3
NEAR_MISS_CHAIN_CAP = 300
HUB_KEEP = 4
MAX_HUB_DETOUR = 1.6
MAX_OPTIONS_PER_FLIGHT_SET = 2
MAX_DFS_STATES = 200_000
MAX_COMPLETE_CHAINS = 5_000
MAX_STOPS_LIMIT = 3
RISK_PENALTY_TRY = {"low": 0.0, "medium": 400.0, "high": 1200.0}

BADGE_BEST = "Önerilen"
BADGE_CHEAPEST = "En Ucuz"
BADGE_FASTEST = "En Hızlı"
BADGE_LOW_RISK = "Düşük Risk"

SCHENGEN = {
    "AT", "BE", "BG", "HR", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
    "IS", "IT", "LV", "LI", "LT", "LU", "MT", "NL", "NO", "PL", "PT", "RO",
    "SK", "SI", "ES", "SE", "CH"
}

REASON_LABELS = {
    "depart_after": "kalkış saati",
    "arrive_by": "son varış",
    "buffer": "aktarma süresi",
    "not_target": "farklı havalimanı",
    "date": "tarih",
    "schengen": "Schengen tek giriş",
}

BUDGET_WARNING = "Arama bütçesi doldu; daha fazla aday var (derin arama için bütçeyi artırın)"
SEARCH_LIMIT_WARNING = "Arama sınırına ulaşıldı; sonuçlar eksik olabilir"
REAL_ONLY_EMPTY_WARNING = ("Gerçek veriyle seçenek bulunamadı — butonu kapatarak tahmini rotaları "
                           "(etiketli) görebilirsin")
MAPS_BUDGET_WARNING = "Saat kaynağı sınırına ulaşıldı (Google Haritalar); bazı tren/otobüs bağlantıları denenmedi"
# Ranking only, never shown (see the module docstring): travel-time band width of the ground price pools, and
# the value used for an unknown leg price when no known ground price exists in the search at all.
RANK_BAND_MINS = 60
UNKNOWN_RANK_TRY = 0.0

# Categories that run only after all others (spare budget only)
LATE_CATEGORIES = frozenset({"side_extra", "via_side"})

# Turkish names of the search categories, used in the "not fully searched" warning (P7)
CATEGORY_NAMES = {
    "direct": "doğrudan uçuşlar",
    "segments": "ara duraklar",
    "lcc_dest": "ucuz havayolu varış havalimanları",
    "lcc_origin": "ucuz havayolu kalkış havalimanları",
    "side_pairs": "yan havalimanı çiftleri",
    "hub_chains": "aktarma merkezleri",
    "side_extra": "diğer yakın havalimanları",
    "next_day": "ertesi gün bağlantıları",
    "far_side": "uzak yakın şehirler (kara yolu bağlantılı)",
    "rolled": "kalış sonrası ertesi gün bağlantıları",
    "via_side": "ara durak yan havalimanları",
}

# Used only when AirportDB has no is_island(). Island airports that are not reachable by road.
FALLBACK_ISLAND_AIRPORTS = frozenset({
    # Greece
    "CHQ", "ZTH", "JTR", "CFU", "EFL", "RHO", "KGS", "HER", "JMK", "JSI", "JSH", "JNX", "PAS", "SMI",
    "LRS",
    # Italy (Sicily, Sardinia, Elba, Lampedusa, Pantelleria), Corsica
    "PMO", "CTA", "TPS", "CAG", "OLB", "AHO", "EBA", "LMP", "PNL", "AJA", "BIA", "FSC",
    # Spain (Balearics, Canaries), Portugal islands
    "PMI", "IBZ", "MAH", "LPA", "TFN", "TFS", "ACE", "FUE", "FNC", "PDL",
    # Ireland, Channel Islands, Isle of Man, Malta, Cyprus, Bornholm, Gotland, Aland
    "DUB", "ORK", "SNN", "KIR", "NOC", "WAT", "JER", "GCI", "IOM", "MLA", "LCA", "PFO", "RNN", "VBY",
    "MHQ",
})

DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%dT%H:%M"
_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_CODE_RE = re.compile(r"\b([A-Z]{3})\b")


# ---------------------------------------------------------------- small helpers

def _station_name(value: Any) -> Optional[str]:
    """A station name as text, or None when missing or blank."""
    text = str(value or "").strip()
    return text or None


def _hhmm(text: Any) -> Optional[Tuple[int, int]]:
    match = _HHMM_RE.match(str(text or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _hm_text(moment: datetime) -> str:
    return f"{moment.hour:02d}:{moment.minute:02d}"


def departure_date_warning(day: str) -> str:
    """"28.10 kalkışlı uygun seçenek bulunamadı" for an ISO departure date (the warning is about departures)."""
    try:
        label = datetime.strptime(str(day), "%Y-%m-%d").strftime("%d.%m")
    except ValueError:
        label = str(day)
    return f"{label} kalkışlı uygun seçenek bulunamadı"


# Human labels for airport transfer legs (the UI shows these as the leg's operator). The costs module
# supplies "label" itself; this table is only for cost modules that do not (fakes, old results).
TRANSFER_FALLBACK_LABEL = "Tahmini transfer"
_TRANSFER_MODE_LABELS = {"bus": "otobüs", "metro": "metro", "train": "tren", "taxi": "taksi"}


def _transfer_label(info: Dict[str, Any]) -> str:
    label = str(info.get("label") or "").strip()
    if label:
        return label
    return _TRANSFER_MODE_LABELS.get(str(info.get("mode") or ""), TRANSFER_FALLBACK_LABEL)


# ---------------------------------------------------------------- time zones
#
# Every instant inside the engine is an aware datetime in UTC: arithmetic (buffers, durations, stays)
# is done on UTC, because adding a timedelta to a zoneinfo datetime is wall-clock arithmetic and is
# wrong across a DST change. The zone of each leg endpoint is kept next to the instant and is used only
# to read a wall-clock time (a station timetable, a flight card, the night window) or to print it.

UTC = timezone.utc


@lru_cache(maxsize=256)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _wall_to_utc(wall: datetime, zone: str) -> datetime:
    """A naive wall-clock time in IANA zone ``zone`` as an aware UTC datetime (fold=0 in an overlap)."""
    return wall.replace(tzinfo=_zone(zone)).astimezone(UTC)


def _to_local(moment: datetime, zone: str) -> datetime:
    """An aware instant as a naive wall-clock time in ``zone``."""
    return moment.astimezone(_zone(zone)).replace(tzinfo=None)


def _fmt(moment: datetime) -> str:
    """Naive local wall-clock text (YYYY-MM-DDTHH:MM) of an already-local datetime."""
    return moment.strftime(DATETIME_FMT)


def _fmt_local(moment: datetime, zone: str) -> str:
    return _fmt(_to_local(moment, zone))


def _fmt_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _place_at_or_after(base: datetime, hhmm: Tuple[int, int], zone: str,
                       not_before: Optional[datetime]) -> datetime:
    """Clock time ``hhmm`` on the local day of ``base`` in ``zone`` (UTC), moved to later days until it is
    not before ``not_before`` (an aware UTC instant)."""
    wall = base.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
    candidate = _wall_to_utc(wall, zone)
    while not_before is not None and candidate < not_before:
        wall += timedelta(days=1)
        candidate = _wall_to_utc(wall, zone)
    return candidate


def _night_wait(start: datetime, end: datetime, zone: str) -> bool:
    """True when the wait from ``start`` to ``end`` (aware UTC) overlaps the local 00:00-06:00 window of the
    waiting place (``zone``) by at least NIGHT_WAIT_MIN_OVERLAP_MINS. The wait is measured in real minutes."""
    if end <= start:
        return False
    local = _to_local(start, zone)
    start_m = local.hour * 60 + local.minute
    end_m = start_m + int((end - start).total_seconds() // 60)
    return _night_overlap_mins(start_m, end_m) >= NIGHT_WAIT_MIN_OVERLAP_MINS


def _ground_arrival(seg: Any, dep: datetime, src_zone: str, dst_zone: str) -> Optional[datetime]:
    """Arrival instant (aware UTC) of a ground segment that departs at ``dep``.

    The segment's arrival_time is a clock time in the destination zone. Its day is the one of the local
    departure date, or a day either side, whichever puts the arrival after the departure and closest to
    departure + duration_mins (the cross-check). Without arrival_time the duration alone decides.
    """
    try:
        minutes: Optional[int] = int(seg.duration_mins)
    except (TypeError, ValueError):
        minutes = None
    expected = dep + timedelta(minutes=minutes) if minutes is not None else None
    arr_hm = _hhmm(getattr(seg, "arrival_time", ""))
    if arr_hm is None:
        return expected
    dep_day = _to_local(dep, src_zone).replace(hour=0, minute=0, second=0, microsecond=0)
    candidates: List[datetime] = []
    for offset in (-1, 0, 1, 2):
        wall = (dep_day + timedelta(days=offset)).replace(hour=arr_hm[0], minute=arr_hm[1])
        moment = _wall_to_utc(wall, dst_zone)
        if moment > dep:
            candidates.append(moment)
    if not candidates:
        return expected
    if expected is None:
        return min(candidates)
    return min(candidates, key=lambda m: (abs((m - expected).total_seconds()), m))


def _moment_key(leg: Dict[str, Any], key: str) -> str:
    """An option leg's instant as a sort/identity key (UTC when the leg carries it)."""
    return str(leg.get(f"{key}_utc") or leg.get(f"{key}_at"))


def _far_tier(ap: Airport) -> int:
    """Probe order of far candidates: LCC bases (flight volume) first, then major cities (the curated city
    table; the bundled data has no population), then other large airports. Nearest first inside a tier."""
    if ap.iata in LCC_HUBS:
        return 0
    if _airports._table_lookup(ap.city) is not None:
        return 1
    return 2


def _sorted_airports(airports: Sequence[Airport]) -> List[Airport]:
    return sorted(airports, key=lambda a: (a.size != "large", a.iata))


def _next_day(date: str) -> str:
    return (datetime.strptime(date, DATE_FMT) + timedelta(days=1)).strftime(DATE_FMT)


def _side_value(ap: Airport, km: float, own: bool, near: bool, history: bool) -> float:
    """Ranking score of a side candidate (higher is better)."""
    value = 10.0 if own else 0.0
    value += 3.0 if ap.size == "large" else 1.0
    if ap.iata in LCC_HUBS:
        value += 4.0 if (own or near) else 1.0
    value += 1.0 if history else 0.0
    return value - km / 100.0


def _layover_codes(raw: Dict[str, Any], frm: str, to: str) -> List[str]:
    ends = {frm, to}
    codes = _CODE_RE.findall(str(raw.get("layover_info") or ""))
    if not codes:
        codes = _CODE_RE.findall(str(raw.get("airports") or ""))
    out: List[str] = []
    for code in codes:
        if code not in ends and code not in out:
            out.append(code)
    return out


def _flight_key(option: Dict[str, Any]) -> Tuple[Tuple[str, str, str, str], ...]:
    """The flight set of an option: (from, to, depart, arrive) per flight leg. Empty for ground-only options."""
    return tuple((l["from"], l["to"], _moment_key(l, "depart"), _moment_key(l, "arrive"))
                 for l in option["legs"] if l["type"] == "flight")


def _totals_close(a: float, b: float) -> bool:
    return abs(a - b) <= DEDUPE_TOTAL_TOLERANCE * max(abs(a), abs(b), 1.0)


def _estimated_ground_legs(option: Dict[str, Any]) -> int:
    return sum(1 for l in option["legs"]
               if l["type"] not in ("flight", "transfer") and l.get("is_estimated"))


def _segment_price(seg: Any) -> Optional[float]:
    """A ground segment's price in TRY, or None when unknown (never a substitute value).

    A zero price on an estimated segment is a placeholder that a live source uses when it has no fare, so
    it counts as unknown too.
    """
    raw = getattr(seg, "price_try", None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if getattr(seg, "price_is_estimated", False) and value <= 0:
        return None
    return value


def _segment_price_from(seg: Any) -> Optional[float]:
    """The indicative "from" price of a segment (price_from_try, real but not date-specific), or None."""
    raw = getattr(seg, "price_from_try", None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _real_segment(seg: Any) -> bool:
    """True when both the times and the price of a ground segment are real (neither is an estimate)."""
    return not (getattr(seg, "is_estimated", False) or getattr(seg, "price_is_estimated", False))


def _transfer_price_is_estimated(info: Dict[str, Any]) -> bool:
    """Transfer prices are estimates unless the cost module says otherwise (official fare: False)."""
    return bool(info.get("price_is_estimated", True))


_SUPPORTS_KWARG_CACHE: Dict[Tuple[Any, str], bool] = {}


def _signature_accepts(func: Any, name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _supports_kwarg(func: Any, name: str) -> bool:
    """True when ``func`` takes a keyword ``name``: declared by name, or through a ``**kwargs`` that forwards it
    (a wrapper such as a timing shim). Checked once per call, no exceptions."""
    key = (getattr(func, "__func__", func), name)
    try:
        if key in _SUPPORTS_KWARG_CACHE:
            return _SUPPORTS_KWARG_CACHE[key]
    except TypeError:
        return _signature_accepts(func, name)

    result = _signature_accepts(func, name)
    try:
        _SUPPORTS_KWARG_CACHE[key] = result
    except TypeError:
        pass
    return result


def _real_only_violation(option: Dict[str, Any]) -> bool:
    """True when an option uses any estimate, so real_data_only drops it: an estimated non-transfer leg
    (estimated ground times or fare) or an estimated cost part (tickets, baggage, transfers, hotel)."""
    for leg in option["legs"]:
        if leg["type"] not in ("flight", "transfer") and leg.get("is_estimated"):
            return True
    return bool((option.get("cost_breakdown") or {}).get("estimated_parts"))


def _parse_flight(raw: Dict[str, Any], base: datetime, fb_from: Optional[str],
                  fb_to: Optional[str], zone_of: Any = None) -> Optional["_Flight"]:
    """A flight card searched on ``base`` (naive local departure date). The card's times are local: the
    departure clock time is in the origin airport's zone, the arrival clock time in the destination's. The
    "+N" suffix counts days from the departure date; without it the arrival is moved to the next day only
    when it would not be after the departure (in UTC). ``zone_of(code)`` gives an airport's IANA zone."""
    times = extract_flight_times(raw)
    if times is None:
        return None
    dep_hm, arr_hm = _hhmm(times[0]), _hhmm(times[1])
    if dep_hm is None or arr_hm is None:
        return None
    codes = _CODE_RE.findall(str(raw.get("airports") or ""))
    frm = codes[0] if len(codes) >= 2 else fb_from
    to = codes[-1] if len(codes) >= 2 else fb_to
    if not frm or not to or frm == to:
        return None
    dep_zone = zone_of(frm) if zone_of is not None else "UTC"
    arr_zone = zone_of(to) if zone_of is not None else "UTC"
    dep = _wall_to_utc(base.replace(hour=dep_hm[0], minute=dep_hm[1], second=0, microsecond=0), dep_zone)
    day = extract_flight_day_offset(raw)
    arr_wall = base.replace(hour=arr_hm[0], minute=arr_hm[1], second=0, microsecond=0) + timedelta(days=day)
    arr = _wall_to_utc(arr_wall, arr_zone)
    while arr <= dep:
        arr_wall += timedelta(days=1)
        arr = _wall_to_utc(arr_wall, arr_zone)
    try:
        price = float(raw.get("total_price_try") or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    layovers = () if raw.get("is_direct") else tuple(_layover_codes(raw, frm, to))
    return _Flight(
        frm=frm, to=to, dep=dep, arr=arr, price=price,
        dep_tz=dep_zone, arr_tz=arr_zone,
        operator=str(raw.get("airline") or "Havayolu"),
        url=str(raw.get("google_flights_url") or raw.get("booking_url") or ""),
        layovers=layovers,
        is_direct=bool(raw.get("is_direct")),
        stops=str(raw.get("stops") or "") or None,
        observed_at=raw.get("observed_at"),
    )


def _coord_for_place(db: Optional[Any], place: Any) -> Optional[Dict[str, float]]:
    """Coordinate for an airport code or city name, from AirportDB. None when unknown."""
    if db is None or not place:
        return None
    raw = str(place).strip()
    if not raw:
        return None
    # 1. Airport code (3 letters)
    if len(raw) == 3 and raw.isalpha():
        ap = db.get(raw)
        if ap is not None:
            return {"lat": round(float(ap.lat), 4), "lon": round(float(ap.lon), 4)}
    # 2. City name -> city_center
    centre = db.city_center(raw)
    if centre is not None:
        return {"lat": round(float(centre[0]), 4), "lon": round(float(centre[1]), 4)}
    # 3. Fallback: db.get
    ap = db.get(raw)
    if ap is not None:
        return {"lat": round(float(ap.lat), 4), "lon": round(float(ap.lon), 4)}
    return None


def _serialise_leg(
    leg: "_Leg",
    db: Optional[Any] = None,
    coord_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """An output leg. depart_at / arrive_at are local wall-clock times of the leg's endpoints (the zones are
    depart_tz / arrive_tz); depart_utc / arrive_utc are the same instants in UTC ("...Z")."""
    if db is None:
        try:
            from travel_engine.airports import AirportDB
            db = AirportDB.load_default()
        except Exception:
            db = None

    if coord_cache is not None:
        if leg.frm in coord_cache:
            from_coord = coord_cache[leg.frm]
        else:
            from_coord = _coord_for_place(db, leg.frm)
            coord_cache[leg.frm] = from_coord

        if leg.to in coord_cache:
            to_coord = coord_cache[leg.to]
        else:
            to_coord = _coord_for_place(db, leg.to)
            coord_cache[leg.to] = to_coord
    else:
        from_coord = _coord_for_place(db, leg.frm)
        to_coord = _coord_for_place(db, leg.to)

    return {
        "type": leg.kind,
        "operator": leg.operator,
        "from": leg.frm,
        "to": leg.to,
        "depart_at": _fmt_local(leg.dep, leg.dep_tz),
        "arrive_at": _fmt_local(leg.arr, leg.arr_tz),
        "depart_tz": leg.dep_tz,
        "arrive_tz": leg.arr_tz,
        "depart_utc": _fmt_utc(leg.dep),
        "arrive_utc": _fmt_utc(leg.arr),
        # None = unknown price (never a substitute); price_known is False exactly then
        "price_try": None if leg.price is None else round(float(leg.price), 2),
        "price_known": leg.price is not None,
        "price_from_try": None if leg.price_from is None else round(float(leg.price_from), 2),
        "is_estimated": bool(leg.estimated),
        "booking_url": leg.url,
        "origin_station_name": leg.origin_station_name,
        "dest_station_name": leg.dest_station_name,
        "source": leg.source,
        "price_is_estimated": leg.price_is_estimated,
        "is_last_service": leg.is_last_service,
        "next_departure_at": _fmt_local(leg.next_dep, leg.dep_tz) if leg.next_dep is not None else None,
        "stops": leg.stops,
        "observed_at": leg.observed_at,
        "from_coord": from_coord,
        "to_coord": to_coord,
        "vehicle_types": list(getattr(leg, "vehicle_types", None) or ()),
        "layovers": list(getattr(leg, "layovers", None) or ()),
    }


class _FallbackCosts:
    """Used only when travel_engine.costs is not importable. Totals are plain sums."""

    def airport_transfer(self, iata: str, at_time: Optional[str] = None) -> Dict[str, Any]:
        return {"duration_mins": 60, "price_try": 0.0, "mode": "unknown", "label": TRANSFER_FALLBACK_LABEL,
                "is_estimated": True, "note": "tahmini"}

    def cost_breakdown(self, legs, hotel_nights=0, hotel_place=None, cabin_bag=True, checked_bag=False,
                       real_data_only=False, hotel_price_try=None, transfers_from_legs=False):
        tickets = sum(float(leg["price_try"]) for leg in legs
                      if leg["type"] != "transfer" and leg["price_try"] is not None)
        transfers = sum(float(leg["price_try"]) for leg in legs
                        if leg["type"] == "transfer" and leg["price_try"] is not None)
        hotel = float(hotel_price_try) if hotel_price_try is not None else (2500.0 * hotel_nights)
        return {"tickets": tickets, "baggage": 0.0, "transfers": transfers, "hotel": hotel,
                "total": tickets + transfers + hotel, "estimated_parts": []}

    def journey_risk(self, legs, separate_tickets):
        return {"level": "medium" if separate_tickets else "low", "score": 0, "reasons": [],
                "connections": 0}


# ---------------------------------------------------------------- internal records

@dataclass(frozen=True)
class _Place:
    label: str
    kind: str                       # "city" | "airport" | "country"
    airports: Tuple[Airport, ...]
    search_name: str                # text sent to the flight provider
    ground_name: str                # text sent to the ground provider
    center: Optional[Tuple[float, float]]
    tz: str = "UTC"                 # IANA zone of the place (its airport, or the city/country's main zone)

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(a.iata for a in self.airports)

    @property
    def transfer_city(self) -> bool:
        return self.kind == "city"


@dataclass(frozen=True)
class _Flight:
    frm: str
    to: str
    dep: datetime
    arr: datetime
    price: float
    operator: str
    url: str
    layovers: Tuple[str, ...]
    is_direct: bool = True          # False for any connection, including a single-ticket one
    stops: Optional[str] = None     # the card's stop wording ("Aktarmasız", "1 aktarma", "bilinmiyor", ...)
    dep_tz: str = "UTC"             # zone of the departure airport
    arr_tz: str = "UTC"             # zone of the arrival airport
    observed_at: Optional[str] = None


@dataclass(frozen=True)
class _Leg:
    kind: str                       # "flight" | "train" | "bus" | "transfer"
    operator: str
    frm: str
    to: str
    dep: datetime
    arr: datetime
    price: Optional[float]          # None = unknown price (never a substitute value)
    estimated: bool
    url: str
    main: bool
    covers: Tuple[str, ...] = ()    # waypoint airports passed by a single-ticket layover
    # Real station names (ground: from the segment; flight: "ATH - Athens ..."; transfer: None)
    origin_station_name: Optional[str] = None
    dest_station_name: Optional[str] = None
    # Where the price and times come from: ground "live" (RegioJet) | "flixbus" | "table" | "estimate",
    # flight "google_flights", transfer "estimate". None when unknown.
    source: Optional[str] = None
    price_is_estimated: Optional[bool] = None
    stops: Optional[str] = None     # flight legs only: the card's stop wording (see _Flight.stops)
    dep_tz: str = "UTC"             # zone of the departure endpoint (dep is an aware UTC instant)
    arr_tz: str = "UTC"             # zone of the arrival endpoint
    price_from: Optional[float] = None  # indicative "from" price when the price is unknown (real, not dated)
    is_last_service: bool = False
    next_dep: Optional[datetime] = None
    observed_at: Optional[str] = None
    vehicle_types: Tuple[str, ...] = ()
    layovers: Tuple[str, ...] = ()
    is_direct: bool = True


@dataclass(frozen=True)
class _State:
    node: Tuple[Any, ...]           # ("p", place_index) | ("a", iata)
    time: Optional[datetime]        # place: when free to leave; airport: landing time; None at start
    legs: Tuple[_Leg, ...]
    mains: int
    wp: int
    done: bool
    stays: Tuple[Dict[str, Any], ...]   # {"place", "mins"} per stop with a minimum stay, in route order


class _BudgetStop(Exception):
    pass


@dataclass
class _Run:
    """One search call. Holds the flight index, caches and the planning state."""

    engine: "RouteEngine"
    origin: str
    destination: str
    dates: List[str]
    arrive_by: Optional[datetime]
    depart_after: Optional[datetime]
    max_stops: int
    via_tokens: Optional[List[str]]
    budget: int
    time_value: float
    min_stay: Dict[str, int]
    cabin_bag: bool
    checked_bag: bool
    costs: Any
    db: AirportDB
    ground: Any
    schengen_single_entry: bool = False

    used: int = 0
    exhausted: bool = False
    warnings: List[str] = field(default_factory=list)
    flight_cache: Dict[Tuple[str, str, str], List[_Flight]] = field(default_factory=dict)
    flight_index: Dict[Tuple[str, str], List[_Flight]] = field(default_factory=dict)
    # Parallel flight searches per batch (1 = one search at a time, as before)
    concurrency: int = 1
    # Flight searches skipped by calendar pruning (not budget-limited, not reported as unsearched)
    pruned: Set[Tuple[str, str, str]] = field(default_factory=set)
    # Bumped whenever flights are added; the best score so far is recomputed only then
    flight_version: int = 0
    best_score_memo: Tuple[int, Optional[float]] = (-1, None)
    score_memo: Dict[Any, Optional[float]] = field(default_factory=dict)
    ground_cache: Dict[Tuple, List[Any]] = field(default_factory=dict)
    flight_stats_start: Dict[str, Any] = field(default_factory=dict)
    ground_stats_start: Dict[str, Any] = field(default_factory=dict)
    transfer_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    coord_cache: Dict[str, Optional[Tuple[float, float]]] = field(default_factory=dict)
    min_stay_codes: Dict[str, int] = field(default_factory=dict)
    places: List[_Place] = field(default_factory=list)
    edges: Dict[int, List[Tuple[int, float]]] = field(default_factory=dict)
    target_by_code: Dict[str, List[int]] = field(default_factory=dict)
    origin_side: List[Tuple[Airport, float, bool]] = field(default_factory=list)
    dest_side: List[Tuple[Airport, float, bool]] = field(default_factory=list)
    origin_extra: List[Tuple[Airport, float, bool]] = field(default_factory=list)
    dest_extra: List[Tuple[Airport, float, bool]] = field(default_factory=list)
    # Far candidates (auto mode): (airport, road km) with a known ground connection from the place
    origin_far: List[Tuple[Airport, float]] = field(default_factory=list)
    dest_far: List[Tuple[Airport, float]] = field(default_factory=list)
    hubs: List[str] = field(default_factory=list)
    # Fixed mode: airport codes searched per place (origin, each waypoint, destination)
    via_tried: List[List[str]] = field(default_factory=list)
    via: bool = False
    via_on_date_input: Optional[Dict[str, str]] = None
    via_on_date: Dict[int, Any] = field(default_factory=dict)
    via_count: int = 0
    side_of: Dict[int, int] = field(default_factory=dict)
    side_role: Dict[int, str] = field(default_factory=dict)
    _country_ground_km: Dict[Tuple[str, int], Optional[float]] = field(default_factory=dict)
    _stay_cache: Dict[int, int] = field(default_factory=dict)
    _transfer_at_cache: Dict[Tuple[str, Optional[datetime]], Dict[str, Any]] = field(default_factory=dict)
    max_mains: int = 1
    truncated: bool = False
    price_cache: Dict[Tuple[str, str], float] = field(default_factory=dict)
    tz_cache: Dict[str, str] = field(default_factory=dict)
    _calendar_prices_memo: Dict[Tuple[str, str, Optional[float]], Dict[str, float]] = field(default_factory=dict)
    _obs_min_price_memo: Dict[Tuple[str, str, str], Optional[float]] = field(default_factory=dict)
    # arrive_by / depart_after as aware UTC instants (set by bind_times): arrive_by is wall-clock time at the
    # destination, depart_after at the origin. arrive_by / depart_after above stay naive local times.
    arrive_by_utc: Optional[datetime] = None
    arrival_limit_utc: Optional[datetime] = None
    depart_after_utc: Optional[datetime] = None
    # "Sadece gerçek veriler": no estimate is used (see the module docstring)
    real_data_only: bool = False
    # Ground segments and options dropped by real_data_only; an empty result then gets REAL_ONLY_EMPTY_WARNING
    real_filtered: int = 0
    # Google Maps page loads of this search (shared by every find_ground call); refusals give MAPS_BUDGET_WARNING
    maps_budget: Optional[MapsBudget] = None
    # Google Hotels page loads of this search
    hotels_budget: Optional[Any] = None
    hotel_cache: Dict[Tuple[str, str], Optional[float]] = field(default_factory=dict)
    # Ranking pools of the known ground prices, memoised on the size of ground_cache
    rank_memo: Dict[str, Any] = field(default_factory=dict)
    # Phase times and counters of this search (OLC-1); None = not traced
    trace: Optional[SearchTrace] = None
    reject_counts: Dict[str, int] = field(default_factory=dict)
    _rejected: Set[Tuple[_Flight, str]] = field(default_factory=set)
    _date_checked: Set[Tuple[str, Tuple[str, ...]]] = field(default_factory=set)

    def phase(self, name: str) -> Any:
        """Context manager timing ``name`` in the trace (does nothing without a trace)."""
        return self.trace.phase(name) if self.trace is not None else contextlib.nullcontext()

    def count(self, name: str, amount: int = 1) -> None:
        if self.trace is not None:
            self.trace.add(name, amount)

    def reject(self, reason: str, flight: Optional[_Flight] = None, count: int = 1) -> None:
        if flight is not None:
            if (flight, reason) in self._rejected:
                return
            self._rejected.add((flight, reason))
        self.reject_counts[reason] = self.reject_counts.get(reason, 0) + count
        self.count(f"reject_{reason}", count)

    # ------------------------------------------------------------ setup

    @property
    def last(self) -> int:
        """Index of the destination place. Auto mode: place 1; fixed mode: the last waypoint."""
        if self.via and self.via_count > 0:
            return self.via_count - 1
        return len(self.places) - 1 if self.via else 1

    def airport_tz(self, code: str) -> str:
        """IANA zone of an airport code (cached; "UTC" for a code the database does not know)."""
        zone = self.tz_cache.get(code)
        if zone is None:
            zone = self.db.timezone(code)
            self.tz_cache[code] = zone
        return zone

    def bind_times(self) -> None:
        """arrive_by is a local time at the destination place and depart_after a local time at the origin:
        both become UTC instants here, once the places (and their zones) are known."""
        if self.arrive_by is not None:
            self.arrive_by_utc = _wall_to_utc(self.arrive_by, self.places[self.last].tz)
            self.arrival_limit_utc = self.arrive_by_utc + timedelta(hours=NEAR_MISS_HOURS)
        if self.depart_after is not None:
            self.depart_after_utc = _wall_to_utc(self.depart_after, self.places[0].tz)

    def resolve(self, text: str) -> Tuple[str, List[Airport]]:
        resolver = getattr(self.db, "_resolve_kind", None)
        if callable(resolver):
            kind, airports = resolver(text)
        else:
            airports = list(self.db.resolve(text))
            kind = "city" if airports else "none"
        if kind == "none" or not airports:
            return "none", []
        return kind, _sorted_airports(airports)

    def make_place(self, text: str, kind: str, airports: Sequence[Airport]) -> _Place:
        if kind == "airport":
            ap = airports[0]
            return _Place(ap.iata, "airport", (ap,), ap.iata, ap.city, (ap.lat, ap.lon),
                          self.airport_tz(ap.iata))
        center = self.db.city_center(text) if kind == "city" else None
        return _Place(text.strip(), kind, tuple(airports), text.strip(), text.strip(), center,
                      self.db.timezone(text))

    def airport_place(self, ap: Airport) -> _Place:
        return _Place(ap.iata, "city", (ap,), ap.iata, ap.city, (ap.lat, ap.lon), self.airport_tz(ap.iata))

    def airport_label(self, iata: str) -> Optional[str]:
        """"ATH - Athens ..." for a flight leg's airport; the bare IATA code if the name is unknown."""
        airport = self.db.get(iata) if callable(getattr(self.db, "get", None)) else None
        name = _station_name(getattr(airport, "name", None))
        if not name:
            return None
        return f"{iata} - {name}"

    def ground_km(self, a: _Place, b: _Place) -> Optional[float]:
        if self.ground is None:
            return None
        try:
            km = self.ground.ground_distance_km(
                from_place=a.ground_name, to_place=b.ground_name,
                from_coord=a.center, to_coord=b.center)
        except Exception as exc:  # a broken distance lookup must not abort the search
            logger.warning("ground_distance_km %s -> %s failed: %s", a.ground_name, b.ground_name, exc)
            return None
        return None if km is None else float(km)

    def is_island_code(self, code: str) -> bool:
        checker = getattr(self.db, "is_island", None)
        if callable(checker):
            try:
                return bool(checker(code))
            except Exception as exc:  # a broken lookup falls back to the static list
                logger.warning("is_island(%s) failed: %s", code, exc)
        return code in FALLBACK_ISLAND_AIRPORTS

    def reachable_from_own(self, cand: Airport, own: Sequence[Airport]) -> bool:
        checker = getattr(self.db, "ground_reachable", None)
        if callable(checker):
            try:
                return any(bool(checker(o.iata, cand.iata)) for o in own)
            except Exception as exc:
                logger.warning("ground_reachable(%s, %s) failed: %s", cand.iata, own[0].iata if own else "", exc)
        # Fallback without ground_reachable: an island is a candidate only for an island place.
        if self.is_island_code(cand.iata):
            return any(self.is_island_code(o.iata) for o in own)
        return True

    def history_codes(self, text: str, codes: Sequence[str], role: str = "origin") -> set:
        log = self.engine.search_log
        if log is None or not codes:
            return set()
        fn = getattr(log, "codes_with_history", None)
        if callable(fn):
            try:
                return set(fn(list(codes), role=role))
            except Exception as exc:
                logger.warning("search log lookup failed: %s", exc)
                return set()
        try:
            rows = log.cheapest_destinations(text, list(codes), days=90)
        except Exception as exc:
            logger.warning("search log lookup failed: %s", exc)
            return set()
        return {code for code, avg in rows if not math.isinf(avg)}

    def history_price(self, origin_code: str, dest_code: str) -> float:
        key = (origin_code, dest_code)
        if key not in self.price_cache:
            price = math.inf
            log = self.engine.search_log
            if log is not None:
                try:
                    rows = log.cheapest_destinations(origin_code, [dest_code], days=90)
                    if rows:
                        price = float(rows[0][1])
                except Exception as exc:
                    logger.warning("search log price lookup failed: %s", exc)
            self.price_cache[key] = price
        return self.price_cache[key]

    def history_order(self, pairs: List[Tuple[Airport, Airport]]) -> List[Tuple[Airport, Airport]]:
        """Pairs with a known cheapest historical price first (cheapest first); the rest keep their order."""
        priced: List[Tuple[float, int, Airport, Airport]] = []
        rest: List[Tuple[Airport, Airport]] = []
        for idx, (a, b) in enumerate(pairs):
            price = self.history_price(a.iata, b.iata)
            if math.isinf(price):
                rest.append((a, b))
            else:
                priced.append((price, idx, a, b))
        priced.sort(key=lambda item: (item[0], item[1]))
        return [(a, b) for _, _, a, b in priced] + rest

    @staticmethod
    def own_order_key(ap: Airport, other_center: Optional[Tuple[float, float]]) -> Tuple[float, float, str]:
        value = _side_value(ap, 0.0, True, True, False)
        dist = haversine_km(other_center[0], other_center[1], ap.lat, ap.lon) if other_center else 0.0
        return (-value, dist, ap.iata)

    def ordered_place(self, place: _Place, other_center: Optional[Tuple[float, float]]) -> _Place:
        """Country places: own airports ordered by score (size, LCC status, distance to the other end)."""
        if place.kind != "country" or not place.airports:
            return place
        ordered = sorted(place.airports, key=lambda ap: self.own_order_key(ap, other_center))
        return replace(place, airports=tuple(ordered))

    def rank_sides(self, place: _Place, text: str, radius: float, keep: int, extra_keep: int,
                   other_center: Optional[Tuple[float, float]],
                   role: str = "origin") -> Tuple[List[Tuple[Airport, float, bool]],
                                                  List[Tuple[Airport, float, bool]]]:
        """(side candidates, extra candidates). Extras are near non-LCC airports without SearchLog history:
        they fail the flight volume gate, so they are searched only with budget left over."""
        own = list(place.airports)
        items: List[Tuple[Airport, float, bool]] = [(ap, 0.0, True) for ap in own]
        near: Dict[str, bool] = {}
        if self.ground is not None and place.center is not None:
            for ap, straight in self.db.nearby_airports(text, radius_km=max(radius, LCC_SIDE_KM), limit=60):
                lcc = ap.iata in LCC_HUBS
                if not lcc and straight > radius:
                    continue
                if not self.reachable_from_own(ap, own):
                    continue
                km = self.ground_km(place, self.airport_place(ap))
                if km is None:
                    continue
                is_near = km <= radius * SIDE_NEAR_FACTOR
                if not is_near and not (lcc and km <= LCC_SIDE_KM):
                    continue
                items.append((ap, km, False))
                near[ap.iata] = is_near
        history = self.history_codes(text, [ap.iata for ap, _, flag in items if not flag], role=role)

        def key(item: Tuple[Airport, float, bool]) -> Tuple[float, float, str]:
            ap, km, flag = item
            value = _side_value(ap, km, flag, near.get(ap.iata, flag), ap.iata in history)
            dist = haversine_km(other_center[0], other_center[1], ap.lat, ap.lon) if other_center else 0.0
            return (-value, dist, ap.iata)

        def pick(candidates: List[Tuple[Airport, float, bool]], limit: int) -> List[Tuple[Airport, float, bool]]:
            out: List[Tuple[Airport, float, bool]] = []
            kept_own = kept_other = 0
            for item in sorted(candidates, key=key):
                if item[2]:
                    if kept_own >= OWN_SIDE_KEEP:
                        continue
                    kept_own += 1
                else:
                    if kept_other >= limit:
                        continue
                    kept_other += 1
                out.append(item)
            return out

        # Flight volume gate for non-own airports: LCC base or SearchLog history.
        gated = [item for item in items
                 if item[2] or item[0].iata in LCC_HUBS or item[0].iata in history]
        extra = [item for item in items
                 if not item[2] and item[0].iata not in LCC_HUBS and item[0].iata not in history
                 and near.get(item[0].iata)]
        return pick(gated, keep), pick(extra, extra_keep)

    def rank_side(self, place: _Place, text: str, radius: float, keep: int,
                  other_center: Optional[Tuple[float, float]],
                  role: str = "origin") -> List[Tuple[Airport, float, bool]]:
        return self.rank_sides(place, text, radius, keep, 0, other_center, role=role)[0]

    def far_candidates(self, place: _Place, text: str, listed: set, date: str,
                       inbound: bool, keep: int = FAR_SIDE_KEEP) -> List[Tuple[Airport, float]]:
        """Far candidates of one end (auto mode, low priority; see C4b in the module docstring).

        LCC or large airports within FAR_ROAD_KM by road, not listed already, reachable from the place's own
        airports, and with a known ground connection on ``date`` (live, FlixBus or table). An estimate-only
        connection counts only when the flight budget is at least FAR_ESTIMATE_MIN_BUDGET. Only ground
        lookups are made here, no flight searches. ``inbound``: the ground leg runs from the candidate to
        the place (destination end).
        """
        if self.ground is None or place.center is None:
            return []
        own = list(place.airports)
        own_codes = {ap.iata for ap in own}
        ranked: List[Tuple[Tuple[int, float, str], Airport, float]] = []
        for ap, _straight in self.db.nearby_airports(text, radius_km=FAR_ROAD_KM, limit=80):
            if ap.iata in listed or ap.iata in own_codes:
                continue
            if ap.iata not in LCC_HUBS and ap.size != "large":
                continue
            if not self.reachable_from_own(ap, own):
                continue
            km = self.ground_km(place, self.airport_place(ap))
            if km is None or km > FAR_ROAD_KM:
                continue
            ranked.append(((_far_tier(ap), km, ap.iata), ap, km))
        ranked.sort(key=lambda item: item[0])
        out: List[Tuple[Airport, float]] = []
        for _key, ap, km in ranked[:FAR_PROBE_LIMIT]:
            if len(out) >= keep:
                break
            other = self.airport_place(ap)
            if inbound:
                segments = self.ground_segments(other, place, date, None)
            else:
                segments = self.ground_segments(place, other, date, None)
            sources = {str(getattr(seg, "source", "") or "") for seg in segments}
            if sources & KNOWN_GROUND_SOURCES or (sources and self.budget >= FAR_ESTIMATE_MIN_BUDGET):
                out.append((ap, km))
        return out

    def pick_hubs(self, own_codes: set, side_codes: set) -> List[str]:
        origin, dest = self.places[0].center, self.places[1].center
        if origin is None or dest is None:
            return []
        direct = haversine_km(origin[0], origin[1], dest[0], dest[1])
        if direct < 50.0:
            return []
        ranked: List[Tuple[float, str]] = []
        for code in LCC_HUBS:
            ap = self.db.get(code)
            if ap is None or code in own_codes or code in side_codes:
                continue
            detour = (haversine_km(origin[0], origin[1], ap.lat, ap.lon)
                      + haversine_km(ap.lat, ap.lon, dest[0], dest[1])) / direct
            if detour <= MAX_HUB_DETOUR:
                ranked.append((detour, code))
        ranked.sort()
        return [code for _, code in ranked[:HUB_KEEP]]

    def setup_auto(self) -> bool:
        kind_o, own_o = self.resolve(self.origin)
        kind_d, own_d = self.resolve(self.destination)
        if not own_o:
            self.warnings.append(f"'{self.origin}' için havalimanı bulunamadı")
            return False
        if not own_d:
            self.warnings.append(f"'{self.destination}' için havalimanı bulunamadı")
            return False
        origin = self.make_place(self.origin, kind_o, own_o)
        dest = self.make_place(self.destination, kind_d, own_d)
        origin = self.ordered_place(origin, dest.center)
        dest = self.ordered_place(dest, origin.center)
        self.places = [origin, dest]
        self.via = False
        self.max_mains = self.max_stops + 1
        # Origin side candidate ranking (departure role)
        self.origin_side, self.origin_extra = self.rank_sides(
            origin, self.origin, self.engine.origin_radius_km, ORIGIN_SIDE_KEEP, EXTRA_SIDE_KEEP, dest.center, role="origin")
        # Destination side candidate ranking (destination role)
        self.dest_side, self.dest_extra = self.rank_sides(
            dest, self.destination, self.engine.dest_radius_km, DEST_SIDE_KEEP, EXTRA_SIDE_KEEP, origin.center, role="destination")
        for code in dest.codes:
            self.target_by_code[code] = [1]
        self.edges[0] = []
        self.edges[1] = []
        for ap, km, own in self.origin_side + self.origin_extra:
            if own:
                continue
            idx = len(self.places)
            self.places.append(self.airport_place(ap))
            self.edges.setdefault(0, []).append((idx, km))
            self.edges.setdefault(idx, [])
        for ap, km, own in self.dest_side + self.dest_extra:
            if own:
                continue
            idx = len(self.places)
            self.places.append(self.airport_place(ap))
            self.edges.setdefault(idx, []).append((1, km))
            self.target_by_code.setdefault(ap.iata, []).append(idx)
        listed = {ap.iata for ap, _, _ in self.origin_side + self.origin_extra + self.dest_side + self.dest_extra}
        if self.max_stops >= 1 and self.ground is not None:
            self.origin_far = self.far_candidates(origin, self.origin, listed, self.dates[0], inbound=False)
            listed |= {ap.iata for ap, _ in self.origin_far}
            self.dest_far = self.far_candidates(dest, self.destination, listed, self.dates[0], inbound=True)
        for ap, km in self.origin_far:
            idx = len(self.places)
            self.places.append(self.airport_place(ap))
            self.edges.setdefault(0, []).append((idx, km))
            self.edges.setdefault(idx, [])
        for ap, km in self.dest_far:
            idx = len(self.places)
            self.places.append(self.airport_place(ap))
            self.edges.setdefault(idx, []).append((1, km))
            self.target_by_code.setdefault(ap.iata, []).append(idx)
        if self.ground is not None and origin.center and dest.center:
            km = self.ground_km(origin, dest)
            if km is not None and km <= ROAD_LIMIT_KM:
                self.edges[0].append((1, km))
        else:
            if self.ground is None:
                self.warnings.append("Kara yolu sağlayıcısı yok; yalnızca uçuş seçenekleri hesaplandı")
        own_codes = set(origin.codes) | set(dest.codes)
        side_codes = listed | {ap.iata for ap, _ in self.origin_far + self.dest_far}
        self.hubs = self.pick_hubs(own_codes, side_codes) if self.max_stops >= 1 else []
        return True

    def setup_via(self) -> bool:
        origin_kind, origin_airports = self.resolve(self.origin)
        if not origin_airports:
            self.warnings.append(f"'{self.origin}' için havalimanı bulunamadı")
            return False
        dest_kind, dest_airports = self.resolve(self.destination)
        if not dest_airports:
            self.warnings.append(f"'{self.destination}' için havalimanı bulunamadı")
            return False
        places = [self.make_place(self.origin, origin_kind, origin_airports)]
        for token in self.via_tokens or []:
            kind, airports = self.resolve(token)
            if not airports:
                self.warnings.append(f"'{token}' hiçbir havalimanıyla eşleşmedi (zorunlu geçiş)")
                return False
            places.append(self.make_place(token, kind, airports))
        places.append(self.make_place(self.destination, dest_kind, dest_airports))
        self.places = places
        self.via = True
        self.via_count = len(places)
        self.max_mains = 10 ** 6
        self.via_on_date = {}
        if self.via_on_date_input:
            for raw_place, dt_val in self.via_on_date_input.items():
                try:
                    d_obj = datetime.strptime(str(dt_val).strip(), DATE_FMT).date()
                except ValueError:
                    continue
                p_text = str(raw_place).strip()
                p_case = p_text.casefold()
                matched = None
                for idx in range(1, self.via_count - 1):
                    p = places[idx]
                    p_label = getattr(p, "label", "").casefold()
                    p_text_attr = getattr(p, "text", "").casefold()
                    if p_case == p_label or (p_text_attr and p_case == p_text_attr) or p_text.upper() in p.codes:
                        matched = idx
                        break
                if matched is None:
                    kind, aps = self.resolve(p_text)
                    if aps:
                        overlapping = []
                        for idx in range(1, self.via_count - 1):
                            p = places[idx]
                            if any(ap.iata in p.codes for ap in aps):
                                overlapping.append((len(p.airports), idx))
                        if overlapping:
                            overlapping.sort(key=lambda item: (item[0], item[1]))
                            matched = overlapping[0][1]
                if matched is not None:
                    self.via_on_date[matched] = d_obj
                else:
                    self.warnings.append(f"'{raw_place}' ara durağı bulunamadı (via_on_date)")
        self.edges = {}
        for i in range(self.via_count - 1):
            a, b = places[i], places[i + 1]
            if self.ground is not None and a.transfer_city and b.transfer_city:
                km = self.ground_km(a, b)
                if km is not None and km <= VIA_ROAD_LIMIT_KM:
                    self.edges[i] = [(i + 1, km)]
        for idx in range(self.via_count):
            place = places[idx]
            for code in place.codes:
                self.target_by_code.setdefault(code, []).append(idx)
        if self.max_stops >= 1 and self.ground is not None and self.dates:
            own_codes = {ap.iata for p in places[:self.via_count] for ap in p.airports}
            listed = set(own_codes)
            last_stop = self.via_count - 1
            for i in range(self.via_count):
                p = self.places[i]
                if p.kind == "country":
                    continue
                if i < last_stop:
                    cands = self.far_candidates(p, p.label, listed, self.dates[0], inbound=False, keep=VIA_SIDE_KEEP)
                    for ap, km in cands[:VIA_SIDE_KEEP]:
                        s = len(self.places)
                        self.places.append(self.airport_place(ap))
                        self.side_of[s] = i
                        self.side_role[s] = "out"
                        self.edges.setdefault(i, []).append((s, km))
                        self.edges.setdefault(s, [])
                        listed.add(ap.iata)
                if i > 0:
                    cands = self.far_candidates(p, p.label, listed, self.dates[0], inbound=True, keep=VIA_SIDE_KEEP)
                    for ap, km in cands[:VIA_SIDE_KEEP]:
                        s = len(self.places)
                        self.places.append(self.airport_place(ap))
                        self.side_of[s] = i
                        self.side_role[s] = "in"
                        self.edges.setdefault(s, []).append((i, km))
                        self.target_by_code.setdefault(ap.iata, []).append(s)
                        listed.add(ap.iata)
        return True

    def setup_min_stay(self) -> None:
        for key, minutes in self.min_stay.items():
            kind, airports = self.resolve(str(key))
            if not airports:
                self.warnings.append(f"'{key}' için minimum kalış yeri bulunamadı")
                continue
            for ap in airports:
                self.min_stay_codes[ap.iata] = max(self.min_stay_codes.get(ap.iata, 0), int(minutes))
        self._stay_cache.clear()  # stay_at_place values depend on min_stay_codes

    # ------------------------------------------------------------ flight searches

    def search(self, frm: Tuple[str, Optional[str]], to: Tuple[str, Optional[str]], date: str) -> List[_Flight]:
        key = (frm[0].upper(), to[0].upper(), date)
        if key in self.flight_cache:
            return self.flight_cache[key]
        if self.used >= self.budget:
            raise _BudgetStop()
        self.used += 1
        raw, error = self.fetch(frm, to, date)
        return self.ingest(frm, to, date, raw, error)

    def fetch(self, frm: Tuple[str, Optional[str]], to: Tuple[str, Optional[str]],
              date: str) -> Tuple[List[Any], Optional[str]]:
        """The provider call of one flight search. Touches no run state, so batches may call it in threads.

        Returns (raw cards, error text). A failing search must not abort the whole plan: it gives no cards.
        """
        try:
            with self.phase("flight_fetch"):
                raw = self.engine.flight_provider.search_flights(
                    origin=frm[0], destination=to[0], departure_date=date, trip_type="oneway") or []
            return list(raw), None
        except Exception as exc:
            logger.warning("Flight search %s->%s on %s failed: %s", frm[0], to[0], date, exc)
            return [], str(exc)

    def ingest(self, frm: Tuple[str, Optional[str]], to: Tuple[str, Optional[str]], date: str,
               raw: List[Any], error: Optional[str]) -> List[_Flight]:
        """Parse the cards of one search and index them. Always called from the planning thread, in plan order."""
        key = (frm[0].upper(), to[0].upper(), date)
        if error is not None:
            self.warnings.append(f"Uçuş araması başarısız: {frm[0]} ➔ {to[0]} ({date})")
        base = datetime.strptime(date, DATE_FMT)
        parsed: List[_Flight] = []
        skipped = 0
        for item in raw:
            flight = _parse_flight(item, base, frm[1], to[1], self.airport_tz)
            if flight is None:
                skipped += 1
            elif not flight.is_direct and not self.via and self.max_stops == 0:
                # A non-stop search takes only flights confirmed non-stop; a connection (or a card whose stops
                # are unknown) is not a non-stop, even when it is sold as one ticket.
                continue
            else:
                parsed.append(flight)
        if skipped:
            # A card without airport codes is kept only when the searched endpoint is one airport (its code is
            # the fallback). Say so, instead of dropping the flights silently.
            self.warnings.append(f"{skipped} uçuş okunamadı (havalimanı ya da saat bilgisi eksik): "
                                 f"{frm[0]} ➔ {to[0]} ({date})")
        self.flight_cache[key] = parsed
        for flight in parsed:
            self.flight_index.setdefault((date, flight.frm), []).append(flight)
        if parsed:
            self.flight_version += 1
        obs_store = getattr(self.engine, "observations", None)
        if obs_store is not None:
            pairs: Dict[Tuple[str, str], List[float]] = {}
            for flight in parsed:
                if flight.frm and flight.to:
                    pairs.setdefault((flight.frm.upper(), flight.to.upper()), []).append(flight.price)
            for (p_frm, p_to), prices in pairs.items():
                pos = [p for p in prices if p > 0]
                if not pos:
                    continue  # no known price for this pair: never record 0 as an observed price
                min_p = min(pos)
                card_count = len(prices)
                try:
                    obs_store.record(
                        origin=p_frm,
                        destination=p_to,
                        travel_date=date,
                        min_price=min_p,
                        card_count=card_count,
                    )
                except Exception as e:
                    logger.warning("Observation record failed for %s->%s (%s): %s", p_frm, p_to, date, e)
        return parsed

    def run_batch(self, items: List[Tuple[Any, Any, str]]) -> None:
        """Run already-budgeted searches: concurrently when there are several and the provider allows it.

        Results are ingested in the order of ``items`` whatever order the searches finish in, so the
        plan (and so the result) does not depend on timing.
        """
        if len(items) > 1 and self.concurrency > 1:
            workers = min(self.concurrency, len(items))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="route-flight") as pool:
                def _run_with_ctx(item):
                    ctx = contextvars.copy_context()
                    return ctx.run(self.fetch, *item)
                fetched = list(pool.map(_run_with_ctx, items))
        else:
            fetched = [self.fetch(*item) for item in items]
        for (frm, to, date), (raw, error) in zip(items, fetched):
            self.ingest(frm, to, date, raw, error)

    @staticmethod
    def request(place: _Place) -> Tuple[str, Optional[str]]:
        fallback = place.codes[0] if len(place.codes) == 1 else None
        return place.search_name, fallback

    def endpoints(self, place: _Place) -> List[Tuple[str, Optional[str]]]:
        if place.kind in ("city", "airport"):
            return [self.request(place)]
        return [(code, code) for code in place.codes[:3]]

    # ------------------------------------------------------------ calendar prices (no browser)

    def calendar_prices(self, frm: Tuple[str, Optional[str]], to: Tuple[str, Optional[str]],
                        max_age_secs: Optional[float] = None) -> Dict[str, float]:
        """Known Google calendar minimum per date for the one-way route frm -> to ({} when unknown)."""
        getter = getattr(self.engine.flight_provider, "get_calendar_prices", None)
        if not callable(getter):
            return {}
        try:
            if max_age_secs is not None and _supports_kwarg(getter, "max_age_secs"):
                prices = getter(frm[0], to[0], "oneway", max_age_secs=max_age_secs)
            else:
                prices = getter(frm[0], to[0], "oneway")
        except Exception:
            logger.debug("Calendar lookup %s->%s failed", frm[0], to[0], exc_info=True)
            return {}
        return prices if isinstance(prices, dict) else {}

    def calendar_price(self, frm: Tuple[str, Optional[str]], to: Tuple[str, Optional[str]], date: str) -> Optional[float]:
        price = self.calendar_prices(frm, to).get(date)
        try:
            value = float(price)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def known_bound(self, frm: Any, to: Any, date: str) -> Optional[float]:
        """Lowest known price bound for (frm, to, date): min of calendar (up to 24h old)
        and observation store (when both frm and to resolve to a specific airport code).
        """
        f_req = frm[0] if isinstance(frm, (tuple, list)) else frm
        f_code = frm[1] if isinstance(frm, (tuple, list)) and len(frm) > 1 else (frm if isinstance(frm, str) else None)
        t_req = to[0] if isinstance(to, (tuple, list)) else to
        t_code = to[1] if isinstance(to, (tuple, list)) and len(to) > 1 else (to if isinstance(to, str) else None)

        cal_bound: Optional[float] = None
        cal_prices = self.calendar_prices((f_req, f_code), (t_req, t_code), max_age_secs=24 * 3600)
        if cal_prices and date in cal_prices:
            try:
                v = float(cal_prices[date])
                if v > 0:
                    cal_bound = v
            except (TypeError, ValueError):
                pass

        obs_bound: Optional[float] = None
        obs_store = getattr(self.engine, "observations", None)
        if obs_store is not None and bool(f_code) and bool(t_code):
            obs_key = (f_code.upper(), t_code.upper(), str(date))
            if obs_key in self._obs_min_price_memo:
                obs_bound = self._obs_min_price_memo[obs_key]
            else:
                getter_obs = getattr(obs_store, "min_price", None)
                if callable(getter_obs):
                    try:
                        p = getter_obs(f_code, t_code, date)
                        if p is not None:
                            v = float(p)
                            if v > 0:
                                obs_bound = v
                    except Exception as e:
                        logger.debug("Observation lookup %s->%s (%s) failed: %s", f_code, t_code, date, e)
                self._obs_min_price_memo[obs_key] = obs_bound

        knowns = [b for b in (cal_bound, obs_bound) if b is not None]
        return min(knowns) if knowns else None

    def order_by_known_bound(self, items: List[Tuple[Any, Any, str]]) -> List[Tuple[Any, Any, str]]:
        """Reorder search items so known-bound items are sorted ascending among themselves,
        while unknown-bound items remain in their original positions.
        Ties preserve their original relative order (stable sort).
        """
        if not items or len(items) <= 1:
            return list(items) if items else []

        bounds = [self.known_bound(item[0], item[1], item[2]) for item in items]
        known_entries = [(idx, item, b) for idx, (item, b) in enumerate(zip(items, bounds)) if b is not None]

        if len(known_entries) <= 1:
            return list(items)

        # Stable sort of known items by bound value ascending
        known_sorted = sorted(known_entries, key=lambda entry: entry[2])

        result = list(items)
        for (orig_idx, _, _), (_, sorted_item, _) in zip(known_entries, known_sorted):
            result[orig_idx] = sorted_item
        return result

    def best_score(self) -> Optional[float]:
        """Lowest option score among the chains the flights found so far allow (None when there is none).

        Recomputed only when flights were added since the last call. The DFS is the same one the final
        result uses; its truncation flag is restored, so this call never changes the outcome.

        Chains whose legs all have known prices are cached in self.score_memo keyed by (legs, stays).
        Chains with unknown price legs are not cached because their rank penalties change as search progresses.
        """
        version, memo = self.best_score_memo
        if version == self.flight_version:
            return memo
        with self.phase("best_score"):
            self.count("best_score_recomputes")
            truncated = self.truncated
            best: Optional[float] = None
            for date in self.dates:
                for state in self.dfs(date):
                    if self.depart_after_utc is not None and state.legs[0].dep < self.depart_after_utc:
                        continue
                    if self.arrive_by_utc is not None and state.legs[-1].arr > self.arrive_by_utc:
                        continue

                    cacheable = all(leg.price is not None for leg in state.legs)
                    try:
                        key = (state.legs, state.stays) if cacheable else None
                        if key is not None and key in self.score_memo:
                            score = self.score_memo[key]
                        else:
                            option = self.engine._option(self, state)
                            score = None if (self.real_data_only and _real_only_violation(option)) else option["score"]
                            if key is not None:
                                self.score_memo[key] = score
                    except TypeError:
                        stays_key = tuple((s["place"], s["mins"]) if isinstance(s, dict) else s for s in state.stays)
                        key = (state.legs, stays_key) if cacheable else None
                        if key is not None and key in self.score_memo:
                            score = self.score_memo[key]
                        else:
                            option = self.engine._option(self, state)
                            score = None if (self.real_data_only and _real_only_violation(option)) else option["score"]
                            if key is not None:
                                self.score_memo[key] = score

                    if score is not None:
                        if best is None or score < best:
                            best = score
            self.truncated = truncated
            self.best_score_memo = (self.flight_version, best)
            return best

    def should_prune(self, label: str, frm: Tuple[str, Optional[str]], to: Tuple[str, Optional[str]], date: str) -> bool:
        """True when this flight search cannot change the result (calendar bound, conservative).

        Every chain that uses a flight of this search costs at least that flight's price, and its score is at
        least its cost. So when the calendar minimum of the route on the date is above CALENDAR_PRUNE_MARGIN x
        the best score found so far, the search cannot produce the best or the cheapest option. Never applied
        to the "direct" category (origin -> destination), and only when the calendar of the route is known.
        Limitation: a pruned connection could still be the fastest one (its duration is not bounded here).
        """
        if label == "direct":
            return False
        price = self.calendar_price(frm, to, date)
        if price is None:
            return False
        best = self.best_score()
        return best is not None and price > CALENDAR_PRUNE_MARGIN * best

    def round_robin(self, pairs: Sequence[Tuple[Any, Any]],
                    dates: Optional[Sequence[str]] = None) -> List[Tuple[Any, Any, str]]:
        """(pair, date) queue: in round c, pair i is searched on date (i + c) mod n_dates.

        Inside one round the pairs with a known price bound come cheapest first (the rest keep their order).
        """
        dates = list(self.dates if dates is None else dates)
        count = len(dates)
        out: List[Tuple[Any, Any, str]] = []
        if count == 0:
            return out
        seen = set()
        for cycle in range(count):
            block: List[Tuple[Any, Any, str]] = []
            for i, (frm, to) in enumerate(pairs):
                date = dates[(i + cycle) % count]
                key = (frm[0].upper(), to[0].upper(), date)
                if key in seen:
                    continue
                seen.add(key)
                block.append((frm, to, date))
            block = self.order_by_known_bound(block)
            out.extend(block)
        return out

    def run_categories(self, categories: List[Tuple[str, Optional[float], List[Tuple[Any, Any, str]]]],
                       capped: bool) -> None:
        """Run the queues in order. With capped=True a category with a share stops at share * budget.

        Searches are reserved against the budget in queue order and run in batches of ``concurrency``.
        A batch is run (and ingested) before the next one is reserved, so pruning sees the flights of
        every earlier batch. With concurrency 1 this is the one-at-a-time loop it always was.
        """
        width = max(1, int(self.concurrency))
        for label, share, queue in categories:
            if label == "via_side":
                limit = VIA_SIDE_MAX_SEARCHES
            else:
                limit = int(share * self.budget) if (capped and share is not None) else None
            spent = 0
            batch: List[Tuple[Any, Any, str]] = []
            batch_keys: Set[Tuple[str, str, str]] = set()

            def flush() -> None:
                if batch:
                    items = list(batch)
                    batch.clear()
                    batch_keys.clear()
                    self.run_batch(items)

            for frm, to, date in queue:
                key = (frm[0].upper(), to[0].upper(), date)
                if key in self.flight_cache or key in batch_keys:
                    continue
                if self.should_prune(label, frm, to, date):
                    self.pruned.add(key)
                    continue
                if self.used >= self.budget:
                    flush()
                    raise _BudgetStop()
                if limit is not None and spent >= limit:
                    break
                self.used += 1
                spent += 1
                batch.append((frm, to, date))
                batch_keys.add(key)
                if len(batch) >= width:
                    flush()
            flush()

    def run_plan(self, categories: List[Tuple[str, Optional[float], List[Tuple[Any, Any, str]]]],
                 next_day: bool) -> None:
        """Passes: capped, then uncapped, then next-day connections, then the LATE categories (extra near
        airports, searched only with budget left after every other category)."""
        categories = [(label, share, self.order_by_known_bound(queue)) for label, share, queue in categories]
        main = [c for c in categories if c[0] not in LATE_CATEGORIES]
        late = [c for c in categories if c[0] in LATE_CATEGORIES]
        try:
            self.run_categories(main, capped=True)
            self.run_categories(main, capped=False)
            if next_day:
                self.run_categories([("next_day", None, self.order_by_known_bound(self.next_day_queue()))], capped=False)
            self.run_categories(late, capped=False)
        except _BudgetStop:
            self.exhausted = True
            self.warnings.append(BUDGET_WARNING)
            plan = list(main)
            if next_day:
                plan.append(("next_day", None, self.order_by_known_bound(self.next_day_queue())))
            plan.extend(late)
            note = self.unsearched_note(plan)
            if note:
                self.warnings.append(note)

    def unsearched_note(self, plan: List[Tuple[str, Optional[float], List[Tuple[Any, Any, str]]]]) -> Optional[str]:
        """Turkish warning naming the categories and dates whose searches did not run (budget exhausted)."""
        pending: Dict[str, List[str]] = {}
        for label, _share, queue in plan:
            for frm, to, date in queue:
                key = (frm[0].upper(), to[0].upper(), date)
                if key in self.flight_cache or key in self.pruned:
                    continue
                dates = pending.setdefault(label, [])
                if date not in dates:
                    dates.append(date)
        if not pending:
            return None
        parts = [f"{CATEGORY_NAMES.get(label, label)} ({', '.join(sorted(dates))})"
                 for label, dates in pending.items()]
        return "Bütçe nedeniyle tam taranamayan aramalar: " + "; ".join(parts)

    def no_option_reason(self) -> Optional[str]:
        """Turkish reason why no option was produced, from the flights that were found (P1)."""
        found = [flight for flights in self.flight_index.values() for flight in flights]
        counts = [(reason, c) for reason, c in self.reject_counts.items() if c > 0]
        top = sorted(counts, key=lambda kv: (-kv[1], kv[0]))[:3]
        details = f" ({', '.join(f'{REASON_LABELS.get(r, r)}: {c}' for r, c in top)})" if top else ""
        if not self.via:
            if not found:
                return f"Hiçbir uçuş bulunamadı ({self.used} arama yapıldı)"
            return (f"{len(found)} uçuş bulundu ama aktarma süresi / son varış saati "
                    f"kurallarına uymadı{details}")
        labels = " → ".join(self.via_tokens or [])
        prefix = f"{labels} üzerinden uygun bağlantı bulunamadı: "
        if not found:
            return prefix + f"hiçbir uçuş bulunamadı ({self.used} arama yapıldı)"
        for k in range(self.via_count - 1):
            a, b = self.places[k], self.places[k + 1]
            if not any(f.frm in a.codes and f.to in b.codes for f in found):
                return prefix + f"{a.label} ➔ {b.label} arasında uçuş bulunamadı"
        matched = [f for f in found if any(
            f.frm in self.places[k].codes and f.to in self.places[k + 1].codes
            for k in range(self.via_count - 1))]
        return prefix + (f"{len(matched)} uçuş bulundu ama aktarma süresi/son varış saati "
                         f"kurallarına uymadı{details}")

    def side_pairs(self, keep, origin_items=None, dest_items=None,
                   ) -> List[Tuple[Tuple[str, Optional[str]], Tuple[str, Optional[str]]]]:
        """Origin-side x dest-side pairs accepted by keep(a, own_a, b, own_b), ordered by side ranks."""
        origin_items = self.origin_side if origin_items is None else origin_items
        dest_items = self.dest_side if dest_items is None else dest_items
        ranked = []
        for i, (ap_a, _, own_a) in enumerate(origin_items):
            for j, (ap_b, _, own_b) in enumerate(dest_items):
                if ap_a.iata == ap_b.iata or not keep(ap_a, own_a, ap_b, own_b):
                    continue
                ranked.append((i + j, i, j, ap_a, ap_b))
        ranked.sort(key=lambda item: item[:3])
        ordered = self.history_order([(item[3], item[4]) for item in ranked])
        return [(self.request(self.airport_place(a)), self.request(self.airport_place(b))) for a, b in ordered]

    def far_pairs(self) -> List[Tuple[Any, Any]]:
        """Pairs of the far category (C4b): far origin -> destination end, and origin end -> far destination.

        Own airports of the other end come first (a far origin straight to the destination airport is the
        cheapest way to discover "ground, then flight"), then LCC ends. Inside each group the side ranks decide.
        """
        far_o = [(ap, km, False) for ap, km in self.origin_far]
        far_d = [(ap, km, False) for ap, km in self.dest_far]
        pairs: List[Tuple[Any, Any]] = []
        if far_o:
            pairs += self.side_pairs(lambda a, oa, b, ob: ob, origin_items=far_o)
            pairs += self.side_pairs(lambda a, oa, b, ob: not ob and b.iata in LCC_HUBS, origin_items=far_o)
        if far_d:
            pairs += self.side_pairs(lambda a, oa, b, ob: oa, dest_items=far_d)
            pairs += self.side_pairs(lambda a, oa, b, ob: not oa and a.iata in LCC_HUBS, dest_items=far_d)
        return pairs

    def next_day_queue(self) -> List[Tuple[Any, Any, str]]:
        """Second flights of separate-ticket chains: connection airport -> destination on the next day.

        Only for connection airports (hubs and dest-side LCC candidates) where a flight of the depart
        date landed, and only when arrive_by allows an arrival on the next day.
        """
        if self.via or self.max_stops < 1:
            return []
        dest_req = self.request(self.places[1])
        dest_codes = set(self.places[1].codes)
        connect = list(dict.fromkeys(list(self.hubs) + [ap.iata for ap, _, own in self.dest_side if not own]))
        out: List[Tuple[Any, Any, str]] = []
        for date in self.dates:
            nxt = _next_day(date)
            if self.arrive_by is not None and self.arrive_by.date() < datetime.strptime(nxt, DATE_FMT).date():
                continue
            landed = {flight.to for (day, _frm), flights in self.flight_index.items() if day == date
                      for flight in flights}
            for code in connect:
                if code in landed and code not in dest_codes:
                    out.append(((code, code), dest_req, nxt))
            # Overnight ground into a far origin candidate: its flights leave the next morning
            for ap, _km in self.origin_far:
                out.append(((ap.iata, ap.iata), dest_req, nxt))
        return out

    def plan_auto(self) -> None:
        """Plan the searches for the automatic (non fixed-stop) mode. Policy: see the module docstring, step 2."""
        origin, dest = self.places[0], self.places[1]
        o_ends, d_ends = self.endpoints(origin), self.endpoints(dest)
        direct = [(a, b) for a in o_ends for b in d_ends]
        categories: List[Tuple[str, Optional[float], List[Tuple[Any, Any, str]]]] = [
            ("direct", None, self.round_robin(direct)),
        ]
        if self.max_stops >= 1:
            lcc_dest = self.side_pairs(lambda a, oa, b, ob: oa and not ob and b.iata in LCC_HUBS)
            lcc_origin = self.side_pairs(lambda a, oa, b, ob: ob and not oa and a.iata in LCC_HUBS)

            def rest(a: Airport, oa: bool, b: Airport, ob: bool) -> bool:
                if oa and ob:
                    return False
                if not oa and not ob:
                    return self.max_stops >= 2
                if oa:
                    return b.iata not in LCC_HUBS
                return a.iata not in LCC_HUBS

            side_rest = self.side_pairs(rest)
            hub_pairs: List[Tuple[Any, Any]] = []
            for hub in self.hubs:
                hub_req = (hub, hub)
                hub_pairs.append((o_ends[0], hub_req))
                hub_pairs.append((hub_req, d_ends[0]))
            if self.max_stops >= 2:
                first_o = next((ap for ap, _, own in self.origin_side if not own), None)
                first_d = next((ap for ap, _, own in self.dest_side if not own), None)
                for hub in self.hubs:
                    if first_o is not None:
                        hub_pairs.append(((first_o.iata, first_o.iata), (hub, hub)))
                    if first_d is not None:
                        hub_pairs.append(((hub, hub), (first_d.iata, first_d.iata)))
            if self.max_stops >= 3:
                top = self.hubs[:3]
                hub_pairs += [((h1, h1), (h2, h2)) for h1 in top for h2 in top if h1 != h2]
            # Extra near airports (no flight volume gate): only with budget left after everything else.
            extra_pairs = self.side_pairs(
                lambda a, oa, b, ob: not (oa and ob) and (oa or ob or self.max_stops >= 2),
                origin_items=self.origin_side + self.origin_extra,
                dest_items=self.dest_side + self.dest_extra)
            far_pairs = self.far_pairs()
            categories += [
                ("lcc_dest", BUDGET_SHARE_LCC_DEST, self.round_robin(lcc_dest)),
                ("lcc_origin", BUDGET_SHARE_LCC_ORIGIN, self.round_robin(lcc_origin)),
                ("side_pairs", BUDGET_SHARE_SIDE_PAIRS, self.round_robin(side_rest)),
                ("far_side", BUDGET_SHARE_FAR_SIDE, self.round_robin(far_pairs)),
                ("hub_chains", None, self.round_robin(hub_pairs)),
                ("side_extra", None, self.round_robin(extra_pairs)),
            ]
        self.run_plan(categories, next_day=self.max_stops >= 1)

    def ranked_ends(self, place: _Place, centers: List[Tuple[float, float]],
                    limit: int) -> List[Tuple[str, Optional[str]]]:
        """Airports of a multi-airport place to search, best first: mainland before islands (a small budget
        must reach the mainland), then LCC bases, large airports, and the nearest to the neighbouring
        waypoints. Each airport is searched by its own code, so the arrival/departure airport is always known."""
        def key(ap: Airport) -> Tuple[Any, ...]:
            near_others = sum(haversine_km(c[0], c[1], ap.lat, ap.lon) for c in centers)
            return (self.is_island_code(ap.iata), ap.size != "large", ap.iata not in LCC_HUBS, near_others, ap.iata)

        return [(ap.iata, ap.iata) for ap in sorted(place.airports, key=key)[:limit]]

    def via_ends(self, idx: int) -> List[Tuple[str, Optional[str]]]:
        """Search endpoints of fixed waypoint ``idx`` (0 = origin, last = destination)."""
        places = self.places
        place = places[idx]
        if len(place.airports) <= 1 and place.kind != "country":
            return [self.request(place)]
        centers = [places[k].center for k in (idx - 1, idx + 1)
                   if 0 <= k < self.via_count and places[k].center is not None]
        limit = VIA_COUNTRY_ENDS if place.kind == "country" else VIA_CITY_ENDS
        return self.ranked_ends(place, centers, limit)

    def plan_via(self) -> None:
        places = self.places
        o_req = self.request(places[0])
        d_req = self.request(places[self.last])
        ends: List[List[Tuple[str, Optional[str]]]] = [self.via_ends(idx) for idx in range(self.via_count)]
        # Airport codes searched per place (a city request is searched by its single airport's code)
        self.via_tried = [[fallback or place.codes[0] for _, fallback in group] if place.codes else []
                          for place, group in zip(places[:self.via_count], ends)]
        segment_tier = []
        for i in range(self.via_count - 1):
            for a in ends[i]:
                for b in ends[i + 1]:
                    segment_tier.append((a, b))
        # Segments after a waypoint also run on later days (a stay or an overnight ground leg moves the
        # traveller on): searched after every depart-date segment, so they never take its budget.
        rolled: List[Tuple[Any, Any, str]] = []
        for i in range(1, self.via_count - 1):
            pairs = [(a, b) for a in ends[i] for b in ends[i + 1]]
            rolled += self.round_robin(pairs, self.roll_dates(i))
        via_side_pairs: List[Tuple[Tuple[str, Optional[str]], Tuple[str, Optional[str]]]] = []
        for i in range(self.via_count - 1):
            exit_ends = [self.request(self.places[s]) for s in range(self.via_count, len(places))
                         if self.side_of.get(s) == i and self.side_role.get(s) == "out"]
            entry_ends = [self.request(self.places[s]) for s in range(self.via_count, len(places))
                          if self.side_of.get(s) == i + 1 and self.side_role.get(s) == "in"]
            all_a = exit_ends + ends[i]
            all_b = entry_ends + ends[i + 1]
            exit_set = set(exit_ends)
            entry_set = set(entry_ends)
            for a in all_a:
                for b in all_b:
                    if a in exit_set or b in entry_set:
                        if (a, b) not in via_side_pairs:
                            via_side_pairs.append((a, b))
        categories: List[Tuple[str, Optional[float], List[Tuple[Any, Any, str]]]] = [
            ("direct", None, self.round_robin([(o_req, d_req)])),
            ("segments", None, self.round_robin(segment_tier)),
            ("rolled", None, rolled),
            ("via_side", None, self.round_robin(via_side_pairs)),
        ]
        self.run_plan(categories, next_day=False)
        departing_codes = {dep_code for (_, dep_code), flist in self.flight_index.items() if flist}
        for i in range(self.via_count):
            if i in self.edges:
                self.edges[i] = [
                    (dest_idx, km) for dest_idx, km in self.edges[i]
                    if not (dest_idx in self.side_of and self.side_role.get(dest_idx) == "out")
                    or (set(self.places[dest_idx].codes) & departing_codes)
                ]

    def stay_before(self, idx: int) -> bool:
        """True when a stop at waypoint 1..idx has a minimum stay."""
        return any(self.stay_at_place(k) > 0 for k in range(1, idx + 1))

    def roll_dates(self, idx: int) -> List[str]:
        """Later dates (after the depart dates, within arrive_by) that the segment starting at waypoint ``idx``
        is searched on: the next day, and the day after when a stay before it can push the traveller on."""
        extra = MAX_ROLL_DAYS if self.stay_before(idx) else 1
        known = set(self.dates)
        out: List[str] = []
        for date in self.dates:
            day = datetime.strptime(date, DATE_FMT)
            for k in range(1, extra + 1):
                cand = (day + timedelta(days=k)).strftime(DATE_FMT)
                if cand in known or cand in out:
                    continue
                if self.arrive_by is not None and self.arrive_by.date() < datetime.strptime(cand, DATE_FMT).date():
                    continue
                out.append(cand)
        if self.via_on_date:
            for k in range(1, idx + 1):
                if k in self.via_on_date:
                    vod = self.via_on_date[k]
                    cand = vod.strftime(DATE_FMT)
                    if cand not in known and cand not in out:
                        if self.arrive_by is None or self.arrive_by.date() >= vod:
                            out.append(cand)
        return sorted(out)

    # ------------------------------------------------------------ transfers, stays, ground

    def transfer(self, code: str, at: Optional[datetime] = None) -> Dict[str, Any]:
        """Airport <-> city transfer for ``code`` starting at ``at`` (the night rule depends on the time)."""
        if at is not None:
            cached = self._transfer_at_cache.get((code, at))
            if cached is not None:
                return cached
        hhmm = _hm_text(_to_local(at, self.airport_tz(code))) if at is not None else None
        key = (code, hhmm)
        if key not in self.transfer_cache:
            try:
                kwargs = {"at_time": hhmm} if hhmm is not None else {}
                info = dict(self.costs.airport_transfer(code, **kwargs))
            except Exception as exc:
                logger.warning("airport_transfer(%s) failed: %s", code, exc)
                self.warnings.append(f"{code} havalimanı transfer bilgisi alınamadı; tahmini değer kullanıldı")
                info = {"duration_mins": 60, "price_try": 0.0, "mode": "unknown", "label": TRANSFER_FALLBACK_LABEL,
                        "is_estimated": True, "note": "tahmini"}
            self.transfer_cache[key] = info
        res = self.transfer_cache[key]
        if at is not None:
            self._transfer_at_cache[(code, at)] = res
        return res

    def stay_at_place(self, idx: int) -> int:
        if idx == 0 or idx == self.last:
            return 0
        val = self._stay_cache.get(idx)
        if val is not None:
            return val
        val = max((self.min_stay_codes.get(code, 0) for code in self.places[idx].codes), default=0)
        self._stay_cache[idx] = val
        return val

    def stay_at_code(self, code: str) -> int:
        return self.min_stay_codes.get(code, 0)

    @staticmethod
    def add_stay(state: _State, label: str, stay: int) -> Tuple[Dict[str, Any], ...]:
        """The stays of a chain that leaves ``label`` now: every stop with a minimum stay is recorded."""
        if stay <= 0 or state.time is None:
            return state.stays
        return state.stays + ({"place": label, "mins": int(stay)},)

    @staticmethod
    def lookup_days(state: _State, date: str, zone: str) -> List[str]:
        """Flight dates a chain can take from where it stands (airport zone ``zone``): the start date, or (after
        arriving) the local day it stands on and the next one. Feasibility is checked on the times, so a flight
        on the next day is taken only when it leaves late enough."""
        if state.time is None:
            return [date]
        day = _to_local(state.time, zone).strftime(DATE_FMT)
        return [day, _next_day(day)]

    def ground_segments(self, src: _Place, dst: _Place, date_str: str, earliest_hm: Optional[str]) -> List[Any]:
        key = (src.ground_name, dst.ground_name, date_str, earliest_hm)
        if key in self.ground_cache:
            self.count("ground_query_cache_hits")
            return self.ground_cache[key]
        if self.ground is None:
            return []
        self.count("ground_queries")
        kwargs: Dict[str, Any] = {}
        if self.real_data_only and _supports_kwarg(self.ground.find_ground, "allow_estimate"):
            kwargs["allow_estimate"] = False  # the distance-estimate fallback is not run at all
        if self.maps_budget is not None and _supports_kwarg(self.ground.find_ground, "maps_budget"):
            kwargs["maps_budget"] = self.maps_budget  # one Google Maps page-load budget for the whole search
        try:
            with self.phase("ground_fetch"):
                segments: List[Any] = []
                live = getattr(self.ground, "find_ground_live", None)
                if callable(live):
                    # The priced live sources only: their whole-day answer is shared by every earliest time of this
                    # pair and day, so a repeated variant costs no source call. Only when they give nothing is
                    # find_ground asked (Maps, table, estimate), which reuses the same shared answer.
                    segments = list(live(
                        from_place=src.ground_name, to_place=dst.ground_name, date_str=date_str,
                        earliest_dep_time=earliest_hm, from_coord=src.center, to_coord=dst.center,
                        max_results=GROUND_MAX_RESULTS) or [])
                if not segments:
                    segments = self.ground.find_ground(
                        from_place=src.ground_name, to_place=dst.ground_name, date_str=date_str,
                        earliest_dep_time=earliest_hm, from_coord=src.center, to_coord=dst.center,
                        max_results=GROUND_MAX_RESULTS, max_road_km=VIA_ROAD_LIMIT_KM, **kwargs) or []
        except Exception as exc:
            logger.warning("find_ground %s -> %s failed: %s", src.ground_name, dst.ground_name, exc)
            self.warnings.append(f"Kara yolu araması başarısız: {src.ground_name} ➔ {dst.ground_name}")
            segments = []
        kept = list(segments)
        if self.real_data_only:
            # Providers that cannot skip their estimate still get it dropped here (same result)
            real = [seg for seg in kept if _real_segment(seg)]
            self.real_filtered += len(kept) - len(real)
            kept = real
        self.ground_cache[key] = kept
        return self.ground_cache[key]

    def cost_options(self) -> Dict[str, Any]:
        """Extra keyword arguments for costs.cost_breakdown: only when real_data_only (older cost objects keep
        their exact signature)."""
        return {"real_data_only": True} if self.real_data_only else {}

    def get_hotel_price(self, place: str, date_str: str) -> Optional[float]:
        provider = getattr(self.engine, "hotel_provider", None)
        if provider is None:
            return None
        city = place
        if hasattr(self.db, "get"):
            airport = self.db.get(place)
            if airport is None and hasattr(self.db, "resolve"):
                resolved = self.db.resolve(place)
                airport = resolved[0] if resolved else None
            if airport is not None and getattr(airport, "city", None):
                city = airport.city
        key = (city.casefold(), date_str)
        if key in self.hotel_cache:
            return self.hotel_cache[key]
        try:
            info = provider.cheapest_night(city, date_str, budget=self.hotels_budget)
            price = float(info["price_try"]) if info and "price_try" in info else None
        except Exception as exc:
            logger.warning("hotel provider lookup failed for %s on %s: %s", city, date_str, exc)
            price = None
        self.hotel_cache[key] = price
        return price

    def _rank_pools(self) -> Dict[Optional[int], float]:
        """Median known ground price per travel-time band (key: band index) and over all bands (key None),
        from the ground segments queried so far. Ranking only. Memoised on the size of ground_cache."""
        size = len(self.ground_cache)
        if self.rank_memo.get("size") == size:
            return self.rank_memo["pools"]
        by_band: Dict[int, List[float]] = {}
        everything: List[float] = []
        for segments in self.ground_cache.values():
            for seg in segments:
                price = _segment_price(seg)
                if price is None:
                    continue
                everything.append(price)
                try:
                    band = int(seg.duration_mins) // RANK_BAND_MINS
                except (TypeError, ValueError):
                    continue
                by_band.setdefault(band, []).append(price)
        pools: Dict[Optional[int], float] = {band: float(statistics.median(vals)) for band, vals in by_band.items()}
        pools[None] = float(statistics.median(everything)) if everything else UNKNOWN_RANK_TRY
        self.rank_memo = {"size": size, "pools": pools}
        return pools

    def rank_penalty(self, leg: "_Leg") -> float:
        """Ranking-only value of a leg with an unknown price. It is never shown and never summed into a total.

        price_from_try when the source gives one; else the median known ground price of the leg's travel-time
        band (see _rank_pools); else UNKNOWN_RANK_TRY. Transfers add 0: no real transfer price exists to
        compare with. A known price adds 0.
        """
        if leg.price is not None or leg.kind == "transfer":
            return 0.0
        if leg.price_from is not None:
            return float(leg.price_from)
        minutes = int((leg.arr - leg.dep).total_seconds() // 60)
        pools = self._rank_pools()
        return pools.get(minutes // RANK_BAND_MINS, pools[None])

    def transfer_price(self, info: Dict[str, Any]) -> Optional[float]:
        """A transfer leg's price: None (unknown) under real_data_only when the price is an estimate."""
        if self.real_data_only and _transfer_price_is_estimated(info):
            return None
        return float(info.get("price_try") or 0.0)

    # ------------------------------------------------------------ chain search (DFS)

    def can_move(self, state: _State) -> bool:
        return state.mains < self.max_mains

    def arrivals(self, flight: _Flight, wp: int) -> List[Tuple[Tuple[Any, ...], Optional[int], int, bool, Tuple[str, ...]]]:
        """(node, transfer_to_place_index or None, new_wp, done, covers) for a flight landing."""
        out = []
        if not self.via:
            for idx in self.target_by_code.get(flight.to, []):
                place = self.places[idx]
                done = idx == 1
                if place.transfer_city:
                    out.append((("p", idx), idx, 0, done, ()))
                else:
                    out.append((("a", flight.to), None, 0, done, ()))
            if flight.to in self.hubs:
                out.append((("a", flight.to), None, 0, False, ()))
            return out
        covers: List[str] = []
        for j in range(wp + 1, self.via_count):
            if j > wp + 1:
                middle = self.places[j - 1]
                hit = next((code for code in flight.layovers if code in middle.codes), None)
                if hit is None:
                    break
                covers.append(hit)
            place = self.places[j]
            if flight.to in place.codes:
                done = j == self.last
                if place.transfer_city:
                    out.append((("p", j), j, j, done, tuple(covers)))
                    # Passing through the airport with no stay: the chain may go on from this airport directly
                    # (airport -> airport, separate-ticket gap) instead of going out to the city and back.
                    if not done and self.stay_at_place(j) == 0:
                        out.append((("a", flight.to), None, j, False, tuple(covers)))
                else:
                    out.append((("a", flight.to), None, j, done, tuple(covers)))
        if wp + 1 < self.via_count:
            next_j = wp + 1
            for s in self.target_by_code.get(flight.to, []):
                if s in self.side_of and self.side_role.get(s) == "in" and self.side_of[s] == next_j:
                    out.append((("p", s), s, next_j - 1, False, ()))
        return out

    def take_flight(self, state: _State, legs_before: Tuple[_Leg, ...], flight: _Flight,
                    stays: Tuple[Dict[str, Any], ...], out: List[_State]) -> None:
        arrivals = self.arrivals(flight, state.wp)
        if not arrivals:
            self.reject("not_target", flight)
            return
        for node, transfer_to, new_wp, done, covers in arrivals:
            flight_leg = _Leg("flight", flight.operator, flight.frm, flight.to, flight.dep, flight.arr,
                              flight.price, False, flight.url, True, covers,
                              self.airport_label(flight.frm), self.airport_label(flight.to),
                              source="google_flights", price_is_estimated=False, stops=flight.stops,
                              dep_tz=flight.dep_tz, arr_tz=flight.arr_tz,
                              observed_at=flight.observed_at,
                              layovers=flight.layovers, is_direct=flight.is_direct)
            if transfer_to is None:
                arrived = flight.arr
                legs = legs_before + (flight_leg,)
                if new_wp in self.via_on_date:
                    local_arr = _to_local(arrived, flight.arr_tz)
                    if local_arr.date() > self.via_on_date[new_wp]:
                        continue
            else:
                transfer = self.transfer(flight.to, flight.arr)
                minutes = int(transfer["duration_mins"])
                price = self.transfer_price(transfer)
                tr_leg = _Leg("transfer", _transfer_label(transfer),
                              flight.to, self.places[transfer_to].ground_name,
                              flight.arr, flight.arr + timedelta(minutes=minutes),
                              price, bool(transfer.get("is_estimated", True)), "", False,
                              origin_station_name=self.airport_label(flight.to),
                              dest_station_name=self.places[transfer_to].label,
                              source="estimate",
                              price_is_estimated=price is not None and _transfer_price_is_estimated(transfer),
                              dep_tz=flight.arr_tz, arr_tz=self.places[transfer_to].tz)
                arrived = flight.arr + timedelta(minutes=minutes)
                legs = legs_before + (flight_leg, tr_leg)
                if new_wp in self.via_on_date:
                    local_arr = _to_local(arrived, self.places[transfer_to].tz)
                    if local_arr.date() > self.via_on_date[new_wp]:
                        continue
            if self.arrival_limit_utc is not None and arrived > self.arrival_limit_utc:
                self.reject("arrive_by", flight)
                continue
            out.append(_State(node, arrived, legs, state.mains + 1, new_wp, done, stays))

    def direct_connection(self, state: _State, idx: int, code: str) -> bool:
        """True when a chain that just landed at airport ``code`` and is now in the city of waypoint ``idx``
        (reached by the airport -> city transfer) would fly out of ``code`` again. Without a stay at an
        intermediate waypoint, arrive_airport -> depart_airport is connected directly (see arrivals), so
        the city round trip is skipped."""
        if idx == 0 or idx == self.last or not state.legs:
            return False
        last = state.legs[-1]
        if last.kind != "transfer" or last.frm != code:
            return False
        return self.stay_at_place(idx) <= 0

    def expand_place(self, state: _State, date: str, out: List[_State]) -> None:
        idx = state.node[1]
        place = self.places[idx]
        effective_wp = self.side_of[idx] if (self.via and idx in self.side_of) else idx
        stay = 0 if (self.via and idx in self.side_of) else self.stay_at_place(effective_wp)
        if self.can_move(state):
            for airport in place.airports:
                tz = self.airport_tz(airport.iata)
                days = self.lookup_days(state, date, tz)
                valid_days = set(days)
                if state.time is None:
                    key = (airport.iata, tuple(sorted(self.dates)))
                    if key not in self._date_checked:
                        self._date_checked.add(key)
                        for (f_day, f_code), flights in self.flight_index.items():
                            if f_code == airport.iata and f_day not in self.dates:
                                for f in flights:
                                    self.reject("date", f)
                else:
                    key = (airport.iata, tuple(sorted(valid_days)))
                    if key not in self._date_checked:
                        self._date_checked.add(key)
                        for (f_day, f_code), flights in self.flight_index.items():
                            if f_code == airport.iata and f_day not in valid_days:
                                for f in flights:
                                    self.reject("date", f)
                for day in days:
                    for flight in self.flight_index.get((day, airport.iata), []):
                        if effective_wp in self.via_on_date:
                            if _to_local(flight.dep, flight.dep_tz).date() < self.via_on_date[effective_wp]:
                                continue
                        if place.transfer_city:
                            if state.time is None:
                                # Start from the daytime estimate, then price the transfer at that start time
                                base_minutes = int(self.transfer(airport.iata)["duration_mins"])
                                start = flight.dep - timedelta(minutes=base_minutes + AIRPORT_BUFFER_MINS)
                                if self.depart_after_utc is not None and start < self.depart_after_utc:
                                    self.reject("depart_after", flight)
                                    continue
                                stays = state.stays
                            else:
                                if self.direct_connection(state, idx, airport.iata):
                                    continue
                                start = state.time + timedelta(minutes=stay)
                                stays = self.add_stay(state, self.places[effective_wp].label, stay)
                            transfer = self.transfer(airport.iata, start)
                            minutes = int(transfer["duration_mins"])
                            if state.time is not None and flight.dep < start + timedelta(minutes=minutes + AIRPORT_BUFFER_MINS):
                                self.reject("buffer", flight)
                                continue
                            price = self.transfer_price(transfer)
                            tr_leg = _Leg("transfer", _transfer_label(transfer),
                                          place.ground_name, airport.iata, start,
                                          start + timedelta(minutes=minutes),
                                          price, bool(transfer.get("is_estimated", True)), "", False,
                                          origin_station_name=place.label,
                                          dest_station_name=self.airport_label(airport.iata),
                                          source="estimate",
                                          price_is_estimated=price is not None
                                          and _transfer_price_is_estimated(transfer),
                                          dep_tz=place.tz, arr_tz=self.airport_tz(airport.iata))
                            self.take_flight(state, state.legs + (tr_leg,), flight, stays, out)
                        else:
                            stays = state.stays
                            if state.time is not None:
                                earliest = state.time + timedelta(minutes=max(SEPARATE_TICKET_GAP_MINS, stay))
                                if flight.dep < earliest:
                                    self.reject("buffer", flight)
                                    continue
                                stays = self.add_stay(state, self.places[effective_wp].label, stay)
                            else:
                                if self.depart_after_utc is not None and flight.dep < self.depart_after_utc:
                                    self.reject("depart_after", flight)
                                    continue
                            self.take_flight(state, state.legs, flight, stays, out)
        if self.ground is not None and self.can_move(state):
            for dest_idx, _km in self.edges.get(idx, []):
                self.take_ground(state, idx, dest_idx, date, out)

    def _append_ground_states(
        self,
        state: _State,
        raw_segs: List[Any],
        src: _Place,
        dst: _Place,
        query_date: str,
        not_before: Optional[datetime],
        dest_idx: int,
        dep_wp_idx: int,
        stay_label: str,
        stay_mins: int,
        prefix_legs: Tuple[_Leg, ...],
        seen: set,
        out: List[_State],
    ) -> int:
        if not raw_segs:
            return 0
        base = datetime(int(query_date[:4]), int(query_date[5:7]), int(query_date[8:10]))
        times: List[int] = sorted(set(
            hh[0] * 60 + hh[1]
            for hh in (_hhmm(seg.departure_time) for seg in raw_segs)
            if hh is not None
        ))
        added = 0
        for seg in raw_segs:
            hhmm = _hhmm(seg.departure_time)
            if hhmm is None:
                continue
            dep = _place_at_or_after(base, hhmm, src.tz, not_before)
            if (dep, seg.operator, seg.price_try) in seen:
                continue
            seen.add((dep, seg.operator, seg.price_try))
            if not_before is None and self.depart_after_utc is not None and dep < self.depart_after_utc:
                continue
            arr = _ground_arrival(seg, dep, src.tz, dst.tz)
            if arr is None:
                continue
            if dep_wp_idx in self.via_on_date:
                if _to_local(dep, src.tz).date() < self.via_on_date[dep_wp_idx]:
                    continue
            if dest_idx in self.via_on_date:
                if _to_local(arr, dst.tz).date() > self.via_on_date[dest_idx]:
                    continue
            if self.arrival_limit_utc is not None and arr > self.arrival_limit_utc:
                continue

            cur_m = hhmm[0] * 60 + hhmm[1]
            later = [m for m in times if m > cur_m]
            if later:
                next_m = later[0]
                diff_mins = next_m - cur_m
                next_dep = dep + timedelta(minutes=diff_mins)
                is_last_service = False
            else:
                next_dep = None
                is_last_service = bool(len(raw_segs) < GROUND_MAX_RESULTS)

            price = _segment_price(seg)
            leg = _Leg(
                str(seg.mode or "train"),
                str(seg.operator or ""),
                str(seg.origin_station),
                str(seg.dest_station),
                dep,
                arr,
                price,
                bool(seg.is_estimated or seg.price_is_estimated),
                get_operator_booking_url(str(seg.operator or "")),
                True,
                (),
                _station_name(getattr(seg, "origin_station_name", None)),
                _station_name(getattr(seg, "dest_station_name", None)),
                source=seg.source or None,
                price_is_estimated=bool(seg.price_is_estimated) and price is not None,
                dep_tz=src.tz,
                arr_tz=dst.tz,
                price_from=_segment_price_from(seg),
                is_last_service=is_last_service,
                next_dep=next_dep,
                vehicle_types=tuple(getattr(seg, "vehicle_types", None) or ()),
            )
            stays = self.add_stay(state, stay_label, stay_mins)
            if self.via:
                if dest_idx in self.side_of and self.side_role.get(dest_idx) == "out":
                    new_wp = self.side_of[dest_idx]
                else:
                    new_wp = dest_idx
            else:
                new_wp = 0
            out.append(_State(
                ("p", dest_idx),
                arr,
                state.legs + prefix_legs + (leg,),
                state.mains + 1,
                new_wp,
                dest_idx == self.last,
                stays,
            ))
            added += 1
        return added
    def take_ground(self, state: _State, idx: int, dest_idx: int, date: str, out: List[_State]) -> None:
        src, dst = self.places[idx], self.places[dest_idx]
        stay = self.stay_at_place(idx)
        # Clock times and dates of the query are local to the departure place
        if state.time is None:
            if self.depart_after is not None:
                day = datetime.strptime(date, DATE_FMT).date()
                if self.depart_after.date() > day:
                    return
                earliest_hm = _hm_text(self.depart_after) if self.depart_after.date() == day else None
            else:
                earliest_hm = None
            not_before = None
            local_before = None
            date_str = date
        else:
            not_before = state.time + timedelta(minutes=max(CITY_STOP_MINS, stay))
            local_before = _to_local(not_before, src.tz)
            earliest_hm = _hm_text(local_before)
            date_str = local_before.strftime(DATE_FMT)
        # Queries: the start day from the earliest time, plus (after a wait) the next day from midnight,
        # so that an overnight wait can take the next morning's service.
        queries: List[Tuple[str, Optional[str]]] = [(date_str, earliest_hm)]
        is_side_transfer = (idx in self.side_of or dest_idx in self.side_of) if self.via else False
        if local_before is not None and not is_side_transfer:
            next_day = (local_before.date() + timedelta(days=1)).strftime(DATE_FMT)
            queries.append((next_day, None))
        elif earliest_hm is None or earliest_hm < OVERNIGHT_FROM_HM:
            # Evening departures (overnight legs that arrive next morning) even when the day has many earlier ones
            queries.append((date_str, OVERNIGHT_FROM_HM))
        seen = set()
        for query_date, query_hm in queries:
            raw_segs = self.ground_segments(src, dst, query_date, query_hm)
            self._append_ground_states(
                state, raw_segs, src, dst, query_date, not_before,
                dest_idx=dest_idx, dep_wp_idx=idx, stay_label=src.label, stay_mins=stay,
                prefix_legs=(), seen=seen, out=out,
            )

    def expand_airport(self, state: _State, date: str, out: List[_State]) -> None:
        code = state.node[1]
        if not self.can_move(state):
            return
        stay = self.stay_at_code(code)
        earliest = state.time + timedelta(minutes=max(SEPARATE_TICKET_GAP_MINS, stay))
        # A separate ticket may continue the next day; such flights exist only where next_day_queue or the
        # rolled searches of fixed mode found them (lookup_days: the day the chain stands on, and the next).
        tz = self.airport_tz(code)
        days = self.lookup_days(state, date, tz)
        valid_days = set(days)
        key = (code, tuple(sorted(valid_days)))
        if key not in self._date_checked:
            self._date_checked.add(key)
            for (f_day, f_code), flights in self.flight_index.items():
                if f_code == code and f_day not in valid_days:
                    for f in flights:
                        self.reject("date", f)
        for day in days:
            for flight in self.flight_index.get((day, code), []):
                if flight.dep < earliest:
                    self.reject("buffer", flight)
                    continue
                if state.wp in self.via_on_date:
                    if _to_local(flight.dep, flight.dep_tz).date() < self.via_on_date[state.wp]:
                        continue
                stays = self.add_stay(state, code, stay)
                self.take_flight(state, state.legs, flight, stays, out)
        if self.via and self.ground is not None and self.can_move(state):
            if 0 < state.wp < self.last and self.places[state.wp].kind == "country":
                next_idx = state.wp + 1
                next_place = self.places[next_idx]
                if next_place.transfer_city:
                    self.take_country_ground(state, code, next_idx, date, out)

    def take_country_ground(self, state: _State, code: str, dest_idx: int, date: str, out: List[_State]) -> None:
        dst = self.places[dest_idx]
        ap = None
        if callable(getattr(self.db, "get", None)):
            ap = self.db.get(code)
        if ap is None and callable(getattr(self.db, "resolve", None)):
            resolved = self.db.resolve(code)
            ap = resolved[0] if resolved else None
        if ap is None or not getattr(ap, "city", None):
            return

        city_name = ap.city
        center = self.db.city_center(city_name) if callable(getattr(self.db, "city_center", None)) else None
        if center is None and hasattr(ap, "lat") and hasattr(ap, "lon"):
            center = (ap.lat, ap.lon)
        tz = self.db.timezone(city_name) if callable(getattr(self.db, "timezone", None)) else None
        if not tz:
            tz = self.airport_tz(code)

        src = _Place(
            label=city_name,
            kind="city",
            airports=(ap,),
            search_name=city_name,
            ground_name=city_name,
            center=center,
            tz=tz,
        )

        km_key = (code, dest_idx)
        if km_key in self._country_ground_km:
            km = self._country_ground_km[km_key]
        else:
            km = self.ground_km(src, dst)
            self._country_ground_km[km_key] = km

        if km is None or km > VIA_ROAD_LIMIT_KM:
            return

        transfer = self.transfer(code, state.time)
        tr_mins = int(transfer.get("duration_mins", 60))
        tr_price = self.transfer_price(transfer)
        tr_dep = state.time
        tr_arr = tr_dep + timedelta(minutes=tr_mins)
        tr_leg = _Leg(
            "transfer",
            _transfer_label(transfer),
            code,
            src.ground_name,
            tr_dep,
            tr_arr,
            tr_price,
            bool(transfer.get("is_estimated", True)),
            "",
            False,
            origin_station_name=self.airport_label(code),
            dest_station_name=src.label,
            source="estimate",
            price_is_estimated=tr_price is not None and _transfer_price_is_estimated(transfer),
            dep_tz=self.airport_tz(code),
            arr_tz=src.tz,
        )

        stay = max(self.stay_at_code(code), self.stay_at_place(state.wp))
        not_before = tr_arr + timedelta(minutes=max(CITY_STOP_MINS, stay))
        local_before = _to_local(not_before, src.tz)
        earliest_hm = _hm_text(local_before)
        date_str = local_before.strftime(DATE_FMT)

        seen = set()
        raw_segs = self.ground_segments(src, dst, date_str, earliest_hm)
        added = self._append_ground_states(
            state, raw_segs, src, dst, date_str, not_before,
            dest_idx=dest_idx, dep_wp_idx=state.wp, stay_label=self.places[state.wp].label, stay_mins=stay,
            prefix_legs=(tr_leg,), seen=seen, out=out,
        )
        if added == 0:
            next_day = (local_before.date() + timedelta(days=1)).strftime(DATE_FMT)
            raw_segs_next = self.ground_segments(src, dst, next_day, None)
            self._append_ground_states(
                state, raw_segs_next, src, dst, next_day, not_before,
                dest_idx=dest_idx, dep_wp_idx=state.wp, stay_label=self.places[state.wp].label, stay_mins=stay,
                prefix_legs=(tr_leg,), seen=seen, out=out,
            )

    def dfs(self, date: str) -> List[_State]:
        # Best first: the key is a lower bound of the final score (hotel, bags, night and risk penalties are
        # added later and are all >= 0), so when a chain limit cuts the search the chains seen are the best ones.
        start = _State(("p", 0), None, (), 0, 0, False, ())
        seq = 0
        queue: List[Tuple[float, int, float, _State]] = [(0.0, seq, 0.0, start)]
        complete: List[_State] = []
        late: List[_State] = []
        steps = 0
        while queue:
            _key, _, parent_cost, state = heapq.heappop(queue)
            steps += 1
            if steps > MAX_DFS_STATES:
                logger.warning("Route search state limit reached on %s", date)
                self.truncated = True
                break
            if state.done:
                arr = state.legs[-1].arr if state.legs else state.time
                if self.arrive_by_utc is not None and arr is not None and arr > self.arrive_by_utc:
                    if len(late) < NEAR_MISS_CHAIN_CAP:
                        late.append(state)
                else:
                    complete.append(state)
                    if len(complete) >= MAX_COMPLETE_CHAINS:
                        self.truncated = True
                        break
                continue
            children: List[_State] = []
            if state.node[0] == "p":
                self.expand_place(state, date, children)
            else:
                self.expand_airport(state, date, children)
            for child in children:
                new_legs = child.legs[len(state.legs):]
                added = sum(leg.price if leg.price is not None else self.rank_penalty(leg) for leg in new_legs)
                child_cost = parent_cost + added
                if child.legs:
                    end = child.time if child.time is not None else child.legs[-1].arr
                    key = child_cost + ((end - child.legs[0].dep).total_seconds() / 3600.0) * self.time_value
                else:
                    key = child_cost
                seq += 1
                heapq.heappush(queue, (key, seq, child_cost, child))
        self.count("dfs_steps", steps)
        self.count("complete_chains", len(complete))
        return complete + late


def _provider_stats(provider: Any) -> Dict[str, Any]:
    getter = getattr(provider, "stats", None)
    if callable(getter):
        try:
            res = getter()
            if isinstance(res, dict):
                return res
        except Exception:
            return {}
    return {}

def _finish_trace(run: "_Run") -> Dict[str, Any]:
    """The trace of a finished search: phase times plus the run's totals (see SearchTrace).

    Provider counters reflect the difference between end and start of search across process-wide
    and instance-level provider stats. Concurrent searches may see overlapping counts.
    """
    trace = run.trace if run.trace is not None else SearchTrace()
    trace.set("flight_searches", run.used)
    trace.set("pruned_searches", len(run.pruned))
    trace.set("budget_exhausted", bool(run.exhausted))
    trace.set("truncated", bool(run.truncated))
    trace.set("maps_page_loads", run.maps_budget.used if run.maps_budget is not None else 0)
    trace.set("hotel_page_loads", int(getattr(run.hotels_budget, "used", 0) or 0))

    flight_start = run.flight_stats_start or {}
    flight_end = _provider_stats(run.engine.flight_provider) if run.engine is not None else {}
    ground_start = run.ground_stats_start or {}
    ground_end = _provider_stats(run.ground)

    google_page_loads = max(0, int(flight_end.get("page_loads", 0) or 0) - int(flight_start.get("page_loads", 0) or 0))
    google_page_secs = max(0.0, round(float(flight_end.get("page_secs", 0.0) or 0.0) - float(flight_start.get("page_secs", 0.0) or 0.0), 3))
    l1_diff = max(0, int(flight_end.get("l1_hits", 0) or 0) - int(flight_start.get("l1_hits", 0) or 0))
    l2_diff = max(0, int(flight_end.get("l2_hits", 0) or 0) - int(flight_start.get("l2_hits", 0) or 0))
    google_cache_hits = l1_diff + l2_diff
    google_blocked = max(0, int(flight_end.get("blocked", 0) or 0) - int(flight_start.get("blocked", 0) or 0))

    src_start = ground_start.get("source_calls") or {}
    src_end = ground_end.get("source_calls") or {}
    all_sources = set(src_start.keys()) | set(src_end.keys())
    ground_source_calls = sum(max(0, int(src_end.get(s, 0) or 0) - int(src_start.get(s, 0) or 0)) for s in all_sources)

    ground_cache_hits = max(0, int(ground_end.get("cache_hits", 0) or 0) - int(ground_start.get("cache_hits", 0) or 0))
    maps_searches = max(0, int(ground_end.get("maps_searches", 0) or 0) - int(ground_start.get("maps_searches", 0) or 0))

    trace.set("google_page_loads", google_page_loads)
    trace.set("google_page_secs", google_page_secs)
    trace.set("google_cache_hits", google_cache_hits)
    trace.set("google_blocked", google_blocked)
    trace.set("ground_source_calls", ground_source_calls)
    trace.set("ground_cache_hits", ground_cache_hits)
    trace.set("maps_searches", maps_searches)

    for name in ("dfs_steps", "complete_chains", "options_built", "best_score_recomputes",
                 "ground_queries", "ground_query_cache_hits"):
        trace.counters.setdefault(name, 0)
    return trace.to_dict()


def _candidates(run: "_Run") -> Dict[str, List[str]]:
    """Airports tried. Auto mode: side candidates and hubs. Fixed mode: the airports searched at the origin,
    the destination, and (as hubs) at each waypoint."""
    if not run.via:
        return {
            "origin_side": [ap.iata for ap, _, _ in run.origin_side],
            "dest_side": [ap.iata for ap, _, _ in run.dest_side],
            "origin_far": [ap.iata for ap, _ in run.origin_far],
            "dest_far": [ap.iata for ap, _ in run.dest_far],
            "hubs": list(run.hubs),
        }
    tried = run.via_tried
    waypoints = [code for group in tried[1:-1] for code in group]
    return {
        "origin_side": list(tried[0]) if tried else [],
        "dest_side": list(tried[-1]) if tried else [],
        "hubs": list(dict.fromkeys(waypoints)),
    }


# ---------------------------------------------------------------- engine

def _violates_schengen(state: _State, db: AirportDB) -> bool:
    """Return True if the chain of legs violates the Schengen single-entry rule."""
    points: List[str] = []
    for i, leg in enumerate(state.legs):
        if i == 0:
            points.append(leg.frm)
        if leg.kind == "flight":
            if not getattr(leg, "is_direct", True) and not getattr(leg, "layovers", ()):
                return True
            for lay in getattr(leg, "layovers", ()):
                points.append(lay)
        points.append(leg.to)

    def _country(point: str) -> str:
        raw = (point or "").strip()
        if len(raw) == 3 and raw.isalpha():
            ap = db.get(raw.upper())
            if ap is not None:
                return ap.country
        airports = db.resolve(raw)
        if airports:
            return airports[0].country
        c = _city_table_country(raw)
        if c:
            return c
        return ""

    # A point whose country is unknown is skipped: it must not count as leaving Schengen
    countries = [c for c in (_country(p) for p in points) if c]
    if not countries:
        return False
    origin_country = countries[0]
    max_allowed = 0 if origin_country in SCHENGEN else 1
    entries = 0
    for prev, curr in zip(countries[:-1], countries[1:]):
        if prev not in SCHENGEN and curr in SCHENGEN:
            entries += 1
    return entries > max_allowed

class RouteEngine:
    """Ranks multi-leg chains (flight, train, bus, airport transfer) from origin to destination."""

    def __init__(self, flight_provider: Any, ground_provider: Any, airport_db: Optional[AirportDB] = None,
                 search_log: Any = None, costs: Any = None,
                 origin_radius_km: float = 300, dest_radius_km: float = 350,
                 max_parallel_searches: Optional[int] = None,
                 hotel_provider: Any = None,
                 observations: Any = None):
        self.flight_provider = flight_provider
        self.ground_provider = ground_provider
        self.airport_db = airport_db or AirportDB.load_default()
        self.search_log = search_log
        self._costs = costs
        self.origin_radius_km = float(origin_radius_km)
        self.dest_radius_km = float(dest_radius_km)
        # None: as many flight searches at once as the provider allows (search_concurrency()); 1: one at a time
        self.max_parallel_searches = max_parallel_searches
        self.hotel_provider = hotel_provider
        self.observations = observations

    def _concurrency(self) -> int:
        if self.max_parallel_searches is not None:
            return max(1, int(self.max_parallel_searches))
        getter = getattr(self.flight_provider, "search_concurrency", None)
        if not callable(getter):
            return 1
        try:
            return max(1, int(getter()))
        except Exception:
            return 1

    def _new_maps_budget(self) -> MapsBudget:
        """The Google Maps page-load budget of one search (the ground provider's maps_budget_limit)."""
        limit = getattr(self.ground_provider, "maps_budget_limit", None)
        return MapsBudget(limit if isinstance(limit, int) else DEFAULT_MAPS_BUDGET)

    def _new_hotels_budget(self) -> Any:
        """The Google Hotels page-load budget of one search."""
        provider = getattr(self, "hotel_provider", None)
        if provider is not None and hasattr(provider, "new_budget"):
            return provider.new_budget()
        return None

    def _costs_module(self, warnings: List[str]) -> Any:
        if self._costs is not None:
            return self._costs
        try:
            from travel_engine import costs as costs_module
        except ImportError:
            warnings.append("Maliyet modülü bulunamadı; tahmini toplam kullanıldı")
            return _FallbackCosts()
        return costs_module

    def search(self, origin: str, destination: str, depart_dates: List[str],
               arrive_by: Optional[str] = None, depart_after: Optional[str] = None,
               max_stops: int = 2,
               via_fixed: Optional[List[str]] = None,
               flight_search_budget: int = 20,
               time_value_try_per_hour: float = DEFAULT_TIME_VALUE_TRY_PER_HOUR,
               min_stay_mins: Optional[Dict[str, int]] = None,
               cabin_bag: bool = False, checked_bag: bool = False,
               max_results: int = 10, real_data_only: bool = False,
               via_on_date: Optional[Dict[str, str]] = None,
               schengen_single_entry: bool = False) -> Dict[str, Any]:
        if not (0 <= int(max_stops) <= MAX_STOPS_LIMIT):
            raise ValueError(f"max_stops must be between 0 and {MAX_STOPS_LIMIT}, got {max_stops!r}")
        dates = self._normalize_dates(depart_dates)
        arrive_by_dt = self._parse_dt(arrive_by, "arrive_by")
        depart_after_dt = self._parse_dt(depart_after, "depart_after")
        time_value = max(0.0, float(time_value_try_per_hour))
        cap = max(0, int(max_results))
        budget = max(0, int(flight_search_budget))
        via_tokens = [str(t).strip() for t in (via_fixed or []) if str(t or "").strip()] or None
        warnings: List[str] = []
        costs_module = self._costs_module(warnings)
        flight_stats_start = _provider_stats(self.flight_provider)
        ground_stats_start = _provider_stats(self.ground_provider)
        run = _Run(
            engine=self, origin=origin, destination=destination, dates=dates,
            arrive_by=arrive_by_dt, depart_after=depart_after_dt, max_stops=int(max_stops),
            via_tokens=via_tokens, budget=budget, time_value=time_value,
            min_stay=dict(min_stay_mins or {}), cabin_bag=bool(cabin_bag), checked_bag=bool(checked_bag),
            costs=costs_module, db=self.airport_db, ground=self.ground_provider,
            concurrency=self._concurrency(), real_data_only=bool(real_data_only),
            maps_budget=self._new_maps_budget(),
            hotels_budget=self._new_hotels_budget(),
            trace=SearchTrace(),
            flight_stats_start=flight_stats_start,
            ground_stats_start=ground_stats_start,
            via_on_date_input=via_on_date,
            schengen_single_entry=bool(schengen_single_entry),
        )
        run.warnings.extend(warnings)
        with run.phase("setup"):
            ok = run.setup_via() if via_tokens else run.setup_auto()
            if ok:
                run.setup_min_stay()
                run.bind_times()
        if ok:
            with run.phase("flight_plan"):
                if via_tokens:
                    run.plan_via()
                else:
                    run.plan_auto()
        with run.phase("build_options"):
            options_with_dates, near_miss_with_dates = self._build_options(run, ok) if ok else ([], [])
        with run.phase("result"):
            result = self._result(run, options_with_dates, near_miss_with_dates, origin, destination, dates, arrive_by, depart_after,
                                  max_stops, via_tokens, budget, time_value, min_stay_mins, cabin_bag,
                                  checked_bag, cap, via_on_date)
        result["trace"] = _finish_trace(run)
        return result

    # ------------------------------------------------------------ options

    def _dedupe_options(self, found: List[Tuple[Dict[str, Any], str]]) -> List[Tuple[Dict[str, Any], str]]:
        if not found:
            return []
        # Identical leg sequences: keep the first (best scoring after the sort below)
        found.sort(key=lambda pair: (pair[0]["score"], pair[0]["true_total_try"],
                                     pair[0]["duration_mins"], pair[0]["depart_utc"]))
        # 1. identical leg sequences (operator, stations and times) are listed once
        seen_legs = set()
        distinct: List[Tuple[Dict[str, Any], str]] = []
        for option, date in found:
            key = tuple((l["type"], l["operator"], l["from"], l["to"], l["depart_at"], l["arrive_at"])
                        for l in option["legs"])
            if key in seen_legs:
                continue
            seen_legs.add(key)
            distinct.append((option, date))
        # 2. near-duplicates: same flights, same kind and true totals within DEDUPE_TOTAL_TOLERANCE. They differ
        #    only in ground timings (e.g. an estimated bus next to a live one). Ground-only options never merge.
        groups: List[List[Tuple[Dict[str, Any], str]]] = []
        buckets: Dict[Tuple[Any, str], List[List[Tuple[Dict[str, Any], str]]]] = {}
        keys: Dict[int, Any] = {}
        for pair in distinct:
            option = pair[0]
            flights = _flight_key(option)
            keys[id(option)] = flights
            if not flights:
                groups.append([pair])
                continue
            bucket = buckets.setdefault((flights, option["kind"]), [])
            for group in bucket:
                if _totals_close(group[0][0]["true_total_try"], option["true_total_try"]):
                    group.append(pair)
                    break
            else:
                group = [pair]
                bucket.append(group)
                groups.append(group)
        # Keep the group member with the fewest estimated ground legs (live beats estimate), then the best score,
        # then the earliest arrival.
        representatives = [
            min(group, key=lambda p: (_estimated_ground_legs(p[0]), p[0]["score"], p[0]["arrive_at"]))
            if len(group) > 1 else group[0]
            for group in groups
        ]
        # 3. at most MAX_OPTIONS_PER_FLIGHT_SET options per flight set
        unique: List[Tuple[Dict[str, Any], str]] = []
        flight_sets: Dict[Tuple, int] = {}
        for option, date in representatives:
            flights = keys.get(id(option))
            if flights is None:
                flights = _flight_key(option)
            if flights:
                count = flight_sets.get(flights, 0)
                if count >= MAX_OPTIONS_PER_FLIGHT_SET:
                    continue
                flight_sets[flights] = count + 1
            unique.append((option, date))
        return unique

    def _build_options(self, run: _Run, ok: bool) -> Tuple[List[Tuple[Dict[str, Any], str]], List[Tuple[Dict[str, Any], str]]]:
        if not ok:
            return [], []
        found: List[Tuple[Dict[str, Any], str]] = []
        near_misses_found: List[Tuple[Dict[str, Any], str]] = []
        for date in run.dates:
            with run.phase("dfs"):
                states = run.dfs(date)
            with run.phase("options"):
                for state in states:
                    if run.depart_after_utc is not None and state.legs[0].dep < run.depart_after_utc:
                        run.reject("depart_after")
                        continue
                    arr_time = state.legs[-1].arr
                    is_near_miss = False
                    if run.arrive_by_utc is not None and arr_time > run.arrive_by_utc:
                        if run.arrival_limit_utc is not None and arr_time <= run.arrival_limit_utc:
                            is_near_miss = True
                        else:
                            continue
                    if run.schengen_single_entry and _violates_schengen(state, run.db):
                        run.reject("schengen")
                        continue
                    option = self._option(run, state)
                    run.count("options_built")
                    if run.real_data_only and _real_only_violation(option):
                        run.real_filtered += 1
                        continue
                    if is_near_miss:
                        late_mins = int(round((arr_time - run.arrive_by_utc).total_seconds() / 60))
                        option["late_by_mins"] = late_mins
                        near_misses_found.append((option, date))
                    else:
                        found.append((option, date))
        if run.truncated:
            run.warnings.append(SEARCH_LIMIT_WARNING)
        return self._dedupe_options(found), self._dedupe_options(near_misses_found)

    def _option(self, run: _Run, state: _State) -> Dict[str, Any]:
        legs = list(state.legs)
        main = [leg for leg in legs if leg.main]
        separate = len(main) >= 2
        if len(main) == 1:
            kind = "direct" if main[0].kind == "flight" else "ground"
        elif any(leg.kind != "flight" for leg in main):
            kind = "side_route"
        else:
            kind = "multi_flight"

        night_places = [a.to for a, b in zip(legs, legs[1:]) if _night_wait(a.arr, b.dep, a.arr_tz)]
        night = bool(night_places)
        hotel_place = night_places[0] if night_places else None

        hotel_price_try = None
        if night and hotel_place:
            for a, b in zip(legs, legs[1:]):
                if _night_wait(a.arr, b.dep, a.arr_tz) and a.to == hotel_place:
                    night_date_str = _to_local(a.arr, a.arr_tz).strftime(DATE_FMT)
                    hotel_price_try = run.get_hotel_price(hotel_place, night_date_str)
                    break

        stops: List[str] = []
        for position, leg in enumerate(main):
            stops.extend(leg.covers)
            if position < len(main) - 1:
                stops.append(leg.to)

        serialised = [_serialise_leg(leg, run.db, run.coord_cache) for leg in legs]

        cost_kwargs = dict(run.cost_options())
        if _supports_kwarg(run.costs.cost_breakdown, "hotel_price_try"):
            cost_kwargs["hotel_price_try"] = hotel_price_try
        if _supports_kwarg(run.costs.cost_breakdown, "transfers_from_legs"):
            cost_kwargs["transfers_from_legs"] = True

        breakdown = run.costs.cost_breakdown(
            serialised, hotel_nights=len(night_places), hotel_place=hotel_place,
            cabin_bag=run.cabin_bag, checked_bag=run.checked_bag, **cost_kwargs)
        # Known parts only: a lower bound when a price is unknown
        true_total = round(float(breakdown.get("total", 0.0)), 2)
        unknown_price_legs = sum(1 for leg in legs if leg.kind != "transfer" and leg.price is None)
        unknown_parts = list(breakdown.get("unknown_parts") or [])
        if any(leg.kind == "transfer" and leg.price is None for leg in legs):
            unknown_parts.append("transfers")
        unknown_parts = list(dict.fromkeys(unknown_parts))
        price_complete = unknown_price_legs == 0 and not unknown_parts
        # Ranking only: the unknown legs' ranking values are added to the score, never to a total
        rank_total = round(true_total + sum(run.rank_penalty(leg) for leg in legs if leg.price is None), 2)
        risk = run.costs.journey_risk(serialised, separate_tickets=separate)
        duration = int((legs[-1].arr - legs[0].dep).total_seconds() // 60)
        level = str(risk.get("level", "medium"))
        score = rank_total + (duration / 60.0) * run.time_value
        if night:
            score += NIGHT_WAIT_PENALTY_TRY * run.time_value / DEFAULT_TIME_VALUE_TRY_PER_HOUR
        score += RISK_PENALTY_TRY.get(level, RISK_PENALTY_TRY["medium"])

        first, last = legs[0], legs[-1]
        return {
            "kind": kind,
            "depart_at": _fmt_local(first.dep, first.dep_tz),
            "arrive_at": _fmt_local(last.arr, last.arr_tz),
            "depart_tz": first.dep_tz,
            "arrive_tz": last.arr_tz,
            "depart_utc": _fmt_utc(first.dep),
            "arrive_utc": _fmt_utc(last.arr),
            "total_try": round(sum(float(leg.price) for leg in legs if leg.price is not None), 2),
            "true_total_try": true_total,
            "total_lower_bound_try": true_total,
            "price_complete": price_complete,
            "unknown_price_legs": unknown_price_legs,
            "unknown_price_parts": unknown_parts,
            "rank_total_try": rank_total,
            "duration_mins": duration,
            "separate_tickets": separate,
            "via_stop": stops[0] if (run.via and stops) else None,
            "night_wait": night,
            "has_estimated_segments": any(leg.estimated for leg in legs),
            "score": round(score, 2),
            "badges": [],
            "legs": serialised,
            "stops": stops,
            "risk": risk,
            "cost_breakdown": breakdown,
            # "stopover" is the first stop with a minimum stay (single-stop consumers); "stopovers" lists all
            "stopover": dict(state.stays[0]) if state.stays else None,
            "stopovers": [dict(stay) for stay in state.stays],
        }

    # ------------------------------------------------------------ result

    def _result(self, run: _Run, found: List[Tuple[Dict[str, Any], str]],
                near_miss_found: List[Tuple[Dict[str, Any], str]],
                origin, destination, dates,
                arrive_by, depart_after, max_stops, via_tokens, budget, time_value,
                min_stay_mins, cabin_bag, checked_bag, cap,
                via_on_date: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        options = [option for option, _ in found]
        near_misses_raw = [option for option, _ in near_miss_found]
        if options:
            best_score = options[0]["score"]
            cheapest = min(options, key=lambda o: (o["rank_total_try"], o["score"]))
            fastest = min(options, key=lambda o: (o["duration_mins"], o["score"]))
            options[0]["badges"].append(BADGE_BEST)
            cheapest["badges"].append(BADGE_CHEAPEST)
            fastest["badges"].append(BADGE_FASTEST)
            for option in options:
                if option["risk"].get("level") == "low" and option["score"] <= best_score * 1.15:
                    option["badges"].append(BADGE_LOW_RISK)
        else:
            cheapest = fastest = None
        if options:
            # Per-date notes only when other dates produced options; with no options at all the
            # general no-option reason below is the only message.
            for day in dates:
                if not any(date == day for _, date in found):
                    run.warnings.append(departure_date_warning(day))
        if not options and run.real_data_only and run.real_filtered:
            run.warnings.append(REAL_ONLY_EMPTY_WARNING)
        if run.maps_budget is not None and run.maps_budget.exhausted:
            run.warnings.append(MAPS_BUDGET_WARNING)
        if not options and run.places:
            reason = run.no_option_reason()
            if reason:
                run.warnings.append(reason)
        top = options[:cap]

        near_misses: List[Dict[str, Any]] = []
        if run.arrive_by is not None:
            main_opts = top if top else options
            priced = [o for o in main_opts if o.get("price_complete") and o.get("true_total_try") is not None]
            if not priced and options:
                priced = [o for o in options if o.get("price_complete") and o.get("true_total_try") is not None]
            valid_candidates = [
                o for o in near_misses_raw
                if o.get("price_complete") and o.get("true_total_try") is not None
            ]
            if priced:
                min_priced = min(float(o["true_total_try"]) for o in priced)
                threshold = min_priced * (1.0 - NEAR_MISS_SAVING)
                qualifying = [o for o in valid_candidates if float(o["true_total_try"]) < threshold]
            else:
                qualifying = valid_candidates
            qualifying.sort(key=lambda o: (float(o["true_total_try"]),
                                          float(o.get("score") if o.get("score") is not None else float("inf"))))
            near_misses = qualifying[:NEAR_MISS_MAX]

        return {
            "query": {
                "origin": origin,
                "destination": destination,
                "depart_dates": dates,
                "arrive_by": arrive_by,
                "depart_after": depart_after,
                "max_stops": max_stops,
                "via_fixed": via_tokens,
                "flight_search_budget": budget,
                "time_value_try_per_hour": time_value,
                "min_stay_mins": dict(min_stay_mins or {}) or None,
                "via_on_date": dict(via_on_date) if via_on_date else None,
                "cabin_bag": bool(cabin_bag),
                "checked_bag": bool(checked_bag),
                "real_data_only": bool(run.real_data_only),
                "schengen_single_entry": bool(run.schengen_single_entry),
                "max_results": cap,
                "resolved": {
                    "origin": [p.iata for p in run.places[0].airports] if run.places else [],
                    "destination": [p.iata for p in run.places[run.last].airports] if run.places else [],
                    # One list of airport codes per fixed waypoint, in route order (fixed mode only)
                    "via": [[p.iata for p in place.airports] for place in run.places[1:run.last]]
                    if run.via and run.places else [],
                },
            },
            "options": top,
            "near_misses": near_misses,
            "best": top[0] if top else None,
            "cheapest": cheapest,
            "fastest": fastest,
            "flight_searches": run.used,
            # Searches skipped because the calendar price already rules them out (see _Run.should_prune)
            "pruned_searches": len(run.pruned),
            "budget_exhausted": run.exhausted,
            "candidates": _candidates(run),
            "reject_schengen": run.reject_counts.get("schengen", 0),
            "warnings": list(dict.fromkeys(run.warnings)),
        }

    # ------------------------------------------------------------ input helpers

    @staticmethod
    def _normalize_dates(depart_dates: Any) -> List[str]:
        items = [depart_dates] if isinstance(depart_dates, str) else list(depart_dates or [])
        dates: List[str] = []
        for item in items:
            try:
                day = datetime.strptime(str(item).strip(), DATE_FMT).strftime(DATE_FMT)
            except ValueError as exc:
                raise ValueError(f"depart_dates must be YYYY-MM-DD, got {item!r}") from exc
            if day not in dates:
                dates.append(day)
        if not dates:
            raise ValueError("depart_dates must contain at least one date")
        return sorted(dates)

    @staticmethod
    def _parse_dt(value: Optional[str], name: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.strptime(str(value).strip(), DATETIME_FMT)
        except ValueError as exc:
            raise ValueError(f"{name} must be YYYY-MM-DDTHH:MM, got {value!r}") from exc
