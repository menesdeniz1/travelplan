#!/usr/bin/env python3
"""TravelPlan Modernized Server with Multi-Modal Route Optimization & Tracking."""

import http.server
import json
import logging
import math
import os
import re
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Dict, Any, List, Optional

# Read <repo>/.env into os.environ before any project import reads settings (variables already set win).
try:
    from travel_engine.envfile import load_env
    load_env()
except ImportError:  # envfile missing: run on the plain process environment
    pass

from travel_engine.fx import get_eur_try_rate
from travel_engine.search_log import SearchLog
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider
from travel_engine.tracker import PriceTracker
from travel_engine.notifier import AlertNotifier
from travel_engine.daemon_tracker import DaemonTracker
from travel_engine.price_history import PriceHistoryManager
from travel_engine.geo_clusters import GeoClusterRegistry
from travel_engine.providers.rail_bus_provider import RailBusProvider
from travel_engine.providers.live_rail_provider import LiveRailProvider
from travel_engine.providers.flixbus_provider import FlixBusProvider
from travel_engine.providers.transitous_provider import TransitousProvider
from travel_engine.providers.google_maps_transit_provider import GoogleMapsTransitProvider
from travel_engine.providers.obilet_fares import ObiletFromPrices
from travel_engine.providers.trenitalia_provider import TrenitaliaProvider
from travel_engine.providers.google_hotels_provider import GoogleHotelsProvider
from travel_engine.multimodal_router import MultiModalRouter
from travel_engine.replay import wrap_flight_provider, wrap_ground_provider
from travel_engine.route_engine import RouteEngine
from travel_engine.observations import ObservationStore
from travel_engine.trip_planner import TripPlanner
from travel_engine.storage import isolate_for_tests, test_run_temp_dir

logger = logging.getLogger("travelplan.server")

# Loopback by default so the API (and the Telegram token endpoints) is not exposed on the LAN.
HOST = os.environ.get("TRAVELPLAN_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.environ.get("TRAVELPLAN_PORT", "8085"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# The only non-/api/ paths that may be served from disk, each with its Content-Type. Everything else is 404.
STATIC_FILES = {
    "/": ("static/index.html", "text/html; charset=utf-8"),
    "/index.html": ("static/index.html", "text/html; charset=utf-8"),
    "/app_v2.html": ("static/index.html", "text/html; charset=utf-8"),  # old address, same page
    "/static/css/app.css": ("static/css/app.css", "text/css; charset=utf-8"),
    "/static/js/app.js": ("static/js/app.js", "application/javascript; charset=utf-8"),
}

# Request bodies larger than this are rejected with 413.
MAX_BODY_BYTES = 1024 * 1024
LOCAL_HOSTNAMES = ("localhost", "127.0.0.1")
_HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")


def _parse_allowed_hosts(raw: str) -> frozenset:
    """Extra host names for the Host and Origin headers (TRAVELPLAN_ALLOWED_HOSTS, comma-separated).

    Only bare host names or IPv4 addresses are accepted: no ports, schemes or wildcards.
    Invalid entries are logged and ignored.
    """
    hosts = set()
    for item in (raw or "").split(","):
        name = item.strip().lower()
        if not name:
            continue
        if _HOSTNAME_RE.fullmatch(name):
            hosts.add(name)
        else:
            logger.warning("TRAVELPLAN_ALLOWED_HOSTS: ignored %r (expected a bare host name)", item.strip())
    return frozenset(hosts)


# Extra LAN names (e.g. "mac.local,192.168.1.20") accepted next to LOCAL_HOSTNAMES and HOST. Empty by default.
ALLOWED_HOSTS = _parse_allowed_hosts(os.environ.get("TRAVELPLAN_ALLOWED_HOSTS", ""))


def _known_host(hostname) -> bool:
    """True for names that address this server: localhost, 127.0.0.1, HOST and ALLOWED_HOSTS."""
    return hostname in LOCAL_HOSTNAMES or hostname == HOST or hostname in ALLOWED_HOSTS

# Trip search limits and input formats (validated by _normalize_trip_query)
TRIP_MAX_DEPART_DATES = 7
TRIP_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
TRIP_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
TRIP_VIA_RE = re.compile(r"[A-Za-zÇĞİÖŞÜçğıöşü][A-Za-zÇĞİÖŞÜçğıöşü .'\-]{1,39}")
TRIP_PLACE_MAX_LEN = 60
# Route engine rules (optional in every trip request; the planner defaults apply when absent)
TRIP_MAX_STOPS = 3              # max_stops: 0..3 automatic stops
TRIP_BUDGET_RANGE = (1, 60)     # budget: flight searches per query
TRIP_MAX_FLEX_DAYS = 3          # flex_days: +-N days around each date
TRIP_MIN_STAY_HOURS_MAX = 720.0 # min_stay_hours: minimum stay between outbound and return
TRIP_MIN_STAY_MINS_MAX = 2880   # min_stay per named stop (48 h)
TRIP_MAX_RETURN_DATES = 7

def _default_search_log_path() -> str:
    """TRAVELPLAN_SEARCH_LOG, else search_log.db in the repo root (a per-process temp file in an isolated test run)."""
    explicit = os.environ.get("TRAVELPLAN_SEARCH_LOG", "").strip()
    if explicit:
        return explicit
    if isolate_for_tests():
        return os.path.join(test_run_temp_dir(), "search_log.db")
    return os.path.join(BASE_DIR, "search_log.db")


SEARCH_LOG_PATH = _default_search_log_path()


def _default_observations_path() -> str:
    """TRAVELPLAN_OBSERVATIONS, else observations.db in the repo root (a per-process temp file in an isolated test run)."""
    explicit = os.environ.get("TRAVELPLAN_OBSERVATIONS", "").strip()
    if explicit:
        return explicit
    if isolate_for_tests():
        return os.path.join(test_run_temp_dir(), "observations.db")
    return os.path.join(BASE_DIR, "observations.db")


OBSERVATIONS_PATH = _default_observations_path()


class _LazyObservations:
    """An ObservationStore that opens its SQLite file on first use.

    Importing this module must not create observations.db.
    The file appears with the first recorded observation; reading never creates it.
    """

    def __init__(self, path: str):
        self._path = path
        self._store: Optional[ObservationStore] = None
        self._open_lock = threading.Lock()

    def _opened(self) -> ObservationStore:
        with self._open_lock:
            if self._store is None:
                self._store = ObservationStore(self._path)
            return self._store

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._opened(), name)

    def min_price(self, origin: str, destination: str, travel_date: str, max_age_days: int = 60) -> Optional[float]:
        if self._store is None and (self._path == "off" or not os.path.exists(self._path)):
            return None
        return self._opened().min_price(origin, destination, travel_date, max_age_days=max_age_days)

    def prune(self, max_age_days: int = 60) -> int:
        if self._store is None and (self._path == "off" or not os.path.exists(self._path)):
            return 0
        return self._opened().prune(max_age_days=max_age_days)


