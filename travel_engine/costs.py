"""Real trip cost and connection-risk helpers (ROADMAP Faz 2c).

Pure helpers for the side-route engine. The only external input is the EUR -> TRY
rate from ``travel_engine.fx.get_eur_try_rate()``, read at call time (fx caches it).
No network access happens here. Scope: Europe and Turkey, one traveller, TRY prices.

Every table in this module holds ROUGH ESTIMATES (October 2026) that should be
reviewed. Any value derived from a table, or from a distance estimate, is reported
with ``is_estimated=True`` so callers can label it "tahmini".

Public API
    baggage_fee_try(airline, cabin_bag=True, checked_bag=False) -> (fee_try, is_estimated)
    airport_transfer(iata, at_time=None) -> {duration_mins, price_try, mode, label, is_estimated, note}
    hotel_estimate_try(place_or_iata) -> (price_try, is_estimated)
    connection_risk(arrive_at, depart_at, separate_tickets, next_mode, min_buffer_mins,
                    is_last_service=False, crosses_night=False) -> {level, score, reasons}
    journey_risk(legs, separate_tickets) -> {level, score, reasons, connections}
    cost_breakdown(legs, hotel_nights=0, hotel_place=None, cabin_bag=True, checked_bag=False,
                   real_data_only=False)
        -> {tickets, baggage, transfers, hotel, total, estimated_parts, unknown_ticket_legs,
            unknown_parts, price_complete}

Rules are documented next to each function. Leg dicts follow the trip option shape:
``type`` ("flight", "transfer", or a ground mode such as "train"/"bus"), ``operator``,
``from``, ``to``, ``depart_at``, ``arrive_at`` ("YYYY-MM-DDTHH:MM"), ``price_try``,
``is_estimated``. ``price_try`` may be None: the price is unknown (a real timetable without a fare).
An unknown price is never counted as 0 and never replaced by an estimate; the total then is a lower
bound (``price_complete`` False).

real_data_only=True (the owner's "Sadece gerçek veriler" rule) uses no estimate at all: a transfer whose
price is an estimate is not counted (listed in ``unknown_parts``), a baggage fee that is an estimate is not
counted (``unknown_parts``), and a hotel night (always an estimate) is not counted and is listed in
``estimated_parts`` so the caller drops the option.

Time zones: when a leg carries ``depart_utc`` / ``arrive_utc`` (ISO with Z, from the route engine), the
gaps are computed on those instants, and the night window is read in the waiting place's zone
(``arrive_tz``). Legs without them are read as naive local times, as before.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from travel_engine import fx
from travel_engine.airports import METRO_GROUPS, AirportDB, haversine_km

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FALLBACK_TRANSFER_MINS = 45
FALLBACK_TRANSFER_EUR = 8.0
FALLBACK_HOTEL_EUR = 60.0

# Distance rule: minutes = 25 + km * 1.5 ; EUR = max(3, km * 0.15).
DISTANCE_BASE_MINS = 25
DISTANCE_MINS_PER_KM = 1.5
DISTANCE_EUR_PER_KM = 0.15
DISTANCE_MIN_EUR = 3.0
# A "city centre" closer than this to the airport is not a usable reference point.
CENTRE_MIN_DISTANCE_KM = 3.0

NIGHT_START = time(0, 0)
NIGHT_END = time(6, 0)

# Public transport is assumed unavailable between these times (ESTIMATE): the airport transfer becomes a taxi.
NIGHT_TRANSPORT_START = time(0, 30)
NIGHT_TRANSPORT_END = time(5, 30)
# Taxi estimate: EUR per km of airport -> city distance, minimum fare, and duration vs. the public transfer.
TAXI_EUR_PER_KM = 1.75
TAXI_MIN_EUR = 25.0
TAXI_DURATION_FACTOR = 0.8

# Human label for a transfer leg, by the internal ``mode`` of airport_transfer(). Never the raw word "estimated".
TRANSFER_MODE_LABELS: Dict[str, str] = {
    "bus": "otobüs",
    "metro": "metro",
    "train": "tren",
    "taxi": "taksi",
    "estimated": "Tahmini transfer",
    "unknown": "Tahmini transfer",
}

# ---------------------------------------------------------------------------
# Baggage (ESTIMATES, EUR, one-way, per passenger, online prices, Oct 2026)
# ---------------------------------------------------------------------------

# canonical carrier -> (cabin trolley EUR, checked bag up to 20 kg EUR)
# Values are rough typical online add-on prices (ESTIMATES, Oct 2026). Pegasus and AJet cheapest
# fares (Basic / Hafif) include only a small under-seat item; a cabin trolley is an extra fee, so it
# is priced here as an estimate (25 EUR) rather than 0. airBaltic is a hybrid carrier and is listed
# with low-cost fees as a rough approximation.
LOW_COST_BAG_EUR: Dict[str, Tuple[float, float]] = {
    "Ryanair": (35.0, 40.0),
    "Wizz Air": (30.0, 40.0),
    "easyJet": (30.0, 40.0),
    "Pegasus": (25.0, 30.0),  # cabin trolley: estimate, Basic fare includes under-seat item only
    "AJet": (25.0, 35.0),     # cabin trolley: estimate, cheapest fare includes under-seat item only
    "SunExpress": (0.0, 35.0),
    "Vueling": (30.0, 40.0),
    "Volotea": (25.0, 35.0),
    "Eurowings": (25.0, 35.0),
    "Transavia": (25.0, 35.0),
    "Jet2": (30.0, 40.0),
    "Norwegian": (30.0, 35.0),
    "Smartwings": (25.0, 35.0),
    "airBaltic": (20.0, 30.0),
    "SKY express": (15.0, 25.0),
}

# Carrier not in either table (e.g. Air Serbia, a garbled card string): the same rough mid-market fee
# as an airBaltic-type carrier. Used for every flight leg whose carrier is unknown, so such legs are
# never priced at zero.
UNKNOWN_CARRIER_BAG_EUR: Tuple[float, float] = (20.0, 30.0)

# Lower-case match aliases (matched on word boundaries, case-insensitive).
_LOW_COST_ALIASES: Dict[str, str] = {
    "ryanair": "Ryanair",
    "wizz air": "Wizz Air", "wizzair": "Wizz Air", "wizz": "Wizz Air",
    "easyjet": "easyJet",
    "pegasus": "Pegasus",
    "ajet": "AJet", "anadolujet": "AJet",
    "sunexpress": "SunExpress", "sun express": "SunExpress",
    "vueling": "Vueling",
    "volotea": "Volotea",
    "eurowings": "Eurowings",
    "transavia": "Transavia",
    "jet2": "Jet2",
    "norwegian": "Norwegian",
    "smartwings": "Smartwings",
    "airbaltic": "airBaltic", "air baltic": "airBaltic",
    "sky express": "SKY express", "skyexpress": "SKY express",
}

# Full-service carriers: no bag fee for a cabin or checked bag on the usual fares.
FULL_SERVICE_CARRIERS: Tuple[str, ...] = (
    "Turkish Airlines", "Lufthansa", "Austrian", "Aegean", "Swiss", "LOT",
    "ITA Airways", "KLM", "Air France", "British Airways", "SAS", "Finnair",
    "TAP Air Portugal", "Iberia", "Brussels Airlines", "Icelandair",
)
_FULL_SERVICE_ALIASES: Dict[str, str] = {
    "turkish airlines": "Turkish Airlines", "turkish": "Turkish Airlines",
    "lufthansa": "Lufthansa", "austrian": "Austrian", "aegean": "Aegean",
    "swiss": "Swiss", "lot polish": "LOT", "lot": "LOT",
    "ita airways": "ITA Airways", "klm": "KLM", "air france": "Air France",
    "british airways": "British Airways", "sas": "SAS", "finnair": "Finnair",
    "tap air portugal": "TAP Air Portugal", "tap": "TAP Air Portugal",
    "iberia": "Iberia", "brussels airlines": "Brussels Airlines",
    "icelandair": "Icelandair",
}

# ---------------------------------------------------------------------------
# Airport transfers (ESTIMATES): iata -> (minutes, EUR, mode, note in Turkish)
# ---------------------------------------------------------------------------

AIRPORT_TRANSFERS: Dict[str, Tuple[int, float, str, str]] = {
    # Western and central Europe
    "BVA": (75, 17.0, "bus", "Beauvais shuttle otobüsü (Paris merkezi, ~75 dk)"),
    "CDG": (35, 12.0, "train", "RER B -> Gare du Nord / Châtelet"),
    "ORY": (30, 10.0, "bus", "Orlybus -> Denfert-Rochereau"),
    "BGY": (50, 10.0, "bus", "Bergamo şehir/istasyon otobüsü (Milano için ayrı otobüs ~75 dk)"),
    "MXP": (50, 13.0, "train", "Malpensa Express -> Milano Cadorna"),
    "LIN": (25, 2.0, "bus", "Bus 73 -> Milano merkez (San Babila)"),
    "FCO": (32, 14.0, "train", "Leonardo Express -> Roma Termini"),
    "CIA": (45, 6.0, "bus", "Terravision / SIT otobüs -> Roma Termini"),
    "MAD": (35, 5.0, "metro", "Metro hattı 8 / Exprés otobüs -> merkez"),
    "BCN": (35, 6.0, "bus", "Aerobús -> Plaça Espanya"),
    "GRO": (75, 16.0, "bus", "Sagalés / Aerobús -> Barselona"),
    "REU": (90, 17.0, "bus", "Reus -> Barselona otobüsü"),
    "LHR": (35, 15.0, "train", "Elizabeth line -> Paddington / Liverpool St."),
    "LGW": (35, 27.0, "train", "Gatwick Express -> Victoria"),
    "STN": (50, 23.0, "train", "Stansted Express -> Liverpool St."),
    "LTN": (60, 22.0, "train", "Servis otobüsü + tren (Luton Parkway -> merkez)"),
    "DUB": (30, 7.0, "bus", "Aircoach -> Dublin merkez"),
    "AMS": (17, 6.0, "train", "NS treni -> Amsterdam Centraal"),
    "BRU": (20, 13.0, "train", "Tren -> Brüksel Centraal / Midi"),
    "CRL": (50, 12.0, "bus", "Havalimanı otobüsü -> Brüksel Midi"),
    "BER": (35, 4.0, "train", "FEX / S9 -> Hauptbahnhof / Alexanderplatz"),
    "MUC": (45, 13.0, "train", "S-Bahn S1 / S8 -> Hauptbahnhof"),
    "FRA": (15, 5.5, "train", "S-Bahn S8 / S9 -> Hauptbahnhof"),
    "ZRH": (12, 6.5, "train", "S-Bahn S2 / S24 -> Hauptbahnhof"),
    "VIE": (16, 14.0, "train", "CAT (City Airport Train) -> Wien Mitte"),
    "BTS": (25, 1.5, "bus", "Bus 61 -> Bratislava Hlavná stanica"),
    "PRG": (45, 3.0, "bus", "AE / 119 otobüs -> Hlavní nádraží (metro bağlantısı)"),
    "BUD": (40, 2.5, "bus", "100E ekspres otobüs -> Deák Ferenc"),
    "WAW": (25, 1.5, "train", "S2 hızlı tren -> Warszawa Centralna"),
    "KRK": (20, 4.0, "train", "Balice Ekspres -> Kraków Główny"),
    "OSL": (20, 19.0, "train", "Flytoget -> Oslo S"),
    "TRF": (110, 19.0, "bus", "Flybussen -> Oslo"),
    "ARN": (20, 27.0, "train", "Arlanda Express -> T-Centralen"),
    "NYO": (80, 18.0, "bus", "Flygbussarna -> Stockholm merkez"),
    # Southern and eastern Europe
    "ATH": (40, 10.0, "metro", "Metro hattı 3 -> Syntagma"),
    "SKG": (45, 3.0, "bus", "X1 otobüs -> Selanik merkezi"),
    "NAP": (30, 1.5, "bus", "Alibus -> Napoli Centrale"),
    "FLR": (25, 6.0, "bus", "Volainbus -> Santa Maria Novella"),
    "TRN": (50, 7.0, "bus", "Sadem otobüs -> Torino Porta Nuova"),
    "VCE": (25, 8.0, "bus", "ATVO / ACTV otobüs -> Piazzale Roma"),
    "TSF": (60, 12.0, "bus", "ATVO otobüs -> Venedik / Mestre"),
    "OTP": (45, 2.0, "bus", "Express otobüs -> Gara de Nord"),
    "ZAG": (30, 5.0, "bus", "Havalimanı otobüsü -> Zagreb otogarı"),
    "LJU": (40, 7.0, "bus", "Otobüs -> Ljubljana merkez"),
    "SKP": (25, 3.0, "bus", "Havalimanı otobüsü -> Üsküp merkez"),
    "TIA": (30, 2.0, "bus", "Havalimanı otobüsü -> Tiran merkez"),
    # Turkey
    "IST": (60, 6.0, "metro", "M11 metro + M2 (Gayrettepe / Taksim)"),
    "SAW": (75, 5.0, "bus", "HAVABUS -> Kadıköy / Taksim"),
    "AYT": (50, 3.0, "bus", "Havaş -> Antalya merkez"),
    "ESB": (50, 3.0, "bus", "Havaş -> Ankara merkez (Kızılay)"),
    "ADB": (50, 3.0, "bus", "Havaş -> İzmir merkez"),
}

# Real (official) one-way transfer fares in EUR, by IATA code. The table above holds estimates only; an entry
# here makes that airport's price a real price (price_is_estimated False). Empty: no official source is bundled.
OFFICIAL_TRANSFER_PRICES_EUR: Dict[str, float] = {}

# ---------------------------------------------------------------------------
# Hotels (ESTIMATES): one budget night per country, EUR
# ---------------------------------------------------------------------------

COUNTRY_HOTEL_EUR: Dict[str, float] = {
    "TR": 45, "GR": 70, "CZ": 60, "AT": 85, "HU": 55, "SK": 55, "DE": 85, "FR": 90,
    "ES": 75, "IT": 85, "NL": 95, "BE": 85, "CH": 130, "PL": 50, "GB": 110, "PT": 65,
    "RU": 60, "SE": 110, "NO": 120, "DK": 120, "FI": 100, "IE": 120, "IS": 150,
    "RO": 40, "BG": 40, "HR": 75, "RS": 45, "SI": 75, "CY": 75, "ME": 50, "MK": 40,
    "AL": 40, "BA": 45, "XK": 40, "MT": 85, "LU": 120, "LT": 50, "LV": 55, "EE": 60,
    "MD": 35, "UA": 40, "BY": 45, "GE": 40, "AM": 45, "AZ": 50, "IL": 110, "EG": 35,
    "MA": 40, "TN": 40,
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}


def _eur_try() -> float:
    rate, _source, _is_live = fx.get_eur_try_rate()
    return float(rate)


def _compile_aliases(aliases: Dict[str, str]) -> List[Tuple[re.Pattern, str]]:
    ordered = sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True)
    return [
        (re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])"), canonical)
        for alias, canonical in ordered
    ]


_LOW_COST_PATTERNS = _compile_aliases(_LOW_COST_ALIASES)
_FULL_SERVICE_PATTERNS = _compile_aliases(_FULL_SERVICE_ALIASES)
_CARRIER_SPLIT_RE = re.compile(r"\s*(?:/|,|;|\+|&|\band\b)\s*", re.IGNORECASE)


def _find_canonical(text: str, patterns: List[Tuple[re.Pattern, str]]) -> Optional[str]:
    folded = text.casefold()
    for pattern, canonical in patterns:
        if pattern.search(folded):
            return canonical
    return None


def _squash(text: str) -> str:
    """Lower-case letters and digits only: "easy Jet", "easyJet" and "Easy-Jet" all become "easyjet"."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


