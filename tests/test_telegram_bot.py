import sys
import os
import hashlib
import io
import json
import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# Add root dir to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import telegram_bot
import database
import config

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


def test_upload_uses_declared_original_hash(monkeypatch, tmp_path):
    """A recompressing client declares the ORIGINAL file's hash; the ledger,
    staged filename, and saved meal must all key on it, not on the received
    (recompressed) bytes."""
    declared = "ab" * 16
    reserved, saved = [], []
    bot = FakeBot()

    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot, "analyze_food_photo_with_retries",
                        lambda client, image_bytes: {"is_food": True, "total_calories": 500})
    monkeypatch.setattr(telegram_bot, "save_meal",
                        lambda chat_id, analysis, source, file_id, img_hash: saved.append(img_hash) or 77)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash",
                        lambda chat_id, image_hash, *a, **k: reserved.append(image_hash) or True)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status", lambda *a, **k: None)
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    app = telegram_bot._build_api_app(bot, object())
    resp = app.test_client().post(
        "/upload",
        headers={"X-API-Key": "test-upload-key"},
        data={"photo": (io.BytesIO(b"recompressed jpeg bytes"), "meal.jpg"),
              "original_hash": declared.upper()},  # case-insensitive
    )

    assert resp.status_code == 200
    assert reserved == [declared]
    assert saved == [declared]


def test_upload_ignores_malformed_original_hash(monkeypatch, tmp_path):
    reserved = []
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash",
                        lambda chat_id, image_hash, *a, **k: reserved.append(image_hash) or False)

    body = b"plain old client bytes"
    expected = hashlib.md5(body).hexdigest()
    app = telegram_bot._build_api_app(FakeBot(), object())
    for bad in ("zz" * 16, "abc123", ""):
        resp = app.test_client().post(
            "/upload",
            headers={"X-API-Key": "test-upload-key"},
            data={"photo": (io.BytesIO(body), "meal.jpg"), "original_hash": bad},
        )
        assert resp.status_code == 200  # duplicate path (reserve False)

    assert reserved == [expected] * 3  # fell back to hashing the bytes


def test_upload_staged_read_failure_keeps_file_as_failed_under_declared_hash(monkeypatch, tmp_path):
    """If the staged file cannot be read back, the upload must land in the
    failed dir keyed under the DECLARED hash with a 'failed' ledger row —
    NOT release the reservation while the file lingers in pending, where the
    boot sweep's md5 fallback would re-key it (recompressed bytes hash
    differently) and enable duplicate meals and double Gemini spend."""
    declared = "ef" * 16
    pending_dir = tmp_path / "pending"
    failed_dir = tmp_path / "failed"
    statuses, released = [], []
    bot = FakeBot()

    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", pending_dir)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "service_health.json")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(
        telegram_bot, "analyze_food_photo_with_retries",
        lambda client, image_bytes: pytest.fail("must not analyze when the staged read fails"),
    )
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        telegram_bot.database, "mark_photo_hash_status",
        lambda *args, **kwargs: statuses.append((args, kwargs)),
    )
    monkeypatch.setattr(
        telegram_bot.database, "release_photo_hash",
        lambda chat_id, image_hash: released.append(image_hash),
    )
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    # Stage normally, then make the staged bytes unreadable before the
    # background read.
    original_stage = telegram_bot._stage_api_upload

    def stage_then_lock(image_bytes, image_hash, filename):
        path = original_stage(image_bytes, image_hash, filename)
        path.chmod(0)
        return path

    monkeypatch.setattr(telegram_bot, "_stage_api_upload", stage_then_lock)

    app = telegram_bot._build_api_app(bot, object())
    try:
        resp = app.test_client().post(
            "/upload",
            headers={"X-API-Key": "test-upload-key"},
            data={"photo": (io.BytesIO(b"recompressed bytes, hash differs from declared"), "meal.jpg"),
                  "original_hash": declared},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"status": "processing_in_background"}

        # File moved out of pending into failed, still keyed by the DECLARED hash.
        assert list(pending_dir.iterdir()) == []
        failed_files = list(failed_dir.iterdir())
        assert len(failed_files) == 1
        assert f"_{declared[:12]}" in failed_files[0].name

        # Ledger row flipped to 'failed' for the declared hash; the
        # reservation was NOT released.
        assert statuses and statuses[0][0] == (12345, declared, "failed")
        assert statuses[0][1].get("source") == "api_upload"
        assert released == []

        # The user is told how to recover.
        assert any("/retry_failed" in m["text"] for m in bot.sent)
    finally:
        for path in list(pending_dir.iterdir() if pending_dir.exists() else []) + \
                list(failed_dir.iterdir() if failed_dir.exists() else []):
            path.chmod(0o644)


def test_retry_and_sweep_resolve_ledger_hash_from_filename(monkeypatch, tmp_path):
    """Staged bytes of a recompressed upload hash differently than the ledger
    key; retry and the startup sweep must resolve through the ledger via the
    filename prefix instead of rehashing."""
    declared = "cd" * 16
    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    staged = failed_dir / f"20260708_010000_{declared[:12]}.jpg"
    staged.write_bytes(b"recompressed bytes that hash differently")

    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot.database, "find_photo_hash_by_prefix",
                        lambda chat_id, prefix: declared if prefix == declared[:12] else None)

    assert telegram_bot._upload_file_ledger_hash(staged, staged.read_bytes()) == declared

    # Legacy file (no ledger row): falls back to hashing the bytes.
    monkeypatch.setattr(telegram_bot.database, "find_photo_hash_by_prefix",
                        lambda chat_id, prefix: None)
    legacy_bytes = staged.read_bytes()
    assert telegram_bot._upload_file_ledger_hash(staged, legacy_bytes) == hashlib.md5(legacy_bytes).hexdigest()


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


def _seed_food_meal(chat_id, date_str, calories, image_hash, description="Meal"):
    database.save_meal(
        chat_id, date_str, "12:00 PM", datetime.now().isoformat(), "test", image_hash, "file",
        {"is_food": True, "meal_description": description, "total_calories": calories},
    )


def test_today_summary_shows_median_typical_day_and_headroom(mock_db, monkeypatch):
    """/today compares against the MEDIAN of the prior 7 user-local days."""
    monkeypatch.setattr(database, "get_android_timezone", lambda *args, **kwargs: "+0800")
    today = database.user_local_now().date()

    _seed_food_meal(12345, today.isoformat(), 500, "hash-today")
    _seed_food_meal(12345, (today - timedelta(days=1)).isoformat(), 1800, "hash-d1")
    _seed_food_meal(12345, (today - timedelta(days=2)).isoformat(), 1200, "hash-d2a")
    _seed_food_meal(12345, (today - timedelta(days=2)).isoformat(), 800, "hash-d2b")
    _seed_food_meal(12345, (today - timedelta(days=3)).isoformat(), 1600, "hash-d3")
    # Non-food and out-of-window rows must not shift the median.
    database.save_meal(12345, (today - timedelta(days=1)).isoformat(), "1:00 PM",
                       datetime.now().isoformat(), "test", "hash-notfood", "file",
                       {"is_food": False, "total_calories": 9999})
    _seed_food_meal(12345, (today - timedelta(days=8)).isoformat(), 5000, "hash-old")

    summary = telegram_bot.format_today_summary(12345)

    # Prior-day totals: 1800, 2000, 1600 -> median 1800; today 500 -> 1300 headroom.
    assert "📊 Typical day: ~1,800 kcal" in summary
    assert "⏳ ~1,300 kcal headroom vs typical" in summary
    assert "above typical" not in summary


