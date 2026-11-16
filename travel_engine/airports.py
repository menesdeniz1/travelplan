"""World airport lookup for side-route search.

Data: ``travel_engine/data/airports.csv``, filtered from the OurAirports
airports dataset (https://ourairports.com/data/; mirror used for the build:
https://davidmegginson.github.io/ourairports-data/airports.csv). OurAirports
data is released into the public domain. Kept rows: ``large_airport`` and
``medium_airport`` with ``scheduled_service == "yes"`` and a 3-letter IATA
code, in European countries plus TR, CY, GE, AM, AZ, IL, EG, MA, TN (all RU
rows included). Columns: iata,name,city,country,lat,lon,size. ``city`` is the OurAirports
``municipality`` field, which is sometimes a district or a neighbouring town
(e.g. Milan Malpensa -> "Ferno (VA)"), so METRO_GROUPS covers the multi-airport
cities that the municipality field does not express.

The runtime never touches the network; the CSV is read from a path relative to
this module.
"""

from __future__ import annotations

import csv
import math
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DATA_PATH = Path(__file__).resolve().parent / "data" / "airports.csv"
EARTH_RADIUS_KM = 6371.0088

_VALID_SIZES = ("large", "medium")


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    city: str
    country: str
    lat: float
    lon: float
    size: str


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


# Characters NFKD cannot fold to ASCII on their own.
_FOLD = str.maketrans({
    "ı": "i", "ø": "o", "ł": "l", "đ": "d", "ß": "ss", "æ": "ae",
    "œ": "oe", "ð": "d", "þ": "th",
})
_SEP_RE = re.compile(r"[\s\-'’`.,()/]+")
_PAREN_RE = re.compile(r"\([^)]*\)")


