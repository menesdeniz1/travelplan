#!/usr/bin/env python3
"""Audit city names of large and medium airports against city_centres.csv (PLN / AIR-4, T09).

Lists large and medium airports from airports.csv whose city column does not match
any city in city_centres.csv.
"""

import csv
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from travel_engine.airports import _norm

DATA_DIR = ROOT / "travel_engine" / "data"
AIRPORTS_PATH = DATA_DIR / "airports.csv"
CITY_CENTRES_PATH = DATA_DIR / "city_centres.csv"


def audit_city_names():
    known_cities = set()
    with open(CITY_CENTRES_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            city = row.get("city", "").strip()
            if city:
                known_cities.add(_norm(city))

    unmatched = []
    with open(AIRPORTS_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            size = row.get("size", "").strip()
            if size in ("large", "medium"):
                city = row.get("city", "").strip()
                iata = row.get("iata", "").strip().upper()
                name = row.get("name", "").strip()
                if _norm(city) not in known_cities:
                    unmatched.append((iata, name, city))

    return unmatched


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    unmatched = audit_city_names()
    print(f"Total unmatched airports (large & medium): {len(unmatched)}")
    print("-" * 80)
    print(f"{'IATA':<6} {'City Column':<30} {'Airport Name'}")
    print("-" * 80)
    for iata, name, city in unmatched:
        print(f"{iata:<6} {city:<30} {name}")


if __name__ == "__main__":
    main()