class _LazySearchLog:
    """A SearchLog that opens its SQLite file on first use.

    Importing this module (the test suite does it several times) must not create search_log.db.
    The file appears with the first recorded search; reading the log never creates it.
    Public attributes are delegated to the real SearchLog. Set TRAVELPLAN_SEARCH_LOG to move it.
    """

    def __init__(self, path: str):
        self._path = path
        self._log: Optional[SearchLog] = None
        self._open_lock = threading.Lock()

    def _opened(self) -> SearchLog:
        with self._open_lock:
            if self._log is None:
                self._log = SearchLog(self._path)
            return self._log

    def __getattr__(self, name: str):
        # Only reached for names not defined here; private names are never delegated (avoids recursion)
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._opened(), name)

    def recent(self, limit: int = 50):
        if self._log is None and not os.path.exists(self._path):
            return []  # nothing recorded yet; reading must not create the file
        return self._opened().recent(limit)

    def recent_runs(self, limit: int = 20):
        if self._log is None and not os.path.exists(self._path):
            return []
        return self._opened().recent_runs(limit)

    def google_page_loads_since(self, since_iso: str):
        if self._log is None and not os.path.exists(self._path):
            return 0
        return self._opened().google_page_loads_since(since_iso)


def _start_background(target, *args) -> threading.Thread:
    """Run target(*args) on a daemon thread so the caller never waits for network I/O."""
    thread = threading.Thread(target=target, args=args, name="background-task", daemon=True)
    thread.start()
    return thread


def _send_flight_health_alert(text: str) -> None:
    try:
        notifier.send_system_message(text)
    except Exception:
        logger.exception("Flight health alert could not be sent")


def _on_flight_health_change(snapshot: Dict[str, Any]) -> None:
    """Tell the owner (web alerts and Telegram) when the Google Flights scraper becomes unhealthy or recovers.

    Runs inside the flight search that changed the health state, so the Telegram send happens on a
    daemon thread: a slow or unreachable Telegram API must not hold up the search.
    """
    if snapshot.get("healthy"):
        text = "✅ Google Flights araması tekrar çalışıyor."
    else:
        text = (
            f"⚠️ Google Flights okunamıyor: son {snapshot.get('consecutive_failures')} arama başarısız "
            f"({snapshot.get('last_status')}). Sonuçlar eksik olabilir."
        )
    _start_background(_send_flight_health_alert, text)


def _on_flight_threshold_alert(text: str, _metrics: Optional[Dict[str, Any]] = None) -> None:
    _start_background(_send_flight_health_alert, text)


search_log = _LazySearchLog(SEARCH_LOG_PATH)
observations = _LazyObservations(OBSERVATIONS_PATH)
google_flights_provider = GoogleFlightsProvider(
    on_health_change=_on_flight_health_change,
    on_threshold_alert=_on_flight_threshold_alert,
    search_log=search_log,
)
tracker =PriceTracker(storage_path=os.path.join(BASE_DIR, "watches.json"))
notifier = AlertNotifier()
price_history_mgr = PriceHistoryManager(storage_path=os.path.join(BASE_DIR, "price_history.json"))
geo_registry = GeoClusterRegistry()
# One ground-source provider for the whole process: priced live sources (RegioJet, FlixBus, Trenitalia),
# Transitous (opt-in), Google Maps as the fallback when those return nothing, obilet fare hints, and the
# estimate table elsewhere. The router, route engine, trip planner and the daemon all share it, so
# /api/health reports the health of the sources they actually use.
rail_provider = RailBusProvider(
    geo_registry,
    live_provider=LiveRailProvider(),
    bus_provider=FlixBusProvider(),
    trenitalia_provider=TrenitaliaProvider(),
    # Opt-in: stays disabled (no network) unless TRAVELPLAN_TRANSITOUS=1; see transitous_provider.py
    timetable_provider=TransitousProvider(),
    timetable_maps_provider=GoogleMapsTransitProvider(),
    bus_from_price_provider=ObiletFromPrices(),
)
google_flights_provider = wrap_flight_provider(google_flights_provider)
rail_provider = wrap_ground_provider(rail_provider)
multimodal_router = MultiModalRouter(
    flight_provider=google_flights_provider,
    rail_provider=rail_provider,
    geo_registry=geo_registry,
)
google_hotels_provider = GoogleHotelsProvider()
# Chain engine behind every trip path: flights, ground legs (rail/bus) and airport transfers
route_engine = RouteEngine(
    google_flights_provider,
    rail_provider,
    search_log=search_log,
    hotel_provider=google_hotels_provider,
    observations=observations,
)
# Built after the router so the legacy path can use the same flight provider and geo registry
trip_planner = TripPlanner(
    google_flights_provider,
    multimodal_router,
    geo_registry,
    route_engine=route_engine,
    search_log=search_log,
)
# Share the provider so daemon scans and UI searches use one result cache
daemon_tracker = DaemonTracker(
    tracker=tracker,
    notifier=notifier,
    provider=google_flights_provider,
    trip_planner=trip_planner,
    rail_provider=rail_provider,
)


def _parse_trip_date(value: str) -> str:
    if not TRIP_DATE_RE.fullmatch(value):
        raise ValueError(f"Tarih biçimi geçersiz: '{value}'. YYYY-AA-GG biçiminde olmalı (ör. 2026-10-27).")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Tarih geçersiz: '{value}'.")
    return value


