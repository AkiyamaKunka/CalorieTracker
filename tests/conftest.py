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
def _clear_android_timezone_cache():
    """database.get_android_timezone carries a 30s in-process TTL cache.

    Tests swap DB_PATH per test, so a cached offset from one test's database
    must never leak into the next (tests that monkeypatch the function itself
    bypass the cache entirely and don't need this). Cleared on both sides so
    a test can neither inherit nor bequeath a stale entry.
    """
    import database
    database.clear_android_timezone_cache()
    yield
    database.clear_android_timezone_cache()


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


# ─── Shared app-API test harness (moved from test_api_analyze_endpoints.py:
# fixtures in conftest need no imports, which keeps ruff F811 quiet in every
# suite that uses them) ───
from types import SimpleNamespace  # noqa: E402

import database  # noqa: E402
import telegram_bot  # noqa: E402


class FakeBot:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))
        return {"ok": True}

    def send_photo(self, *a, **kw):
        self.messages.append(("photo", a, kw))
        return {"ok": True}

    def _redact(self, e):
        return str(e)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "api.db")
    database.init_db()
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "secret-key")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "maybe_warn_android_vpn_inactive",
                        lambda *a, **kw: None)
    bot = FakeBot()
    app = telegram_bot._build_api_app(bot, object())
    return SimpleNamespace(http=app.test_client(), bot=bot)


def _meals():
    import sqlite3
    with sqlite3.connect(database.DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0]


def _ledger_rows():
    import sqlite3
    with sqlite3.connect(database.DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM photo_ingestions").fetchone()[0]
