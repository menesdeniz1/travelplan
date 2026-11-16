import re
import urllib.parse
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from travel_engine.geo_clusters import GeoClusterRegistry
from travel_engine.providers.rail_bus_provider import RailBusProvider, ScheduledSegment

MINS_PER_DAY = 24 * 60
# A transfer wait that overlaps the 00:00-06:00 window by at least this many minutes is a "night wait"
NIGHT_WINDOW_END_MINS = 6 * 60
NIGHT_WAIT_MIN_OVERLAP_MINS = 180
# Hassle penalty added to the ranking score of a night-wait journey (TRY-equivalent)
NIGHT_WAIT_PENALTY_TRY = 1500.0
DEFAULT_HOTEL_TRY = 2500.0
DEFAULT_TIME_VALUE_TRY_PER_HOUR = 150.0
DEFAULT_MAX_VARIANTS_PER_FLIGHT = 2

def get_operator_booking_url(operator: str) -> str:
    op_lower = operator.lower()
    if "regiojet" in op_lower:
        return "https://regiojet.com"
    elif "öbb" in op_lower or "oebb" in op_lower or "railjet" in op_lower:
        return "https://www.oebb.at"
    elif "db" in op_lower or "ice" in op_lower or "bahn" in op_lower:
        return "https://www.bahn.de"
    elif "čd" in op_lower or "cd" in op_lower:
        return "https://www.cd.cz"
    elif "flixbus" in op_lower:
        return "https://www.flixbus.com"
    elif "hellenic" in op_lower:
        return "https://www.hellenictrain.gr"
    return "https://www.google.com/search?q=" + urllib.parse.quote(f"{operator} train tickets")

_HHMM_RE = re.compile(r'\b(\d{1,2}):(\d{2})\b')
# Like _HHMM_RE but also captures an optional "+N" arrival day suffix, e.g. "01:35+1"
_TIMES_WITH_DAY_RE = re.compile(r'\b(\d{1,2}):(\d{2})\b(?:\s*\+\s*(\d+))?')


