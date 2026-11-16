"""Tests for prioritized browser slots (GOO-9 / B06)."""

import os
import random
import threading
import time
import unittest
from unittest import mock

from travel_engine.google_budget import (
    PRIORITY_INTERACTIVE,
    PRIORITY_WATCH,
    PRIORITY_PREFETCH,
    set_current_priority,
    reset_google_budget,
)
from travel_engine.providers.google_flights_provider import (
    _BrowserSlots,
    DEFAULT_BROWSER_STARVATION_LIMIT,
)


class BrowserSlotsPriorityTests(unittest.TestCase):
    def setUp(self):
        reset_google_budget()

    def test_interactive_prioritized_over_waiting_background(self):
        """When a slot frees up, a waiting interactive request gets it before waiting background."""
        slots = _BrowserSlots(capacity=1, starvation_limit=5)
        # Occupy the single slot
        self.assertTrue(slots.acquire(timeout=1.0, priority=PRIORITY_INTERACTIVE))

        execution_order = []
        bg_started = threading.Event()
        int_started = threading.Event()

        def background_worker():
            bg_started.set()
            if slots.acquire(timeout=3.0, priority=PRIORITY_WATCH):
                try:
                    execution_order.append("background")
                finally:
                    slots.release()

        def interactive_worker():
            int_started.set()
            if slots.acquire(timeout=3.0, priority=PRIORITY_INTERACTIVE):
                try:
                    execution_order.append("interactive")
                finally:
                    slots.release()

        t_bg = threading.Thread(target=background_worker)
        t_int = threading.Thread(target=interactive_worker)

        t_bg.start()
        bg_started.wait(timeout=1.0)
        time.sleep(0.05)

        t_int.start()
        int_started.wait(timeout=1.0)
        time.sleep(0.05)

        # Release initial holder; interactive thread should get slot before background
        slots.release()

        t_int.join(timeout=2.0)
        t_bg.join(timeout=2.0)

        self.assertEqual(execution_order, ["interactive", "background"])

    def test_fifo_order_preserved_within_same_priority(self):
        """Requests of the same priority are served in FIFO order."""
        slots = _BrowserSlots(capacity=1)
        self.assertTrue(slots.acquire(timeout=1.0, priority=PRIORITY_INTERACTIVE))

        execution_order = []
        ready1 = threading.Event()
        ready2 = threading.Event()

        def worker(name, ready_evt):
            ready_evt.set()
            if slots.acquire(timeout=3.0, priority=PRIORITY_WATCH):
                try:
                    execution_order.append(name)
                finally:
                    slots.release()

        t1 = threading.Thread(target=worker, args=("bg_first", ready1))
        t2 = threading.Thread(target=worker, args=("bg_second", ready2))

        t1.start()
        ready1.wait(timeout=1.0)
        time.sleep(0.05)

        t2.start()
        ready2.wait(timeout=1.0)
        time.sleep(0.05)

        slots.release()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        self.assertEqual(execution_order, ["bg_first", "bg_second"])

    def test_starvation_prevention_background_gets_slot_after_n_interactive(self):
        """Background request does not starve: gets slot after at most N consecutive interactive."""
        starvation_limit = 2
        slots = _BrowserSlots(capacity=1, starvation_limit=starvation_limit)

        # Hold the slot initially
        self.assertTrue(slots.acquire(timeout=1.0, priority=PRIORITY_INTERACTIVE))

        execution_order = []
        bg_ready = threading.Event()

        def bg_worker():
            bg_ready.set()
            if slots.acquire(timeout=5.0, priority=PRIORITY_WATCH):
                try:
                    execution_order.append("bg")
                finally:
                    slots.release()

        t_bg = threading.Thread(target=bg_worker)
        t_bg.start()
        bg_ready.wait(timeout=1.0)
        time.sleep(0.05)

        # We will enqueue 3 interactive requests: int_1, int_2, int_3
        # Since starvation_limit=2, after int_1 and int_2, bg MUST get the slot before int_3!
        int1_ready = threading.Event()
        int2_ready = threading.Event()
        int3_ready = threading.Event()

        def int_worker(name, ready):
            ready.set()
            if slots.acquire(timeout=5.0, priority=PRIORITY_INTERACTIVE):
                try:
                    execution_order.append(name)
                finally:
                    slots.release()

        t_int1 = threading.Thread(target=int_worker, args=("int_1", int1_ready))
        t_int2 = threading.Thread(target=int_worker, args=("int_2", int2_ready))
        t_int3 = threading.Thread(target=int_worker, args=("int_3", int3_ready))

        t_int1.start()
        int1_ready.wait(timeout=1.0)
        time.sleep(0.02)

        t_int2.start()
        int2_ready.wait(timeout=1.0)
        time.sleep(0.02)

        t_int3.start()
        int3_ready.wait(timeout=1.0)
        time.sleep(0.02)

        # Release initial slot holder
        slots.release()

        t_bg.join(timeout=3.0)
        t_int1.join(timeout=3.0)
        t_int2.join(timeout=3.0)
        t_int3.join(timeout=3.0)

        # int_1 and int_2 run first (2 = starvation_limit), then bg is guaranteed slot before int_3!
        self.assertEqual(execution_order, ["int_1", "int_2", "bg", "int_3"])

    def test_priority_read_from_contextvars_default_interactive(self):
        """Slots acquire reads priority from B05 contextvars if not explicitly given."""
        slots = _BrowserSlots(capacity=1)
        self.assertTrue(slots.acquire(timeout=1.0))

        order = []
        bg_ready = threading.Event()
        int_ready = threading.Event()

        def bg_worker():
            set_current_priority(PRIORITY_WATCH)
            bg_ready.set()
            if slots.acquire(timeout=3.0):
                try:
                    order.append("bg")
                finally:
                    slots.release()

        def int_worker():
            set_current_priority(PRIORITY_INTERACTIVE)
            int_ready.set()
            if slots.acquire(timeout=3.0):
                try:
                    order.append("interactive")
                finally:
                    slots.release()

        t_bg = threading.Thread(target=bg_worker)
        t_int = threading.Thread(target=int_worker)

        t_bg.start()
        bg_ready.wait(timeout=1.0)
        time.sleep(0.05)

        t_int.start()
        int_ready.wait(timeout=1.0)
        time.sleep(0.05)

        slots.release()

        t_int.join(timeout=2.0)
        t_bg.join(timeout=2.0)

        self.assertEqual(order, ["interactive", "bg"])

    def test_timeout_returns_false_and_cleans_queue(self):
        """A waiting request that times out returns False and leaves no leftover state."""
        slots = _BrowserSlots(capacity=1)
        self.assertTrue(slots.acquire(timeout=1.0))

        # Second acquire times out
        t0 = time.monotonic()
        acquired = slots.acquire(timeout=0.08, priority=PRIORITY_WATCH)
        elapsed = time.monotonic() - t0

        self.assertFalse(acquired)
        self.assertGreaterEqual(elapsed, 0.07)
        self.assertEqual(slots.active, 1)
        self.assertEqual(slots.waiting_background, 0)
        self.assertEqual(slots.waiting_total, 0)

        slots.release()
        self.assertEqual(slots.active, 0)

        # Now acquire succeeds immediately
        self.assertTrue(slots.acquire(timeout=0.1, priority=PRIORITY_INTERACTIVE))
        slots.release()
        self.assertEqual(slots.active, 0)

    def test_zero_timeout_immediate_check(self):
        """timeout=0 acts as non-blocking try_acquire."""
        slots = _BrowserSlots(capacity=1)
        self.assertTrue(slots.acquire(timeout=0))
        self.assertEqual(slots.active, 1)
        self.assertFalse(slots.acquire(timeout=0))
        slots.release()
        self.assertEqual(slots.active, 0)
        self.assertTrue(slots.acquire(timeout=0))
        slots.release()

    def test_env_starvation_limit(self):
        """TRAVELPLAN_BROWSER_STARVATION_LIMIT env var sets default starvation limit."""
        with mock.patch.dict(os.environ, {"TRAVELPLAN_BROWSER_STARVATION_LIMIT": "7"}):
            slots = _BrowserSlots(capacity=2)
            self.assertEqual(slots.starvation_limit, 7)

        with mock.patch.dict(os.environ, {"TRAVELPLAN_BROWSER_STARVATION_LIMIT": "invalid"}):
            slots2 = _BrowserSlots(capacity=2)
            self.assertEqual(slots2.starvation_limit, DEFAULT_BROWSER_STARVATION_LIMIT)

    def test_high_contention_no_deadlock(self):
        """Many concurrent threads with random priorities all complete without deadlock."""
        slots = _BrowserSlots(capacity=2, starvation_limit=3)
        completed = []
        completed_lock = threading.Lock()
        threads = []

        def task(tid: int, prio: str):
            if slots.acquire(timeout=5.0, priority=prio):
                try:
                    time.sleep(0.005)
                    with completed_lock:
                        completed.append((tid, prio))
                finally:
                    slots.release()

        priorities = [PRIORITY_INTERACTIVE, PRIORITY_WATCH, PRIORITY_PREFETCH]
        for i in range(16):
            p = priorities[i % len(priorities)]
            t = threading.Thread(target=task, args=(i, p))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(len(completed), 16)
        self.assertEqual(slots.active, 0)
        self.assertEqual(slots.waiting_total, 0)


if __name__ == "__main__":
    unittest.main()
