"""The app-facing analysis endpoints (/api/analyze_photo, /api/text_intent).

These exist so the phone can use the owner's Claude SUBSCRIPTION (the CLI on
this VM) instead of a metered API key. The contract they must keep:

  * auth is mandatory (they expose the subscription to whoever can reach the
    port);
  * they are PURE — no meal row, no ledger reservation, no Telegram message
    (the app owns its own database, and a double-log would be invisible to
    the user until totals drifted);
  * an unavailable/contended CLI answers 503 so the app treats the photo as
    retryable rather than burning it.
"""
import io
from types import SimpleNamespace

import pytest

import claude_analyzer
import database
import telegram_bot


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


ANALYSIS = {"is_food": True, "total_calories": 450, "meal_description": "Salad"}


def test_photo_requires_the_api_key(client, monkeypatch):
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    called = []
    monkeypatch.setattr(claude_analyzer, "analyze_food_photo",
                        lambda b, p=None: called.append(b) or ANALYSIS)
    resp = client.http.post("/api/analyze_photo", data=b"x",
                            content_type="image/jpeg")
    assert resp.status_code == 401
    assert not called, "the CLI must never run for an unauthenticated caller"


def test_photo_returns_analysis_without_touching_the_database(client, monkeypatch):
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(claude_analyzer, "analyze_food_photo", lambda b, p=None: dict(ANALYSIS))
    resp = client.http.post(
        "/api/analyze_photo",
        headers={"X-API-Key": "secret-key"},
        data={"photo": (io.BytesIO(b"jpegbytes"), "m.jpg")},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["analysis"]["total_calories"] == 450
    assert body["analyzed_by"] == "claude"
    # Purity: the app keeps its own rows, and the owner gets no phantom card.
    assert _meals() == 0
    assert _ledger_rows() == 0
    assert client.bot.messages == []


def test_photo_accepts_a_raw_body_too(client, monkeypatch):
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    seen = {}
    monkeypatch.setattr(claude_analyzer, "analyze_food_photo",
                        lambda b, p=None: seen.setdefault("bytes", b) and None or dict(ANALYSIS))
    resp = client.http.post("/api/analyze_photo",
                            headers={"X-API-Key": "secret-key"},
                            data=b"rawjpeg", content_type="image/jpeg")
    assert resp.status_code == 200
    assert seen["bytes"] == b"rawjpeg"


def test_photo_rejects_empty_and_oversize(client, monkeypatch):
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(claude_analyzer, "analyze_food_photo", lambda b, p=None: dict(ANALYSIS))
    empty = client.http.post("/api/analyze_photo",
                             headers={"X-API-Key": "secret-key"},
                             data=b"", content_type="image/jpeg")
    assert empty.status_code == 400

    monkeypatch.setattr(telegram_bot, "API_ANALYZE_MAX_BYTES", 10)
    big = client.http.post("/api/analyze_photo",
                           headers={"X-API-Key": "secret-key"},
                           data=b"x" * 11, content_type="image/jpeg")
    assert big.status_code == 413


def test_photo_503_when_the_cli_is_unavailable_or_busy(client, monkeypatch):
    # Not configured at all.
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: False)
    resp = client.http.post("/api/analyze_photo",
                            headers={"X-API-Key": "secret-key"},
                            data=b"jpeg", content_type="image/jpeg")
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "claude_unavailable"

    # Configured, but the analyzer declined (contended lock / timeout / junk).
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(claude_analyzer, "analyze_food_photo", lambda b, p=None: None)
    resp = client.http.post("/api/analyze_photo",
                            headers={"X-API-Key": "secret-key"},
                            data=b"jpeg", content_type="image/jpeg")
    assert resp.status_code == 503
    assert _meals() == 0


def test_text_intent_happy_path_and_auth(client, monkeypatch):
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(claude_analyzer, "analyze_text_prompt",
                        lambda p: {"result": {"intent": "correction"}})
    unauth = client.http.post("/api/text_intent", json={"prompt": "hi"})
    assert unauth.status_code == 401

    resp = client.http.post("/api/text_intent",
                            headers={"X-API-Key": "secret-key"},
                            json={"prompt": "make lunch 600 kcal"})
    assert resp.status_code == 200
    assert resp.get_json()["result"] == {"intent": "correction"}
    assert _meals() == 0


def test_text_intent_rejects_missing_blank_and_huge_prompts(client, monkeypatch):
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(claude_analyzer, "analyze_text_prompt",
                        lambda p: {"result": {}})
    h = {"X-API-Key": "secret-key"}
    assert client.http.post("/api/text_intent", headers=h, json={}).status_code == 400
    assert client.http.post("/api/text_intent", headers=h,
                            json={"prompt": "   "}).status_code == 400
    assert client.http.post("/api/text_intent", headers=h,
                            json={"prompt": 42}).status_code == 400
    monkeypatch.setattr(telegram_bot, "API_TEXT_MAX_CHARS", 10)
    assert client.http.post("/api/text_intent", headers=h,
                            json={"prompt": "x" * 11}).status_code == 413


def test_text_intent_503_when_declined(client, monkeypatch):
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(claude_analyzer, "analyze_text_prompt", lambda p: None)
    resp = client.http.post("/api/text_intent",
                            headers={"X-API-Key": "secret-key"},
                            json={"prompt": "anything"})
    assert resp.status_code == 503


def test_photo_json_shape_passes_the_apps_prompt_through(client, monkeypatch):
    """The app sends {image_b64, prompt} so its dietary-profile appendix
    reaches the model; the server's own prompt copy has no way to know it."""
    import base64 as b64
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    seen = {}

    def fake(image_bytes, prompt=None):
        seen["bytes"] = image_bytes
        seen["prompt"] = prompt
        return dict(ANALYSIS)

    monkeypatch.setattr(claude_analyzer, "analyze_food_photo", fake)
    resp = client.http.post(
        "/api/analyze_photo",
        headers={"X-API-Key": "secret-key"},
        json={"image_b64": b64.b64encode(b"jpegdata").decode(),
              "prompt": "PROMPT with dietary profile"},
    )
    assert resp.status_code == 200
    assert seen["bytes"] == b"jpegdata"
    assert seen["prompt"] == "PROMPT with dietary profile"


def test_photo_json_rejects_bad_base64_and_missing_image(client, monkeypatch):
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(claude_analyzer, "analyze_food_photo",
                        lambda b, p=None: dict(ANALYSIS))
    h = {"X-API-Key": "secret-key"}
    assert client.http.post("/api/analyze_photo", headers=h,
                            json={"prompt": "x"}).status_code == 400
    bad = client.http.post("/api/analyze_photo", headers=h,
                           json={"image_b64": "!!!not base64!!!"})
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "bad_image_encoding"
