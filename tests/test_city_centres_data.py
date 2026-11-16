"""Offline sanity checks for travel_engine/data/city_centres.csv.

No network access. Only 10 major cities (SPOT_CHECKS below) are spot-checked against
known reference coordinates. Automated Wikidata verification was unreliable (its
coordinates are often administrative centroids, not city centres), so the other rows
are unverified; these tests guard the file's structure, the coordinate ranges and
those anchors only.
"""
import csv
import math
from pathlib import Path

import pytest

CSV_PATH = Path(__file__).resolve().parent.parent / "travel_engine" / "data" / "city_centres.csv"

LAT_RANGE = (27.0, 72.0)
LON_RANGE = (-32.0, 60.0)
# Svalbard lies outside the Europe+TR+Canaries/Azores/Iceland box but is a legitimate
# Norwegian city centre, so it is allow-listed explicitly rather than widening the range.
RANGE_EXCEPTIONS = {("Longyearbyen", "NO")}

SPOT_CHECKS = [
    ("Prague", "CZ", 50.0875, 14.4214),
    ("Paris", "FR", 48.8566, 2.3522),
    ("London", "GB", 51.5072, -0.1276),
    ("Milan", "IT", 45.4642, 9.19),
    ("Istanbul", "TR", 41.0082, 28.9784),
    ("Athens", "GR", 37.9838, 23.7275),
    ("Vienna", "AT", 48.2082, 16.3738),
    ("Berlin", "DE", 52.52, 13.405),
    ("Madrid", "ES", 40.4168, -3.7038),
    ("Rome", "IT", 41.9028, 12.4964),
]


def _load_rows():
    with CSV_PATH.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


ROWS = _load_rows()


def test_csv_header_and_shape():
    with CSV_PATH.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        body = list(reader)
    assert header == ["city", "country", "lat", "lon"]
    assert len(body) > 400
    for line_no, fields in enumerate(body, start=2):
        assert len(fields) == 4, f"line {line_no} has {len(fields)} fields"
        assert fields[0].strip(), f"line {line_no} has empty city"
        assert len(fields[1]) == 2 and fields[1].isupper(), f"line {line_no} bad ISO2 {fields[1]!r}"


def test_coordinates_parse_as_floats():
    for row in ROWS:
        float(row["lat"])
        float(row["lon"])


def test_no_duplicate_city_country():
    seen = set()
    dups = []
    for row in ROWS:
        key = (row["city"], row["country"])
        if key in seen:
            dups.append(key)
        seen.add(key)
    assert not dups, f"duplicate (city, country) rows: {dups}"


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"{r['city']}-{r['country']}")
def test_coordinates_in_european_box(row):
    lat, lon = float(row["lat"]), float(row["lon"])
    if (row["city"], row["country"]) in RANGE_EXCEPTIONS:
        return
    assert LAT_RANGE[0] <= lat <= LAT_RANGE[1], f"{row['city']} lat {lat} out of range"
    assert LON_RANGE[0] <= lon <= LON_RANGE[1], f"{row['city']} lon {lon} out of range"


@pytest.mark.parametrize("city,country,lat,lon", SPOT_CHECKS, ids=[c[0] for c in SPOT_CHECKS])
def test_spot_checks_within_2km(city, country, lat, lon):
    matches = [r for r in ROWS if r["city"] == city and r["country"] == country]
    assert len(matches) == 1, f"expected exactly one {city},{country} row, got {len(matches)}"
    row = matches[0]
    dist = _haversine_km(float(row["lat"]), float(row["lon"]), lat, lon)
    assert dist <= 2.0, f"{city}: CSV ({row['lat']}, {row['lon']}) is {dist:.2f} km from ({lat}, {lon})"
