import os
import threading
import unittest
import urllib.error
from unittest import mock

from travel_engine import fx

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "rail")
FX_URLOPEN = "travel_engine.fx.urllib.request.urlopen"
FRANKFURTER_TRY = 55.7328   # fixtures/rail/fx_frankfurter_latest_eur.json (ECB, 2026-10-16)
FRANKFURTER_CZK = 24.323
OPEN_ER_TRY = 55.678069     # fixtures/rail/er_api_latest_eur.json (open.er-api.com)
OPEN_ER_CZK = 24.329023


def load_fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FxTestCase(unittest.TestCase):
    def setUp(self):
        fx._reset_for_tests()
        self.clock = FakeClock(1000.0)
        clock_patch = mock.patch.object(fx, "_now", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        env = {k: v for k, v in os.environ.items() if k != fx.ENV_VAR}
        env_patch = mock.patch.dict(os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(fx._reset_for_tests)

    def fake_network(self, *replies):
        """Replace urlopen; each call takes the next reply (bytes or an exception)."""
        items = [r if isinstance(r, BaseException) else FakeResponse(r) for r in replies]
        return mock.patch(FX_URLOPEN, side_effect=items)

    def frankfurter(self):
        return load_fixture("fx_frankfurter_latest_eur.json")

    def open_er(self):
        return load_fixture("er_api_latest_eur.json")


class LiveRateTests(FxTestCase):
    def test_live_rate_parsed_from_frankfurter(self):
        with mock.patch(FX_URLOPEN, return_value=FakeResponse(self.frankfurter())) as opened:
            rate, source, is_live = fx.get_eur_try_rate()
        self.assertAlmostEqual(rate, FRANKFURTER_TRY)
        self.assertEqual(source, "frankfurter.dev (ECB)")
        self.assertTrue(is_live)
        url = opened.call_args[0][0].full_url
        self.assertTrue(url.startswith("https://api.frankfurter.dev/v1/latest?"))
        self.assertIn("TRY", url)
        self.assertEqual(opened.call_count, 1)

    def test_second_call_served_from_cache(self):
        with self.fake_network(self.frankfurter()) as opened:
            fx.get_eur_try_rate()
            self.clock.now += 3600
            rate, _, is_live = fx.get_eur_try_rate()
        self.assertEqual(opened.call_count, 1)
        self.assertTrue(is_live)
        self.assertAlmostEqual(rate, FRANKFURTER_TRY)

    def test_cache_expires_after_12_hours(self):
        with self.fake_network(self.frankfurter(), self.frankfurter()) as opened:
            fx.get_eur_try_rate()
            self.clock.now += 11 * 3600 + 59 * 60
            fx.get_eur_try_rate()
            self.assertEqual(opened.call_count, 1)
            self.clock.now += 2 * 60
            fx.get_eur_try_rate()
            self.assertEqual(opened.call_count, 2)

    def test_falls_back_to_open_er_api_when_frankfurter_fails(self):
        with self.fake_network(urllib.error.URLError("down"), self.open_er()) as opened:
            rate, source, is_live = fx.get_eur_try_rate()
        self.assertAlmostEqual(rate, OPEN_ER_TRY)
        self.assertEqual(source, "open.er-api.com")
        self.assertTrue(is_live)
        self.assertEqual(opened.call_count, 2)

    def test_env_override_wins_without_network(self):
        with mock.patch.dict(os.environ, {fx.ENV_VAR: "50.5"}), mock.patch(FX_URLOPEN) as opened:
            self.assertEqual(fx.get_eur_try_rate(), (50.5, "env", False))
        opened.assert_not_called()

    def test_invalid_env_is_ignored(self):
        with mock.patch.dict(os.environ, {fx.ENV_VAR: "abc"}), self.fake_network(self.frankfurter()):
            rate, source, is_live = fx.get_eur_try_rate()
        self.assertEqual(source, "frankfurter.dev (ECB)")
        self.assertTrue(is_live)

    def test_non_positive_env_is_ignored(self):
        with mock.patch.dict(os.environ, {fx.ENV_VAR: "-3"}):
            self.assertIsNone(fx.env_eur_try_rate())

    def test_payload_without_try_is_rejected(self):
        body = b'{"amount": 1.0, "base": "EUR", "rates": {"CZK": 24.3}}'
        with self.fake_network(body, body):
            rate, source, is_live = fx.get_eur_try_rate()
        self.assertEqual((source, is_live), ("default", False))

    def test_garbage_responses_never_raise(self):
        with self.fake_network(b"<html>nope</html>", b"[]"):
            self.assertEqual(fx.get_eur_try_rate(), (fx.DEFAULT_EUR_TRY_RATE, "default", False))

    def test_concurrent_callers_share_one_fetch(self):
        opened = mock.Mock(return_value=FakeResponse(self.frankfurter()))
        results = []
        with mock.patch(FX_URLOPEN, opened):
            threads = [threading.Thread(target=lambda: results.append(fx.get_eur_try_rate())) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r[2] for r in results))


class FallbackTests(FxTestCase):
    def test_last_known_value_used_when_network_fails_after_expiry(self):
        with self.fake_network(self.frankfurter(), urllib.error.URLError("a"), urllib.error.URLError("b")):
            fx.get_eur_try_rate()
            self.clock.now += 13 * 3600
            rate, source, is_live = fx.get_eur_try_rate()
        self.assertAlmostEqual(rate, FRANKFURTER_TRY)
        self.assertEqual((source, is_live), ("last-known", False))

    def test_failed_refresh_is_not_retried_for_10_minutes(self):
        with self.fake_network(urllib.error.URLError("a"), urllib.error.URLError("b")) as opened:
            self.assertEqual(fx.get_eur_try_rate()[1], "default")
            self.assertEqual(opened.call_count, 2)
            self.clock.now += 9 * 60
            self.assertEqual(fx.get_eur_try_rate()[1], "default")
            self.assertEqual(opened.call_count, 2)

    def test_retry_after_backoff_succeeds(self):
        with self.fake_network(urllib.error.URLError("a"), urllib.error.URLError("b"),
                               self.frankfurter()) as opened:
            fx.get_eur_try_rate()
            self.clock.now += 11 * 60
            rate, source, is_live = fx.get_eur_try_rate()
        self.assertEqual(opened.call_count, 3)
        self.assertEqual((source, is_live), ("frankfurter.dev (ECB)", True))
        self.assertAlmostEqual(rate, FRANKFURTER_TRY)

    def test_default_constant_when_nothing_known(self):
        with self.fake_network(urllib.error.URLError("a"), urllib.error.URLError("b")):
            self.assertEqual(fx.get_eur_try_rate(), (fx.DEFAULT_EUR_TRY_RATE, "default", False))

    def test_default_constant_is_the_documented_snapshot(self):
        self.assertEqual(fx.DEFAULT_EUR_TRY_RATE, 54.0)


class ConvertTests(FxTestCase):
    def test_convert_czk_crosses_through_eur(self):
        with self.fake_network(self.frankfurter()):
            value = fx.convert_to_try(2432.3, "CZK")
        # 2432.3 CZK / 24.323 CZK per EUR = 100 EUR; 100 EUR * 55.7328 = 5573.28 TRY
        self.assertAlmostEqual(value, 100 * FRANKFURTER_TRY, places=2)

    def test_convert_huf_and_pln_use_the_same_table(self):
        with self.fake_network(self.frankfurter()):
            huf = fx.convert_to_try(36308, "huf")
            pln = fx.convert_to_try(437, "PLN")
        self.assertAlmostEqual(huf, 100 * FRANKFURTER_TRY, delta=0.5)
        self.assertAlmostEqual(pln, 100 * FRANKFURTER_TRY, delta=0.5)

    def test_convert_eur_uses_eur_try_rate(self):
        with self.fake_network(self.frankfurter()):
            self.assertAlmostEqual(fx.convert_to_try(10, "EUR"), 10 * FRANKFURTER_TRY, places=4)

    def test_convert_try_is_identity(self):
        self.assertEqual(fx.convert_to_try(123.0, "TRY"), 123.0)

    def test_env_override_sets_eur_leg_of_cross_conversion(self):
        with mock.patch.dict(os.environ, {fx.ENV_VAR: "50"}), self.fake_network(self.frankfurter()):
            value = fx.convert_to_try(2432.3, "CZK")
        self.assertAlmostEqual(value, 5000.0, places=2)  # 100 EUR * 50

    def test_unknown_currency_is_none(self):
        with self.fake_network(self.frankfurter()):
            self.assertIsNone(fx.convert_to_try(10, "USD"))

    def test_cross_currency_is_none_when_no_rates_are_known(self):
        with self.fake_network(urllib.error.URLError("a"), urllib.error.URLError("b")):
            self.assertIsNone(fx.convert_to_try(2432.3, "CZK"))

    def test_non_numeric_amount_is_none(self):
        self.assertIsNone(fx.convert_to_try(float("nan"), "CZK"))
        self.assertIsNone(fx.convert_to_try("abc", "EUR"))


if __name__ == "__main__":
    unittest.main()