def test_today_summary_flags_calories_above_typical(mock_db, monkeypatch):
    monkeypatch.setattr(database, "get_android_timezone", lambda *args, **kwargs: "+0800")
    today = database.user_local_now().date()

    _seed_food_meal(12345, today.isoformat(), 2500, "hash-today")
    _seed_food_meal(12345, (today - timedelta(days=1)).isoformat(), 1800, "hash-d1")
    _seed_food_meal(12345, (today - timedelta(days=2)).isoformat(), 1600, "hash-d2")

    summary = telegram_bot.format_today_summary(12345)

    # Median of 1800 and 1600 is 1700; today 2500 -> 800 above.
    assert "📊 Typical day: ~1,700 kcal" in summary
    assert "📈 ~800 kcal above typical" in summary
    assert "headroom" not in summary


def test_today_summary_hides_typical_day_with_sparse_history(mock_db, monkeypatch):
    """Fewer than 2 prior days with data suppresses the typical-day block."""
    monkeypatch.setattr(database, "get_android_timezone", lambda *args, **kwargs: "+0800")
    today = database.user_local_now().date()

    _seed_food_meal(12345, today.isoformat(), 500, "hash-today")
    _seed_food_meal(12345, (today - timedelta(days=1)).isoformat(), 1800, "hash-d1")

    summary = telegram_bot.format_today_summary(12345)

    assert "500 kcal" in summary
    assert "Typical day" not in summary
    assert "headroom" not in summary
    assert "above typical" not in summary


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


@pytest.mark.parametrize("utc_now,tz_offset,expected_date", [
    ("2026-07-08T16:30:00", "+0800", "2026-07-09"),  # 00:30 next-day local
    ("2026-07-08T16:30:00", "-0500", "2026-07-08"),  # 11:30 same-day local
    ("2026-07-08T18:45:00", "+0530", "2026-07-09"),  # 00:15 next-day (India)
    ("2026-07-08T11:00:00", "+1400", "2026-07-09"),  # 01:00 next-day (Kiritimati)
])
def test_meal_dates_follow_the_travelers_local_calendar_day(
        monkeypatch, tmp_path, utc_now, tz_offset, expected_date):
    """S5 regression: a meal is dated on the user's LOCAL calendar day (from
    the device-reported offset), not the server's UTC day, and is retrievable
    for the daily report targeting that local date."""
    monkeypatch.setattr(telegram_bot.database, "DB_PATH", tmp_path / "tz.db")
    telegram_bot.database.init_db()
    telegram_bot.database.update_android_heartbeat(timezone=tz_offset)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)

    fixed = datetime(*map(int, utc_now.replace("T", "-").replace(":", "-").split("-")))

    class FrozenUTC(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fixed.replace(tzinfo=timezone.utc).astimezone(tz)
            return fixed

    monkeypatch.setattr(telegram_bot.database, "datetime", FrozenUTC)
    monkeypatch.setattr(telegram_bot, "datetime", FrozenUTC)

    analysis = {"is_food": True, "total_calories": 500, "meal_description": "traveler meal"}
    meal_id = telegram_bot.save_meal(12345, analysis, "test", "f", "ab" * 16)

    with telegram_bot.database._connect() as conn:
        stored_date = conn.execute(
            "SELECT date FROM meals WHERE id=?", (meal_id,)).fetchone()[0]
    assert stored_date == expected_date

    # And the report window for that local date retrieves it.
    window = telegram_bot.database.get_meals(12345, expected_date, expected_date)
    assert any(m["analysis"]["meal_description"] == "traveler meal" for m in window)


def test_parse_timezone_offset_bounds_are_sane():
    assert database.parse_timezone_offset("+1400") is not None   # max real east
    assert database.parse_timezone_offset("-1200") is not None   # max real west
    assert database.parse_timezone_offset("+1500") is None       # hours > 14
    assert database.parse_timezone_offset("+0860") is None       # minutes > 59


def _intent_client(payload):
    class FakeModels:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text=json.dumps(payload))
    return SimpleNamespace(models=FakeModels())


@pytest.mark.parametrize("bad_indices", [
    ["x"], [0.5], [None], [True], [[]], [{"a": 1}], ["999999999999"], [999, -1],
])
def test_nl_delete_all_invalid_indices_never_crash_or_stash(mock_db, monkeypatch, tmp_path, bad_indices):
    """S14 security regression: a crafted/hallucinated Gemini delete payload
    whose meal_indices are all non-integer or out-of-range must not crash the
    handler (TypeError) and must stash nothing for confirmation."""
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(database, "get_android_timezone", lambda *a, **k: "+0800")
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    telegram_bot._pending_nl_deletes.clear()

    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_meal(12345, today, "12:00", now_iso, "t", "h1", "f1",
                       {"is_food": True, "meal_description": "Apple", "total_calories": 95})
    database.save_meal(12345, today, "12:01", now_iso, "t", "h2", "f2",
                       {"is_food": True, "meal_description": "Banana", "total_calories": 105})

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "delete", "meal_indices": bad_indices, "reason": "x"}), bot, 12345, "delete")

    assert len(_wide_recent_meals(12345)) == 2         # nothing deleted
    assert 12345 not in telegram_bot._pending_nl_deletes  # nothing stashed


def test_nl_delete_mixed_indices_honors_only_the_valid_ones(mock_db, monkeypatch, tmp_path):
    """A mixed list ([0, "y", 2.0]) coerces to the valid integer indices and
    ignores the junk — without crashing."""
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(database, "get_android_timezone", lambda *a, **k: "+0800")
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    telegram_bot._pending_nl_deletes.clear()

    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    for i in range(3):
        database.save_meal(12345, today, f"12:0{i}", now_iso, "t", f"h{i}", f"f{i}",
                           {"is_food": True, "meal_description": f"m{i}", "total_calories": 100})

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "delete", "meal_indices": [0, "y", 2.0], "reason": "x"}), bot, 12345, "delete")

    pending = telegram_bot._pending_nl_deletes[12345]
    assert len(pending["ids"]) == 2                    # indices 0 and 2, not "y"


@pytest.mark.parametrize("bad_index", ["x", None, 1.5, [0], {"a": 1}, True])
def test_nl_correction_hostile_index_never_crashes(mock_db, monkeypatch, tmp_path, bad_index):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(database, "get_android_timezone", lambda *a, **k: "+0800")
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)

    today = database.user_local_now().date().isoformat()
    database.save_meal(12345, today, "12:00", datetime.now().isoformat(), "t", "h1", "f1",
                       {"is_food": True, "meal_description": "Apple", "total_calories": 95})

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "correction", "meal_index": bad_index,
         "analysis": {"is_food": True, "total_calories": 9}, "reason": "x"}), bot, 12345, "fix")

    # Reported as an invalid index rather than crashing; meal unchanged.
    assert any("Invalid meal index" in m["text"] for m in bot.sent)
    assert _wide_recent_meals(12345)[0]["analysis"]["total_calories"] == 95


def test_coerce_meal_index_contract():
    assert telegram_bot._coerce_meal_index(3) == 3
    assert telegram_bot._coerce_meal_index("3") == 3
    assert telegram_bot._coerce_meal_index(2.0) == 2
    assert telegram_bot._coerce_meal_index(2.5) is None
    assert telegram_bot._coerce_meal_index(True) is None
    for bad in ("x", None, [], {}, "", "1.5"):
        assert telegram_bot._coerce_meal_index(bad) is None


def test_text_handler_injects_relative_date_context(mock_db, monkeypatch, tmp_path):
    """Feature 1: the intent prompt tells Gemini today's/yesterday's local
    date so 'yesterday' resolves reliably, and the window reaches back far
    enough to include yesterday's meals."""
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(database, "get_android_timezone", lambda *a, **k: "+0800")
    monkeypatch.setattr(telegram_bot, "_gemini_quota_pause_summary", lambda: None)

    local_today = database.user_local_now().date()
    yesterday = (local_today - timedelta(days=1)).isoformat()
    # A meal logged yesterday must be visible to the correction prompt.
    database.save_meal(12345, yesterday, "08:00 AM", datetime.now().isoformat(),
                       "t", "yh", "yf",
                       {"is_food": True, "meal_description": "Yesterday breakfast",
                        "total_calories": 400, "food_items": []})

    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured["prompt"] = kwargs["contents"][0]
            return SimpleNamespace(text=json.dumps({"intent": "chat", "reply": "ok"}))

    bot = FakeBot()
    telegram_bot.handle_text_message(SimpleNamespace(models=FakeModels()), bot, 12345,
                                     "what did I eat yesterday?")

    prompt = captured["prompt"]
    assert f"today is {local_today.isoformat()}" in prompt
    assert f"yesterday was {yesterday}" in prompt
    assert local_today.strftime("%A") in prompt
    assert "Yesterday breakfast" in prompt          # yesterday's meal is in-window
    assert f"Date: {yesterday}" in prompt


