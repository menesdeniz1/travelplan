"""Resource limits of the Google Flights browser: concurrency cap, one shared browser, request blocking, RSS.

No network and no real Chromium: sync_playwright is replaced by an in-memory fake that counts
launches, contexts and routes. Time is faked only inside the provider module, so the worker's
own poll loop still runs in real time.
"""

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from travel_engine.providers import google_flights_provider as gfp
from travel_engine.providers.google_flights_provider import GoogleFlightsProvider, should_block_request

FLIGHTS_URL = "https://www.google.com/travel/flights?q=one-way%20flights%20from%20Athens%20to%20Prague&hl=tr&gl=tr&curr=TRY"
NO_RESULTS_BODY = "Hiçbir sonuç yok."  # folds to a known no-results marker -> status "no_flights"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeStack:
    """Everything the fake Playwright did during one test."""

    def __init__(self):
        self.launches = []        # one FakeBrowser per chromium.launch call
        self.pw_stops = 0         # Playwright driver shutdowns
        self.contexts_opened = 0
        self.contexts_open = 0
        self.routes = []          # (pattern, handler) registered on pages
        self.start_error = None   # raised by sync_playwright().start() (persistent path)
        self.launch_error = None  # raised by chromium.launch
        self.context_error = None # raised by browser.new_context
        self.goto_error = None    # raised by page.goto
        self.goto_gate = None     # threading.Event: page.goto blocks until it is set
        self.crash_on_goto = False


class _FakePlaywright:
    def __init__(self, stack):
        self._stack = stack
        self.chromium = _FakeChromium(stack)

    def stop(self):
        self._stack.pw_stops += 1


class _FakeChromium:
    def __init__(self, stack):
        self._stack = stack

    def launch(self, **_kwargs):
        if self._stack.launch_error is not None:
            raise self._stack.launch_error
        browser = _FakeBrowser(self._stack)
        self._stack.launches.append(browser)
        return browser


class _FakeLauncher:
    """What sync_playwright() returns: `.start()` for the shared browser, `with` for launch-per-search."""

    def __init__(self, stack):
        self._stack = stack

    def start(self):
        if self._stack.start_error is not None:
            raise self._stack.start_error
        return _FakePlaywright(self._stack)

    def __enter__(self):
        return _FakePlaywright(self._stack)

    def __exit__(self, *_exc):
        return False


class _FakeBrowser:
    def __init__(self, stack):
        self._stack = stack
        self.connected = True
        self.closed = False

    def new_context(self, **_kwargs):
        if self._stack.context_error is not None:
            raise self._stack.context_error
        self._stack.contexts_opened += 1
        self._stack.contexts_open += 1
        return _FakeContext(self._stack, self)

    def is_connected(self):
        return self.connected and not self.closed

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, stack, browser):
        self._stack = stack
        self._browser = browser
        self._closed = False

    def new_page(self):
        return _FakePage(self._stack, self._browser)

    def close(self):
        if not self._closed:
            self._closed = True
            self._stack.contexts_open -= 1


class _FakePage:
    url = FLIGHTS_URL

    def __init__(self, stack, browser):
        self._stack = stack
        self._browser = browser
        self.keyboard = SimpleNamespace(press=lambda *_a, **_k: None)

    def route(self, pattern, handler):
        self._stack.routes.append((pattern, handler))

    def goto(self, _url, **_kwargs):
        if self._stack.goto_gate is not None:
            self._stack.goto_gate.wait(10)
        if self._stack.crash_on_goto:
            self._browser.connected = False
            raise RuntimeError("Target closed")
        if self._stack.goto_error is not None:
            raise self._stack.goto_error

    def query_selector(self, _selector):
        return None

    def query_selector_all(self, _selector):
        return []

    def wait_for_selector(self, _selector, **_kwargs):
        return None

    def wait_for_timeout(self, _ms):
        return None

    def inner_text(self, selector):
        return NO_RESULTS_BODY if selector == "body" else ""


