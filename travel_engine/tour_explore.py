"""Tour exploration across candidate cities (T13 · PLN-15a).

Orchestrates sequential multi-city searches with via and min_stay_mins
to find the best connecting tours among geographically plausible candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Sequence, Tuple

TOUR_CITIES: Tuple[str, ...] = (
    "Budapeşte",
    "Krakov",
    "Katoviçe",
    "Wrocław",
    "Varşova",
    "Viyana",
    "Bratislava",
    "Zagreb",
    "Ljubljana",
    "Venedik",
    "Milano",
    "Bologna",
    "Münih",
    "Berlin",
    "Roma",
    "Napoli",
    "Sofya",
    "Bükreş",
    "Belgrad",
)

_CITY_ALIASES: Dict[str, str] = {
    "katoviçe": "Katowice",
    "katovice": "Katowice",
    "krakov": "Krakow",
    "budapeşte": "Budapest",
    "viyana": "Vienna",
    "varşova": "Warsaw",
    "wrocław": "Wroclaw",
    "münih": "Munich",
    "bükreş": "Bucharest",
    "venedik": "Venice",
    "atina": "Athens",
    "prag": "Prague",
}

_CANONICAL_NAMES: Dict[str, str] = {
    c.lower(): c for c in TOUR_CITIES
}
_CANONICAL_NAMES.update({
    "katowice": "Katoviçe",
    "krakow": "Krakov",
    "cracow": "Krakov",
    "budapest": "Budapeşte",
    "vienna": "Viyana",
    "wien": "Viyana",
    "warsaw": "Varşova",
    "wroclaw": "Wrocław",
    "munich": "Münih",
    "münchen": "Münih",
    "bucharest": "Bükreş",
    "venice": "Venedik",
    "venezia": "Venedik",
    "milan": "Milano",
    "milano": "Milano",
    "rome": "Roma",
    "naples": "Napoli",
    "sofia": "Sofya",
    "belgrade": "Belgrad",
    "athens": "Atina",
    "spata-artemida": "Atina",
    "spata": "Atina",
    "ath": "Atina",
    "ist": "İstanbul",
    "saw": "İstanbul",
    "prague": "Prag",
    "praha": "Prag",
})


def _canonical_place(raw: Any) -> str:
    if not raw or not isinstance(raw, str):
        return str(raw or "")
    text = raw.strip()
    return _CANONICAL_NAMES.get(text.lower(), text)


def _get_coord(db: Any, place: str) -> Optional[Tuple[float, float]]:
    """Resolve latitude and longitude for a city name or IATA code using AirportDB."""
    if db is None:
        return None
    # 1. city_center lookup
    if hasattr(db, "city_center"):
        try:
            c = db.city_center(place)
            if c is not None:
                return c
        except Exception:
            pass
        alt = _CITY_ALIASES.get(place.lower())
        if alt:
            try:
                c = db.city_center(alt)
                if c is not None:
                    return c
            except Exception:
                pass

    # 2. resolve lookup
    if hasattr(db, "resolve"):
        try:
            aps = db.resolve(place) or (db.resolve(alt) if "alt" in locals() and alt else [])
            if aps:
                return (aps[0].lat, aps[0].lon)
        except Exception:
            pass

    # 3. get lookup (IATA)
    if hasattr(db, "get"):
        try:
            ap = db.get(place)
            if ap:
                return (ap.lat, ap.lon)
        except Exception:
            pass

    return None


def default_candidates(
    db_or_origin: Any,
    origin_or_dest: Any = None,
    destination: Any = None,
    limit: Optional[int] = 8,
) -> List[str]:
    """Return candidates from TOUR_CITIES with detour ratio <= 1.6, sorted by detour ratio ascending.

    Supports both (db, origin, destination, limit) and (origin, destination, limit).
    """
    if destination is None:
        from travel_engine.airports import AirportDB
        try:
            db = AirportDB.load_default()
        except Exception:
            db = None
        origin = str(db_or_origin)
        dest = str(origin_or_dest)
        lim = int(limit) if limit is not None else 8
    else:
        db = db_or_origin
        origin = str(origin_or_dest)
        dest = str(destination)
        lim = int(limit) if limit is not None else 8

    from travel_engine.airports import haversine_km

    o_coord = _get_coord(db, origin)
    d_coord = _get_coord(db, dest)
    if o_coord is None or d_coord is None:
        return []

    od_km = haversine_km(o_coord[0], o_coord[1], d_coord[0], d_coord[1])
    if od_km <= 0:
        return []

    # Build exclusion set for origin and destination (names, aliases and IATA codes)
    exclude: set[str] = {origin.lower(), dest.lower()}
    if origin.lower() in _CITY_ALIASES:
        exclude.add(_CITY_ALIASES[origin.lower()].lower())
    if dest.lower() in _CITY_ALIASES:
        exclude.add(_CITY_ALIASES[dest.lower()].lower())

    if db is not None and hasattr(db, "resolve"):
        try:
            for ap in (db.resolve(origin) or []):
                exclude.add(ap.city.lower())
                exclude.add(ap.iata.lower())
            for ap in (db.resolve(dest) or []):
                exclude.add(ap.city.lower())
                exclude.add(ap.iata.lower())
        except Exception:
            pass

    scored: List[Tuple[float, str]] = []
    for city in TOUR_CITIES:
        city_low = city.lower()
        if city_low in exclude or _CITY_ALIASES.get(city_low, "").lower() in exclude:
            continue
        c_coord = _get_coord(db, city)
        if c_coord is None:
            continue
        oc_km = haversine_km(o_coord[0], o_coord[1], c_coord[0], c_coord[1])
        cd_km = haversine_km(c_coord[0], c_coord[1], d_coord[0], d_coord[1])
        ratio = (oc_km + cd_km) / od_km
        if ratio <= 1.6:
            scored.append((ratio, city))

    scored.sort(key=lambda item: item[0])
    return [c for _, c in scored[:lim]]


@dataclass
class _ParsedLeg:
    frm: str
    to: str
    dep: datetime
    arr: datetime
    raw: Dict[str, Any]


def _parse_dt(val: Any) -> Optional[datetime]:
    if isinstance(val, datetime):
        return val
    if not val:
        return None
    s = str(val).strip()
    if "T" in s:
        try:
            # Naive on purpose: nights() compares local wall-clock times only
            return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def nights(option: Dict[str, Any], include_mode: bool = False) -> List[Dict[str, Any]]:
    """Determine nights spent during the trip (at 03:00 local time).

    Returns a list of dicts:
      {"date": "YYYY-MM-DD", "type": "transit", "mode": "bus"|"flight"|"train"} when a leg covers 03:00
      {"date": "YYYY-MM-DD", "type": "stay", "place": city_name} when traveler is staying in a city or airport
    """
    legs = option.get("legs") or option.get("segments") or []
    if not legs:
        return []

    parsed: List[_ParsedLeg] = []
    for leg in legs:
        # Local times: the night is where the traveller is (03:00 there), not 03:00 UTC
        dep = _parse_dt(leg.get("depart_at") or leg.get("depart_utc") or leg.get("departure_time"))
        arr = _parse_dt(leg.get("arrive_at") or leg.get("arrive_utc") or leg.get("arrival_time"))
        if dep is not None and arr is not None:
            parsed.append(_ParsedLeg(
                frm=str(leg.get("from") or leg.get("origin_station") or ""),
                to=str(leg.get("to") or leg.get("dest_station") or ""),
                dep=dep,
                arr=arr,
                raw=leg,
            ))

    if not parsed:
        return []

    first_dep = _parse_dt(option.get("depart_at")) or parsed[0].dep
    last_arr = _parse_dt(option.get("arrive_at")) or parsed[-1].arr

    # Evaluate 03:00 on each calendar date between first_dep and last_arr
    first_date = first_dep.date()
    last_date = last_arr.date()

    res: List[Dict[str, Any]] = []
    curr_ord = first_date.toordinal()
    last_ord = last_date.toordinal()

    for ord_val in range(curr_ord, last_ord + 1):
        d = datetime.fromordinal(ord_val).date()
        target = datetime.combine(d, time(3, 0))
        # 03:00 must be within the trip window (after departure and before arrival)
        if target <= first_dep or target >= last_arr:
            continue

        # Check if an active leg covers this 03:00
        active_leg = None
        for leg in parsed:
            if leg.dep <= target <= leg.arr:
                active_leg = leg
                break

        if active_leg is not None:
            raw_type = str(active_leg.raw.get("type") or "").lower()
            if raw_type == "flight":
                mode = "flight"
            elif raw_type in ("train", "rail"):
                mode = "train"
            else:
                mode = "bus"
            entry = {"date": d.isoformat(), "type": "transit"}
            if include_mode:
                entry["mode"] = mode
            res.append(entry)
        else:
            # Traveler is staying in a city or airport between legs
            prev_leg = None
            next_leg = None
            for leg in parsed:
                if leg.arr <= target:
                    prev_leg = leg
                elif leg.dep >= target and next_leg is None:
                    next_leg = leg

            is_airport = False
            if prev_leg is not None:
                to_code = str(prev_leg.to or "").strip().upper()
                dest_station = str(prev_leg.raw.get("dest_station_name") or "")
                is_flight = prev_leg.raw.get("type") == "flight"
                is_transfer_to_airport = prev_leg.raw.get("type") == "transfer" and len(to_code) == 3 and to_code.isalpha()
                if len(to_code) == 3 and to_code.isalpha() and (is_flight or is_transfer_to_airport or "havaliman" in dest_station.lower() or "airport" in dest_station.lower()):
                    is_airport = True
                    place_raw = to_code
                elif "havaliman" in dest_station.lower() or "airport" in dest_station.lower():
                    is_airport = True
                    place_raw = dest_station
                else:
                    place_raw = prev_leg.raw.get("dest_station_name") or prev_leg.to
            else:
                place_raw = parsed[0].raw.get("origin_station_name") or parsed[0].frm

            canonical = _canonical_place(place_raw)
            if len(place_raw) == 3 and place_raw.isupper():
                try:
                    from travel_engine.airports import AirportDB
                    db = AirportDB.load_default()
                    ap = db.get(place_raw)
                    if ap is not None and ap.city:
                        canonical = _canonical_place(ap.city)
                except Exception:
                    pass

            display_place = f"{canonical} (havalimanı)" if is_airport else canonical

            res.append({
                "date": d.isoformat(),
                "type": "stay",
                "place": display_place,
            })

    return res


def format_nights_line(option: Dict[str, Any]) -> str:
    """Format the nights spent on an option as a single line, or "" when none.

    Example: "Geceler: 🛏 Bratislava · 🚌 yolda · 🛏 Atina (havalimanı)"
    """
    if not option:
        return ""
    night_items = nights(option, include_mode=True)
    if not night_items:
        return ""
    parts: List[str] = []
    for n in night_items:
        ntype = n.get("type")
        if ntype == "transit":
            mode = n.get("mode")
            if mode == "flight":
                parts.append("✈️ yolda")
            elif mode == "train":
                parts.append("🚆 yolda")
            else:
                parts.append("🚌 yolda")
        elif ntype == "stay":
            place = n.get("place") or ""
            parts.append(f"🛏 {place}")
    if not parts:
        return ""
    return f"Geceler: {' · '.join(parts)}"


def _pick_best(options: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not options:
        return None
    complete = [o for o in options if o.get("price_complete")]
    pool = complete if complete else options
    return min(
        pool,
        key=lambda o: (
            o.get("true_total_try") if o.get("true_total_try") is not None else float("inf"),
            o.get("total_try") if o.get("total_try") is not None else float("inf"),
        ),
    )


def explore_tour(
    planner: Any,
    origin: str,
    destination: str,
    depart_dates: Any,
    arrive_by: Optional[str] = None,
    depart_after: Optional[str] = None,
    candidates: Optional[Any] = None,
    min_stay_mins: int = 240,
    budget_per_candidate: int = 8,
    max_candidates: int = 8,
) -> Dict[str, Any]:
    """Execute sequential tour exploration queries over candidate via cities."""
    if isinstance(depart_dates, str):
        dates = [d.strip() for d in depart_dates.split(",") if d.strip()]
    else:
        dates = list(depart_dates)

    max_cands = max(1, min(12, int(max_candidates)))
    budget = max(1, min(20, int(budget_per_candidate)))
    stay_mins = max(0, min(2880, int(min_stay_mins)))

    if candidates is None:
        db = getattr(planner, "airport_db", None) or getattr(planner, "db", None)
        if db is None:
            try:
                from travel_engine.airports import AirportDB
                db = AirportDB.load_default()
            except Exception:
                db = None
        cand_list = default_candidates(db, origin, destination, limit=max_cands)
    else:
        if isinstance(candidates, str):
            cand_list = [c.strip() for c in candidates.split(",") if c.strip()]
        else:
            cand_list = list(candidates)
        cand_list = cand_list[:max_cands]

    # 1. Baseline search without via
    b_res = planner.plan_trip(
        origin=origin,
        destination=destination,
        depart_dates=dates,
        arrive_by=arrive_by,
        depart_after=depart_after,
        flight_search_budget=budget,
    ) or {}
    b_best = _pick_best(b_res.get("options") or [])
    if b_best is not None:
        baseline_row: Optional[Dict[str, Any]] = {
            "candidate": None,
            "true_total_try": b_best.get("true_total_try"),
            "price_complete": b_best.get("price_complete", True),
            "depart_at": b_best.get("depart_at"),
            "arrive_at": b_best.get("arrive_at"),
            "legs": b_best.get("legs", []),
            "diff_vs_baseline_try": 0.0,
            "nights": nights(b_best),
        }
    else:
        baseline_row = None

    # 2. Sequential candidate searches
    rows: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for cand in cand_list:
        c_res = planner.plan_trip(
            origin=origin,
            destination=destination,
            depart_dates=dates,
            arrive_by=arrive_by,
            depart_after=depart_after,
            via=[cand],
            min_stay_mins={cand: stay_mins},
            flight_search_budget=budget,
        ) or {}
        c_opts = c_res.get("options") or []
        best = _pick_best(c_opts)
        if best is None:
            reason = (c_res.get("warnings") or ["uygun seçenek bulunamadı"])[0]
            skipped.append({"candidate": cand, "reason": reason})
            continue

        cand_total = best.get("true_total_try")
        base_total = baseline_row.get("true_total_try") if baseline_row else None
        diff = (
            round(cand_total - base_total, 2)
            if cand_total is not None and base_total is not None
            else None
        )

        rows.append({
            "candidate": cand,
            "true_total_try": cand_total,
            "price_complete": best.get("price_complete", True),
            "depart_at": best.get("depart_at"),
            "arrive_at": best.get("arrive_at"),
            "legs": best.get("legs", []),
            "diff_vs_baseline_try": diff,
            "nights": nights(best),
        })

    # Sort rows ascending by true_total_try
    rows.sort(key=lambda r: (r["true_total_try"] if r["true_total_try"] is not None else float("inf")))

    return {
        "baseline": baseline_row,
        "rows": rows,
        "skipped": skipped,
        "candidates": list(cand_list),
    }
