"""Record golden routes dataset using RouteEngine and live providers (C04 / TST-2a).

Runs the 4 reference scenarios with budget 40, recording all provider calls into tests/golden/<scenario>/.
Respects strict live rate limits:
- TRAVELPLAN_MAX_BROWSERS=1
- At least 8s between Google Flights page loads
- Max 2 Google Maps page loads per scenario
- 5 minutes cooldown between scenarios
- Total flight pages <= 160
- Aborts immediately on captcha / blocked / /sorry/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict

# Isolated environment variables per AJAN_KURALLARI section 6
SCRATCH_DIR = os.path.join(tempfile.gettempdir(), "rv2_scratch", "C04")
os.makedirs(SCRATCH_DIR, exist_ok=True)
os.environ.setdefault("TRAVELPLAN_DB", os.path.join(SCRATCH_DIR, "travelplan.db"))
os.environ.setdefault("TRAVELPLAN_SEARCH_LOG", os.path.join(SCRATCH_DIR, "search_log.db"))
os.environ.setdefault("TRAVELPLAN_FLIGHT_CACHE", os.path.join(SCRATCH_DIR, "fc.db"))
os.environ.setdefault("TRAVELPLAN_GROUND_CACHE", os.path.join(SCRATCH_DIR, "gc.db"))
os.environ.setdefault("TRAVELPLAN_NO_MIGRATE", "1")
os.environ.setdefault("TRAVELPLAN_MAX_BROWSERS", "1")
os.environ.setdefault("TRAVELPLAN_PORT", "8899")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from travel_engine.airports import AirportDB
from travel_engine.geo_clusters import GeoClusterRegistry
from travel_engine.providers.flixbus_provider import FlixBusProvider
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider
from travel_engine.providers.google_hotels_provider import GoogleHotelsProvider
from travel_engine.providers.google_maps_transit_provider import GoogleMapsTransitProvider
from travel_engine.providers.live_rail_provider import LiveRailProvider
from travel_engine.providers.obilet_fares import ObiletFromPrices
from travel_engine.providers.rail_bus_provider import RailBusProvider
from travel_engine.providers.transitous_provider import TransitousProvider
from travel_engine.providers.trenitalia_provider import TrenitaliaProvider
from travel_engine.replay import RecordReplayFlightProvider, RecordReplayGroundProvider
from travel_engine.route_engine import RouteEngine
from travel_engine.search_log import SearchLog

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("golden_record")

GOLDEN_DIR = os.path.join(REPO_ROOT, "tests", "golden")

SCENARIOS: Dict[str, Dict[str, Any]] = {
    "ath_prg": {
        "origin": "Atina",
        "destination": "Prag",
        "depart_dates": ["2026-11-12"],
        "max_stops": 2,
    },
    "ist_prg": {
        "origin": "İstanbul",
        "destination": "Prag",
        "depart_dates": ["2026-11-13"],
        "max_stops": 2,
    },
    "prg_ist_via": {
        "origin": "Prag",
        "destination": "İstanbul",
        "depart_dates": ["2026-11-14"],
        "via_fixed": ["Selanik"],
    },
    "lon_vie": {
        "origin": "Londra",
        "destination": "Viyana",
        "depart_dates": ["2026-11-12"],
        "max_stops": 2,
    },
}


def kill_orphaned_browsers() -> None:
    """Ensure no leftover chrome-headless-shell processes remain on Windows."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "chrome-headless-shell.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass


def build_live_engine(
    scenario_storage_dir: str,
    search_log_path: str,
    maps_budget_limit: int = 2,
) -> RouteEngine:
    """Build RouteEngine with real providers wrapped in record mode."""
    search_log = SearchLog(search_log_path)
    # Rate limit: at least 8.0 seconds between Google Flights requests
    real_fp = GoogleFlightsProvider(
        search_log=search_log,
        min_interval_secs=8.0,
    )
    geo_registry = GeoClusterRegistry()
    real_maps = GoogleMapsTransitProvider()
    real_gp = RailBusProvider(
        geo_registry,
        live_provider=LiveRailProvider(),
        bus_provider=FlixBusProvider(),
        trenitalia_provider=TrenitaliaProvider(),
        timetable_provider=TransitousProvider(),
        timetable_maps_provider=real_maps,
        bus_from_price_provider=ObiletFromPrices(),
    )
    real_gp.maps_budget_limit = maps_budget_limit

    wrapped_fp = RecordReplayFlightProvider(
        provider=real_fp,
        storage_dir=scenario_storage_dir,
        mode="record",
    )
    wrapped_gp = RecordReplayGroundProvider(
        provider=real_gp,
        storage_dir=scenario_storage_dir,
        mode="record",
    )
    wrapped_gp.maps_budget_limit = maps_budget_limit

    hotels = GoogleHotelsProvider()
    return RouteEngine(
        flight_provider=wrapped_fp,
        ground_provider=wrapped_gp,
        search_log=search_log,
        hotel_provider=hotels,
        airport_db=AirportDB.load_default(),
        max_parallel_searches=1,
    )