# Exact match on the squashed name ("TurkishAirlines" from a rejoined "Turkish / Airlines").
_LOW_COST_SQUASHED: Dict[str, str] = {_squash(alias): canonical for alias, canonical in _LOW_COST_ALIASES.items()}
_FULL_SERVICE_SQUASHED: Dict[str, str] = {_squash(alias): canonical for alias, canonical in _FULL_SERVICE_ALIASES.items()}

_KNOWN_CARRIER_KEYS = frozenset(
    {_squash(alias) for alias in _LOW_COST_ALIASES}
    | {_squash(alias) for alias in _FULL_SERVICE_ALIASES}
    | {_squash(name) for name in LOW_COST_BAG_EUR}
    | {_squash(name) for name in FULL_SERVICE_CARRIERS}
)


def _rejoin_split_brands(parts: List[str]) -> List[str]:
    """Undo a brand name that a card split apart: "easy / Jet" -> "easyJet", "Turkish / Airlines".

    Two neighbours are joined when their concatenation is a known carrier and they are not both known
    carriers on their own, so "Wizz / Air" and "Turkish / Airlines" join but "Austrian / Aegean" does not.
    """
    out: List[str] = []
    index = 0
    while index < len(parts):
        if index + 1 < len(parts):
            first, second = _squash(parts[index]), _squash(parts[index + 1])
            if (not (first in _KNOWN_CARRIER_KEYS and second in _KNOWN_CARRIER_KEYS)
                    and first + second in _KNOWN_CARRIER_KEYS):
                out.append(parts[index] + parts[index + 1])
                index += 2
                continue
        out.append(parts[index])
        index += 1
    return out