def extract_flight_times(flight: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Return (departure "HH:MM", arrival "HH:MM") for a flight dict, or None if unknown.

    Accepts explicit departure_time/arrival_time fields, or the GoogleFlightsProvider
    "times" string (e.g. "19:30 – 21:45" or "23:10 – 01:35+1").
    """
    dep = flight.get("departure_time")
    arr = flight.get("arrival_time")
    if dep and arr:
        return str(dep).strip(), str(arr).strip()

    matches = _HHMM_RE.findall(flight.get("times") or "")
    if len(matches) < 2:
        return None
    (dh, dm), (ah, am) = matches[0], matches[1]
    if int(dh) > 23 or int(ah) > 23 or int(dm) > 59 or int(am) > 59:
        return None
    return f"{int(dh):02d}:{dm}", f"{int(ah):02d}:{am}"


def extract_flight_day_offset(flight: Dict[str, Any]) -> int:
    """Days after the departure date on which the flight lands, from the "+N" suffix of its "times" string.

    "23:50 – 01:05+1" -> 1. Returns 0 when there is no suffix or the string is unparsable.
    The caller must still treat an arrival earlier than the departure as a next-day arrival.
    """
    matches = _TIMES_WITH_DAY_RE.findall(flight.get("times") or "")
    if len(matches) < 2:
        return 0
    suffix = matches[1][2]
    return int(suffix) if suffix else 0


def _night_overlap_mins(start_abs: int, end_abs: int) -> int:
    """Minutes of the interval [start_abs, end_abs] (absolute minutes from search-date 00:00) inside a 00:00-06:00 window."""
    if end_abs <= start_abs:
        return 0
    total = 0
    for day in range(start_abs // MINS_PER_DAY, end_abs // MINS_PER_DAY + 1):
        win_start = day * MINS_PER_DAY
        win_end = win_start + NIGHT_WINDOW_END_MINS
        total += max(0, min(end_abs, win_end) - max(start_abs, win_start))
    return total


@dataclass
class MultiModalJourney:
    route_id: str
    origin_city: str
    destination_city: str
    total_price_try: float
    savings_try: float
    total_duration_mins: int
    initial_departure_time: str
    final_arrival_time: str
    segments: List[Dict[str, Any]]
    badges: List[str] = field(default_factory=list)
    map_points: Dict[str, Any] = field(default_factory=dict)
    # True when any segment is not observed live data (ground timetable, transfer buffer)
    has_estimated_segments: bool = False
    # Days after the search date on which the FINAL arrival happens (0 same day, 1 next day, ...)
    arrival_day_offset: int = 0
    night_wait: bool = False
    night_wait_mins: int = 0
    estimated_hotel_try: Optional[float] = None
    hotel_is_estimated: bool = False
    # total_price_try plus the estimated hotel (if any); the basis for savings and "true" cost
    true_total_try: float = 0.0
    # Ranking score: true_total_try + time value of duration + night-wait penalty (lower is better)
    score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_id": self.route_id,
            "origin_city": self.origin_city,
            "destination_city": self.destination_city,
            "total_price_try": self.total_price_try,
            "savings_try": self.savings_try,
            "total_duration_mins": self.total_duration_mins,
            "initial_departure_time": self.initial_departure_time,
            "final_arrival_time": self.final_arrival_time,
            "segments": self.segments,
            "badges": self.badges,
            "map_points": self.map_points,
            "has_estimated_segments": self.has_estimated_segments,
            "arrival_day_offset": self.arrival_day_offset,
            "night_wait": self.night_wait,
            "night_wait_mins": self.night_wait_mins,
            "estimated_hotel_try": self.estimated_hotel_try,
            "hotel_is_estimated": self.hotel_is_estimated,
            "true_total_try": self.true_total_try,
            "score": self.score,
        }

class MultiModalRouter:
    """Smart router that combines flights with regional high-speed rail & bus connections."""

    def __init__(
        self,
        flight_provider: Any,
        rail_provider: Optional[RailBusProvider] = None,
        geo_registry: Optional[GeoClusterRegistry] = None,
    ):
        self.flight_provider = flight_provider
        self.geo_registry = geo_registry or GeoClusterRegistry()
        self.rail_provider = rail_provider or RailBusProvider(self.geo_registry)

    def _parse_hhmm(self, time_str: Optional[str]) -> int:
        if not time_str:
            return 0
        try:
            parts = str(time_str).strip().split(":")
            if len(parts) >= 2:
                h = int(parts[0])
                m = int(parts[1])
                return h * 60 + m
            return 0
        except (ValueError, IndexError):
            return 0

    def _format_hhmm(self, total_mins: int) -> str:
        total_mins = total_mins % (24 * 60)
        h = total_mins // 60
        m = total_mins % 60
        return f"{h:02d}:{m:02d}"

    @staticmethod
    def _add_days(date: str, days: int) -> str:
        if not days:
            return date
        try:
            d_obj = datetime.strptime(date, "%Y-%m-%d")
            return (d_obj + timedelta(days=days)).strftime("%Y-%m-%d")
        except Exception:
            return date

    @staticmethod
    def _hotel_estimate(catchment: Any) -> float:
        """Rough per-night hotel price in the catchment city, where a night wait would be spent."""
        value = getattr(catchment, "avg_hotel_try", None)
        return float(value) if value else DEFAULT_HOTEL_TRY

    def search_multimodal(
        self,
        origin: str,
        destination: str,
        date: str,
        arrival_deadline: Optional[str] = None,
        max_duration_hours: Optional[float] = None,
        min_transfer_buffer_mins: Optional[int] = None,
        prefetched_direct_flights: Optional[List[Dict[str, Any]]] = None,
        time_value_try_per_hour: float = DEFAULT_TIME_VALUE_TRY_PER_HOUR,
        max_variants_per_flight: int = DEFAULT_MAX_VARIANTS_PER_FLIGHT,
    ) -> Dict[str, Any]:
        time_value = max(0.0, float(time_value_try_per_hour))
        max_variants = max(1, int(max_variants_per_flight))

        # 1. Search direct flights (or reuse prefetched)
        if prefetched_direct_flights is not None:
            direct_flights = prefetched_direct_flights
        else:
            direct_flights = self.flight_provider.search_flights(
                origin=origin,
                destination=destination,
                departure_date=date,
                trip_type="oneway"
            ) or []

            cluster = self.geo_registry.get_cluster(destination)
            if not direct_flights and cluster and cluster.primary_airport.lower() != destination.lower():
                direct_flights = self.flight_provider.search_flights(
                    origin=origin,
                    destination=cluster.primary_airport,
                    departure_date=date,
                    trip_type="oneway"
                ) or []

        cheapest_direct_price = 0.0
        cheapest_direct_flight = None
        if direct_flights:
            # Sort by total_price_try
            sorted_direct = sorted(direct_flights, key=lambda f: float(f.get("total_price_try", 0)))
            cheapest_direct_flight = sorted_direct[0]
            cheapest_direct_price = float(cheapest_direct_flight.get("total_price_try", 0))

        # 2. Check destination cluster for catchment airports
        cluster = self.geo_registry.get_cluster(destination)
        # (journey, flight identity, final arrival in absolute minutes, flight+ground price) before dedupe
        candidates: List[Tuple[Tuple[str, str, str], int, float, MultiModalJourney]] = []

        if cluster:
            deadline_mins = self._parse_hhmm(arrival_deadline) if arrival_deadline else None
            max_duration_mins = int(max_duration_hours * 60) if max_duration_hours else None

            for catchment in cluster.catchment_airports:
                # Search flights from origin to this catchment airport
                catchment_flights = self.flight_provider.search_flights(
                    origin=origin,
                    destination=catchment.airport_code,
                    departure_date=date,
                    trip_type="oneway"
                ) or []

                # Default buffer: terminal exit + ground transit to station (strictly >= 90 mins)
                raw_buffer = min_transfer_buffer_mins or self.geo_registry.get_transfer_buffer_mins(catchment.airport_code)
                airport_buffer = max(90, raw_buffer)

                for fl in catchment_flights:
                    fl_times = extract_flight_times(fl)
                    if fl_times is None:
                        # Without real departure/arrival times we cannot validate the connection
                        continue
                    fl_dep, fl_arr = fl_times
                    fl_price = float(fl.get("total_price_try", 0))

                    fl_dep_mins = self._parse_hhmm(fl_dep)
                    fl_arr_mins = self._parse_hhmm(fl_arr)

                    # Day the flight lands on, relative to the search date: the "+N" suffix, or an
                    # arrival clock time earlier than departure (crosses midnight) means the next day.
                    arr_day = max(extract_flight_day_offset(fl), 1 if fl_arr_mins < fl_dep_mins else 0)
                    fl_arr_abs = fl_arr_mins + MINS_PER_DAY * arr_day
                    fl_flight_dur = fl_arr_abs - fl_dep_mins

                    # Earliest train departure from main station (absolute minutes)
                    earliest_train_abs = fl_arr_abs + airport_buffer

                    # Ground connections on the landing day; if none are left (late-evening landing),
                    # fall back to the next morning so the night wait is shown instead of the route vanishing.
                    ground_options = []
                    for ground_day in (arr_day, arr_day + 1):
                        day_start = MINS_PER_DAY * ground_day
                        if earliest_train_abs >= day_start + MINS_PER_DAY:
                            continue
                        segs = self.rail_provider.get_hub_connections(
                            from_hub=catchment.airport_code,
                            to_city=cluster.city_name,
                            date_str=self._add_days(date, ground_day),
                            earliest_dep_time=self._format_hhmm(max(earliest_train_abs - day_start, 0)),
                        )
                        if segs:
                            ground_options = [(ground_day, s) for s in segs]
                            break

                    for ground_day, ground in ground_options:
                        gr_dep_mins = self._parse_hhmm(ground.departure_time)
                        gr_dep_abs = MINS_PER_DAY * ground_day + gr_dep_mins

                        # Enforce transfer buffer: train MUST depart after flight arrival + buffer
                        if gr_dep_abs < fl_arr_abs + airport_buffer:
                            continue
                        transfer_duration = gr_dep_abs - fl_arr_abs

                        gr_arr_mins = self._parse_hhmm(ground.arrival_time)
                        train_crosses_midnight = (gr_arr_mins < gr_dep_mins)
                        ground_duration = (gr_arr_mins + (MINS_PER_DAY if train_crosses_midnight else 0)) - gr_dep_mins
                        final_arr_abs = gr_dep_abs + ground_duration
                        final_day_offset = final_arr_abs // MINS_PER_DAY

                        # Arrival deadline is a same-day (search date) constraint: any later-day arrival fails it
                        if deadline_mins is not None and final_arr_abs > deadline_mins:
                            continue

                        # Total journey minutes: flight duration + transfer duration + ground duration
                        total_journey_mins = fl_flight_dur + transfer_duration + ground_duration

                        # Enforce max duration
                        if max_duration_mins is not None and total_journey_mins > max_duration_mins:
                            continue

                        # Night wait: transfer window overlapping 00:00-06:00 means the traveller sleeps at the catchment
                        night_mins = _night_overlap_mins(fl_arr_abs, gr_dep_abs)
                        is_night_wait = night_mins >= NIGHT_WAIT_MIN_OVERLAP_MINS
                        night_wait_mins = night_mins if is_night_wait else 0
                        estimated_hotel = float(self._hotel_estimate(catchment)) if is_night_wait else None

                        total_cost = fl_price + ground.price_try
                        true_total = total_cost + (estimated_hotel or 0.0)
                        savings = (cheapest_direct_price - true_total) if cheapest_direct_price > 0 else 0.0
                        score = (
                            true_total
                            + (total_journey_mins / 60.0) * time_value
                            # Scaled with time value so time_value=0 really means "rank by price only"
                            + (NIGHT_WAIT_PENALTY_TRY * time_value / DEFAULT_TIME_VALUE_TRY_PER_HOUR if is_night_wait else 0.0)
                        )

                        # Unique route ID combining catchment, flight operator/number, station, ground operator/mode
                        fl_code = (fl.get("flight_number") or fl.get("airline", "FL"))[:6].replace(" ", "").upper()
                        gr_code = f"{ground.mode[:1].upper()}-{ground.operator[:3].replace(' ', '').upper()}"
                        route_id = f"MM-{catchment.airport_code}-{fl_code}-{ground.dest_station[:3]}-{gr_code}-{fl_dep.replace(':', '')}-{ground.departure_time.replace(':', '')}"

                        flight_booking_url = fl.get("booking_token") or f"https://www.google.com/travel/flights?q=Flights%20to%20{catchment.airport_code}%20from%20{origin}%20on%20{date}"
                        ground_booking_url = get_operator_booking_url(ground.operator)

                        transfer_note = f"{airport_buffer} dk asgari transfer & bagaj payı dahil"
                        if is_night_wait:
                            transfer_note += f"; gece bekleme {night_wait_mins} dk (00:00–06:00 arası)"

                        segments = [
                            {
                                "type": "flight",
                                "operator": fl.get("airline", "Airline"),
                                "flight_number": fl.get("flight_number", ""),
                                "from": fl.get("origin", origin),
                                "to": catchment.airport_code,
                                "departure_time": fl_dep,
                                "arrival_time": fl_arr,
                                "duration_mins": fl_flight_dur,
                                "price_try": fl_price,
                                "booking_url": flight_booking_url,
                                "is_estimated": False,
                            },
                            {
                                "type": "transfer",
                                "operator": "Airport Ground Transit",
                                "from": f"{catchment.airport_code} Airport Terminal",
                                "to": ground.origin_station,
                                "departure_time": fl_arr,
                                "arrival_time": ground.departure_time,
                                "duration_mins": transfer_duration,
                                "price_try": 0.0,
                                "note": transfer_note,
                                "booking_url": "",
                                "is_estimated": True,
                                "is_night_wait": is_night_wait,
                            },
                            {
                                "type": ground.mode,
                                "operator": ground.operator,
                                "from": ground.origin_station,
                                "to": ground.dest_station,
                                "departure_time": ground.departure_time,
                                "arrival_time": ground.arrival_time,
                                "duration_mins": ground_duration,
                                "price_try": ground.price_try,
                                "booking_url": ground_booking_url,
                                "is_estimated": ground.is_estimated,
                            }
                        ]

                        orig_c = self.geo_registry.get_coordinates(origin) or {"lat": 37.9838, "lon": 23.7275}
                        catch_c = self.geo_registry.get_coordinates(catchment.airport_code) or self.geo_registry.get_coordinates(catchment.city_name) or {"lat": 48.2082, "lon": 16.3738}
                        dest_c = self.geo_registry.get_coordinates(cluster.primary_airport) or self.geo_registry.get_coordinates(cluster.city_name) or {"lat": 50.0755, "lon": 14.4378}

                        map_points = {
                            "origin": {"name": origin, "lat": orig_c["lat"], "lon": orig_c["lon"]},
                            "catchment": {"name": catchment.city_name, "code": catchment.airport_code, "lat": catch_c["lat"], "lon": catch_c["lon"]},
                            "destination": {"name": cluster.city_name, "code": cluster.primary_airport, "lat": dest_c["lat"], "lon": dest_c["lon"]},
                        }

                        journey = MultiModalJourney(
                            route_id=route_id,
                            origin_city=origin,
                            destination_city=cluster.city_name,
                            total_price_try=total_cost,
                            savings_try=savings,
                            total_duration_mins=total_journey_mins,
                            initial_departure_time=fl_dep,
                            final_arrival_time=ground.arrival_time,
                            segments=segments,
                            badges=[],
                            map_points=map_points,
                            has_estimated_segments=any(s["is_estimated"] for s in segments),
                            arrival_day_offset=final_day_offset,
                            night_wait=is_night_wait,
                            night_wait_mins=night_wait_mins,
                            estimated_hotel_try=estimated_hotel,
                            hotel_is_estimated=estimated_hotel is not None,
                            true_total_try=true_total,
                            score=score,
                        )
                        flight_key = (catchment.airport_code, str(fl.get("airline", "")), fl_dep)
                        candidates.append((flight_key, final_arr_abs, total_cost, journey))

        # Dedupe: per distinct flight keep only the `max_variants` ground connections that arrive earliest
        multimodal_journeys: List[MultiModalJourney] = []
        kept_per_flight: Dict[Tuple[str, str, str], int] = {}
        for flight_key, _final_arr, _total_cost, journey in sorted(candidates, key=lambda c: (c[1], c[2])):
            kept = kept_per_flight.get(flight_key, 0)
            if kept >= max_variants:
                continue
            kept_per_flight[flight_key] = kept + 1
            multimodal_journeys.append(journey)

        # Human ranking: lowest score first (true cost + time value + night-wait penalty)
        multimodal_journeys.sort(key=lambda j: (j.score, j.true_total_try, j.total_duration_mins))

        # Assign badges
        if multimodal_journeys:
            # Cheapest true cost (hotel included)
            cheapest = min(multimodal_journeys, key=lambda j: j.true_total_try)
            cheapest.badges.append("En Ucuz Hibrit")

            # Fastest
            fastest = min(multimodal_journeys, key=lambda j: j.total_duration_mins)
            if "En Hızlı Hibrit" not in fastest.badges:
                fastest.badges.append("En Hızlı Hibrit")

            # Best value = best score (only assign if positive savings achieved!)
            best_val = multimodal_journeys[0]
            if best_val.savings_try > 0 and "En İyi Değer" not in best_val.badges:
                best_val.badges.append("En İyi Değer")

        return {
            "origin": origin,
            "destination": destination,
            "date": date,
            "has_multimodal": len(multimodal_journeys) > 0,
            "cheapest_direct_flight": cheapest_direct_flight,
            "cheapest_direct_price": cheapest_direct_price,
            "multimodal_count": len(multimodal_journeys),
            "multimodal_routes": [j.to_dict() for j in multimodal_journeys],
            "destination_cluster": cluster.city_name if cluster else None,
        }