def record_scenario(
    name: str,
    config: Dict[str, Any],
    out_dir: str = GOLDEN_DIR,
    flight_budget: int = 40,
    maps_budget: int = 2,
) -> Dict[str, Any]:
    """Execute live recording for one scenario."""
    scenario_dir = os.path.join(out_dir, name)
    os.makedirs(scenario_dir, exist_ok=True)
    logger.info("=== Recording scenario [%s] into %s (budget=%d) ===", name, scenario_dir, flight_budget)

    search_log_path = os.environ.get("TRAVELPLAN_SEARCH_LOG", os.path.join(SCRATCH_DIR, "search_log.db"))
    engine = build_live_engine(scenario_dir, search_log_path, maps_budget_limit=maps_budget)

    search_args = dict(config)
    search_args["flight_search_budget"] = flight_budget

    t_start = time.time()
    res = engine.search(**search_args)
    elapsed = time.time() - t_start

    trace = res.get("trace", {})
    counters = trace.get("counters", {}) if isinstance(trace, dict) else getattr(trace, "counters", {})
    warnings = res.get("warnings", [])

    flight_searches = counters.get("flight_searches", 0)
    google_page_loads = counters.get("google_page_loads", 0)
    maps_page_loads = counters.get("maps_page_loads", 0)
    google_blocked = counters.get("google_blocked", 0)

    logger.info(
        "Scenario [%s] finished in %.1f s: flight_searches=%d, google_page_loads=%d, maps_page_loads=%d, options=%d",
        name,
        elapsed,
        flight_searches,
        google_page_loads,
        maps_page_loads,
        len(res.get("options", [])),
    )

    # Captcha / sorry detection
    warning_text = " ".join(str(w) for w in warnings).lower()
    if google_blocked > 0 or "blocked" in warning_text or "captcha" in warning_text or "/sorry/" in warning_text:
        kill_orphaned_browsers()
        msg = f"DUR: Captcha or /sorry/ block detected in scenario {name}! Warnings: {warnings}"
        logger.error(msg)
        raise RuntimeError(msg)

    # Page load limits check
    if google_page_loads > flight_budget:
        logger.warning("Google page loads (%d) exceeded flight budget (%d)", google_page_loads, flight_budget)
    if maps_page_loads > maps_budget:
        logger.warning("Maps page loads (%d) exceeded maps budget limit (%d)", maps_page_loads, maps_budget)

    return {
        "scenario": name,
        "elapsed_secs": round(elapsed, 2),
        "options_count": len(res.get("options", [])),
        "flight_searches": flight_searches,
        "google_page_loads": google_page_loads,
        "maps_page_loads": maps_page_loads,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Record golden route scenarios (C04)")
    parser.add_argument("--scenario", choices=list(SCENARIOS.keys()) + ["all"], default="all")
    parser.add_argument("--cooldown-secs", type=int, default=300, help="Wait between scenarios (default 300s = 5m)")
    parser.add_argument("--flight-budget", type=int, default=40)
    parser.add_argument("--maps-budget", type=int, default=2)
    parser.add_argument("--out-dir", default=GOLDEN_DIR)
    args = parser.parse_args()

    targets = list(SCENARIOS.keys()) if args.scenario == "all" else [args.scenario]
    logger.info("Starting golden recording for scenarios: %s", targets)

    results = []
    total_flight_pages = 0
    total_maps_pages = 0

    try:
        for idx, name in enumerate(targets):
            cfg = SCENARIOS[name]
            stats = record_scenario(
                name,
                cfg,
                out_dir=args.out_dir,
                flight_budget=args.flight_budget,
                maps_budget=args.maps_budget,
            )
            results.append(stats)
            total_flight_pages += stats["google_page_loads"]
            total_maps_pages += stats["maps_page_loads"]

            # If there's another scenario, sleep for cooldown
            if idx < len(targets) - 1:
                logger.info("Cooldown: sleeping %d seconds (5 minutes) before next scenario...", args.cooldown_secs)
                time.sleep(args.cooldown_secs)

    finally:
        kill_orphaned_browsers()

    print("\n" + "=" * 60)
    print("GOLDEN RECORDING COMPLETE SUMMARY:")
    print("=" * 60)
    for r in results:
        print(
            f"Scenario {r['scenario']}: {r['google_page_loads']} flight pages, "
            f"{r['maps_page_loads']} maps pages, {r['options_count']} options, {r['elapsed_secs']}s"
        )
    print("-" * 60)
    print(f"Total Google Flights Pages: {total_flight_pages} (limit: <= 160)")
    print(f"Total Google Maps Pages:    {total_maps_pages}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
