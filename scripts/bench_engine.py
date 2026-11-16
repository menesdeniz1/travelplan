"""Offline benchmark of the route engine (no network): CPU time, counters and the top options.

Fake but realistically dense providers: 30 flight cards per search, hourly train/bus services
05:00-22:00 between any two places within 700 road km. The airport data and the costs module are
the real ones. Prices are invented test data; they are never shown to a user.

Usage (from the repo root):
    python scripts/bench_engine.py                       # all scenarios, one summary line each
    python scripts/bench_engine.py --scenario auto1 --profile
    python scripts/bench_engine.py --scenario all --dump-top 10 --out before.json
    python scripts/bench_engine.py --compare before.json after.json

--dump-top writes the top N options of every scenario (kind, legs, totals, badges) so a performance
change can prove that it did not change the results (--compare exits 1 on any difference).
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import random
import sys
import time
import zlib

# No network and no optional sources, before any travel_engine import.
os.environ.setdefault("TRAVELPLAN_EUR_TRY", "50")
os.environ["TRAVELPLAN_HOTELS"] = "0"
os.environ["TRAVELPLAN_TRANSITOUS"] = "0"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from travel_engine import route_engine as re_mod  # noqa: E402
from travel_engine.airports import AirportDB, haversine_km  # noqa: E402
from travel_engine.providers.rail_bus_provider import ScheduledSegment  # noqa: E402
from travel_engine.route_engine import RouteEngine  # noqa: E402

DB = AirportDB.load_default()
CARDS_PER_SEARCH = 30

SCENARIOS = {
    "auto1": dict(origin="Atina", destination="Prag", depart_dates=["2026-11-12"], max_stops=2,
                  flight_search_budget=20),
    "auto1_by": dict(origin="Atina", destination="Prag", depart_dates=["2026-11-12"], max_stops=2,
                     flight_search_budget=20, arrive_by="2026-11-12T23:59"),
    "auto3": dict(origin="İstanbul", destination="Prag", depart_dates=["2026-11-12", "2026-11-13", "2026-11-14"],
                  max_stops=2, flight_search_budget=30),
    "cal": dict(origin="İstanbul", destination="Prag", depart_dates=["2026-11-12", "2026-11-13", "2026-11-14"],
                max_stops=2, flight_search_budget=30, calendar=True),
    "via": dict(origin="İstanbul", destination="Prag", depart_dates=["2026-10-28"],
                via_fixed=["Selanik", "Bratislava", "Viyana"], min_stay_mins={"Selanik": 480, "Viyana": 600},
                flight_search_budget=30),
}


def _airport(place):
    found = DB.get(place)
    if found is None:
        resolved = DB.resolve(place)
        found = resolved[0] if resolved else None
    return found


class BenchFlights:
    """Deterministic cards for any airport pair; optionally a known Google calendar for every route."""

    def __init__(self, calendar=False):
        self.calls = []
        self.calendar = calendar

    def search_flights(self, origin="", destination="", departure_date="", return_date=None, trip_type="oneway"):
        self.calls.append((origin, destination, departure_date))
        a, b = _airport(origin), _airport(destination)
        if a is None or b is None or a.iata == b.iata:
            return []
        km = haversine_km(a.lat, a.lon, b.lat, b.lon)
        rng = random.Random(zlib.crc32(f"{a.iata}{b.iata}{departure_date}".encode()))
        base_price = 1500 + km * 4.0
        cards = []
        for i in range(CARDS_PER_SEARCH):
            dep_min = 5 * 60 + int(i * (18 * 60) / CARDS_PER_SEARCH) + rng.randint(0, 25)
            direct = rng.random() < 0.35
            dur = int(km / 750 * 60 + 35) + (0 if direct else rng.randint(90, 400))
            arr_abs = dep_min + dur
            day, arr = arr_abs // (24 * 60), arr_abs % (24 * 60)
            times = f"{dep_min // 60:02d}:{dep_min % 60:02d} – {arr // 60:02d}:{arr % 60:02d}" + (f"+{day}" if day else "")
            hub = rng.choice(["VIE", "MUC", "FRA", "IST", "WAW", "ZRH"])
            cards.append({
                "source": "bench", "airline": rng.choice(["Ryanair", "Wizz Air", "Aegean", "Lufthansa", "Austrian"]),
                "times": times, "duration": f"{dur // 60} sa. {dur % 60} dk.",
                "airports": f"{a.iata} ➔ {b.iata}", "stops": "Aktarmasız" if direct else "1 aktarma",
                "stops_count": 0 if direct else 1, "is_direct": direct,
                "layover_info": "" if direct else f"1 sa. 30 dk. {hub}",
                "total_price_try": float(round(base_price * (0.5 + rng.random() * 2.0) * (1.0 if direct else 1.2))),
                "currency": "TRY", "trip_type": "Tek yön", "badge": None,
                "google_flights_url": "https://example.invalid/",
            })
        return cards

    def get_calendar_prices(self, origin, destination, trip_type="oneway"):
        if not self.calendar:
            return {}
        a, b = _airport(origin), _airport(destination)
        if a is None or b is None:
            return {}
        km = haversine_km(a.lat, a.lon, b.lat, b.lon)
        return {d: 1500 + km * 4.0 * 0.6 for d in ("2026-11-12", "2026-11-13", "2026-11-14")}


class BenchGround:
    """Hourly services 05:10-22:10 between any two places within 700 road km (a dense live source)."""

    def __init__(self):
        self.calls = 0

    def _coord(self, place, coord):
        if coord is not None:
            return coord
        centre = DB.city_center(place)
        if centre is not None:
            return centre
        ap = _airport(place)
        return (ap.lat, ap.lon) if ap else None

    def ground_distance_km(self, from_place, to_place, from_coord=None, to_coord=None):
        a, b = self._coord(from_place, from_coord), self._coord(to_place, to_coord)
        if a is None or b is None:
            return None
        return haversine_km(a[0], a[1], b[0], b[1]) * 1.25

    def find_ground_live(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None,
                         to_coord=None, max_results=6, **_kw):
        self.calls += 1
        km = self.ground_distance_km(from_place, to_place, from_coord, to_coord)
        if km is None or km > 700 or km < 15:
            return []
        dur = int(km / 75 * 60 + 20)
        out = []
        for h in range(5, 23):
            dep = f"{h:02d}:10"
            if earliest_dep_time and dep < earliest_dep_time:
                continue
            arr_abs = h * 60 + 10 + dur
            out.append(ScheduledSegment(
                origin_station=from_place, dest_station=to_place, departure_time=dep,
                arrival_time=f"{(arr_abs // 60) % 24:02d}:{arr_abs % 60:02d}", duration_mins=dur,
                price_try=float(round(300 + km * 2.5)), operator="RegioJet" if h % 2 else "FlixBus",
                mode="train" if h % 2 else "bus", date=date_str, is_estimated=False, price_eur=None,
                price_is_estimated=False, source="live" if h % 2 else "flixbus"))
        return out[:max_results]

    def find_ground(self, from_place, to_place, date_str, earliest_dep_time=None, from_coord=None, to_coord=None,
                    max_results=6, max_road_km=700.0, **_kw):
        return []  # everything comes from find_ground_live


def _summary(option):
    return {
        "kind": option.get("kind"),
        "true_total_try": option.get("true_total_try"),
        "score": option.get("score"),
        "badges": sorted(option.get("badges") or []),
        "legs": [[leg.get("type"), leg.get("operator"), leg.get("from"), leg.get("to"),
                  leg.get("depart_at"), leg.get("arrive_at"), leg.get("price_try")] for leg in option.get("legs") or []],
    }


def run_scenario(name, profile=False, dump_top=0):
    params = dict(SCENARIOS[name])
    flights = BenchFlights(calendar=params.pop("calendar", False))
    ground = BenchGround()
    engine = RouteEngine(flights, ground, airport_db=DB, max_parallel_searches=1)
    counters = {"dfs_runs": 0, "complete_chains": 0, "options_built": 0, "best_score_calls": 0}
    patched = []

    def wrap(owner, attr, before=None, after=None):
        original = getattr(owner, attr, None)
        if original is None:
            return  # renamed or removed by a later change: that counter is simply not reported

        def wrapper(*args, **kwargs):
            if before:
                before()
            out = original(*args, **kwargs)
            if after:
                after(out)
            return out

        setattr(owner, attr, wrapper)
        patched.append((owner, attr, original))

    def count_dfs(out):
        counters["dfs_runs"] += 1
        counters["complete_chains"] += len(out)

    wrap(re_mod._Run, "dfs", after=count_dfs)
    wrap(RouteEngine, "_option", before=lambda: counters.__setitem__("options_built", counters["options_built"] + 1))
    wrap(re_mod._Run, "best_score",
         before=lambda: counters.__setitem__("best_score_calls", counters["best_score_calls"] + 1))
    prof = cProfile.Profile() if profile else None
    started = time.perf_counter()
    try:
        if prof:
            prof.enable()
        result = engine.search(**params)
    finally:
        if prof:
            prof.disable()
        for owner, attr, original in patched:
            setattr(owner, attr, original)
    wall = time.perf_counter() - started
    line = (f"{name:6} wall {wall:7.2f}s | flight searches {len(flights.calls):3} | ground queries {ground.calls:4} | "
            f"dfs runs {counters['dfs_runs']:3} | complete chains {counters['complete_chains']:7} | "
            f"options built {counters['options_built']:7} | best_score calls {counters['best_score_calls']:3} | "
            f"pruned {result.get('pruned_searches', 0):3} | options {len(result['options']):3} | "
            f"warnings {len(result['warnings'])}")
    print(line)
    if result.get("trace"):
        print("        trace:", json.dumps(result["trace"], ensure_ascii=False))
    if prof:
        stream = io.StringIO()
        pstats.Stats(prof, stream=stream).sort_stats("cumulative").print_stats(20)
        print("\n".join(stream.getvalue().splitlines()[:48]))
    return {"wall_secs": round(wall, 3), "top": [_summary(o) for o in result["options"][:dump_top]]} if dump_top else None


def compare(path_a, path_b):
    with open(path_a, encoding="utf-8") as fa, open(path_b, encoding="utf-8") as fb:
        a, b = json.load(fa), json.load(fb)
    differences = 0
    for name in sorted(set(a) | set(b)):
        top_a = (a.get(name) or {}).get("top")
        top_b = (b.get(name) or {}).get("top")
        if top_a == top_b:
            print(f"{name}: top options identical")
            continue
        differences += 1
        print(f"{name}: top options DIFFER")
        for i, (x, y) in enumerate(zip(top_a or [], top_b or [])):
            if x != y:
                print(f"  first difference at rank {i + 1}:\n    before: {json.dumps(x, ensure_ascii=False)}\n"
                      f"    after:  {json.dumps(y, ensure_ascii=False)}")
                break
        else:
            print(f"  lengths differ: {len(top_a or [])} vs {len(top_b or [])}")
    return 1 if differences else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", default="all", choices=["all"] + sorted(SCENARIOS))
    parser.add_argument("--profile", action="store_true", help="print the 20 most expensive functions")
    parser.add_argument("--dump-top", type=int, default=0, help="write the top N options per scenario to --out")
    parser.add_argument("--out", default="", help="JSON file for --dump-top")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="compare two --dump-top files")
    args = parser.parse_args(argv)
    if args.compare:
        return compare(*args.compare)
    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    dumped = {}
    for name in names:
        out = run_scenario(name, profile=args.profile, dump_top=args.dump_top)
        if out is not None:
            dumped[name] = out
    if args.dump_top:
        if not args.out:
            parser.error("--dump-top needs --out")
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(dumped, handle, ensure_ascii=False, indent=1)
        print(f"top options written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
