"""Trenitalia provider tests. No network: urlopen is replaced by a fake service.

The fixtures in tests/fixtures/trenitalia are trimmed copies of the lefrecce.it responses captured on
19 Oct 2026: solutions for Milano Centrale -> Venezia S. Lucia (2026-11-09) and Milano Centrale ->
Roma Termini (2026-11-10), plus the two captured location answers. Every other location answer in
this file is synthetic and only exercises the matching rules.
"""

import io
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from travel_engine import fx
from travel_engine.providers import trenitalia_provider as tp
from travel_engine.providers.trenitalia_provider import TrenitaliaProvider

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "trenitalia")

MILANO = 830001700
VENEZIA = 830002593
ROMA = 830008409
DAY_VE = "2026-11-09"
DAY_RM = "2026-11-10"
SYNTH_NAPOLI_CENTRALE = 900101
SYNTH_NAPOLI_AFRAGOLA = 900102
SYNTH_FIRENZE_SMN = 900201


def load_json(name: str):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return json.loads(f.read().decode("utf-8"))


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
    def __init__(self, now: float = 10_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeService:
    """Stands in for urllib.request.urlopen. Records every call and serves scripted answers.

    ``solutions_by_route`` maps (from_id, to_id) to a payload or an exception to raise. Routes not
    listed get an empty solutions list. ``locations`` maps the ``name`` query to a list or an exception.
    """

    def __init__(self):
        self.calls = []
        self.solutions_by_route = {
            (MILANO, VENEZIA): load_json("solutions_milano_venezia_2026-11-09.json"),
            (MILANO, ROMA): load_json("solutions_milano_roma_2026-11-10.json"),
        }
        self.locations = {}
        self.fallback = None  # raised for any call not otherwise scripted, when set

    def __call__(self, request, timeout=None):
        url = request.full_url
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        headers = {k.lower(): v for k, v in request.header_items()}
        self.calls.append({"method": request.get_method(), "url": url, "body": body, "headers": headers})
        if self.fallback is not None:
            raise self.fallback
        if "/locations/search" in url:
            name = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["name"][0]
            answer = self.locations.get(name, [])
            if isinstance(answer, BaseException):
                raise answer
            return FakeResponse(json.dumps(answer).encode("utf-8"))
        if "/ticket/solutions" in url:
            route = (body["departureLocationId"], body["arrivalLocationId"])
            answer = self.solutions_by_route.get(route, {"solutions": []})
            if isinstance(answer, BaseException):
                raise answer
            return FakeResponse(json.dumps(answer).encode("utf-8"))
        raise AssertionError(f"unexpected URL {url}")

    def solution_calls(self):
        return [c for c in self.calls if "/ticket/solutions" in c["url"]]

    def location_calls(self):
        return [c for c in self.calls if "/locations/search" in c["url"]]


def http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://www.lefrecce.it/", code, "err", hdrs=None, fp=io.BytesIO(body))


class _Base(unittest.TestCase):
    def setUp(self):
        tp._reset_shared_state_for_tests()
        self.addCleanup(tp._reset_shared_state_for_tests)
        self.clock = FakeClock()
        self.sleeps = []
        self.service = FakeService()
        self.eur_calls = []

        def fake_convert(amount, currency, timeout=6.0):
            self.eur_calls.append((amount, currency))
            return round(float(amount) * 50.0, 2)

        patches = [
            mock.patch.object(tp.urllib.request, "urlopen", side_effect=self.service),
            mock.patch.object(tp, "_now", self.clock),
            mock.patch.object(tp, "_sleep", side_effect=self.sleeps.append),
            mock.patch.object(fx, "convert_to_try", side_effect=fake_convert),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.provider = TrenitaliaProvider()


class ParseTests(_Base):
    def test_milano_venezia_segments_from_live_solutions(self):
        segs = self.provider.search("Milano", "Venezia", DAY_VE)
        self.assertIsNotNone(segs)
        self.assertEqual(
            [(s.departure_time, s.arrival_time) for s in segs],
            [("13:25", "16:52"), ("13:45", "16:14"), ("14:15", "16:44"), ("15:48", "18:44")],
        )
        first = segs[0]
        self.assertEqual(first.duration_mins, 207)
        self.assertEqual(first.operator, "RE 2627 + RV 3503")
        self.assertEqual(first.mode, "train")
        self.assertEqual(first.source, "trenitalia")
        self.assertFalse(first.is_estimated)
        self.assertFalse(first.price_is_estimated)
        self.assertEqual(first.price_eur, 22.4)
        self.assertEqual(first.price_try, 1120.0)
        self.assertEqual(first.origin_station, "Milano")
        self.assertEqual(first.dest_station, "Venezia")
        self.assertEqual(first.origin_station_name, "Milano Centrale")
        self.assertEqual(first.dest_station_name, "Venezia S. Lucia")
        self.assertEqual(first.date, DAY_VE)

    def test_direct_train_operator_is_acronym_and_number(self):
        segs = self.provider.search("Milano", "Venezia", DAY_VE)
        direct = segs[1]
        self.assertEqual(direct.operator, "FR 9731")
        self.assertEqual((direct.departure_time, direct.arrival_time, direct.duration_mins), ("13:45", "16:14", 149))
        self.assertEqual(direct.price_eur, 19.9)

    def test_train_without_name_shows_acronym_only(self):
        segs = self.provider.search("Milano", "Venezia", DAY_VE)
        ub = next(s for s in segs if s.departure_time == "15:48")
        self.assertEqual(ub.operator, "UB + FR 9743")
        self.assertNotIn("None", ub.operator)

    def test_null_price_stays_unknown(self):
        segs = self.provider.search("Milano", "Roma", DAY_RM, max_transfers=2)
        self.assertEqual(len(segs), 4)
        unpriced = next(s for s in segs if s.operator.startswith("RV 2455 + RV 3911"))
        self.assertIsNone(unpriced.price_eur)
        self.assertIsNone(unpriced.price_try)
        self.assertEqual(unpriced.duration_mins, 650)
        self.assertEqual(unpriced.operator, "RV 2455 + RV 3911 + RV 4155")
        # No fare means no conversion was asked for that trip.
        self.assertNotIn(None, [amount for amount, _ in self.eur_calls])
        self.assertEqual(len(self.eur_calls), 3)

    def test_fare_in_other_currency_or_zero_is_unknown(self):
        self.assertIsNone(tp._euro_amount({"currency": "TRY", "amount": 500}))
        self.assertIsNone(tp._euro_amount({"currency": "€", "amount": 0}))
        self.assertIsNone(tp._euro_amount({"currency": "€", "amount": True}))
        self.assertIsNone(tp._euro_amount(None))
        self.assertEqual(tp._euro_amount({"currency": "€", "amount": 22.4}), 22.4)
        self.assertEqual(tp._euro_amount({"currency": "EUR", "amount": 10}), 10.0)

    def test_request_is_post_with_channel_header_and_body(self):
        self.provider.search("Milano", "Venezia", DAY_VE)
        self.assertEqual(len(self.service.location_calls()), 0)  # both stations are known ids
        call = self.service.solution_calls()[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["channel"], "41")
        self.assertEqual(call["headers"]["content-type"], "application/json")
        body = call["body"]
        self.assertEqual(body["departureLocationId"], MILANO)
        self.assertEqual(body["arrivalLocationId"], VENEZIA)
        self.assertEqual(body["departureTime"], "2026-11-09T00:00:00.000")
        self.assertEqual(body["criteria"]["order"], "DEPARTURE_DATE")
        self.assertEqual(body["criteria"]["limit"], 10)
        self.assertEqual(body["adults"], 1)

    def test_price_try_uses_fx(self):
        segs = self.provider.search("Milano", "Venezia", DAY_VE)
        self.assertIn((22.4, "EUR"), self.eur_calls)
        self.assertEqual(segs[0].price_try, round(22.4 * 50.0, 2))

    def test_max_results_caps_the_list(self):
        segs = self.provider.search("Milano", "Venezia", DAY_VE, max_results=2)
        self.assertEqual([s.departure_time for s in segs], ["13:25", "13:45"])

    def test_empty_solutions_is_empty_list_not_none(self):
        self.service.solutions_by_route = {}
        self.assertEqual(self.provider.search("Milano", "Venezia", DAY_VE), [])
        self.assertEqual(self.provider.health_status()["status"], "ok")

    def test_trips_on_another_day_are_skipped(self):
        self.assertEqual(self.provider.search("Milano", "Venezia", DAY_RM), [])


class TransferTests(_Base):
    def test_max_transfers_zero_keeps_only_direct_trains(self):
        segs = self.provider.search("Milano", "Venezia", DAY_VE, max_transfers=0)
        self.assertEqual([s.operator for s in segs], ["FR 9731", "FR 9733"])

    def test_default_max_transfers_keeps_one_change_and_drops_two(self):
        segs = self.provider.search("Milano", "Roma", DAY_RM)
        self.assertEqual([s.operator for s in segs], ["FR 9607", "RV 2455 + IC 584", "FR 9611"])

    def test_two_changes_kept_when_allowed(self):
        segs = self.provider.search("Milano", "Roma", DAY_RM, max_transfers=2)
        self.assertEqual(len(segs), 4)
        self.assertEqual(segs[1].duration_mins, 540)  # RV + IC: 9h00


class EarliestTests(_Base):
    def test_earliest_departure_filters_and_is_sent_to_the_service(self):
        segs = self.provider.search("Milano", "Venezia", DAY_VE, earliest_dep_time="14:30")
        self.assertEqual([s.departure_time for s in segs], ["15:48"])
        call = self.service.solution_calls()[0]
        self.assertEqual(call["body"]["departureTime"], "2026-11-09T14:30:00.000")

    def test_earliest_departure_is_inclusive(self):
        segs = self.provider.search("Milano", "Venezia", DAY_VE, earliest_dep_time="14:15")
        self.assertEqual([s.departure_time for s in segs], ["14:15", "15:48"])

    def test_malformed_inputs_return_none_without_a_request(self):
        self.assertIsNone(self.provider.search("Milano", "Venezia", "2026-13-01"))
        self.assertIsNone(self.provider.search("Milano", "Venezia", DAY_VE, earliest_dep_time="25:00"))
        self.assertIsNone(self.provider.search("Milano", "Venezia", DAY_VE, earliest_dep_time="9am"))
        self.assertIsNone(self.provider.search("Milano", "Venezia", None))
        self.assertEqual(self.service.calls, [])


class LocationTests(_Base):
    def test_seeded_cities_and_aliases_resolve_without_network(self):
        for place in ("Milano", "Milan", "Roma", "Rome", "Venezia", "Venedik"):
            self.assertTrue(self.provider.can_resolve(place), place)
        self.assertEqual(self.service.calls, [])

    def test_turkish_alias_is_searched_as_the_italian_name(self):
        self.service.locations["Firenze"] = [
            {"id": 900202, "name": "Firenze Rifredi"},
            {"id": SYNTH_FIRENZE_SMN, "name": "Firenze S. M. Novella"},
            {"id": 900203, "name": "Firenze Campo di Marte"},
        ]
        self.assertEqual(self.provider._resolve_station("Floransa"), (SYNTH_FIRENZE_SMN, "Firenze S. M. Novella"))
        url = self.service.location_calls()[0]["url"]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["name"], ["Firenze"])
        self.assertEqual(query["limit"], ["10"])
        self.assertEqual(self.service.location_calls()[0]["headers"]["channel"], "41")

    def test_centrale_is_preferred_over_other_stations(self):
        self.service.locations["Napoli"] = [
            {"id": SYNTH_NAPOLI_AFRAGOLA, "name": "Napoli Afragola"},
            {"id": SYNTH_NAPOLI_CENTRALE, "name": "Napoli Centrale"},
        ]
        self.assertTrue(self.provider.can_resolve("Naples"))
        self.assertEqual(self.provider._resolve_station("Napoli"), (SYNTH_NAPOLI_CENTRALE, "Napoli Centrale"))

    def test_search_uses_the_resolved_station_id(self):
        self.service.locations["Napoli"] = [{"id": SYNTH_NAPOLI_CENTRALE, "name": "Napoli Centrale"}]
        self.service.solutions_by_route[(ROMA, SYNTH_NAPOLI_CENTRALE)] = {"solutions": []}
        self.assertEqual(self.provider.search("Roma", "Napoli", DAY_RM), [])
        body = self.service.solution_calls()[0]["body"]
        self.assertEqual((body["departureLocationId"], body["arrivalLocationId"]), (ROMA, SYNTH_NAPOLI_CENTRALE))

    def test_pick_station_rules(self):
        pick = tp._pick_station
        self.assertEqual(pick("pisa", [(1, "Pisa San Rossore"), (2, "Pisa Centrale")]), (2, "Pisa Centrale"))
        self.assertEqual(pick("pisa", [(1, "Pisa Aeroporto"), (2, "Pisa San Rossore")]), (1, "Pisa Aeroporto"))
        self.assertEqual(pick("pisa", [(3, "Pisa")]), (3, "Pisa"))
        self.assertEqual(pick("milano centrale", [(4, "Milano Centrale")]), (4, "Milano Centrale"))
        self.assertIsNone(pick("pisa", [(5, "Pisana Roma")]))  # a prefix of a word is not the city
        self.assertIsNone(pick("istanbul", [(6, "Pisa Centrale")]))

    def test_non_italian_place_does_not_resolve(self):
        self.service.locations["Istanbul"] = []
        self.assertFalse(self.provider.can_resolve("Istanbul"))
        self.assertIsNone(self.provider.search("Istanbul", "Milano", DAY_VE))
        self.assertEqual(self.service.solution_calls(), [])

    def test_location_service_failure_gives_none_and_error_state(self):
        self.service.locations["Napoli"] = urllib.error.URLError("no route")
        self.assertFalse(self.provider.can_resolve("Napoli"))
        self.assertIsNone(self.provider.search("Roma", "Napoli", DAY_RM))
        self.assertEqual(self.provider.health_status()["status"], "error")

    def test_location_answer_is_cached_for_seven_days(self):
        self.service.locations["Napoli"] = [{"id": SYNTH_NAPOLI_CENTRALE, "name": "Napoli Centrale"}]
        self.provider.can_resolve("Napoli")
        self.clock.advance(6 * 24 * 3600)
        self.provider.can_resolve("Napoli")
        self.assertEqual(len(self.service.location_calls()), 1)
        self.clock.advance(2 * 24 * 3600)
        self.provider.can_resolve("Napoli")
        self.assertEqual(len(self.service.location_calls()), 2)

    def test_parse_locations_skips_bad_items_and_rejects_non_list(self):
        self.assertEqual(
            tp._parse_locations([{"id": 1, "name": "A"}, {"id": "2", "name": "B"}, {"name": "C"}, 5]),
            [(1, "A")],
        )
        with self.assertRaises(ValueError):
            tp._parse_locations({"id": 1})


class CacheTests(_Base):
    def test_search_result_is_cached_for_thirty_minutes(self):
        self.provider.search("Milano", "Venezia", DAY_VE)
        self.provider.search("Milano", "Venezia", DAY_VE)
        self.assertEqual(len(self.service.solution_calls()), 1)
        self.clock.advance(1799)
        self.provider.search("Milano", "Venezia", DAY_VE)
        self.assertEqual(len(self.service.solution_calls()), 1)
        self.clock.advance(2)
        self.provider.search("Milano", "Venezia", DAY_VE)
        self.assertEqual(len(self.service.solution_calls()), 2)

    def test_cache_key_includes_earliest_time_and_date(self):
        self.provider.search("Milano", "Venezia", DAY_VE)
        self.provider.search("Milano", "Venezia", DAY_VE, earliest_dep_time="14:00")
        self.provider.search("Milano", "Venezia", DAY_RM)
        self.assertEqual(len(self.service.solution_calls()), 3)

    def test_failures_are_not_cached(self):
        self.service.fallback = urllib.error.URLError("down")
        self.assertIsNone(self.provider.search("Milano", "Venezia", DAY_VE))
        self.service.fallback = None
        self.assertEqual(len(self.provider.search("Milano", "Venezia", DAY_VE)), 4)


class RateLimitTests(_Base):
    def test_back_to_back_requests_wait_one_second(self):
        self.service.locations["Napoli"] = [{"id": SYNTH_NAPOLI_CENTRALE, "name": "Napoli Centrale"}]
        self.provider.search("Roma", "Napoli", DAY_RM)  # location request, then solutions request
        self.assertEqual(self.sleeps, [1.0])

    def test_requests_spaced_apart_do_not_wait(self):
        self.provider.search("Milano", "Venezia", DAY_VE)
        self.clock.advance(5)
        self.provider.search("Milano", "Roma", DAY_RM)
        self.assertEqual(self.sleeps, [])

    def test_spacing_is_process_wide_across_instances(self):
        other = TrenitaliaProvider()
        self.provider.search("Milano", "Venezia", DAY_VE)
        other.search("Milano", "Roma", DAY_RM)
        self.assertEqual(self.sleeps, [1.0])


class BackoffTests(_Base):
    def test_403_cools_down_for_fifteen_minutes(self):
        self._assert_block_then_recover(403, b"<html>forbidden</html>")

    def test_429_cools_down_for_fifteen_minutes(self):
        self._assert_block_then_recover(429, b"too many")

    def test_400_riprova_cools_down(self):
        self._assert_block_then_recover(400, b'{"message":"Riprova piu tardi"}')

    def _assert_block_then_recover(self, code, body):
        self.service.solutions_by_route[(MILANO, VENEZIA)] = http_error(code, body)
        self.assertIsNone(self.provider.search("Milano", "Venezia", DAY_VE))
        health = self.provider.health_status()
        self.assertEqual(health["status"], "blocked")
        self.assertEqual(health["last_http_status"], code)

        sent = len(self.service.calls)
        self.clock.advance(10 * 60)
        self.service.solutions_by_route[(MILANO, VENEZIA)] = load_json("solutions_milano_venezia_2026-11-09.json")
        self.assertIsNone(self.provider.search("Milano", "Venezia", DAY_VE))  # still cooling down
        self.assertEqual(len(self.service.calls), sent)
        self.assertTrue(self.provider.health_status()["blocked_cooldown_active"])

        self.clock.advance(6 * 60)  # 16 minutes after the block
        self.assertEqual(len(self.provider.search("Milano", "Venezia", DAY_VE)), 4)
        self.assertEqual(self.provider.health_status()["status"], "ok")

    def test_400_without_riprova_is_a_plain_error(self):
        self.service.solutions_by_route[(MILANO, VENEZIA)] = http_error(400, b"bad request")
        self.assertIsNone(self.provider.search("Milano", "Venezia", DAY_VE))
        health = self.provider.health_status()
        self.assertEqual(health["status"], "error")
        self.assertFalse(health["blocked_cooldown_active"])
        self.service.solutions_by_route[(MILANO, VENEZIA)] = load_json("solutions_milano_venezia_2026-11-09.json")
        self.assertEqual(len(self.provider.search("Milano", "Venezia", DAY_VE)), 4)

    def test_cooldown_applies_to_every_instance(self):
        self.service.solutions_by_route[(MILANO, VENEZIA)] = http_error(403)
        self.provider.search("Milano", "Venezia", DAY_VE)
        other = TrenitaliaProvider()
        sent = len(self.service.calls)
        self.assertIsNone(other.search("Milano", "Roma", DAY_RM))
        self.assertEqual(len(self.service.calls), sent)
        self.assertEqual(other.health_status()["status"], "blocked")

    def test_network_error_is_not_a_cooldown(self):
        self.service.solutions_by_route[(MILANO, VENEZIA)] = urllib.error.URLError("timeout")
        self.assertIsNone(self.provider.search("Milano", "Venezia", DAY_VE))
        self.assertFalse(self.provider.health_status()["blocked_cooldown_active"])
        self.assertEqual(self.provider.health_status()["status"], "error")


class ItalianGateTests(_Base):
    """Only Italian, San Marino and Vatican places may reach lefrecce.it; everything else is refused offline."""

    NON_ITALIAN = ("Prag", "Prague", "Praha", "Bratislava", "Athens", "Atina", "Istanbul", "Wien", "Paris Nord",
                   "Sofia", "Budapeşte", "Ljubljana", "Krakow", "Zagreb", "Atlantis")

    def test_non_italian_places_make_no_request(self):
        for place in self.NON_ITALIAN:
            self.assertFalse(self.provider.can_resolve(place), place)
            self.assertIsNone(self.provider.search(place, "Milano", DAY_VE), place)
            self.assertIsNone(self.provider.search("Milano", place, DAY_VE), place)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.sleeps, [])

    def test_italian_places_pass_the_gate(self):
        for place in ("Milano", "Milan", "Venedik", "Mestre", "Roma Termini", "Firenze S. M. Novella",
                      "Venezia Mestre", "Milano Centrale", "San Marino", "Città del Vaticano", "Vatican",
                      "Siena", "Bolzano", "Torino", "Genoa", "Trieste"):
            self.assertTrue(tp.is_italian_place(place), place)

    def test_seeded_italian_names_resolve_without_network(self):
        for place in ("Mestre", "Milano", "Roma", "Venezia"):
            self.assertTrue(self.provider.can_resolve(place), place)
        self.assertEqual(self.service.calls, [])

    def test_italian_town_outside_the_seed_list_is_looked_up_once(self):
        self.service.locations["Siena"] = [{"id": 830009999, "name": "Siena"}]
        self.assertTrue(self.provider.can_resolve("Siena"))
        self.assertTrue(self.provider.can_resolve("siena"))
        self.assertEqual(len(self.service.location_calls()), 1)

    def test_italian_town_without_a_station_is_cached_as_negative(self):
        self.service.locations["Siena"] = []
        self.assertFalse(self.provider.can_resolve("Siena"))
        self.assertFalse(self.provider.can_resolve("Siena"))
        self.assertEqual(len(self.service.location_calls()), 1)

    def test_failed_lookup_is_not_cached(self):
        self.service.locations["Siena"] = urllib.error.URLError("down")
        self.assertFalse(self.provider.can_resolve("Siena"))
        self.service.locations["Siena"] = [{"id": 830009999, "name": "Siena"}]
        self.assertTrue(self.provider.can_resolve("Siena"))
        self.assertEqual(len(self.service.location_calls()), 2)

    def test_negative_answer_for_non_italian_place_is_cached(self):
        self.assertFalse(self.provider.can_resolve("Prag"))
        self.clock.advance(60)
        self.assertFalse(self.provider.can_resolve("Prag"))
        self.assertEqual(self.service.calls, [])

    def test_gate_is_by_name_not_by_station_word(self):
        # "Paris Nord" has a station word the Italian list does not know: the leading "Paris" decides.
        self.assertFalse(tp.is_italian_place("Paris Nord"))
        self.assertFalse(tp.is_italian_place("Lyon Part-Dieu"))
        self.assertFalse(tp.is_italian_place(""))
        self.assertFalse(tp.is_italian_place("   "))

    def test_can_resolve_none_or_blank_is_false(self):
        self.assertFalse(self.provider.can_resolve(None))
        self.assertFalse(self.provider.can_resolve("  "))
        self.assertEqual(self.service.calls, [])


