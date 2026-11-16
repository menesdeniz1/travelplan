"""MOT-1 & MOT-5: engine speed helpers (deduplication buckets and signature cache)."""
import inspect
import unittest
from unittest import mock

from travel_engine.route_engine import _supports_kwarg


class _TestObj:
    def method(self, arg1, arg2=None):
        return arg1


class _UnhashableCallable:
    __hash__ = None

    def __call__(self, foo=1):
        return foo


def _plain_fn(a, b=2):
    return a + b


def _kwargs_fn(**kwargs):
    return kwargs


def _no_arg_fn():
    return 42


class SupportsKwargCacheTests(unittest.TestCase):
    def test_inspect_signature_called_only_once_for_repeated_lookup(self):
        # Unique function so previous tests don't pollute cache
        def unique_fn_for_cache_test(alpha, beta=1):
            pass

        with mock.patch("travel_engine.route_engine.inspect.signature", wraps=inspect.signature) as mock_sig:
            first = _supports_kwarg(unique_fn_for_cache_test, "beta")
            second = _supports_kwarg(unique_fn_for_cache_test, "beta")

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(mock_sig.call_count, 1)

    def test_bound_method_and_function_variations(self):
        obj = _TestObj()
        # Bound method
        self.assertTrue(_supports_kwarg(obj.method, "arg1"))
        self.assertTrue(_supports_kwarg(obj.method, "arg2"))
        self.assertFalse(_supports_kwarg(obj.method, "missing"))

        # Function with **kwargs
        self.assertTrue(_supports_kwarg(_kwargs_fn, "any_arg"))

        # Function without arguments
        self.assertFalse(_supports_kwarg(_no_arg_fn, "any_arg"))

        # Plain function
        self.assertTrue(_supports_kwarg(_plain_fn, "a"))
        self.assertFalse(_supports_kwarg(_plain_fn, "missing"))

    def test_unhashable_callable_supported(self):
        unhashable = _UnhashableCallable()
        self.assertTrue(_supports_kwarg(unhashable, "foo"))
        self.assertFalse(_supports_kwarg(unhashable, "bar"))


class CoordCacheTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone
        from travel_engine.route_engine import _Leg
        now = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        self.leg1 = _Leg(
            kind="flight", operator="Air", frm="IST", to="CDG",
            dep=now, arr=now, price=100.0, estimated=False, url="", main=True,
        )
        self.leg2 = _Leg(
            kind="flight", operator="Air", frm="IST", to="CDG",
            dep=now, arr=now, price=150.0, estimated=False, url="", main=True,
        )

    def test_coord_cache_called_once_per_unique_place(self):
        from travel_engine import route_engine
        from travel_engine.route_engine import _serialise_leg

        coord_cache = {}
        with mock.patch.object(route_engine, "_coord_for_place", wraps=route_engine._coord_for_place) as mock_coord:
            res1 = _serialise_leg(self.leg1, coord_cache=coord_cache)
            res2 = _serialise_leg(self.leg2, coord_cache=coord_cache)

        # 2 unique places (IST, CDG) across 2 legs -> exactly 2 calls
        self.assertEqual(mock_coord.call_count, 2)
        self.assertIn("IST", coord_cache)
        self.assertIn("CDG", coord_cache)
        self.assertIsNotNone(res1["from_coord"])
        self.assertIsNotNone(res1["to_coord"])
        self.assertEqual(res1["from_coord"], res2["from_coord"])
        self.assertEqual(res1["to_coord"], res2["to_coord"])

    def test_cached_and_uncached_output_identical(self):
        from travel_engine.route_engine import _serialise_leg

        uncached = _serialise_leg(self.leg1, coord_cache=None)
        cached = _serialise_leg(self.leg1, coord_cache={})
        self.assertEqual(cached, uncached)


class BestScoreMemoTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone
        from travel_engine.route_engine import _Leg, _State
        now = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
        self.leg_known = _Leg("flight", "Air", "IST", "PRG", now, now, 1500.0, False, "", True)
        self.leg_unknown = _Leg("flight", "Air", "IST", "PRG", now, now, None, False, "", True)
        self.state_known = _State(("p", 1), None, (self.leg_known,), 1, 0, True, ())
        self.state_unknown = _State(("p", 1), None, (self.leg_unknown,), 1, 0, True, ())

    def _make_run(self, mock_engine):
        from travel_engine.route_engine import _Run
        return _Run(
            engine=mock_engine, origin="IST", destination="PRG", dates=["2026-11-12"],
            arrive_by=None, depart_after=None, max_stops=1, via_tokens=None,
            budget=10, time_value=150.0, min_stay={}, cabin_bag=False, checked_bag=False,
            costs=mock.Mock(), db=mock.Mock(), ground=None,
        )

    def test_recompute_does_not_recompute_known_price_chains(self):
        mock_engine = mock.Mock()
        mock_engine._option.return_value = {"score": 250.0}
        run = self._make_run(mock_engine)
        run.dfs = lambda d: [self.state_known]

        first_score = run.best_score()
        self.assertEqual(first_score, 250.0)
        self.assertEqual(mock_engine._option.call_count, 1)
        self.assertIn(((self.leg_known,), ()), run.score_memo)

        # Force recompute: best_score_memo reset
        run.best_score_memo = (-1, None)
        second_score = run.best_score()
        self.assertEqual(second_score, 250.0)
        # Call count must not increase for fully known price chain
        self.assertEqual(mock_engine._option.call_count, 1)

    def test_unknown_price_legs_are_not_memoized(self):
        mock_engine = mock.Mock()
        mock_engine._option.return_value = {"score": 400.0}
        run = self._make_run(mock_engine)
        run.dfs = lambda d: [self.state_unknown]

        first_score = run.best_score()
        self.assertEqual(first_score, 400.0)
        self.assertEqual(mock_engine._option.call_count, 1)
        self.assertEqual(len(run.score_memo), 0)

        # Force recompute
        run.best_score_memo = (-1, None)
        second_score = run.best_score()
        self.assertEqual(second_score, 400.0)
        # Call count must increase for chain with unknown price leg
        self.assertEqual(mock_engine._option.call_count, 2)


if __name__ == "__main__":
    unittest.main()
