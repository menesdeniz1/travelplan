import unicodedata
from dataclasses import dataclass, field
from typing import List, Dict, Optional

# Main commercial airports per country (IATA). Order matters: earlier codes are searched first
# when a country is used as a via point, so the most useful hubs come first.
COUNTRY_AIRPORTS: Dict[str, List[str]] = {
    "GR": [
        "ATH", "SKG", "HER", "RHO", "CFU", "CHQ", "JMK", "JTR", "KGS", "EFL", "ZTH", "PVK",
        "KVA", "IOA", "MJT", "SMI", "JSI", "LXS", "AXD", "VOL", "KLX", "PAS", "JNX", "GPA",
    ],
    "TR": ["IST", "SAW", "ESB", "ADB", "AYT", "BJV", "DLM", "TZX", "GZT"],
    "CZ": ["PRG", "BRQ", "OSR", "KLV", "PED"],
    "AT": ["VIE", "SZG", "GRZ", "INN", "LNZ", "KLU"],
    "SK": ["BTS", "KSC", "POV"],
    "HU": ["BUD", "DEB", "PEV"],
    "DE": ["FRA", "MUC", "BER", "DUS", "HAM", "CGN", "STR", "NUE", "DRS", "LEJ"],
}

@dataclass
class CatchmentAirport:
    airport_code: str
    city_name: str
    transfer_mode: str
    avg_duration_mins: int
    avg_price_try: float
    # Rough per-night hotel price (TRY) in the catchment city, used when a night wait is spent there.
    # Estimate only, not sourced from a live hotel feed.
    avg_hotel_try: Optional[float] = None

@dataclass
class TransitConnection:
    from_id: str
    to_city: str
    mode: str  # "train" or "bus"
    duration_mins: int
    estimated_price_try: float
    primary_operator: str
    station_from: str
    station_to: str

@dataclass
class CityCluster:
    city_name: str
    primary_airport: str
    main_train_station: str
    catchment_airports: List[CatchmentAirport] = field(default_factory=list)
    transit_connections: List[TransitConnection] = field(default_factory=list)