def _parse_trip_datetime(value: Any, label: str) -> Optional[str]:
    """Optional YYYY-MM-DDTHH:MM value. Empty or missing means None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} metin olmalı (YYYY-AA-GGTSS:DD).")
    value = value.strip()
    if not value:
        return None
    if not TRIP_DATETIME_RE.fullmatch(value):
        raise ValueError(f"{label} biçimi geçersiz: '{value}'. YYYY-AA-GGTSS:DD olmalı (ör. 2026-10-28T12:00).")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValueError(f"{label} geçersiz: '{value}'.")
    return value


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _parse_via(raw_value: Any) -> Any:
    """Ordered via list from a comma-separated string or a list. Returns None, one code, or a list."""
    if raw_value is None or raw_value == "":
        via_parts: list = []
    elif isinstance(raw_value, str):
        via_parts = raw_value.split(",")
    elif isinstance(raw_value, list):
        via_parts = [str(v) for v in raw_value]
    else:
        raise ValueError("Ara durak biçimi geçersiz.")
    via_codes = list(dict.fromkeys(v.strip() for v in via_parts if v and v.strip()))
    for code in via_codes:
        if "\ufffd" in code:
            raise ValueError(f"Ara durak adında bozuk karakter var: '{code}'. İstek UTF-8 olmalı; İngilizce ad ya da havalimanı kodu da olur (ör. Munich, MUC).")
        if not TRIP_VIA_RE.fullmatch(code):
            raise ValueError(f"Ara durak geçersiz: '{code}'. Ülke veya şehir adı girin (ör. GR).")
    if len(via_codes) == 1:
        return via_codes[0]
    return via_codes or None


def _parse_date_list(raw_value: Any, max_count: int, label: str) -> List[str]:
    """Comma-separated string or list of YYYY-MM-DD dates, de-duplicated, in the given order."""
    if isinstance(raw_value, str):
        items = raw_value.split(",")
    elif isinstance(raw_value, list):
        items = [str(d) for d in raw_value]
    else:
        items = []
    items = list(dict.fromkeys(d.strip() for d in items if d and d.strip()))
    if not items:
        raise ValueError(f"En az bir {label} girilmeli.")
    if len(items) > max_count:
        raise ValueError(f"En fazla {max_count} {label} girilebilir.")
    return [_parse_trip_date(d) for d in items]


def _int_field(raw: Dict[str, Any], name: str, label: str, low: int, high: int) -> Optional[int]:
    value = raw.get(name)
    if _blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} sayı olmalı.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        number = int(value)
    else:
        raise ValueError(f"{label} sayı olmalı.")
    if not low <= number <= high:
        raise ValueError(f"{label} {low} ile {high} arasında olmalı.")
    return number


def _float_field(raw: Dict[str, Any], name: str, label: str, low: float, high: float) -> Optional[float]:
    value = raw.get(name)
    if _blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} sayı olmalı.")
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} sayı olmalı.")
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{label} {low:g} ile {high:g} arasında olmalı.")
    return number


def _flag_field(raw: Dict[str, Any], name: str, label: str) -> Optional[bool]:
    value = raw.get(name)
    if _blank(value):
        return None
    if isinstance(value, bool):
        return value
    flag = str(value).strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{label} için 0 veya 1 girin.")


def _parse_via_on_date(raw_value: Any, via_codes: Optional[Any]) -> Optional[Dict[str, str]]:
    """Minimum stay on date per named stop: 'Yunanistan:2026-10-31;Selanik:2026-10-31' or dict."""
    if _blank(raw_value):
        return None
    example = "Örnek: Yunanistan:2026-10-31;Selanik:2026-10-31"
    if isinstance(raw_value, dict):
        items = list(raw_value.items())
    elif isinstance(raw_value, str):
        items = []
        for part in raw_value.split(";"):
            part = part.strip()
            if not part:
                continue
            place, sep, dt_str = part.rpartition(":")
            if not sep:
                raise ValueError(f"via_on_date biçimi geçersiz: '{part}'. {example}")
            items.append((place.strip(), dt_str.strip()))
    else:
        raise ValueError(f"via_on_date biçimi geçersiz. {example}")

    via_list = []
    if via_codes:
        via_list = [via_codes] if isinstance(via_codes, str) else list(via_codes)

    norm_via = {str(v).strip().casefold() for v in via_list}
    result = {}
    for place, dt_str in items:
        try:
            datetime.strptime(str(dt_str), "%Y-%m-%d")
        except (TypeError, ValueError):
            raise ValueError(f"Geçersiz tarih formatı: '{dt_str}'. YYYY-MM-DD olmalı.")
        norm_place = str(place).strip().casefold()
        if not via_list or norm_place not in norm_via:
            raise ValueError(f"Ara durak eşleşmedi: '{place}'. Sadece aranan ara duraklar ({', '.join(via_list) if via_list else 'yok'}) için tarih girilebilir.")
        result[place] = str(dt_str)
    return result or None


def _min_stay_field(raw: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """Minimum stay per named stop: "Selanik:480;Budapeşte:120" (minutes) or a {place: minutes} dict."""
    value = raw.get("min_stay_mins")
    if _blank(value):
        return None
    example = "Örnek: Selanik:480;Budapeşte:120"
    if isinstance(value, dict):
        pairs = list(value.items())
    elif isinstance(value, str):
        pairs = []
        for part in value.split(";"):
            if not part.strip():
                continue
            place, sep, minutes = part.rpartition(":")
            if not sep:
                raise ValueError(f"Minimum kalış biçimi geçersiz: '{part.strip()}'. {example}")
            pairs.append((place.strip(), minutes.strip()))
    else:
        raise ValueError(f"Minimum kalış biçimi geçersiz. {example}")
    stays: Dict[str, int] = {}
    for place, minutes in pairs:
        place_text = str(place).strip()
        if not TRIP_VIA_RE.fullmatch(place_text):
            raise ValueError(f"Minimum kalış yeri geçersiz: '{place_text}'. {example}")
        if isinstance(minutes, bool):
            raise ValueError(f"Minimum kalış süresi dakika olmalı: '{place_text}'.")
        try:
            mins = int(str(minutes).strip())
        except ValueError:
            raise ValueError(f"Minimum kalış süresi dakika olmalı: '{place_text}'.")
        if not 0 <= mins <= TRIP_MIN_STAY_MINS_MAX:
            raise ValueError(f"Minimum kalış süresi 0 ile {TRIP_MIN_STAY_MINS_MAX} dakika arasında olmalı.")
        stays[place_text] = mins
    return stays or None


def _normalize_trip_query(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the trip rules shared by /api/trip-search and POST /api/watches.

    Accepts depart_dates as a list or a comma-separated string, via as a code or a
    list, and include_side_routes as a bool or 0/1/true/false. Raises ValueError with
    a Turkish message on invalid input.

    Route engine options are optional. A key is returned only when the request gives it, so
    older requests (and watches stored before the engine) keep their exact shape. The keys are
    the TripPlanner.plan_trip keyword names: max_stops, flight_search_budget, min_stay_mins,
    cabin_bag, checked_bag, real_data_only, flex_days, return_dates, return_arrive_by,
    return_depart_after, return_via, min_stay_hours.
    """
    depart_dates = _parse_date_list(raw.get("depart_dates"), TRIP_MAX_DEPART_DATES, "kalkış tarihi")

    arrive_by = _parse_trip_datetime(raw.get("arrive_by"), "Varış zamanı")
    depart_after = _parse_trip_datetime(raw.get("depart_after"), "En erken kalkış zamanı")
    if arrive_by and depart_after and depart_after >= arrive_by:
        raise ValueError("En erken kalkış zamanı, varış zamanından önce olmalı.")

    via = _parse_via(raw.get("via"))

    flag_raw = raw.get("include_side_routes")
    if flag_raw is None or flag_raw == "":
        include_side_routes = True
    elif isinstance(flag_raw, bool):
        include_side_routes = flag_raw
    else:
        flag = str(flag_raw).strip().lower()
        if flag in ("1", "true", "yes", "on"):
            include_side_routes = True
        elif flag in ("0", "false", "no", "off"):
            include_side_routes = False
        else:
            raise ValueError("include_side_routes için 0 veya 1 girin.")

    trip: Dict[str, Any] = {
        "depart_dates": depart_dates,
        "arrive_by": arrive_by,
        "depart_after": depart_after,
        "via": via,
        "include_side_routes": include_side_routes,
    }

    max_stops = _int_field(raw, "max_stops", "Ara durak sayısı", 0, TRIP_MAX_STOPS)
    if max_stops is not None:
        trip["max_stops"] = max_stops
    budget = _int_field(raw, "flight_search_budget", "Arama bütçesi", *TRIP_BUDGET_RANGE)
    if budget is not None:
        trip["flight_search_budget"] = budget
    min_stay = _min_stay_field(raw)
    if min_stay is not None:
        trip["min_stay_mins"] = min_stay
    via_on_date = _parse_via_on_date(raw.get("via_on_date"), via)
    if via_on_date is not None:
        trip["via_on_date"] = via_on_date
    for name, label in (("cabin_bag", "Kabin bagajı"), ("checked_bag", "Kayıtlı bagaj"),
                        ("real_data_only", "Sadece gerçek veriler"),
                        ("schengen_single_entry", "Tek girişli Schengen")):
        flag = _flag_field(raw, name, label)
        if flag is not None:
            trip[name] = flag
    flex_days = _int_field(raw, "flex_days", "Esnek tarih günü", 0, TRIP_MAX_FLEX_DAYS)
    if flex_days is not None:
        trip["flex_days"] = flex_days

    # Return leg rules need a return date; a stray return rule is an error rather than silently ignored
    return_raw = {name: raw.get(name) for name in ("return_dates", "return_arrive_by", "return_depart_after",
                                                   "return_via", "min_stay_hours", "return_via_on_date")}
    if not _blank(return_raw["return_dates"]):
        trip["return_dates"] = _parse_date_list(
            return_raw["return_dates"], TRIP_MAX_RETURN_DATES, "dönüş tarihi")
        return_arrive = _parse_trip_datetime(return_raw["return_arrive_by"], "Dönüş varış zamanı")
        return_depart = _parse_trip_datetime(return_raw["return_depart_after"], "Dönüş en erken kalkış zamanı")
        if return_arrive and return_depart and return_depart >= return_arrive:
            raise ValueError("Dönüş en erken kalkış zamanı, dönüş varış zamanından önce olmalı.")
        if return_arrive:
            trip["return_arrive_by"] = return_arrive
        if return_depart:
            trip["return_depart_after"] = return_depart
        return_via = _parse_via(return_raw["return_via"])
        if return_via is not None:
            trip["return_via"] = return_via
        stay_hours = _float_field(raw, "min_stay_hours", "Minimum kalış (saat)", 0.0, TRIP_MIN_STAY_HOURS_MAX)
        if stay_hours is not None:
            trip["min_stay_hours"] = stay_hours
        return_vod = _parse_via_on_date(return_raw.get("return_via_on_date"), return_via)
        if return_vod is not None:
            trip["return_via_on_date"] = return_vod
    else:
        stray = [name for name in ("return_arrive_by", "return_depart_after", "return_via", "min_stay_hours", "return_via_on_date")
                 if not _blank(return_raw[name])]
        if stray:
            raise ValueError("Dönüş kuralları için dönüş tarihi girin (dönüş tarihi olmadan kullanılamaz).")
    return trip