def _split_carriers(airline: Any) -> List[str]:
    text = str(airline or "").strip()
    if not text:
        return []
    parts = [part for part in _CARRIER_SPLIT_RE.split(text) if part and part.strip()]
    return _rejoin_split_brands(parts)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _as_utc_or_naive(moment: datetime, want_aware: bool) -> datetime:
    """Keeps the instant aware (UTC) when ``want_aware``; otherwise drops the zone (naive local, legacy)."""
    if want_aware:
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


def _leg_kind(leg: Dict[str, Any]) -> str:
    """'flight', 'transfer' (airport terminal -> station, router-made) or 'ground'."""
    kind = str(leg.get("type") or "").strip().lower()
    if kind == "flight":
        return "flight"
    if kind == "transfer":
        return "transfer"
    return "ground"


def _leg_time(leg: Dict[str, Any], key: str, utc: bool = False) -> datetime:
    """A leg endpoint's instant: the ``<key>_utc`` field when ``utc`` is asked for, else ``<key>_at``."""
    if utc:
        return _parse_dt(leg[f"{key}_utc"])
    value = leg.get(f"{key}_at")
    if value is None:
        value = leg.get(key)
    return _parse_dt(value)


def _crosses_night(start: datetime, end: datetime, zone: Optional[str] = None) -> bool:
    """True when the interval [start, end) overlaps a 00:00-06:00 window.

    With ``zone`` (the waiting place's IANA zone) the window is the local one: the wait is laid on the local
    clock from the local start time, measured in real elapsed minutes. Without it, naive values are read as
    local clock times.
    """
    if end <= start:
        return False
    if zone is not None:
        local_start = _as_utc_or_naive(start, True).astimezone(ZoneInfo(zone)).replace(tzinfo=None)
        start, end = local_start, local_start + (_as_utc_or_naive(end, True) - _as_utc_or_naive(start, True))
    elif start.tzinfo is not None or end.tzinfo is not None:
        start, end = _as_utc_or_naive(start, False), _as_utc_or_naive(end, False)
    day = start.date()
    while day <= end.date():
        window_start = datetime.combine(day, NIGHT_START)
        window_end = datetime.combine(day, NIGHT_END)
        if max(start, window_start) < min(end, window_end):
            return True
        day = day + timedelta(days=1)
    return False