class GeoClusterRegistry:
    """Registry of European City clusters, catchment secondary airports, and ground rail/bus links."""

    @staticmethod
    def _normalize_key(text: Optional[str]) -> str:
        if not text:
            return ""
        mapping = {
            'İ': 'i', 'I': 'i', 'ı': 'i', 'i': 'i',
            'Ğ': 'g', 'ğ': 'g',
            'Ü': 'u', 'ü': 'u',
            'Ş': 's', 'ş': 's',
            'Ö': 'o', 'ö': 'o',
            'Ç': 'c', 'ç': 'c',
        }
        return "".join(mapping.get(c, c.lower()) for c in str(text).strip())

    CITY_ALIASES = {
        "prag": "Prague",
        "viyana": "Vienna",
        "atina": "Athens",
        "selanik": "Thessaloniki",
        "munih": "Munich",
        "budapeste": "Budapest",
        "berlin": "Berlin",
        "bratislava": "Bratislava",
        "istanbul": "Istanbul",
        "ankara": "Ankara",
        "izmir": "Izmir",
        "antalya": "Antalya",
        "roma": "Rome",
        "londra": "London",
        "sofya": "Sofia",
    }

    def __init__(self):
        self._clusters: Dict[str, CityCluster] = {}
        self._airport_buffers: Dict[str, int] = {
            "VIE": 90,  # Schwechat terminal to Wien Hbf (transfer + buffer)
            "BTS": 90,  # Bratislava airport to Hlavna stanica
            "PRG": 90,  # Vaclav Havel airport to hl.n.
            "BUD": 90,  # Budapest Liszt Ferenc to Keleti
            "MUC": 90,  # Munich Airport to Hbf
            "BER": 90,  # BER terminal to Hauptbahnhof
            "DRS": 90,  # Dresden airport to Hbf
            "SKG": 90,  # Thessaloniki airport to train station
            "ATH": 90,  # Athens airport to Larissis station
        }
        self._bootstrap_clusters()

    def _bootstrap_clusters(self):
        # 1. Prague Cluster
        self._clusters["Prague"] = CityCluster(
            city_name="Prague",
            primary_airport="PRG",
            main_train_station="Praha hlavní nádraží (hl.n.)",
            catchment_airports=[
                CatchmentAirport("VIE", "Vienna", "train", 250, 750.0, avg_hotel_try=3600.0),
                CatchmentAirport("BTS", "Bratislava", "train", 260, 650.0, avg_hotel_try=2200.0),
                CatchmentAirport("DRS", "Dresden", "train", 140, 550.0, avg_hotel_try=2300.0),
                CatchmentAirport("BER", "Berlin", "train", 270, 950.0, avg_hotel_try=3200.0),
                CatchmentAirport("BUD", "Budapest", "train", 410, 1100.0, avg_hotel_try=2400.0),
                CatchmentAirport("MUC", "Munich", "bus", 300, 800.0, avg_hotel_try=4200.0),
            ],
            transit_connections=[
                TransitConnection("VIE", "Prague", "train", 250, 750.0, "ÖBB Railjet / RegioJet", "Wien Hbf", "Praha hl.n."),
                TransitConnection("BTS", "Prague", "train", 260, 650.0, "ČD EuroCity / RegioJet", "Bratislava hl.st.", "Praha hl.n."),
                TransitConnection("DRS", "Prague", "train", 140, 550.0, "EuroCity", "Dresden Hbf", "Praha hl.n."),
                TransitConnection("BER", "Prague", "train", 270, 950.0, "EuroCity", "Berlin Hbf", "Praha hl.n."),
                # RegioJet Budapest-Prague trains leave from Kelenföld, not Keleti (verified via RegioJet API, 2026-10-19)
                TransitConnection("BUD", "Prague", "train", 420, 1100.0, "RegioJet", "Budapest Kelenföld", "Praha hl.n."),
                TransitConnection("MUC", "Prague", "bus", 300, 800.0, "FlixBus", "München ZOB", "Praha hl.n."),
            ]
        )

        # 2. Vienna Cluster
        self._clusters["Vienna"] = CityCluster(
            city_name="Vienna",
            primary_airport="VIE",
            main_train_station="Wien Hauptbahnhof (Hbf)",
            catchment_airports=[
                CatchmentAirport("BTS", "Bratislava", "train", 50, 380.0, avg_hotel_try=2200.0),
                CatchmentAirport("BUD", "Budapest", "train", 155, 650.0, avg_hotel_try=2400.0),
                CatchmentAirport("PRG", "Prague", "train", 250, 750.0, avg_hotel_try=2600.0),
            ],
            transit_connections=[
                TransitConnection("BTS", "Vienna", "train", 50, 380.0, "ÖBB / Slovak Rail", "Bratislava hl.st.", "Wien Hbf"),
                TransitConnection("BUD", "Vienna", "train", 155, 650.0, "ÖBB Railjet", "Budapest Keleti", "Wien Hbf"),
                TransitConnection("PRG", "Vienna", "train", 250, 750.0, "RegioJet / Railjet", "Praha hl.n.", "Wien Hbf"),
            ]
        )

        # 3. Bratislava Cluster
        self._clusters["Bratislava"] = CityCluster(
            city_name="Bratislava",
            primary_airport="BTS",
            main_train_station="Bratislava Hlavná Stanica",
            catchment_airports=[
                CatchmentAirport("VIE", "Vienna", "train", 50, 380.0, avg_hotel_try=3600.0),
                CatchmentAirport("BUD", "Budapest", "train", 140, 550.0, avg_hotel_try=2400.0),
            ],
            transit_connections=[
                TransitConnection("VIE", "Bratislava", "train", 50, 380.0, "ÖBB / Slovak Rail", "Wien Hbf", "Bratislava hl.st."),
                TransitConnection("BUD", "Bratislava", "train", 140, 550.0, "EuroCity", "Budapest Keleti", "Bratislava hl.st."),
            ]
        )

        # 4. Budapest Cluster
        self._clusters["Budapest"] = CityCluster(
            city_name="Budapest",
            primary_airport="BUD",
            main_train_station="Budapest Keleti",
            catchment_airports=[
                CatchmentAirport("VIE", "Vienna", "train", 155, 650.0, avg_hotel_try=3600.0),
                CatchmentAirport("BTS", "Bratislava", "train", 140, 550.0, avg_hotel_try=2200.0),
            ],
            transit_connections=[
                TransitConnection("VIE", "Budapest", "train", 155, 650.0, "ÖBB Railjet", "Wien Hbf", "Budapest Keleti"),
                TransitConnection("BTS", "Budapest", "train", 140, 550.0, "EuroCity", "Bratislava hl.st.", "Budapest Keleti"),
            ]
        )

        # 5. Munich Cluster
        self._clusters["Munich"] = CityCluster(
            city_name="Munich",
            primary_airport="MUC",
            main_train_station="München Hauptbahnhof",
            catchment_airports=[
                CatchmentAirport("NUE", "Nuremberg", "train", 65, 450.0, avg_hotel_try=2800.0),
                CatchmentAirport("STR", "Stuttgart", "train", 125, 650.0, avg_hotel_try=3500.0),
                CatchmentAirport("SZG", "Salzburg", "train", 95, 520.0, avg_hotel_try=3800.0),
            ],
            transit_connections=[
                TransitConnection("NUE", "Munich", "train", 65, 450.0, "DB ICE", "Nürnberg Hbf", "München Hbf"),
                TransitConnection("STR", "Munich", "train", 125, 650.0, "DB ICE", "Stuttgart Hbf", "München Hbf"),
                TransitConnection("SZG", "Munich", "train", 95, 520.0, "ÖBB Railjet", "Salzburg Hbf", "München Hbf"),
            ]
        )

        # 6. Berlin Cluster
        self._clusters["Berlin"] = CityCluster(
            city_name="Berlin",
            primary_airport="BER",
            main_train_station="Berlin Hauptbahnhof",
            catchment_airports=[
                CatchmentAirport("DRS", "Dresden", "train", 120, 500.0, avg_hotel_try=2300.0),
                CatchmentAirport("LEJ", "Leipzig", "train", 75, 420.0, avg_hotel_try=2000.0),
            ],
            transit_connections=[
                TransitConnection("DRS", "Berlin", "train", 120, 500.0, "DB EuroCity", "Dresden Hbf", "Berlin Hbf"),
                TransitConnection("LEJ", "Berlin", "train", 75, 420.0, "DB ICE", "Leipzig Hbf", "Berlin Hbf"),
            ]
        )

        # 7. Thessaloniki Cluster
        self._clusters["Thessaloniki"] = CityCluster(
            city_name="Thessaloniki",
            primary_airport="SKG",
            main_train_station="Thessaloniki Railway Station",
            catchment_airports=[
                CatchmentAirport("ATH", "Athens", "train", 260, 850.0, avg_hotel_try=2500.0),
                CatchmentAirport("SOF", "Sofia", "bus", 300, 750.0, avg_hotel_try=1800.0),
            ],
            transit_connections=[
                TransitConnection("ATH", "Thessaloniki", "train", 260, 850.0, "Hellenic Train", "Athens Larissis", "Thessaloniki"),
                TransitConnection("SOF", "Thessaloniki", "bus", 300, 750.0, "Arda Tur / FlixBus", "Sofia Central", "Thessaloniki"),
            ]
        )

        # 8. Athens Cluster
        self._clusters["Athens"] = CityCluster(
            city_name="Athens",
            primary_airport="ATH",
            main_train_station="Athens Larissis Station",
            catchment_airports=[
                CatchmentAirport("SKG", "Thessaloniki", "train", 260, 850.0, avg_hotel_try=2000.0),
            ],
            transit_connections=[
                TransitConnection("SKG", "Athens", "train", 260, 850.0, "Hellenic Train", "Thessaloniki", "Athens Larissis"),
            ]
        )

    def get_cluster(self, city_name_or_code: Optional[str]) -> Optional[CityCluster]:
        if not city_name_or_code:
            return None
        norm = self._normalize_key(city_name_or_code)
        target = self.CITY_ALIASES.get(norm, norm).lower()
        for name, cluster in self._clusters.items():
            if self._normalize_key(name) == target or self._normalize_key(cluster.primary_airport) == norm:
                return cluster
        return None

    def get_transit_connections(self, airport_code: Optional[str], to_city: Optional[str]) -> List[TransitConnection]:
        if not airport_code or not to_city:
            return []
        cluster = self.get_cluster(to_city)
        if not cluster:
            return []
        airport_upper = airport_code.strip().upper()
        return [c for c in cluster.transit_connections if c.from_id.upper() == airport_upper]

    CITY_COORDINATES: Dict[str, Dict[str, float]] = {
        "prague": {"lat": 50.0755, "lon": 14.4378},
        "prg": {"lat": 50.1008, "lon": 14.2600},
        "vienna": {"lat": 48.2082, "lon": 16.3738},
        "vie": {"lat": 48.1103, "lon": 16.5697},
        "bratislava": {"lat": 48.1486, "lon": 17.1077},
        "bts": {"lat": 48.1700, "lon": 17.2127},
        "budapest": {"lat": 47.4979, "lon": 19.0402},
        "bud": {"lat": 47.4369, "lon": 19.2556},
        "munich": {"lat": 48.1351, "lon": 11.5820},
        "muc": {"lat": 48.3537, "lon": 11.7750},
        "berlin": {"lat": 52.5200, "lon": 13.4050},
        "ber": {"lat": 52.3667, "lon": 13.5033},
        "dresden": {"lat": 51.0504, "lon": 13.7373},
        "drs": {"lat": 51.1328, "lon": 13.7672},
        "leipzig": {"lat": 51.3397, "lon": 12.3731},
        "lej": {"lat": 51.4239, "lon": 12.2364},
        "nuremberg": {"lat": 49.4521, "lon": 11.0767},
        "nue": {"lat": 49.4987, "lon": 11.0780},
        "stuttgart": {"lat": 48.7758, "lon": 9.1829},
        "str": {"lat": 48.6899, "lon": 9.2220},
        "salzburg": {"lat": 47.8095, "lon": 13.0550},
        "szg": {"lat": 47.7933, "lon": 13.0044},
        "thessaloniki": {"lat": 40.6401, "lon": 22.9444},
        "skg": {"lat": 40.5197, "lon": 22.9709},
        "athens": {"lat": 37.9838, "lon": 23.7275},
        "ath": {"lat": 37.9364, "lon": 23.9445},
        "sofia": {"lat": 42.6977, "lon": 23.3219},
        "sof": {"lat": 42.6951, "lon": 23.4061},
        "istanbul": {"lat": 41.0082, "lon": 28.9784},
        "ist": {"lat": 41.2753, "lon": 28.7519},
        "saw": {"lat": 40.8986, "lon": 29.3092},
        "ankara": {"lat": 39.9334, "lon": 32.8597},
        "esb": {"lat": 40.1281, "lon": 32.9951},
        "izmir": {"lat": 38.4237, "lon": 27.1428},
        "adb": {"lat": 38.2924, "lon": 27.1570},
        "antalya": {"lat": 36.8969, "lon": 30.7133},
        "ayt": {"lat": 36.8987, "lon": 30.8005},
        "rome": {"lat": 41.9028, "lon": 12.4964},
        "fco": {"lat": 41.8003, "lon": 12.2389},
        "paris": {"lat": 48.8566, "lon": 2.3522},
        "cdg": {"lat": 49.0097, "lon": 2.5479},
        "london": {"lat": 51.5074, "lon": -0.1278},
        "lhr": {"lat": 51.4700, "lon": -0.4543},
    }

    def get_coordinates(self, place_name_or_code: Optional[str]) -> Optional[Dict[str, float]]:
        if not place_name_or_code:
            return None
        norm = self._normalize_key(place_name_or_code)
        target = self.CITY_ALIASES.get(norm, norm).lower()
        return self.CITY_COORDINATES.get(target) or self.CITY_COORDINATES.get(norm)

    def get_transfer_buffer_mins(self, airport_code: str) -> int:
        return max(90, self._airport_buffers.get(airport_code.strip().upper(), 90))

    def list_cities(self) -> List[str]:
        return list(self._clusters.keys())

    def all_clusters(self) -> List[CityCluster]:
        """Every registered city cluster, in registration order."""
        return list(self._clusters.values())

    def stations_for(self, place: Optional[str]) -> List[str]:
        """Rail/bus station names a cluster city or its airport stands for.

        Returns the cluster's main train station, the stations of its transit connections
        (e.g. "Praha hl.n." for Prague), and the stations that other clusters' links leave
        from this cluster's airport (e.g. "Budapest Kelenföld" for BUD). Names are unique and
        keep that order. Returns [] when ``place`` is not a cluster city or airport (a station
        name or an unknown place).
        """
        cluster = self.get_cluster(place)
        if cluster is None:
            return []
        names = [cluster.main_train_station]
        names.extend(conn.station_to for conn in cluster.transit_connections)
        for other in self.all_clusters():
            for conn in other.transit_connections:
                if conn.from_id == cluster.primary_airport:
                    names.append(conn.station_from)
        return list(dict.fromkeys(names))

    # Folded (ASCII, lowercase) country names -> ISO country code
    COUNTRY_NAMES: Dict[str, str] = {
        "greece": "GR", "yunanistan": "GR", "hellas": "GR",
        "turkey": "TR", "turkiye": "TR", "turkiye cumhuriyeti": "TR",
        "czech republic": "CZ", "czechia": "CZ", "cekya": "CZ",
        "austria": "AT", "avusturya": "AT",
        "slovakia": "SK", "slovakya": "SK",
        "hungary": "HU", "macaristan": "HU",
        "germany": "DE", "almanya": "DE",
    }

    # Folded city names (English, lowercase) -> IATA codes for cities that are not cluster primaries
    CITY_AIRPORTS: Dict[str, List[str]] = {
        "istanbul": ["IST", "SAW"],
        "ankara": ["ESB"],
        "izmir": ["ADB"],
        "antalya": ["AYT"],
        "athens": ["ATH"],
        "thessaloniki": ["SKG"],
        "prague": ["PRG"],
        "vienna": ["VIE"],
        "bratislava": ["BTS"],
        "budapest": ["BUD"],
        "munich": ["MUC"],
        "berlin": ["BER"],
        "heraklion": ["HER"],
        "crete": ["HER", "CHQ"],
        "girit": ["HER", "CHQ"],
        "rhodes": ["RHO"],
        "rodos": ["RHO"],
        "corfu": ["CFU"],
        "korfu": ["CFU"],
        "mykonos": ["JMK"],
        "santorini": ["JTR"],
        "kos": ["KGS"],
        "kefalonia": ["EFL"],
        "zakynthos": ["ZTH"],
    }

    @classmethod
    def _fold_text(cls, text: str) -> str:
        """Lowercase ASCII key: strips accents/diacritics, then applies the Turkish letter mapping."""
        decomposed = unicodedata.normalize("NFKD", text)
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
        return cls._normalize_key(stripped)

    def _known_airport_codes(self) -> set:
        codes = set()
        for airports in COUNTRY_AIRPORTS.values():
            codes.update(airports)
        for cluster in self._clusters.values():
            codes.add(cluster.primary_airport)
            codes.update(c.airport_code for c in cluster.catchment_airports)
        codes.update(key.upper() for key in self.CITY_COORDINATES if len(key) == 3)
        return codes

    def is_country(self, token: Optional[str]) -> bool:
        """True when the token is a country name or a 2-letter country code from this module's tables.

        Only the registry's own tables are checked (``COUNTRY_NAMES`` and ``COUNTRY_AIRPORTS``).
        Other countries are recognised by the airport data, see RailBusProvider.
        """
        raw = str(token or "").strip()
        if not raw:
            return False
        if len(raw) == 2 and raw.upper() in COUNTRY_AIRPORTS:
            return True
        return self._fold_text(raw) in self.COUNTRY_NAMES

    def resolve_via(self, token: Optional[str]) -> List[str]:
        """Resolve a via token to IATA codes.

        Accepts a country code ("GR"), a country name in Turkish or English ("Yunanistan", "Greece"),
        a city name ("Atina", "Athens"), or an airport code ("ATH"). Matching ignores case and
        diacritics. Returns uppercase codes, or [] when the token is unknown.
        """
        raw = str(token or "").strip()
        if not raw:
            return []
        if len(raw) == 2 and raw.upper() in COUNTRY_AIRPORTS:
            return list(COUNTRY_AIRPORTS[raw.upper()])

        key = self._fold_text(raw)
        if not key:
            return []
        country = self.COUNTRY_NAMES.get(key)
        if country:
            return list(COUNTRY_AIRPORTS[country])

        english = self.CITY_ALIASES.get(key, key).lower()
        if english in self.CITY_AIRPORTS:
            return list(self.CITY_AIRPORTS[english])
        cluster = self.get_cluster(raw)
        if cluster:
            return [cluster.primary_airport]

        if len(raw) == 3 and raw.upper() in self._known_airport_codes():
            return [raw.upper()]
        return []