def _validate_place(value: str, label: str) -> str:
    if not value:
        raise ValueError(f"{label} zorunludur.")
    if len(value) > TRIP_PLACE_MAX_LEN:
        raise ValueError(f"{label} en fazla {TRIP_PLACE_MAX_LEN} karakter olmalı.")
    return value


def _text_field(raw: Any, label: str) -> str:
    """Stripped text from a JSON or query value. Missing (None) is "", any other non-string is rejected."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{label} metin olmalı.")
    return raw.strip()


def _required_place(raw: Any, label: str) -> str:
    """A city or airport the request must name. Missing or empty raises ValueError (HTTP 400)."""
    return _validate_place(_text_field(raw, label), label)


def _required_date(raw: Any, label: str) -> str:
    """A YYYY-MM-DD date the request must name. Missing or malformed raises ValueError (HTTP 400)."""
    text = _text_field(raw, label)
    if not text:
        raise ValueError(f"{label} zorunludur (YYYY-AA-GG).")
    return _parse_trip_date(text)


def _chat_id_field(raw: Any) -> str:
    """Telegram chat id: a string, or a JSON integer (stored as text). Other types are rejected."""
    if isinstance(raw, bool):
        raise ValueError("Chat ID metin veya tam sayı olmalı.")
    if isinstance(raw, int):
        return str(raw)
    return _text_field(raw, "Chat ID")


def _json_safe(value: Any) -> Any:
    """Copy with NaN/inf floats replaced by None (json.dumps would emit invalid JSON for them)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _health_payload() -> Dict[str, Any]:
    """Body of GET /api/health: flight scraper health, ground sources, EUR/TRY rate, watch counts, last searches."""
    rate, source, is_live = get_eur_try_rate()
    try:
        recent = search_log.recent(10)
    except Exception as e:
        logger.warning(f"Search log could not be read: {e}")
        recent = []
    try:
        today_00_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        search_runs_data = {
            "recent": list(search_log.recent_runs(20)),
            "google_page_loads_today": int(search_log.google_page_loads_since(today_00_utc)),
        }
    except Exception as e:
        logger.warning(f"Search runs could not be read: {e}")
        search_runs_data = {"recent": [], "google_page_loads_today": 0}
    watches = list(tracker.watches.values())
    return _json_safe({
        "flights": google_flights_provider.health_status(),
        "fx": {"rate": rate, "source": source, "is_live": bool(is_live)},
        "watches": {"active": sum(1 for w in watches if w.active), "total": len(watches)},
        "search_log": {"recent": recent},
        "search_runs": search_runs_data,
        "resources": google_flights_provider.resource_status(),
        "ground_sources": rail_provider.sources_health(),
    })