def _min_buffer(prev_kind: str, next_kind: str, separate_tickets: bool) -> int:
    """Minimum connection buffer in minutes (see journey_risk)."""
    if prev_kind == "flight" and next_kind == "flight":
        return 180 if separate_tickets else 60
    if prev_kind == "flight":  # flight -> ground
        return 90
    if next_kind == "flight":  # ground -> flight
        return 150
    return 20  # ground -> ground


def _airport_code(place: Any) -> Optional[str]:
    """IATA code for an airport leg endpoint ('ATH', 'ATH Airport Terminal', 'Atina')."""
    text = str(place or "").strip()
    if not text:
        return None
    db = AirportDB.load_default()
    for candidate in (text, text.split()[0]):
        airport = db.get(candidate)
        if airport is not None:
            return airport.iata
    resolved = db.resolve(text)
    if resolved:
        return resolved[0].iata
    return text.split()[0].upper()


def _transfer_result(minutes: int, price_try: float, mode: str, is_estimated: bool, note: str,
                     price_is_estimated: bool = True) -> Dict[str, Any]:
    """``is_estimated`` covers the duration and mode; ``price_is_estimated`` covers the price alone."""
    return {
        "duration_mins": int(minutes),
        "price_try": round(float(price_try), 2),
        "mode": mode,
        "label": TRANSFER_MODE_LABELS.get(mode, TRANSFER_MODE_LABELS["unknown"]),
        "is_estimated": bool(is_estimated),
        "price_is_estimated": bool(price_is_estimated),
        "note": note,
    }


