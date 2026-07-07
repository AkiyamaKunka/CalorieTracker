import sys
import os
import io
import json
import pytest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# Add root dir to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import telegram_bot
import database

@pytest.fixture
def mock_db(tmp_path):
    # Use a real temp file for the sqlite DB to avoid Pathlib errors
    old_db = database.DB_PATH
    database.DB_PATH = tmp_path / "test_meals.db"
    database.init_db()
    yield
    database.DB_PATH = old_db


class FakeBot:
    def __init__(self):
        self.sent = []
        self.answered = []
        self.deleted = []

    def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, callback_query_id, text=""):
        self.answered.append((callback_query_id, text))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class ImmediateThread:
    """Runs the target synchronously so upload background work is deterministic."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


def _delete_intent_client(indices):
    class FakeModels:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text=json.dumps({
                "intent": "delete",
                "meal_indices": indices,
                "reason": "test",
            }))

    return SimpleNamespace(models=FakeModels())


def _wide_recent_meals(chat_id, days=3):
    """Date-window independent snapshot so tests do not depend on the host clock."""
    return [
        m for m in database.get_meals(chat_id, "1970-01-01", "9999-12-31")
        if m.get("analysis", {}).get("is_food")
    ]


def test_b3_duplicate_prevention(mock_db, monkeypatch):
    """Test Case B3: Duplicate Prevention"""
    import hashlib
    monkeypatch.setattr(database, "get_android_timezone", lambda *args, **kwargs: "+0800")
    img_data = b"fake image data"
    img_hash = hashlib.md5(img_data).hexdigest()

    # Save meal on the user-local date (the clock get_todays_meals uses)
    # with a current server-clock timestamp so it's within the window.
    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_meal(12345, today, "12:00 PM", now_iso, "test", img_hash, "file1", {"is_food": True})

    # Verify it is flagged as duplicate
    is_dup = telegram_bot.is_duplicate_photo(12345, img_hash)
    assert is_dup is True

    # Try with different hash
    is_dup2 = telegram_bot.is_duplicate_photo(12345, "different_hash")
    assert is_dup2 is False


def test_b1_upload_analysis_failure_moves_to_failed_dir(monkeypatch, tmp_path):
    """Test Case B1: Gemini failure on /upload keeps the photo and notifies the user."""
    pending_dir = tmp_path / "pending"
    failed_dir = tmp_path / "failed"
    statuses = []
    bot = FakeBot()

    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", pending_dir)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "service_health.json")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot, "analyze_food_photo_with_retries", lambda client, image_bytes: None)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        telegram_bot.database, "mark_photo_hash_status",
        lambda *args, **kwargs: statuses.append((args, kwargs)),
    )
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    app = telegram_bot._build_api_app(bot, object())
    resp = app.test_client().post(
        "/upload",
        headers={"X-API-Key": "test-upload-key"},
        data={"photo": (io.BytesIO(b"upload-analysis-failure-bytes"), "meal.jpg")},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "processing_in_background"}
    assert list(pending_dir.iterdir()) == []
    assert len(list(failed_dir.iterdir())) == 1
    assert statuses and statuses[0][0][2] == "failed"
    assert any("could not be analyzed" in m["text"] for m in bot.sent)


def test_b1b_upload_during_quota_pause_offers_keep_or_discard(monkeypatch, tmp_path):
    """During a Gemini quota pause, a failed /upload keeps the photo and asks
    the user to keep or discard via inline keyboard instead of a plain error."""
    pending_dir = tmp_path / "pending"
    failed_dir = tmp_path / "failed"
    statuses = []
    bot = FakeBot()

    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", pending_dir)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "service_health.json")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot, "analyze_food_photo_with_retries", lambda client, image_bytes: None)
    monkeypatch.setattr(telegram_bot, "_gemini_quota_pause",
                        lambda: {"until": datetime.now(), "reason": "daily quota", "set_at": ""})
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        telegram_bot.database, "mark_photo_hash_status",
        lambda *args, **kwargs: statuses.append((args, kwargs)),
    )
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    app = telegram_bot._build_api_app(bot, object())
    resp = app.test_client().post(
        "/upload",
        headers={"X-API-Key": "test-upload-key"},
        data={"photo": (io.BytesIO(b"quota-paused-upload-bytes"), "meal.jpg")},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "processing_in_background"}
    assert list(pending_dir.iterdir()) == []
    assert len(list(failed_dir.iterdir())) == 1
    assert statuses and statuses[0][0][2] == "failed"

    decision = bot.sent[-1]
    assert decision["reply_markup"] is not None
    callbacks = [
        button["callback_data"]
        for row in decision["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert any(data.startswith("quota_keep:") for data in callbacks)
    assert any(data.startswith("quota_discard:") for data in callbacks)


def test_b2_upload_oversized_body_returns_413_json(monkeypatch):
    """Test Case B2: oversized multipart bodies are rejected with the JSON error shape."""
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "MAX_API_UPLOAD_BYTES", 128)

    app = telegram_bot._build_api_app(FakeBot(), object())
    resp = app.test_client().post(
        "/upload",
        headers={"X-API-Key": "test-upload-key"},
        data={"photo": (io.BytesIO(b"x" * 1024), "big.jpg")},
    )

    assert resp.status_code == 413
    assert resp.get_json() == {"error": "Photo too large"}


def test_reconcile_endpoint_requires_api_key(monkeypatch):
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")

    app = telegram_bot._build_api_app(FakeBot(), object())
    resp = app.test_client().post("/reconcile", json={"hashes": ["a" * 32]})

    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Unauthorized"}


def test_reconcile_endpoint_rejects_payload_without_hashes(monkeypatch):
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "maybe_warn_android_vpn_inactive", lambda *a, **k: None)

    app = telegram_bot._build_api_app(FakeBot(), object())
    client = app.test_client()
    headers = {"X-API-Key": "test-upload-key"}

    for resp in (
        client.post("/reconcile", headers=headers, json={"wrong": []}),
        client.post("/reconcile", headers=headers, data="not json",
                    content_type="application/json"),
    ):
        assert resp.status_code == 400
        assert resp.get_json() == {"error": "Missing hashes array"}


def test_reconcile_endpoint_reports_only_truly_missing_hashes(monkeypatch, tmp_path):
    """Endpoint contract: logged, reserved, and failed-saved hashes are
    suppressed; only hashes the server has never seen come back."""
    logged = "aa" * 16
    reserved = "bb" * 16
    failed = "cc" * 16
    unknown = "dd" * 16

    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    (failed_dir / f"20260707_120000_{failed[:12]}.jpg").write_bytes(b"x")

    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "maybe_warn_android_vpn_inactive", lambda *a, **k: None)
    monkeypatch.setattr(telegram_bot.database, "get_today_hashes", lambda chat_id: [logged])
    monkeypatch.setattr(telegram_bot.database, "get_reserved_photo_hashes", lambda chat_id: [reserved])

    app = telegram_bot._build_api_app(FakeBot(), object())
    resp = app.test_client().post(
        "/reconcile",
        headers={"X-API-Key": "test-upload-key"},
        json={"hashes": [logged, reserved, failed, unknown]},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"missing_hashes": [unknown]}


def test_ping_endpoint_requires_api_key(monkeypatch):
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")

    app = telegram_bot._build_api_app(FakeBot(), object())
    resp = app.test_client().post("/ping", json={"timezone": "+0800"})

    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Unauthorized"}


def test_ping_endpoint_stores_reported_timezone(monkeypatch):
    heartbeats = []
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "maybe_warn_android_vpn_inactive", lambda *a, **k: None)
    monkeypatch.setattr(
        telegram_bot.database, "update_android_heartbeat",
        lambda timezone=None: heartbeats.append(timezone),
    )

    app = telegram_bot._build_api_app(FakeBot(), object())
    resp = app.test_client().post(
        "/ping", headers={"X-API-Key": "test-upload-key"}, json={"timezone": "+0530"},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
    assert heartbeats == ["+0530"]


def test_ping_endpoint_without_timezone_preserves_stored_offset(monkeypatch):
    """A ping lacking a timezone must pass None so the stored offset —
    which drives all meal dating — is not clobbered back to +0800."""
    heartbeats = []
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "maybe_warn_android_vpn_inactive", lambda *a, **k: None)
    monkeypatch.setattr(
        telegram_bot.database, "update_android_heartbeat",
        lambda timezone=None: heartbeats.append(timezone),
    )

    app = telegram_bot._build_api_app(FakeBot(), object())
    client = app.test_client()
    headers = {"X-API-Key": "test-upload-key"}

    assert client.post("/ping", headers=headers).status_code == 200
    assert client.post("/ping", headers=headers, json={"device": "x"}).status_code == 200
    assert heartbeats == [None, None]


@patch('telegram_bot.Image.open')
def test_b10_gemini_json_parsing(mock_image_open, mock_db):
    """Test Case B10: Actual Gemini JSON Parsing fallback"""
    # Simulate Gemini returning markdown wrapped JSON
    mock_resp = MagicMock()
    mock_resp.text = "```json\n{\n  \"description\": \"Apple\",\n  \"calories\": 95,\n  \"is_food\": true\n}\n```"

    client_mock = MagicMock()
    client_mock.models.generate_content.return_value = mock_resp
    mock_image_open.return_value = MagicMock()

    analysis = telegram_bot.analyze_food_photo(client_mock, b"image")

    assert analysis is not None
    assert analysis["calories"] == 95
    assert analysis["description"] == "Apple"


def test_b12_delete_intent_with_empty_database(mock_db, monkeypatch, tmp_path):
    """Test Case B12: Empty Database Deletion"""
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    bot = FakeBot()

    telegram_bot.handle_text_message(_delete_intent_client([0]), bot, 12345, "delete everything")

    assert any("Cannot delete" in m["text"] for m in bot.sent)


def test_b7_nl_delete_requires_confirmation_then_deletes(mock_db, monkeypatch, tmp_path):
    """Test Case B7: Delete Command Integrity (two-step confirm)"""
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(database, "get_android_timezone", lambda *args, **kwargs: "+0800")
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    telegram_bot._pending_nl_deletes.clear()

    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_meal(12345, today, "12:00", now_iso, "test", "hash1", "file1",
                       {"is_food": True, "meal_description": "Apple", "total_calories": 95})
    database.save_meal(12345, today, "12:01", now_iso, "test", "hash2", "file2",
                       {"is_food": True, "meal_description": "Banana", "total_calories": 105})

    snapshot = _wide_recent_meals(12345)
    assert len(snapshot) == 2
    target = snapshot[0]
    expected_remaining = "Apple" if target["analysis"]["meal_description"] == "Banana" else "Banana"

    bot = FakeBot()
    telegram_bot.handle_text_message(_delete_intent_client([0]), bot, 12345, "delete the first meal")

    # Nothing is deleted before the user confirms
    assert len(_wide_recent_meals(12345)) == 2
    pending = telegram_bot._pending_nl_deletes[12345]
    assert pending["ids"] == [target["id"]]
    token = pending["token"]
    assert token
    markup = bot.sent[-1]["reply_markup"]
    callback_datas = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
    ]
    # The buttons carry the nonce binding them to this confirmation message.
    assert f"nl_delete_confirm:{token}" in callback_datas
    assert f"nl_delete_cancel:{token}" in callback_datas

    handled = telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-confirm",
        "data": f"nl_delete_confirm:{token}",
        "message": {"chat": {"id": 12345}},
    })

    assert handled is True
    remaining = _wide_recent_meals(12345)
    assert len(remaining) == 1
    assert remaining[0]["analysis"]["meal_description"] == expected_remaining
    assert 12345 not in telegram_bot._pending_nl_deletes


def test_nl_delete_cancel_keeps_meals(mock_db, monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(database, "get_android_timezone", lambda *args, **kwargs: "+0800")
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    telegram_bot._pending_nl_deletes.clear()

    today = database.user_local_now().date().isoformat()
    database.save_meal(12345, today, "12:00", datetime.now().isoformat(), "test", "hash1", "file1",
                       {"is_food": True, "meal_description": "Apple", "total_calories": 95})

    bot = FakeBot()
    telegram_bot.handle_text_message(_delete_intent_client([0]), bot, 12345, "delete the apple")
    assert telegram_bot._pending_nl_deletes
    token = telegram_bot._pending_nl_deletes[12345]["token"]

    handled = telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-cancel",
        "data": f"nl_delete_cancel:{token}",
        "message": {"chat": {"id": 12345}},
    })

    assert handled is True
    assert len(_wide_recent_meals(12345)) == 1
    assert 12345 not in telegram_bot._pending_nl_deletes
    assert any("nothing was deleted" in m["text"] for m in bot.sent)