def _console_log_wanted() -> bool:
    """Console logging only when stderr is a terminal, or TRAVELPLAN_LOG_CONSOLE=1 forces it.

    Under launchd (and pythonw.exe) stderr is not a terminal, so the log goes to logs/server.log only.
    """
    if os.environ.get("TRAVELPLAN_LOG_CONSOLE", "").strip() == "1":
        return sys.stderr is not None
    try:
        return sys.stderr is not None and sys.stderr.isatty()
    except (AttributeError, ValueError, OSError):  # no stream, or a closed one
        return False


def _configure_logging() -> None:
    """Log to logs/server.log (rotating, 5 MB x 3) and to the console when _console_log_wanted().

    Called from __main__ only, never at import.
    """
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "server.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if _console_log_wanted():
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)

    def _log_uncaught(exc_type, exc, tb):
        logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _log_uncaught


class ThreadingTravelPlanServer(http.server.ThreadingHTTPServer):
    # Each request runs in its own thread so a long Playwright search does not block others
    allow_reuse_address = True
    daemon_threads = True


class TravelPlanAPIHandler(http.server.BaseHTTPRequestHandler):
    # Not SimpleHTTPRequestHandler: static files are served only from STATIC_FILES

    def _send_json(self, status: int, data: Any):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _origin_allowed(self) -> bool:
        """Allow requests without Origin (same-origin UI, curl) or Origin on localhost:PORT."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            parsed = urllib.parse.urlparse(origin)
            # HOST and ALLOWED_HOSTS are accepted too (safe now that _host_allowed pins the Host header)
            return _known_host(parsed.hostname) and parsed.port == PORT
        except ValueError:
            return False

    def _host_allowed(self) -> bool:
        """Reject requests whose Host is not this server (blocks DNS-rebinding reads)."""
        host = self.headers.get("Host")
        if not host:
            return False
        hostname = urllib.parse.urlsplit(f"//{host}").hostname
        return _known_host(hostname)

    def _serve_static(self, filename: str, content_type: str):
        try:
            with open(os.path.join(BASE_DIR, filename), "rb") as f:
                body = f.read()
        except OSError:
            self._send_json(404, {"error": "Sayfa bulunamadı"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The UI is edited in place: always revalidate instead of serving a stale copy
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        from travel_engine.google_budget import set_current_priority, PRIORITY_INTERACTIVE
        set_current_priority(PRIORITY_INTERACTIVE)
        if not self._host_allowed():
            self._send_json(403, {"error": "Geçersiz Host başlığı"})
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/live-flights":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                origin = _required_place((query.get("origin") or [None])[0], "Kalkış şehri")
                destination = _required_place((query.get("destination") or [None])[0], "Varış şehri")
                dep_date = _required_date((query.get("departure_date") or [None])[0], "Gidiş tarihi")
                trip_type = (query.get("trip_type") or ["roundtrip"])[0]
                ret_date = None
                if trip_type != "oneway":
                    ret_date = _required_date((query.get("return_date") or [None])[0], "Dönüş tarihi")
            except ValueError as e:
                self._send_json(400, {"status": "error", "message": str(e)})
                return

            live_results = google_flights_provider.search_flights(
                origin=origin,
                destination=destination,
                departure_date=dep_date,
                return_date=ret_date if trip_type != "oneway" else None,
                trip_type=trip_type,
            )
            cheapest_val = float(live_results[0]["total_price_try"]) if live_results else 0.0
            cheapest_airline = live_results[0]["airline"] if live_results else "Havayolu"

            if live_results and cheapest_val > 0:
                price_history_mgr.record_price(
                    origin=origin,
                    destination=destination,
                    dep_date=dep_date,
                    trip_type=trip_type,
                    min_price=cheapest_val,
                    airline=cheapest_airline,
                )

            # Retrieve real Google Flights calendar prices if available
            cal_prices = google_flights_provider.get_calendar_prices(origin, destination, trip_type)
            if cal_prices:
                for c_date, c_price in cal_prices.items():
                    price_history_mgr.record_price(
                        origin=origin,
                        destination=destination,
                        dep_date=c_date,
                        trip_type=trip_type,
                        min_price=c_price,
                        airline="Google Flights",
                    )

            history_points = price_history_mgr.get_history(
                origin=origin,
                destination=destination,
                dep_date=dep_date,
                trip_type=trip_type,
                current_price=cheapest_val,
                current_airline=cheapest_airline,
            )

            date_matrix = price_history_mgr.get_date_matrix(
                origin=origin,
                destination=destination,
                dep_date=dep_date,
                trip_type=trip_type,
                current_min_price=cheapest_val,
                cache_lookup=google_flights_provider.cache,
                calendar_lookup=cal_prices,
                current_airline=cheapest_airline,
            )

            self._send_json(200, {
                "origin": origin,
                "destination": destination,
                "departure_date": dep_date,
                "return_date": ret_date if trip_type != "oneway" else None,
                "trip_type": trip_type,
                "count": len(live_results),
                "flights": live_results,
                "price_history": history_points,
                "date_matrix": date_matrix,
            })
            return

        elif path == "/api/price-history":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                origin = _required_place((query.get("origin") or [None])[0], "Kalkış şehri")
                destination = _required_place((query.get("destination") or [None])[0], "Varış şehri")
                dep_date = _required_date((query.get("departure_date") or [None])[0], "Gidiş tarihi")
            except ValueError as e:
                self._send_json(400, {"status": "error", "message": str(e)})
                return
            trip_type = query.get("trip_type", ["oneway"])[0]
            try:
                cur_price = float(query.get("current_price", [0])[0]) if query.get("current_price") else None
            except (ValueError, TypeError):
                cur_price = None
            airline = query.get("airline", ["Havayolu"])[0]
            try:
                days_range = int(query.get("days", [1])[0])
            except (ValueError, TypeError):
                days_range = 1

            history_points = price_history_mgr.get_history(
                origin=origin,
                destination=destination,
                dep_date=dep_date,
                trip_type=trip_type,
                current_price=cur_price,
                current_airline=airline,
                days_range=days_range,
            )
            self._send_json(200, {
                "origin": origin,
                "destination": destination,
                "departure_date": dep_date,
                "trip_type": trip_type,
                "days_range": days_range,
                "history": history_points,
            })
            return

        elif path == "/api/price-matrix":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                origin = _required_place((query.get("origin") or [None])[0], "Kalkış şehri")
                destination = _required_place((query.get("destination") or [None])[0], "Varış şehri")
                dep_date = _required_date((query.get("departure_date") or [None])[0], "Gidiş tarihi")
            except ValueError as e:
                self._send_json(400, {"status": "error", "message": str(e)})
                return
            trip_type = query.get("trip_type", ["oneway"])[0]
            try:
                cur_price = float(query.get("current_price", [30000])[0]) if query.get("current_price") else 30000.0
            except (ValueError, TypeError):
                cur_price = 30000.0
            cur_airline = query.get("airline", [None])[0]

            cal_prices = google_flights_provider.get_calendar_prices(origin, destination, trip_type)

            matrix_data = price_history_mgr.get_date_matrix(
                origin=origin,
                destination=destination,
                dep_date=dep_date,
                trip_type=trip_type,
                current_min_price=cur_price,
                cache_lookup=google_flights_provider.cache,
                calendar_lookup=cal_prices,
                current_airline=cur_airline,
            )
            self._send_json(200, matrix_data)
            return

        elif path == "/api/trip-search":
            query = urllib.parse.parse_qs(parsed.query)

            def _q(name: str) -> str:
                return (query.get(name) or [""])[0].strip()

            try:
                origin = _validate_place(_q("origin"), "Kalkış şehri")
                destination = _validate_place(_q("destination"), "Varış şehri")
                trip = _normalize_trip_query({
                    "depart_dates": _q("depart_dates"),
                    "arrive_by": _q("arrive_by"),
                    "depart_after": _q("depart_after"),
                    "via": _q("via"),
                    "include_side_routes": query.get("include_side_routes", [None])[0],
                    # Route engine rules; URL names differ from the planner's keyword names
                    "max_stops": _q("max_stops"),
                    "flight_search_budget": _q("budget"),
                    "min_stay_mins": _q("min_stay"),
                    "cabin_bag": _q("cabin_bag"),
                    "checked_bag": _q("checked_bag"),
                    "real_data_only": _q("real_data_only"),
                    "schengen_single_entry": _q("schengen_single_entry"),
                    "flex_days": _q("flex_days"),
                    "return_dates": _q("return_dates"),
                    "return_arrive_by": _q("return_arrive_by"),
                    "return_depart_after": _q("return_depart_after"),
                    "return_via": _q("return_via"),
                    "min_stay_hours": _q("min_stay_hours"),
                    "via_on_date": _q("via_on_date"),
                    "return_via_on_date": _q("return_via_on_date"),
                })
                tv_raw = _q("time_value_try_per_hour")
                time_value = 150.0
                if tv_raw:
                    time_value = float(tv_raw)
                    if not (0 <= time_value < float("inf")):
                        raise ValueError("Zaman değeri 0 veya daha büyük bir sayı olmalı.")
            except ValueError as e:
                self._send_json(400, {"status": "error", "message": str(e)})
                return

            try:
                result = trip_planner.plan_trip(
                    origin=origin,
                    destination=destination,
                    time_value_try_per_hour=time_value,
                    **trip,
                )
            except Exception as e:
                logger.error(f"Trip search error: {e}")
                self._send_json(500, {
                    "status": "error",
                    "message": "Yolculuk araması sırasında hata oluştu.",
                    "error": str(e),
                })
                return

            try:
                search_log.record_run(
                    origin=origin,
                    destination=destination,
                    depart_dates=trip.get("depart_dates"),
                    trace=result.get("trace") if isinstance(result, dict) else None,
                )
            except Exception as e:
                logger.warning(f"Failed to record search run: {e}")

            try:
                logger.info("trip_search_trace %s", json.dumps({
                    "origin": origin, "destination": destination,
                    "depart_dates": trip.get("depart_dates"), "trace": result.get("trace"),
                }, ensure_ascii=False, default=str))
            except Exception as e:
                logger.warning(f"Failed to log trip search trace: {e}")

            self._send_json(200, result)
            return

        elif path == "/api/tour-explore":
            query = urllib.parse.parse_qs(parsed.query)

            def _q(name: str) -> str:
                return (query.get(name) or [""])[0].strip()

            try:
                origin = _validate_place(_q("origin"), "Kalkış şehri")
                destination = _validate_place(_q("destination"), "Varış şehri")
                depart_dates = _parse_date_list(_q("depart_dates"), TRIP_MAX_DEPART_DATES, "kalkış tarihi")
                arrive_by = _parse_trip_datetime(_q("arrive_by"), "Varış zamanı")
                depart_after = _parse_trip_datetime(_q("depart_after"), "En erken kalkış zamanı")
                if arrive_by and depart_after and depart_after >= arrive_by:
                    raise ValueError("En erken kalkış zamanı, varış zamanından önce olmalı.")

                raw_params = {k: (v[0] if v else "") for k, v in query.items()}
                cand_raw = _q("candidates")
                candidates = [c.strip() for c in cand_raw.split(",") if c.strip()] if cand_raw else None

                min_stay = _int_field(raw_params, "min_stay", "min_stay", 0, 2880)
                if min_stay is None:
                    min_stay = 240

                budget = _int_field(raw_params, "budget_per_candidate", "budget_per_candidate", 1, 20)
                if budget is None:
                    budget = 8

                max_candidates = _int_field(raw_params, "max_candidates", "max_candidates", 1, 12)
                if max_candidates is None:
                    max_candidates = 8
            except ValueError as e:
                self._send_json(400, {"status": "error", "message": str(e)})
                return

            try:
                from travel_engine.tour_explore import explore_tour
                result = explore_tour(
                    planner=trip_planner,
                    origin=origin,
                    destination=destination,
                    depart_dates=depart_dates,
                    arrive_by=arrive_by,
                    depart_after=depart_after,
                    candidates=candidates,
                    min_stay_mins=min_stay,
                    budget_per_candidate=budget,
                    max_candidates=max_candidates,
                )
                self._send_json(200, result)
            except Exception as e:
                logger.error(f"Tour explore error: {e}")
                self._send_json(500, {
                    "status": "error",
                    "message": "Tur keşfi sırasında hata oluştu.",
                    "error": str(e),
                })
            return

        elif path == "/api/clusters":
            cities = geo_registry.list_cities()
            cluster_data = []
            for c in cities:
                cl = geo_registry.get_cluster(c)
                if cl:
                    cluster_data.append({
                        "city": cl.city_name,
                        "primary_airport": cl.primary_airport,
                        "main_train_station": cl.main_train_station,
                        "catchment_airports": [
                            {
                                "code": a.airport_code,
                                "city": a.city_name,
                                "transfer_mode": a.transfer_mode,
                                "avg_duration_mins": a.avg_duration_mins,
                                "avg_price_try": a.avg_price_try,
                            } for a in cl.catchment_airports
                        ]
                    })
            self._send_json(200, {"count": len(cluster_data), "clusters": cluster_data})
            return

        elif path == "/api/watches":
            self._send_json(200, {
                "watches": [w.to_dict() for w in tracker.watches.values()]
            })
            return

        elif path == "/api/alerts":
            self._send_json(200, {
                "alerts": notifier.get_alerts()
            })
            return

        elif path == "/api/health":
            self._send_json(200, _health_payload())
            return

        elif path == "/api/settings/telegram":
            cfg = notifier.config
            tok = cfg.get("telegram_bot_token", "")
            masked = (tok[:5] + "..." + tok[-4:]) if len(tok) > 10 else ("***" if tok else "")
            self._send_json(200, {
                "has_token": bool(tok),
                "masked_token": masked,
                "chat_id": cfg.get("telegram_chat_id", ""),
                "enabled": cfg.get("notifications_enabled", True),
            })
            return

        # Exact-match whitelist: traversal variants never equal a key, so they 404
        if path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            self._serve_static(filename, content_type)
            return

        if path.startswith("/api/"):
            self._send_json(404, {"error": "Endpoint bulunamadı"})
        else:
            self._send_json(404, {"error": "Sayfa bulunamadı"})

    def do_POST(self):
        from travel_engine.google_budget import set_current_priority, PRIORITY_INTERACTIVE
        set_current_priority(PRIORITY_INTERACTIVE)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Block cross-site POSTs (other websites changing Telegram settings, watches, ...)
        if not self._host_allowed() or not self._origin_allowed():
            self._send_json(403, {"status": "error", "message": "Bu kaynaktan gelen istek kabul edilmiyor."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._send_json(400, {"status": "error", "message": "Geçersiz Content-Length."})
            return
        if content_length < 0:
            self._send_json(400, {"status": "error", "message": "Geçersiz Content-Length."})
            return
        if content_length > MAX_BODY_BYTES:
            self._send_json(413, {"status": "error", "message": "İstek gövdesi çok büyük (en fazla 1 MB)."})
            return

        post_data = self.rfile.read(content_length) if content_length else b""
        try:
            payload = json.loads(post_data.decode("utf-8")) if post_data else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"status": "error", "message": "Geçersiz JSON gövdesi."})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"status": "error", "message": "JSON gövdesi bir nesne olmalı."})
            return

        if path == "/api/watches":
            try:
                origin = _required_place(payload.get("origin"), "Kalkış şehri")
                destination = _required_place(payload.get("destination"), "Varış şehri")
                raw_target = payload.get("target_budget_try")
                if raw_target is None:
                    # Trip watch without a side-route budget: 0 means "never alert on side routes"
                    target_budget = 0.0 if payload.get("trip") else 30000.0
                else:
                    target_budget = float(raw_target)
                departure_date = payload.get("departure_date")
                return_date = payload.get("return_date")
                trip_type = payload.get("trip_type", "oneway")
                current_price = float(payload.get("current_price")) if payload.get("current_price") else None
                max_transfers = int(payload.get("max_transfers", 3))
                departure_time_slot = payload.get("departure_time_slot", "all")
                time_window_start = payload.get("time_window_start")
                time_window_end = payload.get("time_window_end")
                include_multimodal = bool(payload.get("include_multimodal", True))

                # Optional trip rules; target_budget_try is then the side-route budget
                trip_query = None
                if payload.get("trip") is not None:
                    if not isinstance(payload["trip"], dict):
                        raise ValueError("trip bir nesne olmalı.")
                    trip_query = _normalize_trip_query(payload["trip"])
                direct_budget = None
                if payload.get("direct_budget_try") not in (None, ""):
                    direct_budget = float(payload["direct_budget_try"])
                    if not math.isfinite(direct_budget) or direct_budget < 0:
                        raise ValueError("direct_budget_try 0 veya daha büyük bir sayı olmalı.")

                watch = tracker.add_watch(
                    origin=origin,
                    destination=destination,
                    target_budget_try=target_budget,
                    departure_date=departure_date,
                    return_date=return_date,
                    trip_type=trip_type,
                    max_transfers=max_transfers,
                    departure_time_slot=departure_time_slot,
                    time_window_start=time_window_start,
                    time_window_end=time_window_end,
                    current_price=current_price,
                    include_multimodal=include_multimodal,
                    trip_query=trip_query,
                    direct_budget_try=direct_budget,
                )

                # Trigger immediate background scan and alert evaluation
                import threading
                threading.Thread(target=daemon_tracker.scan_watch, args=(watch,), daemon=True).start()

                self._send_json(201, {
                    "status": "success",
                    "message": "Fiyat takibi başlatıldı.",
                    "watch": watch.to_dict(),
                })
            except (ValueError, TypeError) as e:
                self._send_json(400, {"status": "error", "message": f"Geçersiz veri: {str(e)}"})
            return

        elif path == "/api/watches/delete":
            wid = payload.get("id")
            if wid and tracker.delete_watch(wid):
                self._send_json(200, {"status": "success", "message": "Takip silindi."})
            else:
                self._send_json(404, {"status": "error", "message": "Takip kaydı bulunamadı."})
            return

        elif path == "/api/alerts/clear":
            notifier.clear_alerts()
            self._send_json(200, {"status": "success", "message": "Bildirim geçmişi temizlendi."})
            return

        elif path == "/api/tracker/scan-now":
            results = daemon_tracker.scan_all_watches()
            self._send_json(200, {
                "status": "success",
                "scanned_count": len(results),
                "results": results,
            })
            return

        elif path == "/api/settings/telegram":
            try:
                bot_token = _text_field(payload.get("bot_token"), "Bot token")
                chat_id = _chat_id_field(payload.get("chat_id"))
            except ValueError as e:
                self._send_json(400, {"status": "error", "message": str(e)})
                return
            enabled = bool(payload.get("enabled", True))
            test_mode = bool(payload.get("test", False))

            current_cfg = notifier.config
            effective_token = bot_token if (bot_token and not bot_token.startswith("***")) else current_cfg.get("telegram_bot_token", "")
            effective_chat = chat_id if chat_id else current_cfg.get("telegram_chat_id", "")

            notifier.save_config(effective_token, effective_chat, enabled)

            if test_mode:
                test_res = notifier.test_telegram_connection()
                self._send_json(200, {
                    "status": "success",
                    "saved": True,
                    "test_result": test_res,
                })
            else:
                self._send_json(200, {
                    "status": "success",
                    "saved": True,
                    "message": "Telegram ayarları kaydedildi.",
                })
            return

        self._send_json(404, {"error": "Endpoint bulunamadı"})


DEFAULT_SCAN_INTERVAL_SECS = 3600 * 6
MIN_SCAN_INTERVAL_SECS = 900


def _scan_interval_from_env() -> float:
    """Watch scan interval in seconds from TRAVELPLAN_SCAN_INTERVAL: 6 hours when unset or invalid, at least 15 minutes.

    Every scan spends Google pages per watch, so a short interval raises the risk of being blocked.
    """
    raw = os.environ.get("TRAVELPLAN_SCAN_INTERVAL", "").strip()
    if not raw:
        return float(DEFAULT_SCAN_INTERVAL_SECS)
    try:
        value = float(raw)
    except ValueError:
        logger.warning("TRAVELPLAN_SCAN_INTERVAL=%r is not a number; using %d s", raw, DEFAULT_SCAN_INTERVAL_SECS)
        return float(DEFAULT_SCAN_INTERVAL_SECS)
    if not math.isfinite(value):
        logger.warning("TRAVELPLAN_SCAN_INTERVAL=%r is not finite; using %d s", raw, DEFAULT_SCAN_INTERVAL_SECS)
        return float(DEFAULT_SCAN_INTERVAL_SECS)
    if value < MIN_SCAN_INTERVAL_SECS:
        logger.warning("TRAVELPLAN_SCAN_INTERVAL=%s is below %d s; using %d s", raw, MIN_SCAN_INTERVAL_SECS,
                       MIN_SCAN_INTERVAL_SECS)
        return float(MIN_SCAN_INTERVAL_SECS)
    return value


def _run_daemon_worker(
    tracker_inst=None,
    daemon_inst=None,
    now_fn=None,
    sleep_fn=None,
    stop_event=None,
    initial_delay: float = 10.0,
    tick_secs: float = 30.0,
    scan_interval: float = 3600 * 6,
    max_ticks: Optional[int] = None,
    state_store=None,
):
    import time
    from travel_engine.daemon_tracker import is_in_prefetch_window, is_prefetch_enabled
    from travel_engine.storage import set_meta

    tr = tracker_inst if tracker_inst is not None else tracker
    dt = daemon_inst if daemon_inst is not None else daemon_tracker
    sleep = sleep_fn if sleep_fn is not None else time.sleep
    now_func = now_fn if now_fn is not None else datetime.now

    if initial_delay > 0:
        sleep(initial_delay)

    last_scan_time = 0.0
    if state_store is not None:
        try:
            val = state_store.get_meta("daemon:last_scan_at")
            if val is not None:
                last_scan_time = float(val)
        except Exception as e:
            logger.warning("[DaemonWorker] Error reading daemon:last_scan_at from store: %s", e)
            last_scan_time = 0.0

    last_prefetch_date: Optional[str] = None
    ticks = 0

    while stop_event is None or not stop_event.is_set():
        if max_ticks is not None and ticks >= max_ticks:
            break
        ticks += 1

        current_dt = now_func()
        current_ts = current_dt.timestamp() if hasattr(current_dt, "timestamp") else time.time()
        today_str = current_dt.strftime("%Y-%m-%d")

        # 1. 6-hour scan of active watches
        if current_ts - last_scan_time >= scan_interval:
            try:
                active_count = len([w for w in tr.watches.values() if w.active])
                if active_count > 0:
                    logger.info("[DaemonWorker] Scanning %d active watches...", active_count)
                    dt.scan_all_watches()
                last_scan_time = current_ts
                if state_store is not None:
                    try:
                        with state_store.transaction() as conn:
                            set_meta(conn, "daemon:last_scan_at", str(current_ts))
                    except Exception as e:
                        logger.warning("[DaemonWorker] Error writing daemon:last_scan_at to store: %s", e)
            except Exception as e:
                logger.error("[DaemonWorker Error] %s", e)

        # 2. Nightly prefetch window check (once per day)
        if is_prefetch_enabled():
            window = os.environ.get("TRAVELPLAN_PREFETCH_WINDOW", "03:00-05:00")
            if is_in_prefetch_window(current_dt, window):
                if last_prefetch_date != today_str:
                    last_prefetch_date = today_str
                    try:
                        logger.info("[DaemonWorker] Running nightly prefetch for trip watches...")
                        dt.prefetch_watches(now=current_dt)
                    except Exception as e:
                        logger.error("[DaemonWorker Prefetch Error] %s", e)

        if stop_event is not None and stop_event.is_set():
            break
        sleep(tick_secs)


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    _configure_logging()

    # Startup retention prune on observation store
    try:
        observations.prune()
    except Exception as e:
        logger.warning("[Observations] Startup prune failed: %s", e)

    # Start 7/24 background price monitoring loop
    import threading
    scan_interval = _scan_interval_from_env()
    logger.info("Watch scan interval: %.0f s", scan_interval)
    daemon_thread = threading.Thread(
        target=_run_daemon_worker,
        kwargs={"state_store": getattr(tracker, "_store", None), "scan_interval": scan_interval},
        daemon=True,
    )
    daemon_thread.start()

    # Telegram commands (/ara Atina Prag 28.10) for searching from the phone; idles until configured
    from travel_engine.telegram_bot import TelegramCommandBot
    telegram_commands = TelegramCommandBot(
        notifier,
        google_flights_provider,
        multimodal_router,
        tracker=tracker,
        trip_planner=trip_planner,
        health_provider=google_flights_provider,
    )
    telegram_commands.start_in_thread()

    logger.info("[TravelPlan v2] Server running at: http://%s:%s", HOST, PORT)
    with ThreadingTravelPlanServer((HOST, PORT), TravelPlanAPIHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("[TravelPlan v2] Server stopped.")