class _FakeRoute:
    def __init__(self, url, resource_type):
        self.request = SimpleNamespace(url=url, resource_type=resource_type)
        self.outcome = None

    def abort(self):
        self.outcome = "abort"

    def continue_(self):
        self.outcome = "continue"


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Base(unittest.TestCase):
    def setUp(self):
        self.stack = FakeStack()
        self.clock = FakeClock()
        GoogleFlightsProvider.reset_shared_state()
        self.addCleanup(GoogleFlightsProvider.reset_shared_state)
        patches = [
            mock.patch.dict(os.environ, {
                "TRAVELPLAN_BROWSER_REUSE": "1",
                "TRAVELPLAN_MAX_BROWSERS": "2",
                "TRAVELPLAN_BROWSER_IDLE_SECS": "300",
                "TRAVELPLAN_FLIGHT_CACHE": "off",  # these tests count live scrapes; no disk cache
            }),
            mock.patch.object(gfp, "PLAYWRIGHT_AVAILABLE", True),
            mock.patch.object(gfp, "sync_playwright", lambda: _FakeLauncher(self.stack)),
            mock.patch.object(gfp, "time", self.clock),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_provider(self, **kwargs):
        kwargs.setdefault("min_interval_secs", 0.0)
        return GoogleFlightsProvider(**kwargs)

    def search(self, provider, day=15):
        """One-way Athens -> Prague on 2026-12-<day>. A new date avoids the result cache."""
        return provider.search_flights(
            origin="Athens",
            destination="Prague",
            departure_date=f"2026-12-{day:02d}",
            return_date=None,
            trip_type="oneway",
        )


class SharedBrowserTests(_Base):
    def test_no_browser_before_the_first_search(self):
        status = self.make_provider().resource_status()
        self.assertFalse(status["browser_running"])
        self.assertEqual(status["searches_since_launch"], 0)
        self.assertEqual(self.stack.launches, [])

    def test_one_browser_serves_several_searches(self):
        provider = self.make_provider()
        for day in (15, 16, 17):
            self.assertEqual(self.search(provider, day), [])
            self.assertEqual(provider.last_search_status["status"], "no_flights")
        self.assertEqual(len(self.stack.launches), 1)
        self.assertEqual(self.stack.contexts_opened, 3)
        status = provider.resource_status()
        self.assertTrue(status["browser_running"])
        self.assertEqual(status["searches_since_launch"], 3)

    def test_each_search_uses_a_fresh_context_that_is_closed(self):
        provider = self.make_provider()
        self.search(provider, day=15)
        self.search(provider, day=16)
        self.assertEqual(self.stack.contexts_opened, 2)
        self.assertEqual(self.stack.contexts_open, 0)

    def test_relaunch_after_the_configured_number_of_searches(self):
        provider = self.make_provider()
        with mock.patch.object(gfp, "RELAUNCH_AFTER_SEARCHES", 3):  # read when the worker is created
            for day in range(15, 22):  # seven searches
                self.search(provider, day)
        self.assertEqual(len(self.stack.launches), 3)
        self.assertEqual([b.closed for b in self.stack.launches], [True, True, False])
        self.assertEqual(provider.resource_status()["searches_since_launch"], 1)

    def test_browser_that_crashes_during_a_search_is_relaunched_for_the_next_one(self):
        provider = self.make_provider()
        self.stack.crash_on_goto = True
        self.assertEqual(self.search(provider, day=15), [])
        self.assertEqual(provider.last_search_status["status"], "error")
        self.assertIn("Target closed", provider.last_search_status["detail"])
        self.assertTrue(self.stack.launches[0].closed)
        self.assertFalse(provider.resource_status()["browser_running"])

        self.stack.crash_on_goto = False
        self.assertEqual(self.search(provider, day=16), [])
        self.assertEqual(provider.last_search_status["status"], "no_flights")
        self.assertEqual(len(self.stack.launches), 2)

    def test_page_error_is_reported_and_keeps_the_browser(self):
        provider = self.make_provider()
        self.stack.goto_error = RuntimeError("Timeout 25000ms exceeded")
        self.search(provider, day=15)
        self.assertEqual(provider.last_search_status["status"], "error")
        self.assertTrue(provider.last_search_status["detail"].startswith("RuntimeError"))
        self.assertTrue(provider.resource_status()["browser_running"])

        self.stack.goto_error = None
        self.search(provider, day=16)
        self.assertEqual(provider.last_search_status["status"], "no_flights")
        self.assertEqual(len(self.stack.launches), 1)

    def test_persistent_start_failure_falls_back_to_launch_per_search(self):
        provider = self.make_provider()
        self.stack.start_error = RuntimeError("driver missing")
        self.assertEqual(self.search(provider, day=15), [])
        self.assertEqual(provider.last_search_status["status"], "no_flights")
        self.assertEqual(len(self.stack.launches), 1)  # the fallback launch only
        self.assertFalse(provider.resource_status()["browser_running"])
        self.assertEqual(provider.health_status()["consecutive_failures"], 0)

    def test_context_failure_drops_the_shared_browser_then_falls_back(self):
        provider = self.make_provider()
        self.stack.context_error = RuntimeError("Target closed")
        self.search(provider, day=15)
        self.assertEqual(provider.last_search_status["status"], "error")
        self.assertEqual(len(self.stack.launches), 2)  # shared browser, then the fallback browser
        self.assertTrue(self.stack.launches[0].closed)
        self.assertFalse(provider.resource_status()["browser_running"])

    def test_shared_browser_timeout_is_an_error_and_no_second_browser_is_started(self):
        provider = self.make_provider()
        self.stack.goto_gate = threading.Event()
        try:
            with mock.patch.object(gfp, "BROWSER_JOB_TIMEOUT_SECS", 0.2):
                self.assertEqual(self.search(provider, day=15), [])
            self.assertEqual(provider.last_search_status["status"], "error")
            self.assertTrue(provider.last_search_status["detail"].startswith("_WorkerTimeout"))
            self.assertEqual(len(self.stack.launches), 1)
        finally:
            self.stack.goto_gate.set()

    def test_persistent_mode_can_be_switched_off(self):
        provider = self.make_provider()
        with mock.patch.dict(os.environ, {"TRAVELPLAN_BROWSER_REUSE": "0"}):
            self.assertFalse(gfp.browser_reuse_enabled())
            self.search(provider, day=15)
            self.search(provider, day=16)
        self.assertEqual(len(self.stack.launches), 2)  # launch-per-search: one browser per search
        self.assertFalse(provider.resource_status()["browser_running"])

    def test_every_page_registers_the_request_blocker(self):
        provider = self.make_provider()
        self.search(provider, day=15)
        self.assertEqual(len(self.stack.routes), 1)
        pattern, handler = self.stack.routes[0]
        self.assertEqual(pattern, "**/*")
        self.assertIs(handler, gfp._route_request)


class BrowserWorkerTests(_Base):
    def test_idle_browser_is_closed_after_the_idle_period(self):
        worker = gfp._BrowserWorker(idle_secs=300, relaunch_after=50, poll_secs=0.01)
        self.addCleanup(worker.shutdown, True)
        worker.run(lambda browser: None, timeout=5)
        self.assertTrue(worker.browser_running())

        self.clock.now += 299
        time.sleep(0.1)  # several poll cycles, still inside the idle window
        self.assertTrue(worker.browser_running())

        self.clock.now += 2  # 301 s without a job
        self.assertTrue(_wait_until(lambda: not worker.browser_running()))
        self.assertTrue(self.stack.launches[0].closed)
        self.assertEqual(self.stack.pw_stops, 1)

    def test_each_job_restarts_the_idle_timer(self):
        worker = gfp._BrowserWorker(idle_secs=300, relaunch_after=50, poll_secs=0.01)
        self.addCleanup(worker.shutdown, True)
        worker.run(lambda browser: None, timeout=5)
        self.clock.now += 200
        worker.run(lambda browser: None, timeout=5)
        self.clock.now += 200  # 200 s since the last job: not idle yet
        time.sleep(0.1)
        self.assertTrue(worker.browser_running())
        self.clock.now += 150
        self.assertTrue(_wait_until(lambda: not worker.browser_running()))

    def test_shutdown_closes_the_browser_and_the_driver(self):
        worker = gfp._BrowserWorker(idle_secs=300, relaunch_after=50, poll_secs=0.01)
        worker.run(lambda browser: None, timeout=5)
        worker.shutdown(wait=True)
        self.assertFalse(worker.browser_running())
        self.assertTrue(self.stack.launches[0].closed)
        self.assertEqual(self.stack.pw_stops, 1)
        with self.assertRaises(RuntimeError):
            worker.run(lambda browser: None, timeout=5)


class ConcurrencyCapTests(_Base):
    def test_semaphore_caps_concurrent_searches(self):
        active = {"now": 0, "peak": 0}
        guard = threading.Lock()

        def fake_run_scrape(_self, *_args, **_kwargs):
            with guard:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.05)  # real time: the test module's time is not patched
            with guard:
                active["now"] -= 1
            return gfp._new_scrape_state()

        with mock.patch.object(GoogleFlightsProvider, "_run_scrape", fake_run_scrape):
            provider = self.make_provider()
            threads = [threading.Thread(target=self.search, args=(provider, day)) for day in range(15, 21)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(active["peak"], 2)  # TRAVELPLAN_MAX_BROWSERS=2 in setUp

    def test_busy_when_no_slot_frees_up_in_time(self):
        slots = gfp._browser_slots()
        self.assertTrue(slots.acquire(timeout=0))
        self.assertTrue(slots.acquire(timeout=0))
        try:
            provider = self.make_provider()
            with mock.patch.object(gfp, "BROWSER_SLOT_WAIT_SECS", 0.05):
                self.assertEqual(self.search(provider, day=15), [])
            self.assertEqual(provider.last_search_status["status"], "error")
            self.assertEqual(provider.last_search_status["detail"], "busy")
            self.assertEqual(self.stack.launches, [])
        finally:
            slots.release()
            slots.release()

    def test_active_pages_counts_slots_in_use(self):
        provider = self.make_provider()
        slots = gfp._browser_slots()
        self.assertEqual(provider.resource_status()["active_pages"], 0)
        self.assertTrue(slots.acquire(timeout=0))
        self.assertEqual(provider.resource_status()["active_pages"], 1)
        slots.release()
        self.assertEqual(provider.resource_status()["active_pages"], 0)

    def test_max_browsers_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"TRAVELPLAN_MAX_BROWSERS": "3"}):
            self.assertEqual(gfp.max_browsers_from_env(), 3)
        with mock.patch.dict(os.environ, {"TRAVELPLAN_MAX_BROWSERS": "0"}):
            self.assertEqual(gfp.max_browsers_from_env(), 1)
        with mock.patch.dict(os.environ, {"TRAVELPLAN_MAX_BROWSERS": "many"}):
            self.assertEqual(gfp.max_browsers_from_env(), 1)
        with mock.patch.dict(os.environ, {"TRAVELPLAN_MAX_BROWSERS": ""}):
            self.assertEqual(gfp.max_browsers_from_env(), 1)  # default: one browser

    def test_idle_seconds_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"TRAVELPLAN_BROWSER_IDLE_SECS": "20"}):
            self.assertEqual(gfp.browser_idle_secs_from_env(), 20.0)
        with mock.patch.dict(os.environ, {"TRAVELPLAN_BROWSER_IDLE_SECS": ""}):
            self.assertEqual(gfp.browser_idle_secs_from_env(), 300.0)