# ─── Fitness & diet command surface ────────────────────────────────
CHAT = 12345


def _stable_tz(monkeypatch):
    monkeypatch.setattr(database, "get_android_timezone", lambda *a, **k: "+0800")


class _RaisingClient:
    """A Gemini client that fails the test if it is ever called."""

    class _Models:
        def generate_content(self, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("Gemini was called during a deterministic path")

    def __init__(self):
        self.models = self._Models()


# ── /weight ──
def test_weight_logs_bare_number_and_reads_back(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_weight(bot, CHAT, ["72.5"])

    latest = database.get_latest_body_weight(CHAT)
    assert latest is not None
    assert latest["weight_kg"] == 72.5
    assert latest["source"] == "telegram"
    assert any("72.5 kg" in m["text"] for m in bot.sent)


def test_weight_accepts_pounds(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_weight(bot, CHAT, ["159", "lb"])
    latest = database.get_latest_body_weight(CHAT)
    assert latest is not None
    assert 72.0 <= latest["weight_kg"] <= 72.3   # 159 lb ≈ 72.1 kg


@pytest.mark.parametrize("bad", [["10"], ["500"], ["notaweight"]])
def test_weight_out_of_bounds_is_rejected_and_saves_nothing(mock_db, monkeypatch, bad):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_weight(bot, CHAT, bad)
    assert database.get_latest_body_weight(CHAT) is None
    assert any("couldn't read a weight" in m["text"] for m in bot.sent)


def test_weight_status_shows_seven_day_trend(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    today = database.user_local_now().date()
    database.save_body_weight(CHAT, (today - timedelta(days=3)).isoformat(), 73.0, source="telegram")
    database.save_body_weight(CHAT, today.isoformat(), 72.0, source="telegram")

    status = telegram_bot.format_weight_status(CHAT)
    assert "72 kg" in status
    assert "7-day trend" in status
    assert "▼ -1 kg" in status


def test_weight_status_warns_when_stale(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    today = database.user_local_now().date()
    database.save_body_weight(CHAT, (today - timedelta(days=30)).isoformat(), 70.0, source="telegram")
    status = telegram_bot.format_weight_status(CHAT)
    assert "days old" in status


# ── /diet ──
def test_diet_sets_mode_and_status_shows_targets_and_disclaimer(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_diet(bot, CHAT, ["high_protein"])
    assert database.get_fitness_profile(CHAT)["diet_mode"] == "high_protein"

    database.save_body_weight(CHAT, database.user_local_now().date().isoformat(), 70.0)
    status = telegram_bot.format_diet_status(CHAT)
    assert "High-protein" in status
    assert nutrition_disclaimer() in status
    # 2.0 g/kg * 70 kg = 140 g protein target
    assert "P 140g" in status


def test_diet_target_and_protein_subcommands_persist(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_diet(bot, CHAT, ["target", "2000", "150", "200", "60"])
    profile = database.get_fitness_profile(CHAT)
    assert profile["target_calories"] == 2000
    assert profile["target_protein_g"] == 150
    assert profile["target_carbs_g"] == 200
    assert profile["target_fat_g"] == 60

    telegram_bot._cmd_diet(bot, CHAT, ["protein", "2.0"])
    assert database.get_fitness_profile(CHAT)["protein_g_per_kg"] == 2.0


# ── /macros ──
def test_macros_with_diet_set_produces_report(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_fitness_profile(CHAT, diet_mode="high_protein")
    database.save_body_weight(CHAT, today, 70.0)
    database.save_meal(CHAT, today, "12:00", now_iso, "test", "mh1", "f",
                       {"is_food": True, "meal_description": "Chicken bowl",
                        "total_calories": 600, "total_protein_g": 55,
                        "total_carbs_g": 40, "total_fat_g": 20})

    bot = FakeBot()
    telegram_bot._cmd_macros(bot, CHAT, ["today"])
    text = bot.sent[-1]["text"]
    assert "Macro check" in text
    assert "High-protein" in text
    assert "Protein" in text
    assert "Window: today" in text


def test_macros_no_meals_nudges(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_macros(bot, CHAT, ["today"])
    text = bot.sent[-1]["text"]
    assert "No meals logged" in text
    assert "No diet mode set" in text


def _save_daily_meals(days_ago_to_analysis):
    """One meal per (days-ago, analysis) pair, on the user-local calendar."""
    today = database.user_local_now().date()
    now_iso = datetime.now().isoformat()
    for days_ago, analysis in days_ago_to_analysis:
        day = (today - timedelta(days=days_ago)).isoformat()
        database.save_meal(CHAT, day, "12:00", now_iso, "t",
                           f"avg-{days_ago}-{analysis.get('total_carbs_g')}", "f",
                           {"is_food": True, "meal_description": "Meal", **analysis})


def test_macros_week_judges_daily_average_not_summed_week(mock_db, monkeypatch):
    """A perfectly adherent keto week (7 days x 40g carbs) must read as ok —
    summing the week against the single-day 50g cap misread it as ~280g over."""
    _stable_tz(monkeypatch)
    database.save_fitness_profile(CHAT, diet_mode="keto")
    _save_daily_meals([(d, {"total_calories": 1800, "total_protein_g": 110,
                            "total_carbs_g": 40, "total_fat_g": 140})
                       for d in range(7)])

    bot = FakeBot()
    telegram_bot._cmd_macros(bot, CHAT, ["week"])
    text = bot.sent[-1]["text"]
    assert "Daily average over last 7 days (7 logged)" in text
    assert "Carbs: 40g / 50g" in text     # the average day, not the 280g sum
    assert "over the 50g cap" not in text


def test_macros_week_still_flags_a_genuinely_over_week(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    database.save_fitness_profile(CHAT, diet_mode="keto")
    _save_daily_meals([(d, {"total_calories": 2200, "total_protein_g": 100,
                            "total_carbs_g": 120, "total_fat_g": 150})
                       for d in range(7)])

    bot = FakeBot()
    telegram_bot._cmd_macros(bot, CHAT, ["week"])
    text = bot.sent[-1]["text"]
    assert "Daily average over last 7 days (7 logged)" in text
    assert "over the 50g cap" in text     # 120g/day average is truly over


def test_macros_partial_week_averages_over_logged_days_only(mock_db, monkeypatch):
    """3 logged days in a 7-day window average over 3, not 7 — dividing by
    the window length would understate every day the user actually logged."""
    _stable_tz(monkeypatch)
    _save_daily_meals([(d, {"total_calories": 600, "total_protein_g": 30,
                            "total_carbs_g": 60, "total_fat_g": 20})
                       for d in (0, 2, 4)])

    bot = FakeBot()
    telegram_bot._cmd_macros(bot, CHAT, ["7"])
    text = bot.sent[-1]["text"]
    assert "Daily average over last 7 days (3 logged)" in text
    assert "Calories: <b>600</b>" in text  # 1800/3, not 1800/7 (~257) or 1800


# ── /workout & /train ──
def test_workout_splits_groups_from_notes(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_workout(bot, CHAT, ["legs", "chest", "felt", "strong"])
    today = database.user_local_now().date().isoformat()
    rows = database.get_workouts(CHAT, today, today)
    assert len(rows) == 1
    assert rows[0]["muscle_groups"] == "legs chest"
    assert rows[0]["notes"] == "felt strong"
    assert rows[0]["source"] == "telegram"


def test_train_recommends_longest_untrained(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    today = database.user_local_now().date()
    database.save_workout(CHAT, today.isoformat(), muscle_groups="legs", source="telegram")
    database.save_workout(CHAT, (today - timedelta(days=5)).isoformat(),
                          muscle_groups="chest", source="telegram")

    text = telegram_bot.format_train_recommendation(CHAT)
    assert "Legs: today" in text
    assert "Chest: 5d ago" in text
    assert "not in last 14d" in text
    # shoulders is the first never-trained group in tracking order
    assert "Due next: <b>Shoulders</b>" in text


# ── /activity ──
def test_activity_positional_saves_kcal_steps_km(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_activity(bot, CHAT, ["450", "8000", "5"])
    today = database.user_local_now().date().isoformat()
    rows = database.get_activities(CHAT, today, today)
    assert len(rows) == 1
    assert rows[0]["active_calories"] == 450
    assert rows[0]["distance_km"] == 5
    assert rows[0]["raw"]["steps"] == 8000


def test_activity_clamps_out_of_range_kcal(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_activity(bot, CHAT, ["999999"])
    today = database.user_local_now().date().isoformat()
    rows = database.get_activities(CHAT, today, today)
    assert rows[0]["active_calories"] == telegram_bot.ACTIVITY_KCAL_MAX


def test_activity_date_form(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_activity(bot, CHAT, ["2026-07-10", "300"])
    rows = database.get_activities(CHAT, "2026-07-10", "2026-07-10")
    assert len(rows) == 1
    assert rows[0]["active_calories"] == 300


def test_activity_no_arg_shows_net(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_meal(CHAT, today, "12:00", now_iso, "test", "ah1", "f",
                       {"is_food": True, "meal_description": "Lunch", "total_calories": 600})
    database.save_activity(CHAT, today, source="manual", active_calories=450)

    bot = FakeBot()
    telegram_bot._cmd_activity(bot, CHAT, [])
    text = bot.sent[-1]["text"]
    assert "Net:" in text
    assert "150 kcal" in text          # 600 eaten − 450 burned


# ── /train_run ──
def test_train_run_5k_race_echoes_vdot_around_50(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_train_run(bot, CHAT, ["5k", "19:57"])

    profile = database.get_fitness_profile(CHAT)
    assert 49.0 <= profile["vdot"] <= 51.0
    assert profile["race_time_seconds"] == 1197
    assert profile["race_label"] == "5K"
    reply = bot.sent[-1]["text"]
    assert "VDOT" in reply
    assert "50" in reply


def test_train_run_vdot_and_race_subcommands(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_train_run(bot, CHAT, ["vdot", "50"])
    assert database.get_fitness_profile(CHAT)["vdot"] == 50.0

    # Dynamic future date: hardcoding one would start getting rejected as
    # "in the past" once the calendar passes it.
    future = (database.user_local_today() + timedelta(days=60)).isoformat()
    telegram_bot._cmd_train_run(bot, CHAT, ["race", future])
    assert database.get_fitness_profile(CHAT)["goal_race_date"] == future


def test_train_run_race_rejects_past_date_and_stores_nothing(mock_db, monkeypatch):
    """A past goal race pins the plan to base phase forever with zero
    feedback — it must bounce with a clear reply instead of storing."""
    _stable_tz(monkeypatch)
    bot = FakeBot()
    past = (database.user_local_today() - timedelta(days=1)).isoformat()
    telegram_bot._cmd_train_run(bot, CHAT, ["race", past])

    reply = bot.sent[-1]["text"]
    assert "in the past" in reply
    profile = database.get_fitness_profile(CHAT)
    assert not (profile or {}).get("goal_race_date")


def test_train_run_race_accepts_today(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    today = database.user_local_today().isoformat()
    telegram_bot._cmd_train_run(bot, CHAT, ["race", today])
    assert database.get_fitness_profile(CHAT)["goal_race_date"] == today
    # Race day itself is not "passed": no stale notice anywhere.
    assert "has passed" not in telegram_bot.format_todays_run(CHAT)
    assert "has passed" not in telegram_bot.format_week_plan(CHAT)


def test_stale_stored_race_date_notice_in_run_and_plan(mock_db, monkeypatch):
    """A goal_race_date stored before past dates were rejected must surface a
    one-line notice instead of silently showing base phase forever."""
    _stable_tz(monkeypatch)
    past = (database.user_local_today() - timedelta(days=10)).isoformat()
    database.save_fitness_profile(CHAT, vdot=50.0, goal_race_date=past)

    run_text = telegram_bot.format_todays_run(CHAT)
    assert f"Race date {past} has passed" in run_text
    assert "/train_run race" in run_text

    plan_text = telegram_bot.format_week_plan(CHAT)
    assert f"Race date {past} has passed" in plan_text


def test_train_run_no_arg_uses_default_plan(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_train_run(bot, CHAT, [])
    text = bot.sent[-1]["text"]
    assert "Today's run" in text
    assert "VDOT 45" in text           # default profile VDOT


# ── /plan & /profile ──
def test_plan_marks_today_and_lists_paces(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    text = telegram_bot.format_week_plan(CHAT)
    today_abbr = database.user_local_now().date().strftime("%A")[:3]
    assert "This week" in text
    assert "👉" in text
    assert today_abbr in text
    assert "Paces/km" in text
    assert "VDOT 45" in text


def test_profile_pretty_print_and_empty(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    assert "No profile yet" in telegram_bot.format_fitness_profile(CHAT)

    database.save_fitness_profile(CHAT, diet_mode="keto", vdot=50.0)
    database.save_body_weight(CHAT, database.user_local_now().date().isoformat(), 70.0)
    text = telegram_bot.format_fitness_profile(CHAT)
    assert "Fitness profile" in text
    assert "Keto" in text
    assert "VDOT: 50" in text
    assert "70 kg" in text


# ── format_today_summary net line ──
def test_today_summary_shows_net_when_activity_present(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_meal(CHAT, today, "12:00", now_iso, "test", "th1", "f",
                       {"is_food": True, "meal_description": "Lunch", "total_calories": 600})
    database.save_activity(CHAT, today, source="manual", active_calories=450)

    summary = telegram_bot.format_today_summary(CHAT)
    assert "Burned: 450 kcal" in summary
    assert "Net:" in summary


def test_today_summary_omits_net_without_activity(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_meal(CHAT, today, "12:00", now_iso, "test", "th2", "f",
                       {"is_food": True, "meal_description": "Lunch", "total_calories": 600})
    summary = telegram_bot.format_today_summary(CHAT)
    assert "Net:" not in summary
    assert "Burned:" not in summary


def test_today_summary_shows_distance_only_activity_without_net(mock_db, monkeypatch):
    """'ran 9.2 km' with no kcal must not vanish from /today — it gets an
    activity line, while the net line still requires an actual burn."""
    _stable_tz(monkeypatch)
    today = database.user_local_now().date().isoformat()
    database.save_meal(CHAT, today, "12:00", datetime.now().isoformat(), "test", "th3", "f",
                       {"is_food": True, "meal_description": "Lunch", "total_calories": 600})
    database.save_activity(CHAT, today, source="manual", activity_type="run",
                           distance_km=9.2, raw={"steps": 8000})

    summary = telegram_bot.format_today_summary(CHAT)
    assert "🏃 Activity: 9.2 km · 8,000 steps" in summary
    assert "Net:" not in summary
    assert "Burned:" not in summary


def test_activity_status_distance_only_shows_without_net(mock_db, monkeypatch):
    """/activity with a distance-only session: totals show, but no 'Burned:
    0 kcal' and no net line implying the run burned nothing."""
    _stable_tz(monkeypatch)
    today = database.user_local_now().date().isoformat()
    database.save_meal(CHAT, today, "12:00", datetime.now().isoformat(), "test", "th4", "f",
                       {"is_food": True, "meal_description": "Lunch", "total_calories": 600})
    database.save_activity(CHAT, today, source="manual", activity_type="run",
                           distance_km=9.2, raw={"steps": 8000})

    bot = FakeBot()
    telegram_bot._cmd_activity(bot, CHAT, [])
    text = bot.sent[-1]["text"]
    assert "Distance: 9.2 km" in text
    assert "Steps: 8,000" in text
    assert "Net:" not in text
    assert "Burned:" not in text


# ── NL write-intents ──
def test_nl_log_weight_saves(mock_db, monkeypatch, tmp_path):
    _stable_tz(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_gemini_quota_pause_summary", lambda: None)
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    bot = FakeBot()
    client = _intent_client({"intent": "log_weight", "weight_kg": 72.5,
                             "reply": "ok"})
    telegram_bot.handle_text_message(client, bot, CHAT, "I weigh 72.5 kg this morning")

    latest = database.get_latest_body_weight(CHAT)
    assert latest is not None and latest["weight_kg"] == 72.5


def test_nl_log_activity_saves(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_gemini_quota_pause_summary", lambda: None)
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    bot = FakeBot()
    client = _intent_client({"intent": "log_activity", "active_calories": 450,
                             "steps": 0, "distance_km": 5, "reply": "ok"})
    telegram_bot.handle_text_message(client, bot, CHAT, "burned 450 kcal running 5 km")

    today = database.user_local_now().date().isoformat()
    rows = database.get_activities(CHAT, today, today)
    assert len(rows) == 1
    assert rows[0]["active_calories"] == 450
    assert rows[0]["distance_km"] == 5


# ── Deterministic zero-Gemini fast path ──
def test_run_query_answered_without_gemini(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    # _RaisingClient explodes if the Gemini path is reached.
    telegram_bot.handle_text_message(_RaisingClient(), bot, CHAT, "what should I run today?")
    assert any("Today's run" in m["text"] for m in bot.sent)


def test_macro_query_answered_without_gemini(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot.handle_text_message(_RaisingClient(), bot, CHAT, "how's my protein?")
    assert any("No meals logged" in m["text"] or "Macro check" in m["text"] for m in bot.sent)


def test_plan_query_answered_without_gemini(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot.handle_text_message(_RaisingClient(), bot, CHAT, "what's this week's plan?")
    assert any("This week" in m["text"] for m in bot.sent)


# ── Classifier-regression: the prompt still drives the original intents ──
def test_text_handler_prompt_preserves_intents_and_adds_fitness():
    prompt = config.TEXT_HANDLER_PROMPT
    for intent in ('"new_meal"', '"correction"', '"delete"', '"chat"'):
        assert intent in prompt
    # The critical disambiguation rule for meals is intact.
    assert 'meals_list is empty, it MUST be a "new_meal"' in prompt
    # Exactly the two new write-intents were added, with their fields.
    assert '"log_weight"' in prompt
    assert '"log_activity"' in prompt
    for field in ("weight_kg", "active_calories", "steps", "distance_km"):
        assert field in prompt
    # Food is never reclassified as a fitness log.
    assert 'never "log_weight"' in prompt


def test_new_meal_correction_delete_still_route_after_prompt_change(mock_db, monkeypatch):
    """Handler routing regression: the existing intents still act correctly."""
    _stable_tz(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_gemini_quota_pause_summary", lambda: None)
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)

    # new_meal saves a meal
    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client({
        "intent": "new_meal",
        "analysis": {"is_food": True, "meal_description": "Toast", "total_calories": 200},
        "reply": "ok",
    }), bot, CHAT, "I had toast")
    assert len(_wide_recent_meals(CHAT)) == 1

    # correction updates it
    target = _wide_recent_meals(CHAT)[0]
    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client({
        "intent": "correction", "meal_index": 0,
        "analysis": {"is_food": True, "meal_description": "Toast", "total_calories": 350},
        "reason": "bigger",
    }), bot, CHAT, "make it 350")
    assert _wide_recent_meals(CHAT)[0]["analysis"]["total_calories"] == 350
    assert target["id"] == _wide_recent_meals(CHAT)[0]["id"]

    # delete stashes a pending confirmation
    telegram_bot._pending_nl_deletes.clear()
    bot = FakeBot()
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", CHAT)
    telegram_bot.handle_text_message(_delete_intent_client([0]), bot, CHAT, "delete it")
    assert CHAT in telegram_bot._pending_nl_deletes


def nutrition_disclaimer():
    import nutrition
    return nutrition.DISCLAIMER


def test_fitness_prefetch_does_not_hijack_activity_logs(mock_db, monkeypatch):
    """An activity/meal sentence that merely mentions running must NOT be
    intercepted by the deterministic read-only fast path."""
    _stable_tz(monkeypatch)
    bot = FakeBot()
    assert telegram_bot._maybe_answer_fitness_query(
        bot, CHAT, "I went for a run today and burned 400 cal") is False
    assert telegram_bot._maybe_answer_fitness_query(bot, CHAT, "I ate a chicken salad") is False
    # But the genuine read queries are still caught.
    assert telegram_bot._maybe_answer_fitness_query(bot, CHAT, "what should I run today?") is True
    assert telegram_bot._maybe_answer_fitness_query(bot, CHAT, "how's my protein?") is True


# ═══ Deep-review additions (append-only) ═══════════════════════════
# Command-arg fuzz, HTML injection, quota-pause resilience, hostile NL
# payloads, intent interplay, edit-window boundaries and /today net math.

def _no_pause(monkeypatch):
    monkeypatch.setattr(telegram_bot, "_gemini_quota_pause_summary", lambda: None)


def _counting_client(payload):
    """A Gemini stub that records every generate_content call it receives."""
    calls = []

    class _Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text=json.dumps(payload))

    return SimpleNamespace(models=_Models()), calls


# ── /weight arg fuzz ────────────────────────────────────────────────
@pytest.mark.parametrize("args", [["-5"], ["1e400"], ["NaN"], ["72,5"], ["abc", "kg"]])
def test_weight_junk_args_rejected_and_db_untouched(mock_db, monkeypatch, args):
    """Negative, overflow, NaN, and comma-decimal junk must all reply with the
    usage hint and never write a body_weight row (a phantom weigh-in would
    poison the 7-day trend Robert reads every morning)."""
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_weight(bot, CHAT, args)
    assert database.get_latest_body_weight(CHAT) is None
    assert any("couldn't read a weight" in m["text"] for m in bot.sent)


def test_weight_fullwidth_unicode_digits_are_understood(mock_db, monkeypatch):
    # A Chinese-locale keyboard emits fullwidth digits; they parse to the
    # same number instead of bouncing with a confusing error.
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_weight(bot, CHAT, ["７２.５"])
    latest = database.get_latest_body_weight(CHAT)
    assert latest is not None
    assert latest["weight_kg"] == 72.5


def test_weight_scientific_notation_junk_is_not_misread_as_weight(mock_db, monkeypatch):
    # Regression: _WEIGHT_UNIT_RE used to extract the exponent digits of
    # scientific notation ('1e72' -> 72 kg); fixed with a (?<![\w.]) lookbehind.
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_weight(bot, CHAT, ["1e72"])
    assert database.get_latest_body_weight(CHAT) is None


def test_weight_no_args_shows_status_without_writing(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_weight(bot, CHAT, [])
    assert "No weigh-ins logged yet" in bot.sent[-1]["text"]
    assert database.get_latest_body_weight(CHAT) is None


# ── /train_run arg fuzz ─────────────────────────────────────────────
@pytest.mark.parametrize("args", [
    ["19:99"],              # bare time with no distance
    ["abc"],
    ["5k"],                 # distance alone
    ["5k", "0:00"],         # zero time would divide-by-zero in VDOT math
    ["5k", "abc"],
    ["5k", "-19:30"],       # negative total seconds
    ["0k", "19:00"],        # zero distance
    ["-5k", "19:00"],
    ["vdot"],
    ["vdot", "999"],        # far outside the 20..85 physical range
    ["vdot", "abc"],
    ["race", "not-a-date"],
    ["race", "2026-13-45"], # ISO-shaped but impossible calendar date
])
def test_train_run_junk_args_reply_helpfully_and_write_nothing(mock_db, monkeypatch, args):
    """Every malformed /train_run form must answer with usage help and leave
    the fitness profile unwritten — a garbage VDOT or race would corrupt every
    subsequent pace in /plan."""
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_train_run(bot, CHAT, args)
    assert database.get_fitness_profile(CHAT) is None
    assert bot.sent  # always answers, never goes silent


def test_train_run_marathon_sub3_computes_daniels_vdot(mock_db, monkeypatch):
    """Robert's actual use case: a 2:59:59 marathon stores the exact seconds,
    the Marathon label, and a Daniels VDOT in the low-to-mid 50s."""
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_train_run(bot, CHAT, ["marathon", "2:59:59"])
    profile = database.get_fitness_profile(CHAT)
    assert profile["race_label"] == "Marathon"
    assert profile["race_time_seconds"] == 10799
    assert abs(profile["race_distance_km"] - 42.195) < 0.001
    assert 52.0 <= profile["vdot"] <= 56.0
    reply = bot.sent[-1]["text"]
    assert "VDOT" in reply
    assert "Marathon" in reply


def test_train_run_rejects_impossible_seconds_field(mock_db, monkeypatch):
    # 19:99 is never a real race time; treating it as 1239 s trains the user
    # at paces derived from a race they did not run.
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_train_run(bot, CHAT, ["5k", "19:99"])
    assert database.get_fitness_profile(CHAT) is None
    assert bot.sent  # replies with usage help, never goes silent
    assert "/train_run" in bot.sent[-1]["text"]


# ── /activity arg fuzz ──────────────────────────────────────────────
@pytest.mark.parametrize("args", [
    ["-500"], ["1e400"], ["NaN"], ["-1", "-2", "-3"], ["99999999999999999999"],
])
def test_activity_junk_args_never_store_negative_or_infinite_values(mock_db, monkeypatch, args):
    """Negative/overflow/NaN activity args must clamp to nothing: no negative
    or infinite value may reach the DB, or the /today net line would inflate
    the user's remaining calories."""
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_activity(bot, CHAT, args)
    rows = database.get_activities(CHAT, "1970-01-01", "9999-12-31")
    for row in rows:
        assert row["active_calories"] in (None, 0, 0.0)
        assert row["distance_km"] in (None, 0, 0.0)
    assert telegram_bot._sum_active_calories(rows) == 0
    assert bot.sent


def test_activity_invalid_calendar_date_saves_nothing(mock_db, monkeypatch):
    # '2026-13-45' passes the ISO shape regex but not the calendar; the row
    # must be rejected, not saved under a nonsense date no query can reach.
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_activity(bot, CHAT, ["2026-13-45", "500"])
    assert database.get_activities(CHAT, "1970-01-01", "9999-12-31") == []
    assert any("isn't valid" in m["text"] for m in bot.sent)


def test_activity_date_form_clamps_negative_kcal_to_zero(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_activity(bot, CHAT, ["2026-07-09", "-300"])
    rows = database.get_activities(CHAT, "2026-07-09", "2026-07-09")
    assert len(rows) == 1
    assert rows[0]["active_calories"] == 0  # clamped, never negative


# ── /macros selector fuzz ───────────────────────────────────────────
# Multi-day windows now report the per-day average, so their header is the
# "Daily average over …" form rather than "Window: …" (pin moved for the
# multi-day averaging change).
@pytest.mark.parametrize("selector,header", [
    ("0", "Window: today"),      # zero-day window clamps up to today
    ("-3", "Window: today"),     # negative clamps up to today
    ("9999", "Daily average over last 31 days (1 logged)"),  # oversize clamps to the 31-day cap
    ("nan", "Window: today"),    # unparsable falls back to today
])
def test_macros_selector_junk_clamps_to_sane_window(mock_db, monkeypatch, selector, header):
    _stable_tz(monkeypatch)
    today = database.user_local_now().date().isoformat()
    database.save_meal(CHAT, today, "12:00", datetime.now().isoformat(), "t",
                       f"mh-{selector}", "f",
                       {"is_food": True, "meal_description": "Bowl", "total_calories": 500,
                        "total_protein_g": 30, "total_carbs_g": 40, "total_fat_g": 20})
    bot = FakeBot()
    telegram_bot._cmd_macros(bot, CHAT, [selector])
    text = bot.sent[-1]["text"]
    assert header in text
    assert "Macro check" in text


def test_explicit_diet_gram_targets_override_mode_split_in_macros(mock_db, monkeypatch):
    """'/diet target 2000 150 100 80' sets explicit gram targets; /macros must
    judge intake against THOSE numbers, not carbs/fat re-derived from the
    high-protein %-split (which would claim 200g C / 67g F and contradict the
    plan the user typed in)."""
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_diet(bot, CHAT, ["high_protein"])
    telegram_bot._cmd_diet(bot, CHAT, ["target", "2000", "150", "100", "80"])

    today = database.user_local_now().date().isoformat()
    database.save_meal(CHAT, today, "12:00", datetime.now().isoformat(), "t",
                       "target-h", "f",
                       {"is_food": True, "meal_description": "Big bowl",
                        "total_calories": 1200, "total_protein_g": 90,
                        "total_carbs_g": 120, "total_fat_g": 40})

    bot = FakeBot()
    telegram_bot._cmd_macros(bot, CHAT, ["today"])
    text = bot.sent[-1]["text"]
    assert "Protein: 90g / 150g" in text
    assert "Carbs: 120g / 100g ⬆️" in text  # over the user's 100g, not under the split's 200g
    assert "Fat: 40g / 80g" in text


# ── HTML injection end-to-end ───────────────────────────────────────
def test_workout_reply_escapes_html_in_notes_and_groups(mock_db, monkeypatch):
    """Workout notes and unrecognized group tokens are user-controlled text
    rendered in an HTML parse_mode message — raw tags must never survive."""
    _stable_tz(monkeypatch)
    bot = FakeBot()
    telegram_bot._cmd_workout(bot, CHAT, ["legs", "<b>onclick</b>"])
    reply = bot.sent[-1]["text"]
    assert "<b>onclick</b>" not in reply
    assert "&lt;b&gt;onclick&lt;/b&gt;" in reply

    # An unrecognized first token makes the WHOLE arg string the group name.
    bot2 = FakeBot()
    telegram_bot._cmd_workout(bot2, CHAT, ["<script>alert(1)</script>", "day"])
    reply2 = bot2.sent[-1]["text"]
    assert "<script>" not in reply2
    assert "&lt;script&gt;" in reply2


def test_profile_and_plan_escape_hostile_stored_race_fields(mock_db, monkeypatch):
    """Stored-then-displayed: race_label/goal_race_date written by any future
    path (Garmin sync, DB edit) must come back escaped from /profile, and a
    junk race date must be dropped (not echoed) by /plan."""
    _stable_tz(monkeypatch)
    database.save_fitness_profile(
        CHAT,
        vdot=50.0,
        race_label="<b onmouseover=steal()>5K</b>",
        race_time_seconds=1197,
        goal_race_date="<script>alert(1)</script>",
    )
    profile_text = telegram_bot.format_fitness_profile(CHAT)
    assert "<b onmouseover" not in profile_text
    assert "&lt;b onmouseover=steal()&gt;5K&lt;/b&gt;" in profile_text
    assert "<script>" not in profile_text

    plan_text = telegram_bot.format_week_plan(CHAT)
    assert "<script>" not in plan_text
    assert "This week" in plan_text  # plan still renders despite the junk date


def test_correction_reply_escapes_hostile_meal_and_gemini_text(mock_db, monkeypatch):
    """A hostile stored description AND hostile Gemini-authored fields
    (new description, reason) all pass through the correction reply — every
    one must be escaped."""
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    today = database.user_local_now().date().isoformat()
    database.save_meal(CHAT, today, "12:00", datetime.now().isoformat(), "t", "hx", "f",
                       {"is_food": True, "meal_description": "<script>owned</script>",
                        "total_calories": 500})
    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client({
        "intent": "correction", "meal_index": 0,
        "analysis": {"is_food": True, "meal_description": "<b>Fixed</b>", "total_calories": 450},
        "reason": "<img src=x onerror=alert(1)>",
    }), bot, CHAT, "fix that meal")
    reply = bot.sent[-1]["text"]
    assert "<script>" not in reply
    assert "<img" not in reply
    assert "&lt;script&gt;owned&lt;/script&gt;" in reply
    assert "&lt;b&gt;Fixed&lt;/b&gt;" in reply
    assert "&lt;img src=x onerror=alert(1)&gt;" in reply


# ── Quota-pause resilience: all nine fitness commands are pure DB ──
_FITNESS_COMMAND_CALLS = [
    ("/weight", lambda bot: telegram_bot._cmd_weight(bot, CHAT, [])),
    ("/diet", lambda bot: telegram_bot._cmd_diet(bot, CHAT, [])),
    ("/macros", lambda bot: telegram_bot._cmd_macros(bot, CHAT, ["today"])),
    ("/workout", lambda bot: telegram_bot._cmd_workout(bot, CHAT, ["legs"])),
    ("/train", lambda bot: bot.send_message(CHAT, telegram_bot.format_train_recommendation(CHAT))),
    ("/activity", lambda bot: telegram_bot._cmd_activity(bot, CHAT, [])),
    ("/train_run", lambda bot: telegram_bot._cmd_train_run(bot, CHAT, [])),
    ("/plan", lambda bot: bot.send_message(CHAT, telegram_bot.format_week_plan(CHAT))),
    ("/profile", lambda bot: bot.send_message(CHAT, telegram_bot.format_fitness_profile(CHAT))),
]


@pytest.mark.parametrize("name,invoke", _FITNESS_COMMAND_CALLS,
                         ids=[name for name, _ in _FITNESS_COMMAND_CALLS])
def test_fitness_commands_answer_during_gemini_quota_pause(mock_db, monkeypatch, name, invoke):
    """During a Gemini quota pause the fitness/diet surface must stay fully
    usable — these commands are pure DB + arithmetic and must never bounce
    with the 'Gemini is paused' message."""
    _stable_tz(monkeypatch)
    monkeypatch.setattr(
        telegram_bot, "_gemini_quota_pause",
        lambda: {"until": datetime.now() + timedelta(hours=6),
                 "reason": "daily quota", "set_at": ""},
    )
    monkeypatch.setattr(telegram_bot, "_gemini_quota_pause_summary",
                        lambda: "Paused until tomorrow (daily quota).")
    bot = FakeBot()
    invoke(bot)
    assert bot.sent, f"{name} sent no reply during a quota pause"
    assert all("Gemini is paused" not in m["text"] for m in bot.sent)


# ── NL hostile Gemini payloads for the two new write-intents ───────
@pytest.mark.parametrize("bad_field", ["72.5", [72.5], -50, float("inf"), None])
def test_nl_log_weight_hostile_field_saves_nothing(mock_db, monkeypatch, bad_field):
    """A hallucinated weight_kg (string, list, negative, inf, null) must save
    nothing and still answer — string numerics are deliberately NOT trusted."""
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    bot = FakeBot()
    client = _intent_client({"intent": "log_weight", "weight_kg": bad_field, "reply": "ok"})
    telegram_bot.handle_text_message(client, bot, CHAT, "please log my weight")
    assert database.get_latest_body_weight(CHAT) is None
    assert any("couldn't read a valid body weight" in m["text"] for m in bot.sent)


def test_nl_log_weight_numeric_field_fallback_saves(mock_db, monkeypatch):
    # When the sentence has no parsable number, a bounded numeric weight_kg
    # from the model is the fallback that actually saves.
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    bot = FakeBot()
    client = _intent_client({"intent": "log_weight", "weight_kg": 72.5, "reply": "ok"})
    telegram_bot.handle_text_message(client, bot, CHAT, "log my usual morning weight")
    latest = database.get_latest_body_weight(CHAT)
    assert latest is not None
    assert latest["weight_kg"] == 72.5


@pytest.mark.parametrize("payload", [
    {"active_calories": float("inf"), "steps": "many", "distance_km": -3},
    {"active_calories": float("nan"), "steps": -5, "distance_km": "far"},
    {"active_calories": None, "steps": None, "distance_km": None},
    {"active_calories": [450], "steps": {"n": 1}, "distance_km": False},
])
def test_nl_log_activity_hostile_payload_saves_nothing(mock_db, monkeypatch, payload):
    """inf/NaN/negative/typed-junk activity fields coerce to zero; with no
    usable number the handler must refuse the save and say so."""
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    bot = FakeBot()
    client = _intent_client({"intent": "log_activity", "reply": "ok", **payload})
    telegram_bot.handle_text_message(client, bot, CHAT, "log my workout from earlier")
    assert database.get_activities(CHAT, "1970-01-01", "9999-12-31") == []
    assert any("couldn't find any activity numbers" in m["text"] for m in bot.sent)


def test_nl_log_activity_keeps_valid_fields_and_drops_junk(mock_db, monkeypatch):
    # Partial junk must not poison the valid part: kcal saves, the negative
    # distance and non-numeric steps are dropped rather than stored.
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    bot = FakeBot()
    client = _intent_client({"intent": "log_activity", "active_calories": 450,
                             "steps": "many", "distance_km": -3, "reply": "ok"})
    telegram_bot.handle_text_message(client, bot, CHAT, "gym session done")
    today = database.user_local_now().date().isoformat()
    rows = database.get_activities(CHAT, today, today)
    assert len(rows) == 1
    assert rows[0]["active_calories"] == 450
    assert rows[0]["distance_km"] is None
    assert rows[0]["raw"] is None
    reply = bot.sent[-1]["text"]
    assert "450 kcal" in reply
    assert "km" not in reply


# ── Intent interplay: fast-path vs Gemini routing ──────────────────
def test_mixed_activity_and_meal_sentence_goes_to_gemini_once(mock_db, monkeypatch):
    """'ran 10k ... and had a protein shake after' mentions running AND food:
    the deterministic regex pre-check must NOT hijack it — exactly one Gemini
    classification happens and its intent is executed."""
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    client, calls = _counting_client({"intent": "log_activity", "active_calories": 400,
                                      "steps": 0, "distance_km": 10, "reply": "ok"})
    bot = FakeBot()
    telegram_bot.handle_text_message(
        client, bot, CHAT, "ran 10k this morning and had a protein shake after")
    assert len(calls) == 1
    today = database.user_local_now().date().isoformat()
    rows = database.get_activities(CHAT, today, today)
    assert len(rows) == 1
    assert rows[0]["distance_km"] == 10


def test_plain_macro_question_answers_without_gemini(mock_db, monkeypatch):
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    client, calls = _counting_client({"intent": "chat", "reply": "ok"})
    bot = FakeBot()
    telegram_bot.handle_text_message(client, bot, CHAT, "how is my protein today")
    assert calls == []  # deterministic path must answer with zero Gemini spend
    assert any("No meals logged" in m["text"] or "Macro check" in m["text"]
               for m in bot.sent)


# ── Feature 1: TEXT_EDIT_WINDOW_DAYS boundary + old-meal correction ─
def test_prompt_meal_window_includes_edge_day_and_excludes_beyond(mock_db, monkeypatch):
    """A meal dated exactly WINDOW-1 days back is the last one Gemini may
    edit; one dated WINDOW+1 days back must be invisible to the prompt so the
    model can never 'correct' ancient history."""
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    window = telegram_bot.TEXT_EDIT_WINDOW_DAYS
    today = database.user_local_now().date()
    now = datetime.now()
    edge_date = (today - timedelta(days=window - 1)).isoformat()
    beyond_date = (today - timedelta(days=window + 1)).isoformat()
    database.save_meal(CHAT, edge_date, "08:00", (now - timedelta(days=window - 1)).isoformat(),
                       "t", "edge-h", "f",
                       {"is_food": True, "meal_description": "Edge oatmeal",
                        "total_calories": 300, "food_items": []})
    database.save_meal(CHAT, beyond_date, "08:00", (now - timedelta(days=window + 1)).isoformat(),
                       "t", "beyond-h", "f",
                       {"is_food": True, "meal_description": "Ancient burrito",
                        "total_calories": 700, "food_items": []})

    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured["prompt"] = kwargs["contents"][0]
            return SimpleNamespace(text=json.dumps({"intent": "chat", "reply": "ok"}))

    bot = FakeBot()
    telegram_bot.handle_text_message(SimpleNamespace(models=FakeModels()), bot, CHAT,
                                     "what did I eat last week?")
    prompt = captured["prompt"]
    assert "Edge oatmeal" in prompt
    assert f"Date: {edge_date}" in prompt
    assert "Ancient burrito" not in prompt


def test_correction_by_index_updates_the_exact_five_day_old_row(mock_db, monkeypatch):
    """A correction aimed at a 5-day-old meal must update THAT row (resolved
    by DB id from the snapshot) and leave today's meal untouched."""
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    today = database.user_local_now().date()
    now = datetime.now()
    old_date = (today - timedelta(days=5)).isoformat()
    old_id = database.save_meal(CHAT, old_date, "12:00",
                                (now - timedelta(days=5)).isoformat(), "t", "old-h", "f",
                                {"is_food": True, "meal_description": "Monday ramen",
                                 "total_calories": 400})
    new_id = database.save_meal(CHAT, today.isoformat(), "12:00", now.isoformat(),
                                "t", "new-h", "f",
                                {"is_food": True, "meal_description": "Today salad",
                                 "total_calories": 300})
    bot = FakeBot()
    # Oldest timestamp sorts first, so the 5-day-old meal is index 0.
    telegram_bot.handle_text_message(_intent_client({
        "intent": "correction", "meal_index": 0,
        "analysis": {"is_food": True, "meal_description": "Monday ramen",
                     "total_calories": 850},
        "reason": "underestimated",
    }), bot, CHAT, "monday's ramen was actually 850 kcal")

    meals = {m["id"]: m for m in database.get_meals(CHAT, "1970-01-01", "9999-12-31")}
    assert meals[old_id]["analysis"]["total_calories"] == 850
    assert meals[old_id]["corrected"] is True
    assert meals[new_id]["analysis"]["total_calories"] == 300
    assert meals[new_id]["corrected"] is False


# ── /today net line arithmetic ──────────────────────────────────────
def test_today_summary_net_arithmetic_is_exact(mock_db, monkeypatch):
    """Net = sum(meals) − sum(activities), to the kcal: 350+250 eaten, 450
    burned → exactly 150."""
    _stable_tz(monkeypatch)
    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_meal(CHAT, today, "08:00", now_iso, "t", "net-a", "f",
                       {"is_food": True, "meal_description": "Eggs", "total_calories": 350})
    database.save_meal(CHAT, today, "12:00", now_iso, "t", "net-b", "f",
                       {"is_food": True, "meal_description": "Salad", "total_calories": 250})
    database.save_activity(CHAT, today, source="manual", active_calories=450)

    summary = telegram_bot.format_today_summary(CHAT)
    assert "🔥 Burned: 450 kcal" in summary
    assert "⚖️ Net: <b>150 kcal</b>" in summary


def test_today_summary_negative_net_renders_signed(mock_db, monkeypatch):
    # A long-run day can legitimately burn more than was eaten; the net must
    # show a signed negative, not wrap or hide.
    _stable_tz(monkeypatch)
    today = database.user_local_now().date().isoformat()
    database.save_meal(CHAT, today, "08:00", datetime.now().isoformat(), "t", "neg-a", "f",
                       {"is_food": True, "meal_description": "Toast", "total_calories": 600})
    database.save_activity(CHAT, today, source="manual", active_calories=1000)

    summary = telegram_bot.format_today_summary(CHAT)
    assert "⚖️ Net: <b>-400 kcal</b>" in summary


def test_today_summary_ignores_activity_from_other_days(mock_db, monkeypatch):
    """Yesterday's run must not leak into today's net — the burned window is
    the user-local day, same as the meals window."""
    _stable_tz(monkeypatch)
    today = database.user_local_now().date()
    yesterday = (today - timedelta(days=1)).isoformat()
    database.save_meal(CHAT, today.isoformat(), "08:00", datetime.now().isoformat(),
                       "t", "oth-a", "f",
                       {"is_food": True, "meal_description": "Toast", "total_calories": 600})
    database.save_activity(CHAT, yesterday, source="manual", active_calories=450)

    summary = telegram_bot.format_today_summary(CHAT)
    assert "Burned:" not in summary
    assert "Net:" not in summary


# ── Regression: hostile Gemini payloads & fast-path hijacks ─────────
@pytest.mark.parametrize("bad_intent", [["correction"], {"intent": "delete"}, None, 7])
def test_nl_unhashable_or_nonstring_intent_falls_back_to_chat(mock_db, monkeypatch, bad_intent):
    """A Gemini-supplied non-string intent (list/dict/null/number) must not
    raise TypeError out of handle_text_message (which would kill the polling
    loop) — it falls through to the chat reply like the old ==-chain did."""
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    bot = FakeBot()
    client = _intent_client({"intent": bad_intent, "reply": "hostile-intent reply"})
    telegram_bot.handle_text_message(client, bot, CHAT, "what did I eat today?")
    assert any("hostile-intent reply" in m["text"] for m in bot.sent)
    # Nothing was misrouted into a write path.
    assert database.get_meals(CHAT, "1970-01-01", "9999-12-31") == []
    assert database.get_activities(CHAT, "1970-01-01", "9999-12-31") == []


@pytest.mark.parametrize("text", [
    "had 2 eggs and toast before today's run",
    "burned 450 kcal on today's run",
    "log my run today: 9.2km",
    "chicken salad wrap, trying to hit my macros",
])
def test_fitness_fastpath_ignores_logging_shaped_messages(mock_db, monkeypatch, text):
    """Meal/activity LOGGING sentences that merely contain 'today's run' or
    'my macros' must reach the Gemini NL pipeline (so they get saved), not be
    swallowed by the zero-Gemini read-only fast path."""
    _stable_tz(monkeypatch)
    bot = FakeBot()
    assert telegram_bot._maybe_answer_fitness_query(bot, CHAT, text) is False
    assert bot.sent == []


def test_fitness_fastpath_logging_message_still_logs_via_gemini(mock_db, monkeypatch):
    """End-to-end: 'burned 450 kcal on today's run' goes through exactly one
    Gemini classification and the activity is actually saved."""
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    client, calls = _counting_client({"intent": "log_activity", "active_calories": 450,
                                      "steps": 0, "distance_km": 0, "reply": "ok"})
    bot = FakeBot()
    telegram_bot.handle_text_message(client, bot, CHAT, "burned 450 kcal on today's run")
    assert len(calls) == 1
    today = database.user_local_now().date().isoformat()
    rows = database.get_activities(CHAT, today, today)
    assert len(rows) == 1
    assert rows[0]["active_calories"] == 450


def test_fitness_fastpath_still_answers_plain_questions(mock_db, monkeypatch):
    """The guard must not break the genuine zero-Gemini queries."""
    _stable_tz(monkeypatch)
    bot = FakeBot()
    assert telegram_bot._maybe_answer_fitness_query(bot, CHAT, "what should I run today?") is True
    assert telegram_bot._maybe_answer_fitness_query(bot, CHAT, "how's my protein?") is True
    assert telegram_bot._maybe_answer_fitness_query(bot, CHAT, "what's this week's plan?") is True
