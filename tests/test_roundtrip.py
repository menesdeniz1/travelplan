import json
import unittest
from datetime import datetime

from travel_engine.roundtrip import (
    BADGE_BEST,
    BADGE_CHEAPEST,
    BADGE_FASTEST,
    SIDE_CANDIDATES,
    combine_roundtrip,
)


def _opt(kind, depart, arrive, price, score=None, **extra):
    option = {
        "kind": kind,
        "depart_at": depart,
        "arrive_at": arrive,
        "true_total_try": price,
        "duration_mins": _minutes(depart, arrive),
    }
    if score is not None:
        option["score"] = score
    option.update(extra)
    return option


def _minutes(depart, arrive):
    d = datetime.fromisoformat(depart)
    a = datetime.fromisoformat(arrive)
    return int((a - d).total_seconds() // 60)


OUT = _opt("direct", "2026-10-20T08:00", "2026-10-20T10:00", 1000, score=1100)
RET_LATE = _opt("direct", "2026-10-23T18:00", "2026-10-23T20:00", 1000, score=1100)


class CombineRoundtripTests(unittest.TestCase):
    def test_basic_pair_fields_and_label(self):
        combos = combine_roundtrip([OUT], [RET_LATE])
        self.assertEqual(len(combos), 1)
        combo = combos[0]
        self.assertIs(combo["outbound"], OUT)
        self.assertIs(combo["return"], RET_LATE)
        self.assertEqual(combo["total_try"], 2000.0)
        self.assertEqual(combo["stay_hours"], 80.0)
        self.assertEqual(combo["score"], 2200.0)
        self.assertEqual(combo["label"], "Gidiş: Direkt uçuş + Dönüş: Direkt uçuş")
        self.assertIn(BADGE_BEST, combo["badges"])

    def test_return_before_outbound_arrival_is_excluded(self):
        early_return = _opt("direct", "2026-10-20T09:00", "2026-10-20T11:00", 900, score=900)
        self.assertEqual(combine_roundtrip([OUT], [early_return]), [])

    def test_min_stay_filter_with_boundary_included(self):
        short_stay = _opt("direct", "2026-10-20T20:00", "2026-10-20T22:00", 900, score=900)  # 10h after arrival
        exact_day = _opt("direct", "2026-10-21T10:00", "2026-10-21T12:00", 900, score=900)  # exactly 24h
        combos = combine_roundtrip([OUT], [short_stay, exact_day], min_stay_hours=24)
        self.assertEqual(len(combos), 1)
        self.assertIs(combos[0]["return"], exact_day)
        self.assertEqual(combos[0]["stay_hours"], 24.0)

    def test_max_stay_days_filter(self):
        # stay = 2026-10-20T10:00 -> 2026-10-23T18:00 = 3d8h = 80h
        self.assertEqual(combine_roundtrip([OUT], [RET_LATE], max_stay_days=3), [])
        self.assertEqual(len(combine_roundtrip([OUT], [RET_LATE], max_stay_days=4)), 1)

    def test_top_n_limits_results_and_zero_gives_empty(self):
        outs = [_opt("direct", f"2026-10-2{i}T08:00", f"2026-10-2{i}T09:00", 1000 + i, score=1000 + i)
                for i in range(1, 4)]
        rets = [_opt("direct", f"2026-11-0{i}T08:00", f"2026-11-0{i}T09:00", 900 + i, score=900 + i)
                for i in range(1, 5)]
        self.assertEqual(len(combine_roundtrip(outs, rets)), 10)
        self.assertEqual(len(combine_roundtrip(outs, rets, top_n=2)), 2)
        self.assertEqual(combine_roundtrip(outs, rets, top_n=0), [])

    def test_results_sorted_by_score(self):
        cheap_slow = _opt("ground", "2026-10-20T08:00", "2026-10-20T16:00", 300, score=900)
        pricey_fast = _opt("direct", "2026-10-20T08:00", "2026-10-20T09:00", 2000, score=2100)
        combos = combine_roundtrip([pricey_fast, cheap_slow], [RET_LATE])
        self.assertEqual([c["score"] for c in combos], sorted(c["score"] for c in combos))
        self.assertIs(combos[0]["outbound"], cheap_slow)

    def test_empty_inputs(self):
        self.assertEqual(combine_roundtrip([], []), [])
        self.assertEqual(combine_roundtrip([OUT], []), [])
        self.assertEqual(combine_roundtrip([], [RET_LATE]), [])

    def test_badges_assigned_to_correct_combinations(self):
        # A+R: score 2200, total 2000, travel 240 min -> Önerilen + En Hızlı
        # B+R: score 2500, total 1500, travel 720 min -> En Ucuz
        b_out = _opt("ground", "2026-10-20T08:00", "2026-10-20T20:00", 500, score=1400)
        combos = combine_roundtrip([OUT, b_out], [RET_LATE])
        by_outbound = {id(c["outbound"]): c for c in combos}
        best = by_outbound[id(OUT)]
        cheapest = by_outbound[id(b_out)]
        self.assertEqual(combos[0], best)
        self.assertEqual(best["badges"], [BADGE_BEST, BADGE_FASTEST])
        self.assertEqual(cheapest["badges"], [BADGE_CHEAPEST])
        self.assertEqual(combos[0]["badges"].count(BADGE_BEST), 1)

    def test_single_combo_gets_all_applicable_badges(self):
        combos = combine_roundtrip([OUT], [RET_LATE])
        self.assertEqual(combos[0]["badges"], [BADGE_BEST, BADGE_CHEAPEST, BADGE_FASTEST])

    def test_score_fallback_when_option_has_no_score(self):
        out = _opt("direct", "2026-10-20T08:00", "2026-10-20T09:00", 1000)  # 60 min
        ret = _opt("direct", "2026-10-21T08:00", "2026-10-21T09:00", 500)  # 60 min
        combos = combine_roundtrip([out], [ret], time_value_try_per_hour=150.0)
        # (1000 + 1h*150) + (500 + 1h*150)
        self.assertEqual(combos[0]["score"], 1800.0)

    def test_fallback_adds_night_wait_and_separate_ticket_penalties(self):
        out = _opt("via_separate", "2026-10-20T08:00", "2026-10-20T09:00", 1000, separate_tickets=True)
        ret = _opt("direct", "2026-10-21T08:00", "2026-10-21T09:00", 500, night_wait=True)
        combos = combine_roundtrip([out], [ret], time_value_try_per_hour=150.0)
        # out: 1000 + 150 + 500 (separate); ret: 500 + 150 + 1500 (night wait at tv 150)
        self.assertEqual(combos[0]["score"], 1000 + 150 + 500 + 500 + 150 + 1500)

    def test_unpriced_and_bad_timestamp_options_are_skipped(self):
        unpriced = _opt("direct", "2026-10-20T08:00", "2026-10-20T09:00", None, score=10)
        bad_time = {"kind": "direct", "depart_at": "soon", "arrive_at": "later", "true_total_try": 100}
        self.assertEqual(combine_roundtrip([unpriced, bad_time], [RET_LATE]), [])
        self.assertEqual(combine_roundtrip([OUT], [unpriced]), [])

    def test_mixed_naive_and_aware_timestamps_do_not_raise(self):
        aware_ret = _opt("direct", "2026-10-23T18:00+00:00", "2026-10-23T20:00+00:00", 900, score=900)
        self.assertEqual(combine_roundtrip([OUT], [aware_ret]), [])

    def test_only_top_side_candidates_are_paired(self):
        outs = [_opt("direct", "2026-10-20T08:00", "2026-10-20T09:00", 1000, score=float(i))
                for i in range(SIDE_CANDIDATES + 5)]
        rets = [RET_LATE]
        combos = combine_roundtrip(outs, rets, top_n=1000)
        self.assertEqual(len(combos), SIDE_CANDIDATES)
        kept_scores = {c["outbound"]["score"] for c in combos}
        self.assertNotIn(float(SIDE_CANDIDATES + 4), kept_scores)
        self.assertIn(0.0, kept_scores)

    def test_unknown_kind_label_falls_back_to_kind_string(self):
        weird = _opt("teleport", "2026-10-20T08:00", "2026-10-20T09:00", 1000, score=1100)
        combos = combine_roundtrip([weird], [RET_LATE])
        self.assertEqual(combos[0]["label"], "Gidiş: teleport + Dönüş: Direkt uçuş")

    def test_output_is_json_safe(self):
        combos = combine_roundtrip([OUT], [RET_LATE])
        json.dumps(combos, allow_nan=False)
        self.assertTrue(all(isinstance(c["badges"], list) for c in combos))

    def test_utc_aware_timestamps_pair_correctly(self):
        out = _opt("direct", "2026-10-20T08:00+00:00", "2026-10-20T10:00+00:00", 1000, score=1100)
        ret = _opt("direct", "2026-10-23T18:00+00:00", "2026-10-23T20:00+00:00", 1000, score=1100)
        combos = combine_roundtrip([out], [ret])
        self.assertEqual(combos[0]["stay_hours"], 80.0)


class CrossTimezoneTests(unittest.TestCase):
    """Stay hours use depart_utc / arrive_utc when both sides have them (Istanbul +03, London BST +01 here)."""

    def _out(self, **extra):
        # Istanbul 08:00 (05:00Z) -> London 10:00 local (09:00Z)
        return _opt("direct", "2026-10-20T08:00", "2026-10-20T10:00", 1000, score=1100,
                    depart_utc="2026-10-20T05:00:00Z", arrive_utc="2026-10-20T09:00:00Z",
                    depart_tz="Europe/Istanbul", arrive_tz="Europe/London", **extra)

    def test_return_from_london_at_10_local_next_day_is_24_hours(self):
        ret = _opt("direct", "2026-10-21T10:00", "2026-10-21T12:00", 900, score=900,
                   depart_utc="2026-10-21T09:00:00Z", arrive_utc="2026-10-21T11:00:00Z",
                   depart_tz="Europe/London", arrive_tz="Europe/London")
        combos = combine_roundtrip([self._out()], [ret])
        self.assertEqual(combos[0]["stay_hours"], 24.0)

    def test_return_from_istanbul_is_compared_on_the_instant_not_the_wall_clock(self):
        # Leaves Istanbul 12:00 local (09:00Z): 24 h after the London arrival. The local strings would
        # say 26 h (10:00 London vs 12:00 Istanbul), so the UTC instants decide.
        ret = _opt("direct", "2026-10-21T12:00", "2026-10-21T14:00", 900, score=900,
                   depart_utc="2026-10-21T09:00:00Z", arrive_utc="2026-10-21T11:00:00Z",
                   depart_tz="Europe/Istanbul", arrive_tz="Europe/London")
        combos = combine_roundtrip([self._out()], [ret], min_stay_hours=24)
        self.assertEqual([c["stay_hours"] for c in combos], [24.0])

    def test_min_stay_uses_the_utc_gap(self):
        # 22 h by instant (08:00Z next day), but the local strings read 24 h.
        ret = _opt("direct", "2026-10-21T10:00", "2026-10-21T12:00", 900, score=900,
                   depart_utc="2026-10-21T07:00:00Z", arrive_utc="2026-10-21T09:00:00Z",
                   depart_tz="Europe/Istanbul", arrive_tz="Europe/London")
        self.assertEqual(combine_roundtrip([self._out()], [ret], min_stay_hours=23), [])

    def test_max_stay_uses_the_utc_gap(self):
        ret = _opt("direct", "2026-10-21T12:00", "2026-10-21T14:00", 900, score=900,
                   depart_utc="2026-10-21T09:00:00Z", arrive_utc="2026-10-21T11:00:00Z")
        # The stay is exactly 24 h by instant: kept at 1 day (inclusive), dropped at 0.5 day.
        self.assertEqual(len(combine_roundtrip([self._out()], [ret], max_stay_days=1)), 1)
        self.assertEqual(combine_roundtrip([self._out()], [ret], max_stay_days=0.5), [])

    def test_legacy_side_without_utc_uses_the_local_strings(self):
        ret = _opt("direct", "2026-10-21T10:00", "2026-10-21T12:00", 900, score=900)  # no UTC fields
        combos = combine_roundtrip([self._out()], [ret])
        self.assertEqual(combos[0]["stay_hours"], 24.0)

    def test_half_a_utc_pair_is_ignored(self):
        ret = _opt("direct", "2026-10-21T12:00", "2026-10-21T14:00", 900, score=900,
                   depart_utc="2026-10-21T09:00:00Z")  # arrive_utc missing: local strings are used
        combos = combine_roundtrip([self._out()], [ret])
        self.assertEqual(combos[0]["stay_hours"], 26.0)

    def test_naive_utc_value_is_not_an_instant(self):
        ret = _opt("direct", "2026-10-21T12:00", "2026-10-21T14:00", 900, score=900,
                   depart_utc="2026-10-21T09:00:00", arrive_utc="2026-10-21T11:00:00")
        combos = combine_roundtrip([self._out()], [ret])
        self.assertEqual(combos[0]["stay_hours"], 26.0)

    def test_utc_pair_works_without_local_strings(self):
        ret = {"kind": "direct", "true_total_try": 900, "score": 900,
               "depart_utc": "2026-10-21T09:00:00Z", "arrive_utc": "2026-10-21T11:00:00Z"}
        combos = combine_roundtrip([self._out()], [ret])
        self.assertEqual(combos[0]["stay_hours"], 24.0)


if __name__ == "__main__":
    unittest.main()
