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