def is_night_transport_time(at_time: Any) -> bool:
    """True when ``at_time`` ("HH:MM", a time or a datetime) is inside the no-public-transport window."""
    if at_time is None or at_time == "":
        return False
    if isinstance(at_time, datetime):
        clock = at_time.time()
    elif isinstance(at_time, time):
        clock = at_time
    else:
        text = str(at_time).strip()
        try:
            hour, minute = text.split(":")[:2]
            clock = time(int(hour), int(minute))
        except (ValueError, TypeError):
            return False
    return NIGHT_TRANSPORT_START <= clock <= NIGHT_TRANSPORT_END


def _night_taxi_result(km: float, public_minutes: int, rate: float) -> Dict[str, Any]:
    eur = max(TAXI_MIN_EUR, km * TAXI_EUR_PER_KM)
    minutes = round(public_minutes * TAXI_DURATION_FACTOR)
    note = (f"Gece saatlerinde toplu taşıma yok varsayımı (00:30-05:30): taksi, merkeze yaklaşık "
            f"{km:.0f} km (tahmini).")
    return _transfer_result(minutes, eur * rate, "taxi", True, note)


def _distance_to_centre_km(airport) -> Optional[float]:
    """Distance from the airport to a city reference point, or None when none is usable.

    AirportDB.city_center() returns the airport's own position for single-airport cities,
    so it is only usable when it differs from the airport. Multi-airport metro groups
    (METRO_GROUPS) give a real reference point, e.g. LCY -> London.
    """
    db = AirportDB.load_default()
    candidates = [airport.city]
    candidates.extend(name for name, codes in METRO_GROUPS.items() if airport.iata in codes)
    for name in candidates:
        centre = db.city_center(name)
        if centre is None:
            continue
        km = haversine_km(airport.lat, airport.lon, centre[0], centre[1])
        if km >= CENTRE_MIN_DISTANCE_KM:
            return km
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def baggage_fee_try(airline: str, cabin_bag: bool = True, checked_bag: bool = False) -> Tuple[float, bool]:
    """Bag fee in TRY for one flight leg. Returns ``(fee_try, is_estimated)``.

    Rules
    - No bag requested (both flags False): ``(0.0, False)``.
    - Carrier string is split on ``/ , ; + &`` and "and" (codeshares), and split brand names are
      rejoined ("easy / Jet" -> easyJet). Matching is case-insensitive and on word boundaries.
    - If ANY part is a low-cost carrier, the fee is the MAXIMUM over the low-cost parts
      (cabin trolley + checked bag, when requested). It is an estimate, except when the
      requested items are all included in the fare (none of the low-cost carriers today, since even
      their cheapest fares only include an under-seat item).
    - Otherwise, if any part is unknown (or the string is empty), the default unknown-carrier fee
      applies (UNKNOWN_CARRIER_BAG_EUR, always an estimate). Zero is never used for an unknown
      carrier, so every flight leg of an option is priced the same way.
    - Otherwise (all parts are known full-service carriers), ``(0.0, False)``.
    """
    if not cabin_bag and not checked_bag:
        return 0.0, False

    best_eur: Optional[float] = None
    unknown = False
    parts = _split_carriers(airline)
    for part in parts:
        canonical = _find_canonical(part, _LOW_COST_PATTERNS) or _LOW_COST_SQUASHED.get(_squash(part))
        if canonical is not None:
            cabin, checked = LOW_COST_BAG_EUR[canonical]
            eur = (cabin if cabin_bag else 0.0) + (checked if checked_bag else 0.0)
            best_eur = eur if best_eur is None else max(best_eur, eur)
            continue
        if (_find_canonical(part, _FULL_SERVICE_PATTERNS) or _FULL_SERVICE_SQUASHED.get(_squash(part))) is None:
            unknown = True

    if best_eur is not None:
        if best_eur <= 0.0:
            return 0.0, False  # only an allowance included in the fare was requested
        return round(best_eur * _eur_try(), 2), True
    if unknown or not parts:
        cabin, checked = UNKNOWN_CARRIER_BAG_EUR
        eur = (cabin if cabin_bag else 0.0) + (checked if checked_bag else 0.0)
        return round(eur * _eur_try(), 2), True
    return 0.0, False