def _norm(text: Optional[str]) -> str:
    """Case- and diacritic-insensitive key: 'İSTANBUL' -> 'istanbul', 'Münih' -> 'munih'."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text.translate(_FOLD).casefold())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _SEP_RE.sub(" ", s).strip()


# Multi-airport metros. Keys are English municipality-style names; values are IATA codes.
# Only codes present in the CSV are used, so a missing code is silently skipped.
METRO_GROUPS: Dict[str, Tuple[str, ...]] = {
    "London": ("LHR", "LGW", "STN", "LTN", "LCY", "SEN"),
    "Paris": ("CDG", "ORY", "BVA"),
    "Milan": ("MXP", "LIN", "BGY"),
    "Rome": ("FCO", "CIA"),
    "Istanbul": ("IST", "SAW"),
    "Moscow": ("SVO", "DME", "VKO"),
    "Stockholm": ("ARN", "BMA", "NYO"),
    "Oslo": ("OSL", "TRF"),
    "Brussels": ("BRU", "CRL"),
    "Berlin": ("BER",),
    "Vienna": ("VIE",),
    "Prague": ("PRG",),
    "Bratislava": ("BTS",),
    "Budapest": ("BUD",),
    "Athens": ("ATH",),
    "Barcelona": ("BCN",),
    "Venice": ("VCE", "TSF"),
    "Florence": ("FLR",),
    "Naples": ("NAP",),
    "Turin": ("TRN",),
    "Bucharest": ("OTP",),
    "Krakow": ("KRK",),
    "Ljubljana": ("LJU",),
    "Zagreb": ("ZAG",),
    "Skopje": ("SKP",),
    "Tirana": ("TIA",),
}

# Three-letter city codes (not airports) that expand to a metro group.
CITY_CODES: Dict[str, str] = {
    "LON": "London", "PAR": "Paris", "MIL": "Milan", "ROM": "Rome",
    "MOW": "Moscow", "STO": "Stockholm",
}

# Turkish and other exonyms -> English municipality names (before normalisation).
# Scope: Europe and Turkey (the data set is restricted to Europe and its neighbours).
_CITY_ALIASES: Dict[str, str] = {
    "İstanbul": "Istanbul",
    "Atina": "Athens", "Athina": "Athens",
    "Prag": "Prague", "Praha": "Prague",
    "Viyana": "Vienna", "Wien": "Vienna",
    "Münih": "Munich", "Muenchen": "Munich",
    "Budapeşte": "Budapest",
    "Selanik": "Thessaloniki", "Salonika": "Thessaloniki",
    "Roma": "Rome",
    "Milano": "Milan",
    "Londra": "London",
    "Moskova": "Moscow", "Moskva": "Moscow",
    "Varşova": "Warsaw", "Varsova": "Warsaw", "Warszawa": "Warsaw",
    "Brüksel": "Brussels", "Bruksel": "Brussels", "Bruxelles": "Brussels",
    "Kopenhag": "Copenhagen", "København": "Copenhagen",
    "Lizbon": "Lisbon", "Lisboa": "Lisbon",
    "Venedik": "Venice", "Venezia": "Venice",
    "Floransa": "Florence", "Firenze": "Florence",
    "Napoli": "Naples",
    "Cenevre": "Geneva", "Genève": "Geneva",
    "Zürih": "Zurich", "Zürich": "Zurich",
    "Belgrad": "Belgrade",
    "Sofya": "Sofia",
    "Bükreş": "Bucharest", "Bukres": "Bucharest",
    "Tiflis": "Tbilisi",
    "Bakü": "Baku",
    "Kahire": "Cairo",
    "Barselona": "Barcelona",
    "Edinburg": "Edinburgh",
    "Krakov": "Krakow", "Kraków": "Krakow", "Cracow": "Krakow",
    "Saraybosna": "Sarajevo",
    "Üsküp": "Skopje",
    "Tiran": "Tirana",
    "Frankfurt": "Frankfurt am Main",
    "Leipzig": "Schkeuditz",
    "Graz": "Feldkirchen bei Graz",
    "Cologne": "Köln (Cologne)", "Kolonya": "Köln (Cologne)", "Köln": "Köln (Cologne)",
    "Basel": "Bâle / Mulhouse", "Bâle": "Bâle / Mulhouse", "Bale": "Bâle / Mulhouse",
    "İzmir": "Gaziemir", "Izmir": "Gaziemir",
    # T14: Ostrava and European / Turkish airports unresolved by city name
    "Ostrava": "Mošnov",
    "Banja Luka": "Mahovljani",
    "Tuzla": "Dubrave Gornje",
    "Bursa": "Yenişehir",
    "Bergamo": "Orio al Serio (BG)",
    "Verona": "Caselle (VR)",
    "Torino": "Caselle Torinese (TO)",
    "Genoa": "Genova (GE)",
    "Lamezia": "Lamezia Terme (CZ)",
    "Karlsruhe": "Rheinmünster",
    "Baden-Baden": "Rheinmünster",
    "Münster": "Greven",
    "Munster": "Greven",
    "Osnabrück": "Greven",
    "Osnabruck": "Greven",
    "Rzeszów": "Jasionka",
    "Rzeszow": "Jasionka",
    "Modlin": "Nowy Dwór Mazowiecki",
    "Torp": "Sandefjord(Torp)",
    "Chania": "Souda",
    "Hanya": "Souda",
    "Corfu": "Kerkyra (Corfu)",
    "Korfu": "Kerkyra (Corfu)",
    "Murcia": "Corvera",
    "Ercan": "Tymbou (Kirklar)",
    "Lefkoşa": "Tymbou (Kirklar)",
    "Lefkosa": "Tymbou (Kirklar)",
    "Lille": "Lesquin",
    "Marseille": "Marignane",
    "Montpellier": "Montpellier/Méditerranée",
    "Toulouse": "Toulouse/Blagnac",
    "Brescia": "Montichiari (BS)",
    "Ancona": "Falconara Marittima (AN)",
    "Trieste": "Ronchi dei Legionari/Trieste",
    "Tampere": "Tampere / Pirkkala",
    "Västerås": "Stockholm / Västerås",
    "Vasteras": "Stockholm / Västerås",
    "Vitoria": "Alava",
    "Vitoria-Gasteiz": "Alava",
    "Jerez": "Jerez de la Frontera",
    "Trapani": "Trapani (TP)",
    "Crotone": "Isola di Capo Rizzuto (KR)",
}

# Country names (Turkish and English) -> ISO 3166-1 alpha-2.
_COUNTRY_NAMES: Dict[str, Tuple[str, ...]] = {
    "GR": ("Greece", "Yunanistan", "Ellada"),
    "TR": ("Turkey", "Türkiye", "Turkiye"),
    "CZ": ("Czechia", "Czech Republic", "Çekya", "Çek Cumhuriyeti"),
    "AT": ("Austria", "Avusturya"),
    "SK": ("Slovakia", "Slovakya"),
    "HU": ("Hungary", "Macaristan"),
    "DE": ("Germany", "Almanya"),
    "IT": ("Italy", "İtalya"),
    "FR": ("France", "Fransa"),
    "ES": ("Spain", "İspanya"),
    "NL": ("Netherlands", "Hollanda"),
    "BE": ("Belgium", "Belçika"),
    "CH": ("Switzerland", "İsviçre"),
    "PL": ("Poland", "Polonya"),
    "GB": ("United Kingdom", "UK", "Great Britain", "Britain", "England", "İngiltere", "Birleşik Krallık"),
    "PT": ("Portugal", "Portekiz"),
    "RU": ("Russia", "Rusya"),
    "SE": ("Sweden", "İsveç"),
    "NO": ("Norway", "Norveç"),
    "DK": ("Denmark", "Danimarka"),
    "FI": ("Finland", "Finlandiya"),
    "IE": ("Ireland", "İrlanda"),
    "RO": ("Romania", "Romanya"),
    "BG": ("Bulgaria", "Bulgaristan"),
    "HR": ("Croatia", "Hırvatistan"),
    "RS": ("Serbia", "Sırbistan"),
    "SI": ("Slovenia", "Slovenya"),
    "CY": ("Cyprus", "Kıbrıs"),
    "EG": ("Egypt", "Mısır"),
    "AM": ("Armenia", "Ermenistan"),
    "AZ": ("Azerbaijan", "Azerbaycan"),
    "GE": ("Georgia", "Gürcistan"),
    "IL": ("Israel", "İsrail"),
    "TN": ("Tunisia", "Tunus"),
    "MA": ("Morocco", "Fas"),
}

_CITY_ALIAS_KEYS: Dict[str, str] = {_norm(k): _norm(v) for k, v in _CITY_ALIASES.items()}
_COUNTRY_ALIAS_KEYS: Dict[str, str] = {
    _norm(name): iso for iso, names in _COUNTRY_NAMES.items() for name in names
}


CITY_CENTRES_PATH = Path(__file__).resolve().parent / "data" / "city_centres.csv"

# Airports on islands with no road or rail link to the mainland (ferry or air only).
# Every code must exist in airports.csv (checked by tests/test_airports.py).
ISLAND_AIRPORTS: frozenset = frozenset({
    # Greece
    "AOK", "CFU", "CHQ", "EFL", "HER", "JKH", "JMK", "JSH", "JSI", "JTR", "KGS", "MJT", "RHO", "SMI", "ZTH",
    # Balearics
    "IBZ", "MAH", "PMI",
    # Canaries
    "ACE", "FUE", "LPA", "SPC", "TFN", "TFS", "VDE",
    # Madeira and Azores
    "FLW", "FNC", "GRW", "HOR", "PDL", "PIX", "PXO", "SJZ", "SMA", "TER",
    # Sardinia, Corsica, Sicily and other Italian islands
    "AHO", "AJA", "BIA", "CAG", "CIY", "CLY", "CTA", "EBA", "FSC", "LMP", "OLB", "PMO", "PNL", "TPS",
    # Malta, Cyprus, Faroe
    "ECN", "FAE", "LCA", "MLA", "PFO",
    # Iceland (GRY is Grimsey, its own island)
    "AEY", "EGS", "GRY", "HFN", "IFJ", "KEF", "RKV", "VPN",
    # Channel Islands and Isle of Man
    "ACI", "GCI", "IOM", "JER",
    # Baltic, Nordic and Svalbard islands
    "KDL", "LYR", "MHQ", "RNN", "URE", "VBY",
    # Croatia (Brac; Krk is bridged and excluded)
    "BWK",
    # UK islands: Shetland, Orkney, Hebrides, Inner and Western Isles
    "BEB", "BRR", "EOI", "ILY", "KOI", "LSI", "SYY", "TRE",
})

# Multi-airport islands. Single-airport islands form their own group.
_ISLAND_GROUPS: Dict[str, Tuple[str, ...]] = {
    "Crete": ("CHQ", "HER", "JSH"),
    "Sardinia": ("AHO", "CAG", "OLB"),
    "Corsica": ("AJA", "BIA", "CLY", "FSC"),
    "Sicily": ("CIY", "CTA", "PMO", "TPS"),
    "Cyprus": ("ECN", "LCA", "PFO"),
    "Iceland": ("AEY", "EGS", "HFN", "IFJ", "KEF", "RKV", "VPN"),
    "Tenerife": ("TFN", "TFS"),
}
_ISLAND_GROUP_OF: Dict[str, str] = {c: g for g, codes in _ISLAND_GROUPS.items() for c in codes}

# Countries whose airports are all on islands (fallback for airports not in ISLAND_AIRPORTS).
_ISLAND_COUNTRIES = frozenset({"GG", "JE", "IM", "FO", "IS", "MT", "CY"})
# Northern Ireland airports: on the island of Ireland, but country code GB.
_NORTHERN_IRELAND = frozenset({"BFS", "BHD", "LDY"})

# ----- time zones -------------------------------------------------------------------------------
# IANA zone of every country in the bundled data (current rules, DST included: zoneinfo applies them
# to each date). Countries with more than one zone are split by the airport tables below.
UTC_FALLBACK = "UTC"
COUNTRY_TIMEZONES: Dict[str, str] = {
    "AL": "Europe/Tirane", "AM": "Asia/Yerevan", "AT": "Europe/Vienna", "AZ": "Asia/Baku",
    "BA": "Europe/Sarajevo", "BE": "Europe/Brussels", "BG": "Europe/Sofia", "BY": "Europe/Minsk",
    "CH": "Europe/Zurich", "CY": "Asia/Nicosia", "CZ": "Europe/Prague", "DE": "Europe/Berlin",
    "DK": "Europe/Copenhagen", "EE": "Europe/Tallinn", "EG": "Africa/Cairo", "ES": "Europe/Madrid",
    "FI": "Europe/Helsinki", "FO": "Atlantic/Faroe", "FR": "Europe/Paris", "GB": "Europe/London",
    "GE": "Asia/Tbilisi", "GG": "Europe/London", "GI": "Europe/Gibraltar", "GR": "Europe/Athens",
    "HR": "Europe/Zagreb", "HU": "Europe/Budapest", "IE": "Europe/Dublin", "IL": "Asia/Jerusalem",
    "IM": "Europe/London", "IS": "Atlantic/Reykjavik", "IT": "Europe/Rome", "JE": "Europe/London",
    "LT": "Europe/Vilnius", "LU": "Europe/Luxembourg", "LV": "Europe/Riga", "MA": "Africa/Casablanca",
    "MD": "Europe/Chisinau", "ME": "Europe/Podgorica", "MK": "Europe/Skopje", "MT": "Europe/Malta",
    "NL": "Europe/Amsterdam", "NO": "Europe/Oslo", "PL": "Europe/Warsaw", "PT": "Europe/Lisbon",
    "RO": "Europe/Bucharest", "RS": "Europe/Belgrade", "RU": "Europe/Moscow", "SE": "Europe/Stockholm",
    "SI": "Europe/Ljubljana", "SK": "Europe/Bratislava", "TN": "Africa/Tunis", "TR": "Europe/Istanbul",
    "UA": "Europe/Kyiv", "XK": "Europe/Belgrade",
}

# Airports whose zone differs from their country's (Portugal, Spain: islands on other offsets).
_ZONE_BY_IATA: Dict[str, str] = {}
for _zone, _codes in (
    # Azores (UTC-1 / UTC+0): one zone, Atlantic/Azores
    ("Atlantic/Azores", ("PDL", "TER", "HOR", "SMA", "FLW", "PIX", "GRW", "SJZ")),
    # Madeira (follows Lisbon rules)
    ("Atlantic/Madeira", ("FNC", "PXO")),
    # Canary Islands (UTC+0 / UTC+1 with the Spanish DST rule of Atlantic/Canary)
    ("Atlantic/Canary", ("TFS", "TFN", "LPA", "ACE", "FUE", "SPC", "VDE", "GMZ")),
):
    for _code in _codes:
        _ZONE_BY_IATA[_code] = _zone

# Russia: longitude bands, then the exceptions for the Volga and Ural cities (the bands are approximate
# east of the Urals: island and remote settlements can differ by an hour).
_RU_BANDS: Tuple[Tuple[float, str], ...] = (
    (22.0, "Europe/Kaliningrad"),
    (60.0, "Europe/Moscow"),
    (73.0, "Asia/Yekaterinburg"),
    (80.0, "Asia/Omsk"),
    (104.0, "Asia/Krasnoyarsk"),
    (120.0, "Asia/Irkutsk"),
    (132.0, "Asia/Yakutsk"),
    (150.0, "Asia/Vladivostok"),
    (160.0, "Asia/Magadan"),
)
_RU_KAMCHATKA = "Asia/Kamchatka"
_RU_EXCEPTIONS: Dict[str, str] = {
    # Samara time (UTC+4): Saratov, Astrakhan, Ulyanovsk, Samara, Balakovo, Udmurtia (Izhevsk)
    **{code: "Europe/Samara" for code in ("GSV", "ASF", "ULV", "ULY", "BWO", "KUF", "IJK")},
    # Yekaterinburg time (UTC+5): Orenburg, Ufa, Perm, Orsk, Magnitogorsk and the Khanty-Mansi and Yamal
    # cities between 73 and 77 degrees east (the Omsk band would otherwise apply)
    **{code: "Asia/Yekaterinburg" for code in ("REN", "UFA", "PEE", "OSW", "MQF", "SGC", "KGP", "NOJ", "NJC", "NUX")},
    # Komi and Nenets (Moscow time, UTC+3) east of the 60th meridian
    **{code: "Europe/Moscow" for code in ("VKT", "AMV")},
    # Chita (UTC+9), Sakhalin (UTC+11), Bratsk in Irkutsk oblast (UTC+8, not the Krasnoyarsk band)
    "HTA": "Asia/Chita", "UUS": "Asia/Sakhalin", "BTK": "Asia/Irkutsk",
}
del _zone, _codes, _code


def _ru_zone(ap: "Airport") -> str:
    if ap.iata in _RU_EXCEPTIONS:
        return _RU_EXCEPTIONS[ap.iata]
    if ap.lon >= 160.0:
        return _RU_KAMCHATKA
    for limit, zone in _RU_BANDS:
        if ap.lon < limit:
            return zone
    return _RU_KAMCHATKA


def airport_zone(ap: "Airport") -> str:
    """IANA time zone of an airport (see COUNTRY_TIMEZONES; UTC_FALLBACK for an unknown country)."""
    if ap.iata in _ZONE_BY_IATA and ap.country in ("PT", "ES"):
        return _ZONE_BY_IATA[ap.iata]
    if ap.country == "RU":
        return _ru_zone(ap)
    return COUNTRY_TIMEZONES.get(ap.country, UTC_FALLBACK)


def _load_city_centres(path: Path) -> Dict[str, Tuple[float, float]]:
    table: Dict[str, Tuple[float, float]] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            key = _norm(row.get("city"))
            if not key:
                continue
            try:
                table.setdefault(key, (float(row["lat"]), float(row["lon"])))
            except (KeyError, ValueError, TypeError):
                continue
    return table


_CITY_CENTRES: Dict[str, Tuple[float, float]] = _load_city_centres(CITY_CENTRES_PATH)


def _load_city_countries(path: Path) -> Dict[str, str]:
    """City name (normalised) -> ISO country code from the city-centre table."""
    table: Dict[str, str] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            key = _norm(row.get("city"))
            country = (row.get("country") or "").strip().upper()
            if key and len(country) == 2:
                table.setdefault(key, country)
    return table


_CITY_COUNTRIES: Dict[str, str] = _load_city_countries(CITY_CENTRES_PATH)

# Airport municipality names (as in airports.csv, parentheses removed) that are not
# themselves listed cities -> the listed city the airport serves.
_MUNICIPALITY_CITY_RAW: Dict[str, str] = {
    "Gaziemir": "İzmir",
    "Ferno": "Milan", "Segrate": "Milan",
    "Orio al Serio": "Bergamo",
    "Caselle Torinese": "Turin",
    "Ciampino": "Rome",
    "Otopeni": "Bucharest",
    "Rinas": "Tirana",
    "Zgornji Brnik": "Ljubljana",
    "Balice": "Krakow",
    "Ilinden": "Skopje",
    "Velika Gorica": "Zagreb",
    "Zaventem": "Brussels",
    "Schkeuditz": "Leipzig",
    "Spata-Artemida": "Athens",
    "Tymbou": "Nicosia",
    "Souda": "Chania",
    "Kerkyra": "Corfu",
    "Santorini Island": "Fira",
    "Kos Island": "Kos",
    "Chios Island": "Chios",
    "Samos Island": "Samos",
    "Karpathos Island": "Karpathos",
    "Kefallinia Island": "Argostoli",
    "Tenerife": "Santa Cruz de Tenerife",
    "Gran Canaria Island": "Las Palmas",
    "San Bartolomé": "Arrecife",
    "El Matorral": "Puerto del Rosario",
    "El Hierro Island": "Valverde",
    "Sta Cruz de la Palma": "Santa Cruz de la Palma",
    "Vágar": "Tórshavn",
    "Castletown": "Douglas",
    "St. Peter": "St Helier",
    "Lesquin": "Lille",
    "Bâle / Mulhouse": "Basel",
    "Campo nell'Elba": "Portoferraio",
    "Kopitnari": "Kutaisi",
}
_MUNICIPALITY_CITY: Dict[str, str] = {_norm(k): v for k, v in _MUNICIPALITY_CITY_RAW.items()}


def _table_lookup(name: Optional[str]) -> Optional[Tuple[float, float]]:
    """City-centre table lookup with the same normalisation and aliases as city search."""
    key = _norm(name)
    if not key:
        return None
    if key in _CITY_CENTRES:
        return _CITY_CENTRES[key]
    alias = _CITY_ALIAS_KEYS.get(key)
    if alias and alias in _CITY_CENTRES:
        return _CITY_CENTRES[alias]
    return None


def _city_table_country(name: Optional[str]) -> Optional[str]:
    """ISO country of a city in the city-centre table (same normalisation and aliases as _table_lookup)."""
    key = _norm(name)
    if not key:
        return None
    if key in _CITY_COUNTRIES:
        return _CITY_COUNTRIES[key]
    alias = _CITY_ALIAS_KEYS.get(key)
    if alias and alias in _CITY_COUNTRIES:
        return _CITY_COUNTRIES[alias]
    return None


def _municipality_centre(city: str) -> Optional[Tuple[float, float]]:
    """City centre for an airport's own municipality field (e.g. 'Pendik, Istanbul', 'Ferno (VA)')."""
    for part in (city or "").split(","):
        bare = _PAREN_RE.sub("", part)
        target = _MUNICIPALITY_CITY.get(_norm(bare))
        centre = _table_lookup(target) if target else _table_lookup(bare)
        if centre is not None:
            return centre
    return None