class ResourceStatusTests(_Base):
    def test_resource_keys_and_types(self):
        provider = self.make_provider()
        self.search(provider, day=15)
        status = provider.resource_status()
        self.assertEqual(
            set(status),
            {"browser_running", "browsers_running", "active_pages", "max_browsers", "searches_since_launch",
             "process_rss_mb"},
        )
        self.assertIsInstance(status["browser_running"], bool)
        self.assertIsInstance(status["browsers_running"], int)
        self.assertIsInstance(status["active_pages"], int)
        self.assertEqual(status["max_browsers"], 2)
        self.assertIsInstance(status["searches_since_launch"], int)
        self.assertTrue(status["process_rss_mb"] is None or isinstance(status["process_rss_mb"], float))

    def test_process_rss_is_positive_on_windows_and_none_elsewhere(self):
        rss = gfp.process_rss_mb()
        if os.name == "nt":
            self.assertIsInstance(rss, float)
            self.assertGreater(rss, 0.0)
        else:
            self.assertIsNone(rss)


class RequestBlockingTests(unittest.TestCase):
    def test_media_font_and_image_resource_types_are_blocked(self):
        for resource_type in ("image", "media", "font"):
            with self.subTest(resource_type=resource_type):
                self.assertTrue(should_block_request("https://www.google.com/travel/flights", resource_type))

    def test_analytics_and_ads_hosts_are_blocked(self):
        urls = [
            "https://www.googletagmanager.com/gtm.js?id=GTM-1",
            "https://www.google-analytics.com/g/collect",
            "https://stats.g.doubleclick.net/r/collect",
            "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(should_block_request(url, "script"))

    def test_image_file_extensions_are_blocked_whatever_the_type(self):
        self.assertTrue(should_block_request("https://www.google.com/logos/doodle.png", "other"))
        self.assertTrue(should_block_request("https://www.google.com/static/icon.svg?v=2", ""))

    def test_flight_text_and_scripts_are_not_blocked(self):
        cases = [
            ("https://www.google.com/travel/flights?q=one-way%20flights", "document"),
            ("https://www.google.com/_/TravelFrontendUi/data/batchexecute?rpcids=x", "fetch"),
            ("https://www.google.com/travel/flights/static/app.js", "script"),
            ("https://www.google.com/async/x", "xhr"),
        ]
        for url, resource_type in cases:
            with self.subTest(url=url):
                self.assertFalse(should_block_request(url, resource_type))

    def test_route_handler_aborts_blocked_requests_and_continues_the_rest(self):
        blocked = _FakeRoute("https://www.google.com/logos/doodle.jpg", "image")
        allowed = _FakeRoute("https://www.google.com/travel/flights", "document")
        gfp._route_request(blocked)
        gfp._route_request(allowed)
        self.assertEqual(blocked.outcome, "abort")
        self.assertEqual(allowed.outcome, "continue")


class BrowserPoolTests(_Base):
    """N browsers, one per worker thread: parallel searches need no shared Playwright object."""

    def make_pool(self, size=2):
        pool = gfp._BrowserPool(size=size, idle_secs=300, relaunch_after=50, poll_secs=0.01)
        self.addCleanup(pool.shutdown, True)
        return pool

    def test_two_jobs_run_inside_two_browsers_at_the_same_time(self):
        pool = self.make_pool(size=2)
        barrier = threading.Barrier(2, timeout=5)  # both jobs must be inside a browser together

        def job(_browser):
            barrier.wait()
            return "ok"

        results = []
        threads = [threading.Thread(target=lambda: results.append(pool.run(job, timeout=10))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(len(self.stack.launches), 2)
        self.assertEqual(pool.browsers_running(), 2)

    def test_sequential_jobs_keep_using_one_browser(self):
        pool = self.make_pool(size=2)
        for _ in range(4):
            self.assertEqual(pool.run(lambda _browser: "ok", timeout=5), "ok")
        self.assertEqual(len(self.stack.launches), 1)
        self.assertEqual(pool.browsers_running(), 1)
        self.assertEqual(pool.searches_since_launch(), 4)

    def test_a_timed_out_job_keeps_its_browser_busy_and_the_next_job_goes_elsewhere(self):
        pool = self.make_pool(size=2)
        gate = threading.Event()
        self.addCleanup(gate.set)

        def stuck(_browser):
            gate.wait(5)
            return "late"

        with self.assertRaises(gfp._WorkerTimeout):
            pool.run(stuck, timeout=0.05)
        self.assertEqual(pool.run(lambda _browser: "fast", timeout=5), "fast")
        self.assertEqual(len(self.stack.launches), 2)  # the second job did not queue behind the stuck one
        gate.set()
        self.assertTrue(_wait_until(lambda: pool._busy == [0, 0]))

    def test_a_failing_job_releases_its_worker(self):
        pool = self.make_pool(size=2)

        def broken(_browser):
            raise RuntimeError("page crashed")

        with self.assertRaises(RuntimeError):
            pool.run(broken, timeout=5)
        self.assertEqual(pool._busy, [0, 0])

    def test_shutdown_closes_every_browser(self):
        pool = self.make_pool(size=2)
        threads = [threading.Thread(target=pool.run, args=(lambda _b: None, 5)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        pool.shutdown(wait=True)
        self.assertTrue(pool.closed)
        self.assertTrue(all(browser.closed for browser in self.stack.launches))
        with self.assertRaises(RuntimeError):
            pool.run(lambda _b: None, timeout=5)

    def test_parallel_provider_searches_each_get_a_browser(self):
        self.stack.goto_gate = threading.Event()
        self.addCleanup(self.stack.goto_gate.set)
        provider = self.make_provider()
        threads = [threading.Thread(target=self.search, args=(provider, day)) for day in (15, 16)]
        for thread in threads:
            thread.start()
        # Both pages are open at once; a serialised worker would never get to the second one.
        self.assertTrue(_wait_until(lambda: self.stack.contexts_open == 2))
        self.assertEqual(provider.resource_status()["browsers_running"], 2)
        self.stack.goto_gate.set()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(len(self.stack.launches), 2)

    def test_provider_reports_one_search_per_browser_as_concurrency(self):
        provider = self.make_provider()
        self.assertEqual(provider.search_concurrency(), 2)
        with mock.patch.dict(os.environ, {"TRAVELPLAN_MAX_BROWSERS": "3"}):
            self.assertEqual(provider.search_concurrency(), 3)


if __name__ == "__main__":
    unittest.main()