def airport_transfer(iata: str, at_time: Any = None) -> Dict[str, Any]:
    """Airport <-> city centre or main station, one way, for one traveller.

    Order of resolution
    1. Table ``AIRPORT_TRANSFERS`` (estimates, mode and note are given).
    2. Distance rule for other airports in AirportDB:
       minutes = 25 + km * 1.5, EUR = max(3, km * 0.15), mode "estimated".
       Used only when a usable city reference point exists (see _distance_to_centre_km).
    3. Fallback: 45 minutes, EUR 8, mode "unknown" (unknown code or no reference point).

    Night rule (``at_time`` = the transfer's start, "HH:MM"): between 00:30 and 05:30 public
    transport is assumed unavailable, so a known airport gets a taxi instead (mode "taxi"):
    EUR = max(25, km * 1.75) with km the airport -> centre distance (taken from the table duration
    when no reference point exists), minutes = 0.8 x the public transfer duration.

    A city name is accepted too (resolved through AirportDB to its main airport).
    ``is_estimated`` is always True. ``label`` is the human name of the mode for the UI.
    """
    rate = _eur_try()
    text = str(iata or "").strip()
    db = AirportDB.load_default()
    airport = db.get(text) if text else None
    if airport is None and text:
        resolved = db.resolve(text)
        airport = resolved[0] if resolved else None

    public = _public_transfer(airport, rate)
    if airport is not None and is_night_transport_time(at_time):
        km = _distance_to_centre_km(airport)
        if km is None:
            km = max(0.0, (public["duration_mins"] - DISTANCE_BASE_MINS) / DISTANCE_MINS_PER_KM)
        return _night_taxi_result(km, public["duration_mins"], rate)
    return public


def _public_transfer(airport, rate: float) -> Dict[str, Any]:
    """The daytime estimate (table, distance rule or fallback). See airport_transfer."""
    if airport is not None and airport.iata in AIRPORT_TRANSFERS:
        minutes, eur, mode, note = AIRPORT_TRANSFERS[airport.iata]
        official = OFFICIAL_TRANSFER_PRICES_EUR.get(airport.iata)
        if official is not None:
            return _transfer_result(minutes, official * rate, mode, True, note + " (resmi ücret)",
                                    price_is_estimated=False)
        return _transfer_result(minutes, eur * rate, mode, True, note)

    if airport is not None:
        km = _distance_to_centre_km(airport)
        if km is not None:
            minutes = round(DISTANCE_BASE_MINS + km * DISTANCE_MINS_PER_KM)
            eur = max(DISTANCE_MIN_EUR, km * DISTANCE_EUR_PER_KM)
            note = f"Mesafeden tahmin: merkeze yaklaşık {km:.0f} km (tahmini)."
            return _transfer_result(minutes, eur * rate, "estimated", True, note)

    return _transfer_result(
        FALLBACK_TRANSFER_MINS,
        FALLBACK_TRANSFER_EUR * rate,
        "unknown",
        True,
        "Havalimanı veri setinde yok ya da merkez uzaklığı belirlenemedi; varsayılan tahmin.",
    )


def hotel_estimate_try(place_or_iata: str) -> Tuple[float, bool]:
    """One budget hotel night in TRY as ``(price_try, is_estimated=True)``.

    The country is taken from the airport (IATA code) or resolved from a city/country name
    through AirportDB. Countries missing from ``COUNTRY_HOTEL_EUR`` (or unknown places)
    use ``FALLBACK_HOTEL_EUR`` (60 EUR).
    """
    rate = _eur_try()
    text = str(place_or_iata or "").strip()
    country: Optional[str] = None
    if text:
        db = AirportDB.load_default()
        airport = db.get(text)
        if airport is None:
            resolved = db.resolve(text)
            airport = resolved[0] if resolved else None
        if airport is not None:
            country = airport.country
    eur = COUNTRY_HOTEL_EUR.get(country or "", FALLBACK_HOTEL_EUR)
    return round(eur * rate, 2), True


