"""Offline replay test for golden routes benchmark (C04, C05)."""

import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel_engine.airports import AirportDB
from travel_engine.observations import ObservationStore
from travel_engine.replay import RecordReplayFlightProvider, RecordReplayGroundProvider
from travel_engine.route_engine import RouteEngine

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")
BASELINE_PATH = os.path.join(GOLDEN_DIR, "baseline.json")

SCENARIOS = {
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


def evaluate_scenario(
    name: str, config: Dict[str, Any], golden_base: str = GOLDEN_DIR, with_memory: bool = False
) -> Dict[str, Any]:
    """Run a scenario in offline replay mode with budgets 40 and 20, returning hit metrics."""
    scenario_dir = os.path.join(golden_base, name)
    if not os.path.isdir(scenario_dir):
        raise FileNotFoundError(f"Recorded cassette directory not found: {scenario_dir}")

    fp = RecordReplayFlightProvider(provider=None, storage_dir=scenario_dir, mode="replay")
    gp = RecordReplayGroundProvider(provider=None, storage_dir=scenario_dir, mode="replay")
    db = AirportDB.load_default()

    store = None
    tmp_ctx = None
    if with_memory:
        tmp_ctx = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmp_ctx.name, f"obs_{name}.db")
        store = ObservationStore(db_path)

    try:
        # 1. Budget 40: find best known true_total_try (preferring price_complete)
        engine40 = RouteEngine(fp, gp, airport_db=db, max_parallel_searches=1, observations=store)
        kwargs40 = dict(config)
        kwargs40["flight_search_budget"] = 40
        res40 = engine40.search(**kwargs40)
        options40 = res40.get("options", [])

        complete_opts = [o for o in options40 if o.get("price_complete") and o.get("true_total_try") is not None]
        if complete_opts:
            best_known = min(o["true_total_try"] for o in complete_opts)
        else:
            valid_opts = [o for o in options40 if o.get("true_total_try") is not None]
            best_known = min((o["true_total_try"] for o in valid_opts), default=None)

        # 2. Budget 20: test if any of top 3 is within 2% of best_known
        engine20 = RouteEngine(fp, gp, airport_db=db, max_parallel_searches=1, observations=store)
        kwargs20 = dict(config)
        kwargs20["flight_search_budget"] = 20
        res20 = engine20.search(**kwargs20)
        options20 = res20.get("options", [])
        top3 = options20[:3]
        top3_prices = [o["true_total_try"] for o in top3 if o.get("true_total_try") is not None]
        budget_20_best = min(top3_prices) if top3_prices else None

        hit = False
        if best_known is not None and top3_prices:
            margin_cap = best_known * 1.02
            hit = any(p <= margin_cap for p in top3_prices)

        misses = getattr(fp, "replay_misses", 0) + getattr(gp, "replay_misses", 0)
        return {
            "best_known": best_known,
            "budget_20_best": budget_20_best,
            "hit": hit,
            "misses": misses,
            "options_b40": len(options40),
            "options_b20": len(options20),
        }
    finally:
        if store is not None:
            store.close()
        if tmp_ctx is not None:
            try:
                tmp_ctx.cleanup()
            except Exception:
                pass


def evaluate_all(golden_base: str = GOLDEN_DIR, with_memory: bool = False) -> Dict[str, Any]:
    """Evaluate all scenarios and compute aggregate score."""
    scenario_results = {}
    hits = 0
    total_misses = 0
    for name, cfg in SCENARIOS.items():
        res = evaluate_scenario(name, cfg, golden_base=golden_base, with_memory=with_memory)
        scenario_results[name] = res
        if res["hit"]:
            hits += 1
        total_misses += res["misses"]

    score = hits / len(SCENARIOS) if SCENARIOS else 0.0
    return {
        "score": round(score, 4),
        "total_misses": total_misses,
        "scenarios": scenario_results,
    }


def generate_baseline(out_path: str = BASELINE_PATH, golden_base: str = GOLDEN_DIR) -> Dict[str, Any]:
    """Generate baseline.json from current engine replay results."""
    eval_no_mem = evaluate_all(golden_base=golden_base, with_memory=False)
    eval_mem = evaluate_all(golden_base=golden_base, with_memory=True)
    baseline_payload = {
        "score": eval_no_mem["score"],
        "total_misses": eval_no_mem["total_misses"],
        "scenarios": {
            name: {
                "best_known": s["best_known"],
                "budget_20_best": s["budget_20_best"],
                "hit": s["hit"],
                "misses": s["misses"],
            }
            for name, s in eval_no_mem["scenarios"].items()
        },
        "score_with_memory": eval_mem["score"],
        "total_misses_with_memory": eval_mem["total_misses"],
        "scenarios_with_memory": {
            name: {
                "best_known": s["best_known"],
                "budget_20_best": s["budget_20_best"],
                "hit": s["hit"],
                "misses": s["misses"],
            }
            for name, s in eval_mem["scenarios"].items()
        },
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(baseline_payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return baseline_payload


class GoldenRoutesTests(unittest.TestCase):
    def test_golden_routes_replay_matches_baseline(self):
        if not os.path.exists(BASELINE_PATH):
            self.fail(f"Baseline file missing: {BASELINE_PATH}. Generate with scripts/golden_record.py.")

        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            baseline = json.load(f)

        eval_res = evaluate_all(golden_base=GOLDEN_DIR, with_memory=False)
        score = eval_res["score"]
        base_score = float(baseline.get("score", 0.0))

        # Raporlama için senaryo çıktıları
        print(f"\n[Golden Routes - No Memory] Score: {score:.2f} (baseline: {base_score:.2f}), misses: {eval_res['total_misses']}")
        for name, s in eval_res["scenarios"].items():
            print(
                f"  - {name}: hit={s['hit']}, best_known={s['best_known']}, "
                f"b20_best={s['budget_20_best']}, misses={s['misses']}"
            )

        self.assertGreaterEqual(
            score,
            base_score,
            f"Golden route score {score:.2f} fell below baseline {base_score:.2f}",
        )

    def test_golden_routes_with_memory_matches_baseline(self):
        if not os.path.exists(BASELINE_PATH):
            self.fail(f"Baseline file missing: {BASELINE_PATH}. Generate with scripts/golden_record.py.")

        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            baseline = json.load(f)

        if "score_with_memory" not in baseline:
            self.fail(f"Baseline missing score_with_memory in {BASELINE_PATH}. Run --update-baseline.")

        eval_res = evaluate_all(golden_base=GOLDEN_DIR, with_memory=True)
        score = eval_res["score"]
        base_score = float(baseline.get("score_with_memory", 0.0))

        # Raporlama için senaryo çıktıları
        print(f"\n[Golden Routes - With Memory] Score: {score:.2f} (baseline: {base_score:.2f}), misses: {eval_res['total_misses']}")
        for name, s in eval_res["scenarios"].items():
            print(
                f"  - {name}: hit={s['hit']}, best_known={s['best_known']}, "
                f"b20_best={s['budget_20_best']}, misses={s['misses']}"
            )

        self.assertGreaterEqual(
            score,
            base_score,
            f"Golden route with-memory score {score:.2f} fell below baseline {base_score:.2f}",
        )


if __name__ == "__main__":
    if "--update-baseline" in sys.argv:
        payload = generate_baseline()
        print("Generated baseline.json:")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        unittest.main()
