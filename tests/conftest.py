"""Session-wide test isolation.

pytest imports this file before any test module. Several test modules import
``server_v2``, which builds its PriceTracker / PriceHistoryManager / AlertNotifier and
the search log at import time from the real repo-root paths. To keep the user's real
data out of the test run, the environment is pointed at a session temp directory here,
before those imports happen, and legacy JSON migration is switched off.
"""

import atexit
import os
import shutil
import tempfile

import pytest

_SESSION_DIR = tempfile.mkdtemp(prefix="travelplan_tests_")

# Forced (not setdefault): a developer's shell must not redirect the tests to real data.
os.environ["TRAVELPLAN_DB"] = os.path.join(_SESSION_DIR, "travelplan.db")
os.environ["TRAVELPLAN_SEARCH_LOG"] = os.path.join(_SESSION_DIR, "search_log.db")
os.environ["TRAVELPLAN_NO_MIGRATE"] = "1"


def _cleanup_session_dir():
    try:
        shutil.rmtree(_SESSION_DIR, ignore_errors=True)
    except Exception:
        pass


atexit.register(_cleanup_session_dir)


@pytest.fixture(autouse=True)
def _isolated_database(monkeypatch):
    """Give every test its own empty database file.

    The session-wide TRAVELPLAN_DB makes every store share one file, so rows written
    by one test (watches, alerts, price points) would show up in the next test's
    freshly built trackers. Each test therefore gets a new file inside the session
    directory. Module-level objects created at import time keep the session file.
    """
    test_dir = tempfile.mkdtemp(prefix="db_", dir=_SESSION_DIR)
    monkeypatch.setenv("TRAVELPLAN_DB", os.path.join(test_dir, "travelplan.db"))
    yield