def connection_risk(arrive_at: str, depart_at: str, separate_tickets: bool, next_mode: str,
                    min_buffer_mins: int, is_last_service: bool = False,
                    crosses_night: bool = False) -> Dict[str, Any]:
    """Risk of one connection. Returns ``{"level": low|medium|high, "score": 0-100, "reasons": [...]}``.

    ``slack = (depart_at - arrive_at) in minutes - min_buffer_mins``.

    Score contributions (capped at 100)
    - depart before arrive (bad data): +60, forced high.
    - separate_tickets: +15 ("ayrı biletler").
    - slack < 30: +40 and forced high.
    - separate_tickets and slack < 90: +25.
    - is_last_service ("günün son seferi"): +30.
    - crosses_night (00:00-06:00 inside the gap): +15.
    - next_mode == "flight" and slack < 60: +10 (check-in and security need room).

    Level
    - high if bad data, slack < 30, or score >= 60.
    - medium if score >= 30.
    - low otherwise.
    """
    arrive = _parse_dt(arrive_at)
    depart = _parse_dt(depart_at)
    if (arrive.tzinfo is None) != (depart.tzinfo is None):
        # One aware and one naive value cannot be subtracted: compare both as naive local clock times
        arrive, depart = _as_utc_or_naive(arrive, False), _as_utc_or_naive(depart, False)
    gap = int((depart - arrive).total_seconds() // 60)
    buffer = int(min_buffer_mins)
    slack = gap - buffer

    score = 0
    reasons: List[str] = []
    forced_high = False

    if gap < 0:
        score += 60
        forced_high = True
        reasons.append("varış saati kalkıştan sonra görünüyor (veri hatası olabilir)")
    if separate_tickets:
        score += 15
        reasons.append("ayrı biletler: gecikmede bağlantı garanti edilmez")
    if gap >= 0 and slack < 30:
        score += 40
        forced_high = True
        if slack < 0:
            reasons.append(f"bekleme {gap} dk, minimum tampon {buffer} dk karşılanmıyor")
        else:
            reasons.append(f"aktarma payı yalnızca {slack} dk (minimum tampon {buffer} dk)")
    elif separate_tickets and slack < 90:
        score += 25
        reasons.append(f"ayrı biletle aktarma payı {slack} dk (90 dk altı riskli)")
    if is_last_service:
        score += 30
        reasons.append("günün son seferi: kaçırılırsa aynı gün alternatif yok")
    if crosses_night:
        score += 15
        reasons.append("gece geçişi: bekleme gece saatlerine denk geliyor")
    if next_mode == "flight" and gap >= 0 and slack < 60:
        score += 10
        reasons.append("uçuş öncesi check-in ve güvenlik için pay dar")

    score = min(100, score)
    if not reasons:
        reasons.append(f"yeterli pay ({slack} dk)")

    if forced_high or score >= 60:
        level = "high"
    elif score >= 30:
        level = "medium"
    else:
        level = "low"
    return {"level": level, "score": score, "reasons": reasons}


def journey_risk(legs: Sequence[Dict[str, Any]], separate_tickets: bool) -> Dict[str, Any]:
    """Risk along a whole itinerary.

    Transfer legs (``type == "transfer"``) are removed first. The remaining legs are paired
    consecutively, and each pair is one connection. The gap runs from the arrival of the
    first leg to the departure of the second, so any removed transfer time is included.

    Minimum buffers (``_min_buffer``)
    - flight -> flight: 180 min (separate tickets) or 60 min (same ticket).
    - flight -> ground: 90 min.
    - ground -> flight: 150 min.
    - ground -> ground: 20 min.

    ``crosses_night`` is True when the gap overlaps 00:00-06:00. ``is_last_service`` is read
    from the next leg (``is_last_service`` or ``last_service`` key), when the route engine sets it.

    Returns ``{"level", "score", "reasons", "connections"}``. ``level`` and ``score`` are the
    worst values over all connections. Reasons of medium/high connections are merged and
    prefixed with the station (low connections only appear in ``connections``).
    """
    sequence = [leg for leg in legs if _leg_kind(leg) != "transfer"]
    connections: List[Dict[str, Any]] = []
    for prev, nxt in zip(sequence, sequence[1:]):
        prev_kind, next_kind = _leg_kind(prev), _leg_kind(nxt)
        buffer = _min_buffer(prev_kind, next_kind, separate_tickets)
        # UTC instants when both legs carry them (a connection never mixes aware and naive values)
        utc = bool(prev.get("arrive_utc")) and bool(nxt.get("depart_utc"))
        arrive = _leg_time(prev, "arrive", utc)
        depart = _leg_time(nxt, "depart", utc)
        zone = prev.get("arrive_tz") if utc else None
        result = connection_risk(
            arrive_at=arrive.isoformat(timespec="minutes"),
            depart_at=depart.isoformat(timespec="minutes"),
            separate_tickets=separate_tickets,
            next_mode=next_kind,
            min_buffer_mins=buffer,
            is_last_service=bool(nxt.get("is_last_service") or nxt.get("last_service")),
            crosses_night=_crosses_night(arrive, depart, str(zone) if zone else None),
        )
        connections.append({
            "at": prev.get("to"),
            "from_type": prev_kind,
            "to_type": next_kind,
            "buffer_mins": buffer,
            **result,
        })

    if not connections:
        return {"level": "low", "score": 0, "reasons": [], "connections": []}

    worst = max(connections, key=lambda c: (_LEVEL_RANK[c["level"]], c["score"]))
    merged: List[str] = []
    for conn in connections:
        if conn["level"] == "low":
            continue  # low connections keep their informational reasons per connection only
        for reason in conn["reasons"]:
            text = f"{conn['at']} aktarması: {reason}" if conn.get("at") else reason
            if text not in merged:
                merged.append(text)
    return {
        "level": worst["level"],
        "score": max(c["score"] for c in connections),
        "reasons": merged,
        "connections": connections,
    }


def cost_breakdown(legs: Sequence[Dict[str, Any]], hotel_nights: int = 0,
                   hotel_place: Optional[str] = None,
                   cabin_bag: bool = False, checked_bag: bool = False,
                   real_data_only: bool = False,
                   hotel_price_try: Optional[float] = None,
                   transfers_from_legs: bool = False) -> Dict[str, Any]:
    """Real total cost in TRY for an itinerary (known parts only; see the module docstring).

    - tickets: sum of ``price_try`` over non-transfer legs whose price is known. A leg with
      ``price_try`` None is not counted and adds 1 to ``unknown_ticket_legs``.
      Transfer legs are ignored, because their cost comes from ``airport_transfer``.
    - baggage: ``baggage_fee_try`` summed over flight legs (one-way each). With real_data_only an
      estimated fee is not counted: it goes to ``unknown_parts`` as "baggage".
    - transfers: one ``airport_transfer`` per airport and direction, applied when
      * a flight departs from an airport that is the trip start, or is preceded by a ground leg
        (direction "to airport"), and
      * a flight arrives at an airport that is the trip end, or is followed by a ground leg
        (direction "to city").
      Airport-to-airport connections (flight -> flight) get no transfer. Each (airport,
      direction) pair is counted once. With real_data_only a transfer whose price is an estimate is
      not counted and goes to ``unknown_parts`` as "transfers".
      With ``transfers_from_legs`` (the route engine sets it) the transfers part is the sum of the
      ``type == "transfer"`` legs' own ``price_try`` instead, so the total matches the legs shown: no
      airport rule runs, a leg with ``price_try`` None makes "transfers" unknown, and with real_data_only
      a leg whose price is an estimate (``price_is_estimated``) is not counted either.
    - hotel: real hotel price (``hotel_price_try``) when available, or ``hotel_nights`` times
      ``hotel_estimate_try(place)``. With a real hotel price the hotel is counted and marked as
      not estimated; without it and with real_data_only the hotel is not counted ("hotel" in
      ``unknown_parts``) and stays in ``estimated_parts``.
    - total: sum of the known parts (a lower bound when ``price_complete`` is False).
    - estimated_parts: names among "tickets", "baggage", "transfers", "hotel" that contain
      estimated values that ARE counted in the figures above (with real_data_only: the hotel only).
    - unknown_parts: names among "baggage", "transfers", "hotel" that are not counted because their
      price is unknown (or excluded as an estimate under real_data_only).
    - price_complete: True when no ticket price is unknown and no part is in ``unknown_parts``.
    """
    real = bool(real_data_only)
    sequence = [leg for leg in legs if _leg_kind(leg) != "transfer"]

    tickets = 0.0
    tickets_estimated = False
    unknown_ticket_legs = 0
    for leg in sequence:
        price = leg.get("price_try")
        if price is None:
            unknown_ticket_legs += 1
        else:
            tickets += float(price)
        tickets_estimated = tickets_estimated or bool(leg.get("is_estimated"))

    baggage = 0.0
    baggage_estimated = False
    baggage_unknown = False
    for leg in sequence:
        if _leg_kind(leg) != "flight":
            continue
        fee, is_estimated = baggage_fee_try(
            str(leg.get("operator") or leg.get("airline") or ""), cabin_bag, checked_bag,
        )
        if is_estimated and real:
            baggage_unknown = True
            continue
        baggage += fee
        baggage_estimated = baggage_estimated or is_estimated

    transfers = 0.0
    transfers_estimated = False
    transfers_unknown = False

    if transfers_from_legs:
        for leg in legs:
            if leg.get("type") == "transfer":
                price = leg.get("price_try")
                if price is None:
                    transfers_unknown = True
                    continue
                estimated = bool(leg.get("price_is_estimated", True))
                if real and estimated:
                    transfers_unknown = True
                    continue
                transfers += float(price)
                transfers_estimated = transfers_estimated or estimated
    else:
        counted: set = set()

        def take_transfer(code: str) -> None:
            nonlocal transfers, transfers_estimated, transfers_unknown
            result = airport_transfer(code)
            price_estimated = bool(result.get("price_is_estimated", True))
            if price_estimated and real:
                transfers_unknown = True
                return
            transfers += float(result["price_try"])
            transfers_estimated = transfers_estimated or price_estimated

        for index, leg in enumerate(sequence):
            if _leg_kind(leg) != "flight":
                continue
            prev = sequence[index - 1] if index > 0 else None
            nxt = sequence[index + 1] if index + 1 < len(sequence) else None

            if prev is None or _leg_kind(prev) == "ground":
                code = _airport_code(leg.get("from"))
                if code and (code, "to_airport") not in counted:
                    counted.add((code, "to_airport"))
                    take_transfer(code)

            if nxt is None or _leg_kind(nxt) == "ground":
                code = _airport_code(leg.get("to"))
                if code and (code, "to_city") not in counted:
                    counted.add((code, "to_city"))
                    take_transfer(code)

    hotel = 0.0
    hotel_applies = int(hotel_nights or 0) > 0
    hotel_unknown = False
    hotel_is_estimated = False
    if hotel_applies:
        if hotel_price_try is not None:
            hotel = float(hotel_price_try)
            hotel_is_estimated = False
        elif real:
            hotel_unknown = True  # a hotel price is always an estimate: not counted under real_data_only
            hotel_is_estimated = True
        else:
            place = hotel_place or (sequence[0].get("to") if sequence else "") or ""
            per_night, _ = hotel_estimate_try(str(place))
            hotel = int(hotel_nights) * per_night
            hotel_is_estimated = True

    estimated_parts: List[str] = []
    if tickets_estimated:
        estimated_parts.append("tickets")
    if baggage_estimated:
        estimated_parts.append("baggage")
    if transfers_estimated:
        estimated_parts.append("transfers")
    if hotel_applies and hotel_is_estimated:
        estimated_parts.append("hotel")

    unknown_parts: List[str] = []
    if baggage_unknown:
        unknown_parts.append("baggage")
    if transfers_unknown:
        unknown_parts.append("transfers")
    if hotel_unknown:
        unknown_parts.append("hotel")

    tickets = round(tickets, 2)
    baggage = round(baggage, 2)
    transfers = round(transfers, 2)
    hotel = round(hotel, 2)
    return {
        "tickets": tickets,
        "baggage": baggage,
        "transfers": transfers,
        "hotel": hotel,
        "total": round(tickets + baggage + transfers + hotel, 2),
        "estimated_parts": estimated_parts,
        "unknown_ticket_legs": unknown_ticket_legs,
        "unknown_parts": unknown_parts,
        "price_complete": unknown_ticket_legs == 0 and not unknown_parts,
    }