def _metro_name(iata: str) -> Optional[str]:
    for name, codes in METRO_GROUPS.items():
        if iata in codes:
            return name
    return None


def _sort_airports(items: Iterable[Airport]) -> List[Airport]:
    return sorted(items, key=lambda a: (a.size != "large", a.iata))


class AirportDB:
    """In-memory airport index. Use ``AirportDB.load_default()`` for the bundled data."""

    _default: Optional["AirportDB"] = None
    _default_lock = threading.Lock()

    def __init__(self, airports: Iterable[Airport]):
        self._airports: List[Airport] = []
        self._by_iata: Dict[str, Airport] = {}
        self._city_index: Dict[str, List[Airport]] = {}
        self._countries: set = set()
        for ap in airports:
            if ap.iata in self._by_iata:
                continue
            self._airports.append(ap)
            self._by_iata[ap.iata] = ap
            self._countries.add(ap.country)
            self._index_city(ap)
        self._metro_index: Dict[str, Tuple[str, ...]] = {
            _norm(name): codes for name, codes in METRO_GROUPS.items()
        }

    def _index_city(self, ap: Airport) -> None:
        keys = set()
        full = _norm(ap.city)
        if full:
            keys.add(full)
        for part in ap.city.split(","):
            key = _norm(_PAREN_RE.sub("", part))
            if key:
                keys.add(key)
        for key in keys:
            self._city_index.setdefault(key, []).append(ap)

    @classmethod
    def from_csv(cls, path: Path = DATA_PATH) -> "AirportDB":
        airports: List[Airport] = []
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    airports.append(Airport(
                        iata=row["iata"].strip().upper(),
                        name=row["name"],
                        city=row["city"],
                        country=row["country"].strip().upper(),
                        lat=float(row["lat"]),
                        lon=float(row["lon"]),
                        size=row["size"].strip(),
                    ))
                except (KeyError, ValueError, AttributeError):
                    continue
        return cls(airports)

    @classmethod
    def load_default(cls) -> "AirportDB":
        with cls._default_lock:
            if AirportDB._default is None:
                AirportDB._default = AirportDB.from_csv(DATA_PATH)
            return AirportDB._default

    # ----- lookups -------------------------------------------------------

    def get(self, iata: str) -> Optional[Airport]:
        if not iata:
            return None
        return self._by_iata.get(iata.strip().upper())

    def find_city(self, name: str) -> List[Airport]:
        """All airports serving the city (exonyms and metro groups included), large first."""
        key = _norm(name)
        if not key:
            return []
        key = _CITY_ALIAS_KEYS.get(key, key)
        found: Dict[str, Airport] = {}
        for ap in self._city_index.get(key, ()):
            found[ap.iata] = ap
        for code in self._metro_index.get(key, ()):
            ap = self._by_iata.get(code)
            if ap is not None:
                found[ap.iata] = ap
        return _sort_airports(found.values())

    def country_airports(self, iso2: str) -> List[Airport]:
        code = (iso2 or "").strip().upper()
        if len(code) != 2:
            return []
        return _sort_airports(a for a in self._airports if a.country == code)

    def _resolve_kind(self, place: str) -> Tuple[str, List[Airport]]:
        """Return ("airport" | "city" | "country" | "none", airports)."""
        raw = (place or "").strip()
        if not raw:
            return "none", []
        if re.fullmatch(r"[A-Za-z]{3}", raw):
            ap = self.get(raw)
            if ap is not None:
                return "airport", [ap]
            city_code = CITY_CODES.get(raw.upper())
            if city_code:
                airports = self.find_city(city_code)
                if airports:
                    return "city", airports
        if re.fullmatch(r"[A-Za-z]{2}", raw) and raw.upper() in self._countries:
            return "country", self.country_airports(raw)
        airports = self.find_city(raw)
        if airports:
            return "city", airports
        iso = _COUNTRY_ALIAS_KEYS.get(_norm(raw))
        if iso:
            return "country", self.country_airports(iso)
        return "none", []

    def resolve(self, place: str) -> List[Airport]:
        """IATA code, city name (any supported language) or country code/name -> airports."""
        return self._resolve_kind(place)[1]

    def timezone(self, place: str) -> str:
        """IANA time zone of an airport code, a city, a country (code or name), or "UTC" when unknown.

        A place with several airports takes the zone of its first airport in the order of ``resolve``
        (large airports first), so a multi-zone country is placed by its main airport.
        """
        raw = (place or "").strip()
        if re.fullmatch(r"[A-Za-z]{3}", raw):
            ap = self.get(raw)
            if ap is not None:
                return airport_zone(ap)
        kind, airports = self._resolve_kind(raw)
        if kind == "country":
            # The country's main zone (Spain: Madrid, not the Canaries; Russia: Moscow).
            iso = raw.upper() if re.fullmatch(r"[A-Za-z]{2}", raw) else _COUNTRY_ALIAS_KEYS.get(_norm(raw))
            if iso and iso in COUNTRY_TIMEZONES:
                return COUNTRY_TIMEZONES[iso]
        if airports:
            return airport_zone(airports[0])
        # A city without an airport (Lyon): the country's main zone, from the city-centre table.
        iso = _city_table_country(raw)
        if iso and iso in COUNTRY_TIMEZONES:
            return COUNTRY_TIMEZONES[iso]
        return UTC_FALLBACK

    @staticmethod
    def _centre(kind: str, airports: Sequence[Airport]) -> Optional[Tuple[float, float]]:
        if kind not in ("airport", "city") or not airports:
            return None
        large = [a for a in airports if a.size == "large"] or list(airports)
        lat = sum(a.lat for a in large) / len(large)
        lon = sum(a.lon for a in large) / len(large)
        return lat, lon

    def city_center(self, place: str) -> Optional[Tuple[float, float]]:
        """City-centre coordinates.

        Bare airport codes keep their old meaning (the airport itself). For city names,
        the curated city_centres.csv table is preferred; otherwise the mean of the city's
        large airports is used. None for countries and unknown places.
        """
        kind, airports = self._resolve_kind(place)
        if kind != "airport":
            raw = (place or "").strip()
            if re.fullmatch(r"[A-Za-z]{3}", raw) and raw.upper() in CITY_CODES:
                raw = CITY_CODES[raw.upper()]
            table = _table_lookup(raw)
            if table is not None:
                return table
        return self._centre(kind, airports)

    def airport_city_km(self, iata: str) -> Optional[float]:
        """Straight-line km from the airport to its city centre.

        Uses the airport's own municipality when it is a listed city (or mapped via
        _MUNICIPALITY_CITY_RAW), otherwise the centre of its METRO_GROUPS main city.
        None when neither is known.
        """
        ap = self.get(iata)
        if ap is None:
            return None
        centre = _municipality_centre(ap.city) or self._metro_centre(ap.iata)
        if centre is None:
            return None
        return haversine_km(ap.lat, ap.lon, centre[0], centre[1])

    def metro_city_km(self, iata: str) -> Optional[float]:
        """Straight-line km from the airport to the centre of its METRO_GROUPS main city."""
        ap = self.get(iata)
        if ap is None:
            return None
        centre = self._metro_centre(ap.iata)
        if centre is None:
            return None
        return haversine_km(ap.lat, ap.lon, centre[0], centre[1])

    @staticmethod
    def _metro_centre(iata: str) -> Optional[Tuple[float, float]]:
        name = _metro_name(iata)
        return _table_lookup(name) if name else None

    # ----- landmass / ground reachability ---------------------------------

    def is_island(self, iata: str) -> bool:
        return (iata or "").strip().upper() in ISLAND_AIRPORTS

    def landmass(self, iata: str) -> str:
        """Landmass of an airport: "island:<IATA>", "GB", "IE" or "continent".

        Returns "" for codes not in the database.
        """
        code = (iata or "").strip().upper()
        ap = self._by_iata.get(code)
        if ap is None:
            return ""
        if code in ISLAND_AIRPORTS or ap.country in _ISLAND_COUNTRIES:
            return f"island:{code}"
        if code in _NORTHERN_IRELAND or ap.country == "IE":
            return "IE"
        if ap.country == "GB":
            return "GB"
        return "continent"

    def ground_reachable(self, iata_a: str, iata_b: str) -> bool:
        """True when a road or rail (or Eurostar) link plausibly joins the two airports.

        Same landmass, same multi-airport island, or GB <-> continent via the Channel Tunnel.
        Unknown codes and island <-> other land are False (ferry-only or impossible).
        """
        la, lb = self.landmass(iata_a), self.landmass(iata_b)
        if not la or not lb:
            return False
        if la == lb:
            return True
        if la.startswith("island:") and lb.startswith("island:"):
            ca, cb = la[len("island:"):], lb[len("island:"):]
            return _ISLAND_GROUP_OF.get(ca, ca) == _ISLAND_GROUP_OF.get(cb, cb)
        return {la, lb} == {"GB", "continent"}

    def _scan(self, lat: float, lon: float, radius_km: float,
              sizes: Sequence[str]) -> List[Tuple[Airport, float]]:
        hits = []
        for ap in self._airports:
            if ap.size not in sizes:
                continue
            d = haversine_km(lat, lon, ap.lat, ap.lon)
            if d <= radius_km:
                hits.append((ap, d))
        hits.sort(key=lambda item: item[1])
        return hits

    def nearby(self, lat: float, lon: float, radius_km: float,
               sizes: Sequence[str] = _VALID_SIZES, limit: int = 30) -> List[Tuple[Airport, float]]:
        """Airports within radius_km of a point, nearest first."""
        return self._scan(lat, lon, radius_km, sizes)[:limit]

    def nearby_airports(self, place: str, radius_km: float = 350,
                        limit: int = 15) -> List[Tuple[Airport, float]]:
        """Airports around a city/airport, excluding the place's own airports."""
        kind, own = self._resolve_kind(place)
        centre = self._centre(kind, own)
        if centre is None:
            return []
        own_codes = {a.iata for a in own}
        hits = self._scan(centre[0], centre[1], radius_km, _VALID_SIZES)
        return [(ap, d) for ap, d in hits if ap.iata not in own_codes][:limit]