class HealthTests(_Base):
    def test_health_states_and_counters(self):
        health = self.provider.health_status()
        self.assertEqual(health["status"], "unknown")
        self.assertEqual(health["requests"], 0)
        self.assertEqual(
            set(health),
            {
                "source", "status", "last_ok_at", "last_error", "last_error_at", "last_http_status",
                "consecutive_failures", "requests", "blocked_cooldown_active", "cached_locations",
                "cached_searches",
            },
        )
        self.assertEqual(health["source"], "trenitalia")

        self.provider.search("Milano", "Venezia", DAY_VE)
        health = self.provider.health_status()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["requests"], 1)
        self.assertIsNotNone(health["last_ok_at"])
        self.assertEqual(health["cached_searches"], 1)

        self.service.solutions_by_route[(MILANO, ROMA)] = urllib.error.URLError("boom")
        self.assertIsNone(self.provider.search("Milano", "Roma", DAY_RM))
        health = self.provider.health_status()
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertIn("boom", health["last_error"])

        self.service.solutions_by_route[(MILANO, ROMA)] = load_json("solutions_milano_roma_2026-11-10.json")
        self.provider.search("Milano", "Roma", DAY_RM)
        health = self.provider.health_status()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNone(health["last_error"])

    def test_unexpected_shape_is_a_recorded_failure(self):
        self.service.solutions_by_route[(MILANO, VENEZIA)] = {"unexpected": True}
        self.assertIsNone(self.provider.search("Milano", "Venezia", DAY_VE))
        health = self.provider.health_status()
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["last_http_status"], 200)

    def test_non_json_body_is_a_recorded_failure(self):
        with mock.patch.object(tp.urllib.request, "urlopen", side_effect=lambda req, timeout=None: FakeResponse(b"<html>")):
            self.assertIsNone(self.provider.search("Milano", "Venezia", DAY_VE))
        self.assertEqual(self.provider.health_status()["status"], "error")

    def test_unexpected_exception_never_escapes(self):
        self.service.fallback = RuntimeError("surprise")
        self.assertIsNone(self.provider.search("Milano", "Venezia", DAY_VE))
        self.assertFalse(self.provider.can_resolve("Milano Centrale X"))
        self.assertEqual(self.provider.health_status()["status"], "error")


if __name__ == "__main__":
    unittest.main()
