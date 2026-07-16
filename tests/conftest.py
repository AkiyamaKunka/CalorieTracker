import sys
import os

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture(autouse=True)
def _isolate_service_health_ledger(tmp_path, monkeypatch):
    """Keep every test away from the real ~/CalorieTracker/logs/service_health.json.

    telegram_bot and daily_report resolve their ledger path through a module
    global (SERVICE_HEALTH_PATH, defaulting to the production path under
    Path.home()). Tests that exercise those code paths without patching it
    read AND write the developer's real ledger — real-world state such as an
    active gemini quota_pause_until then flips test outcomes (a pause that
    expired mid-run caused the 2026-07-16 22:46 one-off suite failure).
    Redirect the global to a per-test file by default; tests that patch
    SERVICE_HEALTH_PATH themselves simply override this.
    """
    path = tmp_path / "_autouse_service_health.json"
    for mod_name in ("telegram_bot", "daily_report"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "SERVICE_HEALTH_PATH"):
            monkeypatch.setattr(mod, "SERVICE_HEALTH_PATH", path)


@pytest.fixture(autouse=True)
def _no_real_pushplus_pushes(monkeypatch):
    """Tests must never send a real WeChat push.

    config loads the developer's .env, so utils.PUSHPLUS_TOKEN can hold a
    LIVE token during test runs — and code paths like the getUpdates
    streak-6 outage alert call utils.push_wechat as a side effect of
    ordinary backoff tests. push_wechat reads the module global at call
    time; null it by default. Tests exercising push_wechat set their own
    token (their setattr runs after this one and wins).
    """
    utils_mod = sys.modules.get("utils")
    if utils_mod is None:
        import utils as utils_mod
    monkeypatch.setattr(utils_mod, "PUSHPLUS_TOKEN", None)
