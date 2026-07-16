import sys
import os
import hashlib
import io
import service_health
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
def mock_db(tmp_path, monkeypatch):
    # Use a real temp file for the sqlite DB to avoid Pathlib errors.
    # monkeypatch guarantees restoration even if init_db raises mid-fixture.
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_meals.db")
    database.init_db()
    # Freeze the user-local clock for the duration of the test. Dozens of
    # tests compute `today = database.user_local_now().date()` during setup
    # and then call handlers that recompute user_local_today() themselves;
    # if host midnight falls between those two reads the seeded rows land on
    # "yesterday" and the assertions break. One value per test removes the
    # straddle. Tests that need a specific clock re-patch user_local_now and
    # win because their setattr runs after this one.
    frozen_local_now = database.user_local_now()
    monkeypatch.setattr(
        database, "user_local_now",
        lambda device_name="android_watcher": frozen_local_now,
    )
    yield


class FakeBot:
    def __init__(self):
        self.sent = []
        self.answered = []
        self.deleted = []

    def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def send_photo(self, chat_id, photo_bytes, caption="", parse_mode="HTML"):
        # Caption text is recorded as the message text so assertions see the
        # same words the user would, caption or plain message alike.
        self.sent.append({"chat_id": chat_id, "text": caption, "reply_markup": None,
                          "photo_bytes": photo_bytes})
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
                        lambda chat_id, analysis, source, file_id, img_hash, **kwargs: saved.append(img_hash) or 77)
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


@pytest.mark.parametrize("captured_at,expect_capture_dating", [
    ("2026-07-10 16:58:08", True),     # sane backfill: capture-day ledger
    ("garbage", False),                 # unparseable -> upload-time dating
    ("2199-01-01 00:00:00", False),     # future -> rejected
    ("2020-01-01 12:00:00", False),     # older than the honor window -> rejected
])
def test_upload_captured_at_dates_meal_on_capture_day(mock_db, monkeypatch, tmp_path,
                                                      captured_at, expect_capture_dating):
    # A photo taken days ago but uploaded today (nightly sync / offline-queue
    # drain) must land on the ledger of the day it was TAKEN — the wrong-day
    # dating found in the 2026-07-14 on-device test session.
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(database, "get_android_timezone", lambda *a, **k: "+0800")
    monkeypatch.setattr(
        telegram_bot, "analyze_food_photo_with_retries",
        lambda client, image_bytes: {"is_food": True, "meal_description": "Backfill",
                                     "total_calories": 300, "food_items": []},
    )
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    app = telegram_bot._build_api_app(FakeBot(), object())
    resp = app.test_client().post(
        "/upload",
        headers={"X-API-Key": "test-upload-key"},
        data={
            "photo": (io.BytesIO(b"backfilled-photo-bytes-" + captured_at.encode()), "meal.jpg"),
            "captured_at": captured_at,
        },
    )

    assert resp.status_code == 200
    if expect_capture_dating:
        meals = database.get_meals(12345, "2026-07-10", "2026-07-10")
        assert len(meals) == 1
        assert meals[0]["time"] == "04:58 PM"
    else:
        today = database.user_local_now().date().isoformat()
        meals = database.get_meals(12345, today, today)
        assert len(meals) == 1                     # fell back to upload-time dating


def test_upload_accepts_raw_body_image(mock_db, monkeypatch, tmp_path):
    # iOS Shortcuts "Request Body: File" sends the image as the raw request
    # body (no multipart) — a workaround for an iOS bug where Form file
    # fields silently coerce to text. The server accepts it, honoring the
    # X-Captured-At header in place of the form field.
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(database, "get_android_timezone", lambda *a, **k: "+0800")
    monkeypatch.setattr(
        telegram_bot, "analyze_food_photo_with_retries",
        lambda client, image_bytes: {"is_food": True, "meal_description": "Raw upload",
                                     "total_calories": 200, "food_items": []},
    )
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    app = telegram_bot._build_api_app(FakeBot(), object())
    resp = app.test_client().post(
        "/upload",
        headers={"X-API-Key": "test-upload-key", "X-Client-Platform": "iOS",
                 "X-Captured-At": "2026-07-11 09:30:00"},
        data=b"\xff\xd8\xff\xe0" + b"jpeg-ish-body" * 10,
        content_type="image/jpeg",
    )

    assert resp.status_code == 200
    meals = database.get_meals(12345, "2026-07-11", "2026-07-11")
    assert len(meals) == 1                       # dated by the header
    assert meals[0]["time"] == "09:30 AM"


def test_upload_raw_body_non_image_still_rejected(mock_db, monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)

    app = telegram_bot._build_api_app(FakeBot(), object())
    resp = app.test_client().post(
        "/upload",
        headers={"X-API-Key": "test-upload-key"},
        data=b"this is just text, not an image, and must not reach Gemini",
        content_type="text/plain",
    )

    assert resp.status_code == 400


def test_photo_analysis_prefers_claude_and_skips_gemini(monkeypatch, tmp_path):
    # Claude-first: when the subscription analyzer answers, Gemini must not
    # even be consulted (no quota spend, no API call).
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot.claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(
        telegram_bot.claude_analyzer, "analyze_food_photo",
        lambda image_bytes: {"is_food": True, "meal_description": "From Claude",
                             "total_calories": 300, "food_items": []},
    )

    class ExplodingGemini:
        def __getattr__(self, name):
            raise AssertionError("Gemini must not be touched when Claude succeeds")

    result = telegram_bot.analyze_food_photo_with_retries(ExplodingGemini(), b"img")
    assert result["meal_description"] == "From Claude"


def test_photo_analysis_falls_back_to_gemini_when_claude_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot.claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(telegram_bot.claude_analyzer, "analyze_food_photo",
                        lambda image_bytes: None)   # window exhausted / CLI error
    monkeypatch.setattr(
        telegram_bot, "_analyze_food_photo_once",
        lambda client, image_bytes: {"is_food": True, "meal_description": "From Gemini",
                                     "total_calories": 400, "food_items": []},
    )

    result = telegram_bot.analyze_food_photo_with_retries(object(), b"img")
    assert result["meal_description"] == "From Gemini"


def test_photo_analysis_unconfigured_claude_is_invisible(monkeypatch, tmp_path):
    # Default state (knob off): behavior must be byte-identical to before —
    # straight to Gemini, no Claude involvement.
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot.claude_analyzer, "is_configured", lambda: False)

    def boom(image_bytes):
        raise AssertionError("analyze_food_photo must not run when unconfigured")

    monkeypatch.setattr(telegram_bot.claude_analyzer, "analyze_food_photo", boom)
    monkeypatch.setattr(
        telegram_bot, "_analyze_food_photo_once",
        lambda client, image_bytes: {"is_food": False},
    )

    assert telegram_bot.analyze_food_photo_with_retries(object(), b"img") == {"is_food": False}

# ── /upload raw-body path: magic bytes, size caps, headers, dedup ────
# Targets: _looks_like_image, _parse_captured_at, and the raw-body branch
# of /upload (iOS Shortcuts "Request Body: File" workaround).

from werkzeug.test import EnvironBuilder as _EnvironBuilder, run_wsgi_app as _run_wsgi_app

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _upload_app(monkeypatch, tmp_path, analysis="skip"):
    """Standard /upload rig: tmp dirs, test API key, synchronous background.

    analysis: "food" (logs a meal), "skip" (not food), "fail" (Gemini gave
    up -> None), or "forbidden" (the request must be rejected before any
    analysis happens).
    """
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(database, "get_android_timezone", lambda *a, **k: "+0800")
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))
    if analysis == "forbidden":
        def stub(client, image_bytes):
            raise AssertionError("analysis must not run for this request")
    else:
        stub = {
            "food": lambda client, image_bytes: {"is_food": True, "meal_description": "Pinned",
                                                 "total_calories": 111, "food_items": []},
            "skip": lambda client, image_bytes: {"is_food": False},
            "fail": lambda client, image_bytes: None,
        }[analysis]
    monkeypatch.setattr(telegram_bot, "analyze_food_photo_with_retries", stub)
    bot = FakeBot()
    return telegram_bot._build_api_app(bot, object()), bot


# ---- _looks_like_image magic bytes ----------------------------------

@pytest.mark.parametrize("brand", [b"heic", b"heix", b"mif1", b"avif", b"isom"])
def test_looks_like_image_accepts_any_ftyp_brand(brand):
    # HEIC/HEIF/AVIF share the ISO BMFF layout: 4-byte size + 'ftyp' + brand.
    # The check keys on 'ftyp' only, so EVERY brand is accepted — including
    # 'isom' (an MP4 video). Pinned as the documented permissive design:
    # a non-image BMFF container gets a 200 and fails analysis later.
    assert telegram_bot._looks_like_image(b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 8) is True


@pytest.mark.parametrize("header,verdict", [
    (JPEG_MAGIC, False),                        # bare 3-byte magic: under the 12-byte floor
    (JPEG_MAGIC + b"\x00" * 8, False),          # 11 bytes: still under the floor
    (JPEG_MAGIC + b"\x00" * 9, True),           # exactly 12: smallest accepted JPEG
    (PNG_SIG + b"abc", False),                  # 11 bytes
    (PNG_SIG + b"abcd", True),                  # exactly 12
    (b"", False),                               # empty body
    (b"RIFF\x24\x00\x00\x00WAVE", False),       # RIFF but wrong tag (a WAV file)
    (b"RIFF\x24\x00\x00\x00WEBP", True),        # real WebP header
    (b"II*\x00" + b"\x00" * 8, True),           # TIFF little-endian
    (b"MM\x00*" + b"\x00" * 8, True),           # TIFF big-endian
    (PNG_SIG + b"\xffnot-real-png-chunks", True),  # prefix-only check: trailing garbage OK
])
def test_looks_like_image_magic_and_length_gates(header, verdict):
    assert telegram_bot._looks_like_image(header) is verdict


def test_upload_raw_body_bare_jpeg_magic_rejected_before_analysis(mock_db, monkeypatch, tmp_path):
    # A 3-byte body IS the JPEG magic, but the 12-byte floor rejects it
    # before staging or analysis: 400, nothing reserved, nothing sent.
    app, bot = _upload_app(monkeypatch, tmp_path, analysis="forbidden")
    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data=JPEG_MAGIC, content_type="image/jpeg")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "No photo provided"}
    assert bot.sent == []


def test_upload_raw_body_minimal_jpeg_fails_analysis_gracefully(mock_db, monkeypatch, tmp_path):
    # The smallest body that passes the magic check (12 bytes: magic +
    # padding) is not a decodable image. The route must still 200 (accepted
    # for background processing) and then fail analysis gracefully: photo
    # kept in the failed dir, user notified, nothing logged.
    app, bot = _upload_app(monkeypatch, tmp_path, analysis="fail")
    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data=JPEG_MAGIC + b"\x00" * 9, content_type="image/jpeg")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "processing_in_background"}
    assert len(list((tmp_path / "failed").iterdir())) == 1
    assert list((tmp_path / "pending").iterdir()) == []
    assert any("could not be analyzed" in m["text"] for m in bot.sent)
    assert database.get_meals(12345, "1970-01-01", "9999-12-31") == []


@pytest.mark.parametrize("brand", [b"heic", b"heix", b"mif1", b"avif"])
def test_upload_raw_body_heic_family_accepted(mock_db, monkeypatch, tmp_path, brand):
    app, _ = _upload_app(monkeypatch, tmp_path, analysis="food")
    body = b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 20
    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data=body, content_type="application/octet-stream")
    assert resp.status_code == 200
    assert len(database.get_meals(12345, "1970-01-01", "9999-12-31")) == 1


def test_upload_raw_body_png_with_trailing_garbage_accepted(mock_db, monkeypatch, tmp_path):
    # Only the 8-byte PNG signature is checked; trailing junk must not
    # demote the body to "not an image".
    app, _ = _upload_app(monkeypatch, tmp_path, analysis="food")
    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data=PNG_SIG + b"\xffgarbage-not-real-chunks",
                                  content_type="image/png")
    assert resp.status_code == 200
    assert len(database.get_meals(12345, "1970-01-01", "9999-12-31")) == 1


def test_upload_raw_body_content_type_lie_accepted_by_design(mock_db, monkeypatch, tmp_path):
    # iOS Shortcuts often declares text/plain for file bodies; the server
    # trusts magic bytes over the Content-Type header. Pinned as design.
    app, _ = _upload_app(monkeypatch, tmp_path, analysis="food")
    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data=JPEG_MAGIC + b"\xe0" + b"jpeg-body" * 4,
                                  content_type="text/plain")
    assert resp.status_code == 200
    assert len(database.get_meals(12345, "1970-01-01", "9999-12-31")) == 1


# ---- Content-Length absent (chunked transfer) -----------------------

def _post_upload_chunked(app, body, content_type="image/jpeg"):
    """POST /upload the way a chunked client arrives at WSGI: no
    CONTENT_LENGTH in the environ, body readable to EOF.

    Bypasses the Flask test client because its environ round-trip
    (EnvironBuilder.from_environ) re-derives CONTENT_LENGTH.
    """
    builder = _EnvironBuilder(path="/upload", method="POST", data=body,
                              content_type=content_type,
                              headers={"X-API-Key": "test-upload-key",
                                       "Transfer-Encoding": "chunked"})
    environ = builder.get_environ()
    environ.pop("CONTENT_LENGTH", None)
    environ.pop("HTTP_CONTENT_LENGTH", None)
    environ["wsgi.input_terminated"] = True
    app_iter, status, _headers = _run_wsgi_app(app, environ, buffered=True)
    return int(status.split()[0]), json.loads(b"".join(app_iter))


def test_upload_chunked_no_content_length_over_cap_still_413(mock_db, monkeypatch, tmp_path):
    # With Transfer-Encoding: chunked, request.content_length is None, so the
    # early size guard is skipped. The cap must still hold via the
    # get_data()[:MAX+1] slice + len check. Body is 2 bytes over the cap.
    app, bot = _upload_app(monkeypatch, tmp_path, analysis="forbidden")
    # Shrink the cap AFTER building the app: the route reads the module
    # global per-request, while app.config['MAX_CONTENT_LENGTH'] keeps the
    # real value so Werkzeug's own limit cannot mask the route's logic.
    monkeypatch.setattr(telegram_bot, "MAX_API_UPLOAD_BYTES", 8192)
    body = JPEG_MAGIC + b"\x00" * (8192 + 2 - len(JPEG_MAGIC))
    status, payload = _post_upload_chunked(app, body)
    assert status == 413
    assert payload == {"error": "Photo too large"}
    assert bot.sent == []


def test_upload_chunked_no_content_length_at_cap_accepted(mock_db, monkeypatch, tmp_path):
    # Exactly-at-cap stays accepted (the cap is inclusive): pins the
    # [:MAX+1] slice against off-by-one regressions.
    app, _ = _upload_app(monkeypatch, tmp_path, analysis="skip")
    monkeypatch.setattr(telegram_bot, "MAX_API_UPLOAD_BYTES", 8192)
    body = JPEG_MAGIC + b"\x00" * (8192 - len(JPEG_MAGIC))
    status, payload = _post_upload_chunked(app, body)
    assert status == 200
    assert payload == {"status": "processing_in_background"}


# ---- Hybrid multipart/raw confusion ---------------------------------

def test_upload_multipart_empty_photo_field_wins_over_raw_sniff(mock_db, monkeypatch, tmp_path):
    # When a multipart 'photo' FILE field exists but is empty, the multipart
    # branch wins and reports "Empty photo" — the server must not fall back
    # to sniffing the raw (multipart-encoded) body.
    app, _ = _upload_app(monkeypatch, tmp_path, analysis="forbidden")
    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data={"photo": (io.BytesIO(b""), "empty.jpg")})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Empty photo"}


def test_upload_multipart_photo_text_field_falls_to_raw_and_400s(mock_db, monkeypatch, tmp_path):
    # 'photo' sent as a plain form VALUE (the iOS coercion failure mode) is
    # not in request.files: the raw branch runs, sees a form-encoded body
    # (already consumed by form parsing), and rejects with the other error.
    app, _ = _upload_app(monkeypatch, tmp_path, analysis="forbidden")
    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data={"photo": "this-is-text-not-a-file"})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "No photo provided"}


# ---- captured_at: source priority + validation window ---------------

@pytest.mark.parametrize("form_value_kind", ["form-wins", "empty-form-falls-back"])
def test_upload_captured_at_form_field_beats_header(mock_db, monkeypatch, tmp_path, form_value_kind):
    # Both the form field and X-Captured-At present: the form field wins.
    # An EMPTY form field is falsy, so the header takes over.
    app, _ = _upload_app(monkeypatch, tmp_path, analysis="food")
    base = database.user_local_now()
    form_day = (base - timedelta(days=2)).date()
    header_day = (base - timedelta(days=3)).date()
    form_value = f"{form_day.isoformat()} 09:00:00" if form_value_kind == "form-wins" else ""
    resp = app.test_client().post(
        "/upload",
        headers={"X-API-Key": "test-upload-key",
                 "X-Captured-At": f"{header_day.isoformat()} 18:00:00"},
        data={"photo": (io.BytesIO(b"priority-body-" + form_value_kind.encode()), "meal.jpg"),
              "captured_at": form_value},
    )
    assert resp.status_code == 200
    if form_value_kind == "form-wins":
        meals = database.get_meals(12345, form_day.isoformat(), form_day.isoformat())
        assert len(meals) == 1
        assert meals[0]["time"] == "09:00 AM"
    else:
        meals = database.get_meals(12345, header_day.isoformat(), header_day.isoformat())
        assert len(meals) == 1
        assert meals[0]["time"] == "06:00 PM"


_FROZEN_LOCAL = datetime(2026, 7, 15, 12, 0, 0)


def _freeze_user_clock(monkeypatch):
    monkeypatch.setattr(database, "user_local_now", lambda *a, **k: _FROZEN_LOCAL)


def test_parse_captured_at_future_boundary_inclusive_at_plus_1h(monkeypatch):
    # Exactly now+1h is ACCEPTED (comparison is strict '>'), one second past
    # is rejected. Pins the inclusive/exclusive semantics of the skew window.
    _freeze_user_clock(monkeypatch)
    at_edge = _FROZEN_LOCAL + timedelta(hours=1)
    assert telegram_bot._parse_captured_at(at_edge.strftime("%Y-%m-%d %H:%M:%S")) == at_edge
    past_edge = at_edge + timedelta(seconds=1)
    assert telegram_bot._parse_captured_at(past_edge.strftime("%Y-%m-%d %H:%M:%S")) is None


def test_parse_captured_at_age_boundary_inclusive_at_max_age(monkeypatch):
    # Exactly now-45d is ACCEPTED (strict '<'), one second older is rejected.
    _freeze_user_clock(monkeypatch)
    at_edge = _FROZEN_LOCAL - timedelta(days=telegram_bot.CAPTURED_AT_MAX_AGE_DAYS)
    assert telegram_bot._parse_captured_at(at_edge.strftime("%Y-%m-%d %H:%M:%S")) == at_edge
    past_edge = at_edge - timedelta(seconds=1)
    assert telegram_bot._parse_captured_at(past_edge.strftime("%Y-%m-%d %H:%M:%S")) is None


@pytest.mark.parametrize("raw,expected", [
    ("  2026-07-10 16:58:08  ", datetime(2026, 7, 10, 16, 58, 8)),   # outer ws stripped
    ("2026-07-10  16:58:08", datetime(2026, 7, 10, 16, 58, 8)),      # strptime tolerates ws runs
    ("2026-07-10\t16:58:08", datetime(2026, 7, 10, 16, 58, 8)),      # tab separator tolerated
    ("2026-07-10 16:58:08Z", None),                                  # tz suffixes rejected...
    ("2026-07-10 16:58:08+08:00", None),
    ("2026-07-10 16:58:08 UTC", None),
    ("2026-07-10T16:58:08", None),                                   # ISO 'T' rejected
    ("", None),
    (None, None),
])
def test_parse_captured_at_spacing_and_timezone_suffixes(monkeypatch, raw, expected):
    # Unparseable values fall back to upload-time dating (None), they never
    # raise out of the route.
    _freeze_user_clock(monkeypatch)
    assert telegram_bot._parse_captured_at(raw) == expected


# ---- X-Original-Hash normalization + dedup via the raw path ---------

def test_upload_raw_body_uppercase_original_hash_normalized(mock_db, monkeypatch, tmp_path):
    # iOS hex is uppercase; the ledger keys on lowercase. The declared hash
    # must be lowercased before it replaces the md5 of the recompressed body.
    app, _ = _upload_app(monkeypatch, tmp_path, analysis="food")
    declared = "ABCDEF0123456789ABCDEF0123456789"
    resp = app.test_client().post("/upload",
                                  headers={"X-API-Key": "test-upload-key",
                                           "X-Original-Hash": declared},
                                  data=JPEG_MAGIC + b"\xe0uppercase-hash-body",
                                  content_type="image/jpeg")
    assert resp.status_code == 200
    meals = database.get_meals(12345, "1970-01-01", "9999-12-31")
    assert len(meals) == 1
    assert meals[0]["image_hash"] == declared.lower()


@pytest.mark.parametrize("junk", [
    "abcdef0123456789abcdef012345678",      # 31 chars: too short
    "abcdef0123456789abcdef0123456789a",    # 33 chars: too long
    "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",     # 32 chars but not hex
    "0xabcdef0123456789abcdef01234567",     # 0x-prefixed
    "   ",                                  # whitespace only
])
def test_upload_raw_body_non_md5_original_hash_ignored(mock_db, monkeypatch, tmp_path, junk):
    # Junk declarations must be ignored (fall back to md5 of the received
    # bytes), never crash and never become the ledger key.
    app, _ = _upload_app(monkeypatch, tmp_path, analysis="food")
    body = JPEG_MAGIC + b"\xe0junk-hash-body"
    resp = app.test_client().post("/upload",
                                  headers={"X-API-Key": "test-upload-key",
                                           "X-Original-Hash": junk},
                                  data=body, content_type="image/jpeg")
    assert resp.status_code == 200
    meals = database.get_meals(12345, "1970-01-01", "9999-12-31")
    assert len(meals) == 1
    assert meals[0]["image_hash"] == hashlib.md5(body).hexdigest()


def test_upload_raw_body_declared_hash_of_saved_meal_is_duplicate(mock_db, monkeypatch, tmp_path):
    # The phone recompresses before upload, so the BYTES differ from the
    # already-logged photo. Dedup must still fire off the DECLARED original
    # hash — case-insensitively — even via the raw-body path.
    app, bot = _upload_app(monkeypatch, tmp_path, analysis="forbidden")
    saved_hash = "0123456789abcdef0123456789abcdef"
    today = database.user_local_now().date().isoformat()
    database.save_meal(12345, today, "12:00 PM", datetime.now().isoformat(),
                       "api_auto", saved_hash, "f", {"is_food": True})
    resp = app.test_client().post("/upload",
                                  headers={"X-API-Key": "test-upload-key",
                                           "X-Original-Hash": saved_hash.upper()},
                                  data=JPEG_MAGIC + b"\xe0recompressed-different-bytes",
                                  content_type="image/jpeg")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "duplicate"}
    assert len(database.get_meals(12345, "1970-01-01", "9999-12-31")) == 1
    assert bot.sent == []


def test_upload_raw_body_declared_hash_of_failed_upload_saved_for_retry(mock_db, monkeypatch, tmp_path):
    # A declared hash matching a kept-for-retry failed upload must short-
    # circuit to already_saved_for_retry (no re-analysis, no double file).
    app, bot = _upload_app(monkeypatch, tmp_path, analysis="forbidden")
    failed_hash = "fedcba9876543210fedcba9876543210"
    failed_dir = tmp_path / "failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / f"20260714T010101_{failed_hash[:12]}.jpg").write_bytes(b"earlier-failed-bytes")
    resp = app.test_client().post("/upload",
                                  headers={"X-API-Key": "test-upload-key",
                                           "X-Original-Hash": failed_hash},
                                  data=JPEG_MAGIC + b"\xe0retry-candidate-bytes",
                                  content_type="image/jpeg")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "already_saved_for_retry"}
    assert bot.sent == []
    assert len(list(failed_dir.iterdir())) == 1


# ---- Just-under-cap body: staging must not choke --------------------

def test_upload_raw_body_just_under_cap_stages_and_completes(mock_db, monkeypatch, tmp_path):
    # MAX-1 bytes: the largest body the cap allows. Must pass both size
    # guards, stage to disk, and complete the (stubbed) pipeline. The
    # zero-filled tail keeps allocation cheap so the test stays fast.
    app, _ = _upload_app(monkeypatch, tmp_path, analysis="skip")
    max_bytes = telegram_bot.MAX_API_UPLOAD_BYTES
    body = JPEG_MAGIC + bytes(max_bytes - 1 - len(JPEG_MAGIC))
    assert len(body) == max_bytes - 1
    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data=body, content_type="image/jpeg")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "processing_in_background"}
    assert list((tmp_path / "pending").iterdir()) == []   # not food -> discarded


# ---- Telegram photo handler (async analysis) ------------------------

class PhotoBot(FakeBot):
    """FakeBot + the photo-path surface (file download, redaction)."""

    def __init__(self, image_bytes=b"telegram photo bytes"):
        super().__init__()
        self.image_bytes = image_bytes
        self.redacted = []

    def get_file(self, file_id):
        if isinstance(self.image_bytes, Exception):
            raise self.image_bytes
        return self.image_bytes

    def _redact(self, value):
        self.redacted.append(value)
        return str(value)


def test_handle_photo_message_ignores_non_photo_messages(monkeypatch):
    bot = PhotoBot()
    monkeypatch.setattr(telegram_bot, "analyze_food_photo",
                        lambda *a, **k: pytest.fail("must not analyze without an image"))

    assert telegram_bot.handle_photo_message(object(), bot, 12345, {"text": "hello"}) is False
    assert telegram_bot.handle_photo_message(
        object(), bot, 12345,
        {"document": {"file_id": "doc-1", "mime_type": "application/pdf"}},
    ) is False
    assert bot.sent == []


def test_handle_photo_message_food_photo_saved_via_background_thread(mock_db, monkeypatch, tmp_path):
    reserves, statuses, saved = [], [], []
    bot = PhotoBot()

    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash",
                        lambda *args, **kwargs: reserves.append((args, kwargs)) or True)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status",
                        lambda *args, **kwargs: statuses.append((args, kwargs)))
    monkeypatch.setattr(telegram_bot, "analyze_food_photo",
                        lambda client, image_bytes: {"is_food": True,
                                                     "meal_description": "Noodles",
                                                     "total_calories": 480})
    monkeypatch.setattr(
        telegram_bot, "save_meal",
        lambda chat_id, analysis, source, file_id, img_hash: saved.append((source, file_id, img_hash)) or 41,
    )
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    handled = telegram_bot.handle_photo_message(
        object(), bot, 12345, {"photo": [{"file_id": "low-res"}, {"file_id": "hi-res"}]})

    assert handled is True
    expected_hash = hashlib.md5(bot.image_bytes).hexdigest()
    # Reservation keeps the deliberate-resend reclaim semantics.
    assert reserves[0][0] == (12345, expected_hash, "telegram")
    assert reserves[0][1] == {"reclaim_statuses": {"failed", "skipped", "deleted"}}
    assert saved == [("telegram", "hi-res", expected_hash)]  # highest-res photo wins
    assert statuses == [((12345, expected_hash, "saved", 41), {"source": "telegram"})]
    # The processing placeholder was deleted and the result was sent.
    assert bot.deleted == [(12345, 1)]
    assert "Noodles" in bot.sent[-1]["text"]
    # The crash-recovery copy is removed once the meal is saved.
    assert list((tmp_path / "pending").iterdir()) == []


def test_handle_photo_message_spawns_thread_instead_of_blocking(monkeypatch, tmp_path):
    """The long-poll caller must return before analysis runs: the spawn goes
    through the module-level threading.Thread (non-daemon, so shutdown joins
    it) and analysis only happens inside the spawned target."""
    spawns = []

    class RecordingThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            spawns.append({"target": target, "args": args, "daemon": daemon})

        def start(self):
            pass  # deliberately never runs the target

    bot = PhotoBot()
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: True)
    monkeypatch.setattr(telegram_bot, "analyze_food_photo",
                        lambda *a, **k: pytest.fail("analysis must run on the background thread"))
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=RecordingThread))

    handled = telegram_bot.handle_photo_message(
        object(), bot, 12345, {"photo": [{"file_id": "f1"}]})

    assert handled is True
    assert len(spawns) == 1
    assert spawns[0]["target"] is telegram_bot._analyze_telegram_photo_background
    assert not spawns[0]["daemon"]  # non-daemon: SIGTERM shutdown joins in-flight analyses
    # The crash-recovery copy is staged BEFORE the thread spawns and its
    # path handed to the background worker, which owns its cleanup.
    staged = list((tmp_path / "pending").iterdir())
    assert len(staged) == 1
    assert spawns[0]["args"][6] == staged[0]


def test_handle_photo_message_duplicate_short_circuits_before_reserving(monkeypatch):
    bot = PhotoBot()
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: True)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash",
                        lambda *a, **k: pytest.fail("duplicates must not reserve"))
    monkeypatch.setattr(telegram_bot, "analyze_food_photo",
                        lambda *a, **k: pytest.fail("duplicates must not analyze"))
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    handled = telegram_bot.handle_photo_message(
        object(), bot, 12345, {"photo": [{"file_id": "f1"}]})

    assert handled is True
    assert "same photo you already sent" in bot.sent[-1]["text"]


def test_handle_photo_message_reservation_denied_skips_analysis(monkeypatch):
    bot = PhotoBot()
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: False)
    monkeypatch.setattr(telegram_bot, "analyze_food_photo",
                        lambda *a, **k: pytest.fail("denied reservation must not analyze"))
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    handled = telegram_bot.handle_photo_message(
        object(), bot, 12345, {"photo": [{"file_id": "f1"}]})

    assert handled is True
    assert "already being processed or was already logged" in bot.sent[-1]["text"]


def test_handle_photo_message_analysis_failure_releases_and_notifies(monkeypatch, tmp_path):
    released = []
    bot = PhotoBot()

    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "service_health.json")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: True)
    monkeypatch.setattr(telegram_bot.database, "release_photo_hash",
                        lambda chat_id, image_hash: released.append(image_hash))
    monkeypatch.setattr(telegram_bot, "analyze_food_photo", lambda client, image_bytes: None)
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    handled = telegram_bot.handle_photo_message(
        object(), bot, 12345, {"photo": [{"file_id": "f1"}]})

    assert handled is True
    assert released == [hashlib.md5(bot.image_bytes).hexdigest()]
    assert "couldn't analyze that photo" in bot.sent[-1]["text"]
    # The crash-recovery copy is removed on the failure path too.
    assert list((tmp_path / "pending").iterdir()) == []


def test_handle_photo_message_background_crash_releases_and_redacts(monkeypatch, tmp_path):
    released = []
    bot = PhotoBot()

    def explode(client, image_bytes):
        raise RuntimeError("boom with https://api.telegram.org/botSECRET/getFile")

    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: True)
    monkeypatch.setattr(telegram_bot.database, "release_photo_hash",
                        lambda chat_id, image_hash: released.append(image_hash))
    monkeypatch.setattr(telegram_bot, "analyze_food_photo", explode)
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    handled = telegram_bot.handle_photo_message(
        object(), bot, 12345, {"photo": [{"file_id": "f1"}]})

    assert handled is True
    assert released == [hashlib.md5(bot.image_bytes).hexdigest()]
    assert bot.redacted  # the token-bearing error went through bot._redact
    assert "Error processing your photo" in bot.sent[-1]["text"]
    # Even a crashing analysis must not leak the crash-recovery copy.
    assert list((tmp_path / "pending").iterdir()) == []


def test_handle_photo_message_download_failure_reports_without_release(monkeypatch):
    bot = PhotoBot(image_bytes=OSError("network down"))
    monkeypatch.setattr(telegram_bot.database, "release_photo_hash",
                        lambda *a, **k: pytest.fail("nothing was reserved, nothing to release"))
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    handled = telegram_bot.handle_photo_message(
        object(), bot, 12345, {"photo": [{"file_id": "f1"}]})

    assert handled is True
    assert bot.redacted
    assert "Error processing your photo" in bot.sent[-1]["text"]


def test_handle_photo_message_staging_failure_still_analyzes(mock_db, monkeypatch):
    """An unwritable disk must not block analysis — the photo just loses its
    crash-recovery copy, matching pre-staging behavior."""
    saved = []
    bot = PhotoBot()

    def stage_explodes(image_bytes, image_hash, filename):
        raise OSError("read-only file system")

    monkeypatch.setattr(telegram_bot, "_stage_api_upload", stage_explodes)
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *a, **k: True)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status", lambda *a, **k: None)
    monkeypatch.setattr(telegram_bot, "analyze_food_photo",
                        lambda client, image_bytes: {"is_food": True,
                                                     "meal_description": "Toast",
                                                     "total_calories": 210})
    monkeypatch.setattr(telegram_bot, "save_meal", lambda *a, **k: saved.append(a) or 7)
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    handled = telegram_bot.handle_photo_message(
        object(), bot, 12345, {"photo": [{"file_id": "f1"}]})

    assert handled is True
    assert len(saved) == 1
    assert "Toast" in bot.sent[-1]["text"]


def test_photo_background_quota_pause_moves_staged_copy_to_failed_dir(mock_db, monkeypatch, tmp_path):
    """When analysis fails during a Gemini pause, the already-staged
    crash-recovery copy is reused (not re-staged) and lands in the failed
    dir for /retry_failed."""
    stage_calls = []
    real_stage = telegram_bot._stage_api_upload

    def counting_stage(image_bytes, image_hash, filename):
        stage_calls.append(filename)
        return real_stage(image_bytes, image_hash, filename)

    statuses = []
    bot = PhotoBot()
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "_stage_api_upload", counting_stage)
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *a, **k: True)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status",
                        lambda *args, **kwargs: statuses.append((args, kwargs)))
    monkeypatch.setattr(telegram_bot, "analyze_food_photo", lambda client, image_bytes: None)
    monkeypatch.setattr(telegram_bot, "_gemini_quota_pause", lambda: True)
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))

    handled = telegram_bot.handle_photo_message(
        object(), bot, 12345, {"photo": [{"file_id": "f1"}]})

    assert handled is True
    assert stage_calls == ["telegram_photo.jpg"]         # staged exactly once
    assert list((tmp_path / "pending").iterdir()) == []  # nothing left behind
    failed = list((tmp_path / "failed").iterdir())
    assert len(failed) == 1
    expected_hash = hashlib.md5(bot.image_bytes).hexdigest()
    assert expected_hash[:12] in failed[0].name
    assert statuses == [((12345, expected_hash, "failed"), {"source": "telegram"})]


def test_retry_all_failed_uses_claude_when_gemini_paused(monkeypatch, tmp_path):
    """The batch gate mirrors the single-file gate: a Gemini pause only holds
    the batch when the Claude analyzer can't step in."""
    failed_file = tmp_path / "20260715T010101_abcabcabcabc.jpg"
    failed_file.write_bytes(b"failed-bytes")
    retried = []

    monkeypatch.setattr(telegram_bot, "_failed_upload_items", lambda: [failed_file])
    monkeypatch.setattr(telegram_bot, "_gemini_quota_pause_summary",
                        lambda: "paused until midnight")
    monkeypatch.setattr(telegram_bot, "_retry_failed_upload_path",
                        lambda client, path: retried.append(path) or
                        {"status": "logged", "name": path.name, "message": "ok"})

    monkeypatch.setattr(telegram_bot.claude_analyzer, "is_configured", lambda: False)
    held = telegram_bot.retry_all_failed_uploads(object(), limit=3)
    assert "Batch retry held" in held
    assert retried == []

    monkeypatch.setattr(telegram_bot.claude_analyzer, "is_configured", lambda: True)
    result = telegram_bot.retry_all_failed_uploads(object(), limit=3)
    assert "Batch retry held" not in result
    assert retried == [failed_file]
    assert "Logged: 1" in result


# ---- Analyzer observability -----------------------------------------

def test_photo_analysis_claude_success_tagged_analyzed_by(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot.claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(
        telegram_bot.claude_analyzer, "analyze_food_photo",
        lambda image_bytes: {"is_food": True, "meal_description": "From Claude",
                             "total_calories": 300, "food_items": []},
    )

    result = telegram_bot.analyze_food_photo_with_retries(object(), b"img")

    assert result["analyzed_by"] == "claude"


def test_photo_analysis_gemini_success_logs_model_name(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot.claude_analyzer, "is_configured", lambda: False)
    monkeypatch.setattr(
        telegram_bot, "_analyze_food_photo_once",
        lambda client, image_bytes: {"is_food": False},
    )

    with caplog.at_level("INFO", logger="calorie_bot"):
        result = telegram_bot.analyze_food_photo_with_retries(object(), b"img")

    assert result == {"is_food": False}
    gemini_logs = [r.message for r in caplog.records if "analyzed by Gemini" in r.message]
    assert gemini_logs and telegram_bot.GEMINI_MODEL in gemini_logs[0]


def test_format_safe_config_reports_claude_analyzer_without_token_value(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-secret-value")
    monkeypatch.setattr(telegram_bot.claude_analyzer, "status_label", lambda: "enabled")

    result = telegram_bot.format_safe_config()

    assert "Claude analyzer: <code>enabled</code>" in result
    assert "Claude OAuth token set: <code>True</code>" in result
    assert "sk-ant-oat-secret-value" not in result


@pytest.mark.parametrize("label,expected", [
    ("enabled", "✅ Claude analyzer enabled (CLI found)"),
    ("off", "⚪ Claude analyzer off — photos go straight to Gemini"),
    ("enabled, CLI missing", "❌ Claude analyzer enabled but the CLI was not found"),
])
def test_run_doctor_reports_claude_analyzer_state(monkeypatch, tmp_path, label, expected):
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "service_health.json")
    monkeypatch.setattr(telegram_bot.database, "get_meals", lambda *args: [])
    monkeypatch.setattr(telegram_bot, "run_gemini_probe", lambda client: "🟢 <b>Gemini probe OK</b>")
    monkeypatch.setattr(telegram_bot.claude_analyzer, "status_label", lambda: label)

    result = telegram_bot.run_doctor(object())

    assert expected in result


# ---- Compound NL instructions (2026-07-16 crash-loop regression) -----

def _seed_three_meals():
    """Breakfast/lunch/dinner rows; returns the snapshot the prompt would show."""
    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    for time_str, image_hash, file_id, desc, cal in [
        ("08:00", "hash-b", "file-b", "白粥", 150),
        ("12:30", "hash-l", "file-l", "面条", 550),
        ("19:00", "hash-d", "file-d", "米饭", 400),
    ]:
        database.save_meal(12345, today, time_str, now_iso, "test", image_hash, file_id,
                           {"is_food": True, "meal_description": desc, "total_calories": cal})
    return _wide_recent_meals(12345)


ROAST_DUCK_ANALYSIS = {
    "is_food": True,
    "meal_description": "烧鸭饭",
    "total_calories": 780,
    "total_protein_g": 35,
    "total_carbs_g": 90,
    "total_fat_g": 28,
    "food_items": [{"name": "烧鸭饭", "estimated_calories": 780,
                    "protein_g": 35, "carbs_g": 90, "fat_g": 28}],
}


def _compound_nl_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(database, "get_android_timezone", lambda *args, **kwargs: "+0800")
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    telegram_bot._pending_nl_deletes.clear()


def _assert_correct_lunch_delete_dinner(bot, snapshot):
    """Shared outcome checks: lunch corrected in-place, dinner pending confirm."""
    lunch = next(m for m in snapshot if m["analysis"]["meal_description"] == "面条")
    dinner = next(m for m in snapshot if m["analysis"]["meal_description"] == "米饭")

    after = {m["id"]: m for m in _wide_recent_meals(12345)}
    assert len(after) == 3, "nothing may be deleted before the user confirms"
    assert after[lunch["id"]]["analysis"]["meal_description"] == "烧鸭饭"
    assert after[lunch["id"]]["analysis"]["total_calories"] == 780

    pending = telegram_bot._pending_nl_deletes[12345]
    assert pending["ids"] == [dinner["id"]]

    texts = [m["text"] for m in bot.sent]
    assert any("烧鸭饭" in t and "✏️" in t for t in texts), "correction reply missing"
    markup = bot.sent[-1]["reply_markup"]
    datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert f"nl_delete_confirm:{pending['token']}" in datas


def test_nl_compound_bare_array_crash_regression(mock_db, monkeypatch, tmp_path):
    """The exact 2026-07-16 production failure: a compound Chinese instruction
    made Gemini return a bare JSON ARRAY of intent objects; result.get()
    raised AttributeError, killing the process before the offset was
    confirmed — systemd then crash-looped on the re-delivered message.
    The array shape must now execute all actions."""
    _compound_nl_setup(monkeypatch, tmp_path)
    snapshot = _seed_three_meals()
    lunch_idx = next(i for i, m in enumerate(snapshot)
                     if m["analysis"]["meal_description"] == "面条")
    dinner_idx = next(i for i, m in enumerate(snapshot)
                      if m["analysis"]["meal_description"] == "米饭")

    payload = [
        {"intent": "correction", "meal_index": lunch_idx,
         "reason": "第二顿饭改为烧鸭饭", "analysis": ROAST_DUCK_ANALYSIS},
        {"intent": "delete", "meal_indices": [dinner_idx], "reason": "用户要求删除第三顿饭"},
    ]

    bot = FakeBot()
    telegram_bot.handle_text_message(
        _intent_client(payload), bot, 12345,
        "我是说第二顿饭是烧鸭饭 你重新准确算一下第二顿饭 删除第三顿饭")

    _assert_correct_lunch_delete_dinner(bot, snapshot)


def test_nl_compound_multi_object_shape(mock_db, monkeypatch, tmp_path):
    """The designed compound shape: {"intent": "multi", "actions": [...]}."""
    _compound_nl_setup(monkeypatch, tmp_path)
    snapshot = _seed_three_meals()
    lunch_idx = next(i for i, m in enumerate(snapshot)
                     if m["analysis"]["meal_description"] == "面条")
    dinner_idx = next(i for i, m in enumerate(snapshot)
                      if m["analysis"]["meal_description"] == "米饭")

    payload = {
        "intent": "multi",
        "actions": [
            {"intent": "correction", "meal_index": lunch_idx,
             "reason": "改为烧鸭饭", "analysis": ROAST_DUCK_ANALYSIS},
            {"intent": "delete", "meal_indices": [dinner_idx], "reason": "删除第三顿"},
        ],
        "reply": "正在更正第二顿并删除第三顿",
    }

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(payload), bot, 12345,
                                     "第二顿是烧鸭饭，删除第三顿")

    _assert_correct_lunch_delete_dinner(bot, snapshot)


def test_nl_compound_caps_at_max_actions(mock_db, monkeypatch, tmp_path):
    """Model runaway (dozens of actions) is capped at NL_MAX_ACTIONS."""
    _compound_nl_setup(monkeypatch, tmp_path)
    payload = {"intent": "multi",
               "actions": [{"intent": "chat", "reply": f"reply {i}"} for i in range(8)]}

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(payload), bot, 12345, "hello hello")

    replies = [m["text"] for m in bot.sent if m["text"].startswith("reply ")]
    assert replies == [f"reply {i}" for i in range(telegram_bot.NL_MAX_ACTIONS)]


def test_nl_compound_partial_failure_continues(mock_db, monkeypatch, tmp_path):
    """One crashing action must not abort its siblings or the process."""
    _compound_nl_setup(monkeypatch, tmp_path)

    def exploding_correction(bot, chat_id, text, meals, result):
        raise RuntimeError("boom")

    monkeypatch.setitem(telegram_bot._NL_INTENT_HANDLERS, "correction", exploding_correction)
    payload = {"intent": "multi", "actions": [
        {"intent": "correction", "meal_index": 0, "analysis": {}},
        {"intent": "chat", "reply": "still here"},
    ]}

    bot = PhotoBot()  # the executor's failure path redacts via bot._redact
    telegram_bot.handle_text_message(_intent_client(payload), bot, 12345, "fix and chat")

    texts = [m["text"] for m in bot.sent]
    assert "still here" in texts
    assert any("1 of 2" in t for t in texts)


@pytest.mark.parametrize("payload", ["just a string", 42, [], ["a", 3], None])
def test_nl_unusable_response_shapes_reply_gracefully(mock_db, monkeypatch, tmp_path, payload):
    """Any non-actionable JSON type gets a friendly reply, never a crash."""
    _compound_nl_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(payload), bot, 12345, "gibberish request")
    assert "couldn't work out what to do" in bot.sent[-1]["text"]


def test_handle_text_message_safe_contains_any_crash(monkeypatch):
    """The safe dispatcher is the last line of defense: an exception escaping
    handle_text_message previously killed the process pre-offset-confirm and
    crash-looped under systemd. It must be contained, redacted, and reported."""
    bot = PhotoBot()

    def explode(*args, **kwargs):
        raise RuntimeError("boom https://api.telegram.org/botSECRET/sendMessage")

    monkeypatch.setattr(telegram_bot, "handle_text_message", explode)
    telegram_bot.handle_text_message_safe(object(), bot, 12345, "anything")

    assert bot.redacted, "the token-bearing error must pass through bot._redact"
    assert "Something went wrong" in bot.sent[-1]["text"]


def test_handle_text_message_safe_survives_notify_failure(monkeypatch):
    """Even the failure notice failing must not raise into the poll loop."""
    bot = PhotoBot()
    monkeypatch.setattr(telegram_bot, "handle_text_message",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bot, "send_message",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("telegram down")))

    telegram_bot.handle_text_message_safe(object(), bot, 12345, "anything")  # must not raise


# ---- Pre-deploy adversarial review fixes (compound NL hardening) -----

def test_nl_correction_refuses_empty_or_nonfood_analysis(mock_db, monkeypatch, tmp_path):
    """A correction whose analysis is {} / missing / non-food must NOT be
    written: it would hide the meal from every is_food view — a silent
    delete wearing a success reply, without the delete path's confirmation."""
    _compound_nl_setup(monkeypatch, tmp_path)
    snapshot = _seed_three_meals()
    lunch = next(m for m in snapshot if m["analysis"]["meal_description"] == "面条")
    lunch_idx = snapshot.index(lunch)

    for bad_analysis in [{}, {"is_food": False, "meal_description": "x"}, None]:
        payload = {"intent": "correction", "meal_index": lunch_idx, "analysis": bad_analysis}
        bot = PhotoBot()
        telegram_bot.handle_text_message(_intent_client(payload), bot, 12345, "改一下第二顿")
        assert "left the meal unchanged" in bot.sent[-1]["text"] or "unchanged" in bot.sent[-1]["text"]

    after = {m["id"]: m for m in _wide_recent_meals(12345)}
    assert after[lunch["id"]]["analysis"]["meal_description"] == "面条"
    assert after[lunch["id"]]["analysis"]["total_calories"] == 550


def test_nl_compound_all_failed_wording_never_claims_partial_success(mock_db, monkeypatch, tmp_path):
    """When every action fails, the note must not say 'the rest were applied'."""
    _compound_nl_setup(monkeypatch, tmp_path)

    def explode(bot, chat_id, text, meals, result):
        raise RuntimeError("boom")

    monkeypatch.setitem(telegram_bot._NL_INTENT_HANDLERS, "correction", explode)
    monkeypatch.setitem(telegram_bot._NL_INTENT_HANDLERS, "delete", explode)
    payload = {"intent": "multi", "actions": [
        {"intent": "correction", "meal_index": 0, "analysis": ROAST_DUCK_ANALYSIS},
        {"intent": "delete", "meal_indices": [1]},
    ]}

    bot = PhotoBot()
    telegram_bot.handle_text_message(_intent_client(payload), bot, 12345, "fix and delete")

    final = bot.sent[-1]["text"]
    assert "All 2 requested actions failed" in final
    assert "the rest were applied" not in final


def test_nl_compound_duplicate_delete_actions_merge_into_one(mock_db, monkeypatch, tmp_path):
    """Two delete actions would dead-end the first confirmation's buttons
    (_pending_nl_deletes is one slot per chat) — the normalizer merges them."""
    _compound_nl_setup(monkeypatch, tmp_path)
    snapshot = _seed_three_meals()
    payload = {"intent": "multi", "actions": [
        {"intent": "delete", "meal_indices": [0], "reason": "第一顿"},
        {"intent": "delete", "meal_indices": [2], "reason": "第三顿"},
    ]}

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(payload), bot, 12345, "删除第一顿和第三顿")

    pending = telegram_bot._pending_nl_deletes[12345]
    assert sorted(pending["ids"]) == sorted([snapshot[0]["id"], snapshot[2]["id"]])
    confirms = [m for m in bot.sent if m.get("reply_markup")]
    assert len(confirms) == 1, "exactly one confirmation prompt, holding both meals"
    assert len(_wide_recent_meals(12345)) == 3  # nothing deleted before confirm


def test_nl_single_intent_wins_over_hallucinated_actions(mock_db, monkeypatch, tmp_path):
    """A real single intent carrying a decomposition-style 'actions' list must
    execute its own intent, not the unknown sub-steps."""
    _compound_nl_setup(monkeypatch, tmp_path)
    snapshot = _seed_three_meals()
    lunch_idx = next(i for i, m in enumerate(snapshot)
                     if m["analysis"]["meal_description"] == "面条")
    payload = {
        "intent": "correction", "meal_index": lunch_idx, "analysis": ROAST_DUCK_ANALYSIS,
        "actions": [{"step": "recalculate"}, {"step": "update"}],
    }

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(payload), bot, 12345, "第二顿是烧鸭饭")

    after = {m["id"]: m for m in _wide_recent_meals(12345)}
    lunch_id = snapshot[lunch_idx]["id"]
    assert after[lunch_id]["analysis"]["meal_description"] == "烧鸭饭"
    assert not any("not sure what you mean" in m["text"] for m in bot.sent)


def test_redact_is_total_for_unprintable_values():
    """_redact is called inside last-line-of-defense except blocks; a value
    whose __str__ raises must yield a placeholder, not a new exception."""
    bot = telegram_bot.TelegramBot("123:abc")

    class BadStr(Exception):
        def __str__(self):
            raise ValueError("even __str__ is broken")

    assert telegram_bot.TelegramBot._redact(bot, BadStr()) == "<unprintable BadStr>"
    # Normal-path behavior unchanged: the raw token is still scrubbed.
    assert telegram_bot.TelegramBot._redact(bot, "plain 123:abc text") == "plain <token> text"


# ---- Photo echo for API uploads --------------------------------------

def _real_jpeg_bytes(px=64):
    from PIL import Image as PILImage
    buf = io.BytesIO()
    PILImage.new("RGB", (px, px), (200, 120, 40)).save(buf, format="JPEG")
    return buf.getvalue()


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        action = self.responses.pop(0)
        if isinstance(action, Exception):
            raise action
        resp = SimpleNamespace(json=lambda: action)
        resp.raise_for_status = lambda: None
        return resp


def test_send_photo_multipart_shape_and_caption():
    bot = telegram_bot.TelegramBot("123:abc")
    bot.session = _FakeSession([{"ok": True, "result": {"message_id": 7}}])

    result = bot.send_photo(12345, b"jpegbytes", caption="<b>Meal</b>")

    assert result == {"message_id": 7}
    call = bot.session.calls[0]
    assert call["url"].endswith("/sendPhoto")
    assert call["files"]["photo"][1] == b"jpegbytes"
    assert call["data"]["caption"] == "<b>Meal</b>"
    assert call["data"]["parse_mode"] == "HTML"


def test_send_photo_retry_keeps_caption_drops_parse_mode():
    """The meal card rides in the caption — the retry must never sacrifice
    it (mirror of send_message's parse_mode-only fallback)."""
    bot = telegram_bot.TelegramBot("123:abc")
    bot.session = _FakeSession([OSError("network"), {"ok": True, "result": {"message_id": 9}}])
    assert bot.send_photo(12345, b"x", caption="cap") == {"message_id": 9}
    retry = bot.session.calls[1]["data"]
    assert retry["caption"] == "cap"
    assert "parse_mode" not in retry

    bot.session = _FakeSession([OSError("network"), OSError("network")])
    assert bot.send_photo(12345, b"x", caption="cap") is None  # caller falls back to text

    bot.session = _FakeSession([{"ok": False, "description": "bad"}])
    assert bot.send_photo(12345, b"x") is None


def test_compress_photo_for_echo_shapes():
    out = telegram_bot._compress_photo_for_echo(_real_jpeg_bytes())
    assert out[:3] == b"\xff\xd8\xff"  # decodable image -> recompressed JPEG

    junk_small = b"\xff\xd8\xff" + b"not really an image"
    assert telegram_bot._compress_photo_for_echo(junk_small) == junk_small  # JPEG magic: as-is

    junk_huge = b"\xff\xd8\xff" + bytes(9_500_000)
    assert telegram_bot._compress_photo_for_echo(junk_huge) is None  # oversize

    # Telegram only accepts JPEG/PNG/WebP: undecodable bytes in any other
    # format must NOT be passed through to a guaranteed-futile upload.
    tiff_junk = b"II*\x00" + b"telegram would reject this"
    assert telegram_bot._compress_photo_for_echo(tiff_junk) is None
    png_junk = b"\x89PNG\r\n\x1a\n" + b"truncated png"
    assert telegram_bot._compress_photo_for_echo(png_junk) == png_junk


def test_echo_meal_photo_caption_vs_split_vs_fallback(monkeypatch):
    photo_ok = {"message_id": 1}

    class EchoBot(FakeBot):
        def __init__(self, photo_results):
            super().__init__()
            self.photo_results = list(photo_results)
            self.photo_calls = []

        def send_photo(self, chat_id, photo_bytes, caption="", parse_mode="HTML"):
            self.photo_calls.append({"caption": caption})
            return self.photo_results.pop(0)

    jpeg = _real_jpeg_bytes()

    # Short caption -> ONE photo message carrying the card as caption.
    bot = EchoBot([photo_ok])
    telegram_bot._echo_meal_photo(bot, 12345, jpeg, "short card")
    assert bot.photo_calls == [{"caption": "short card"}]
    assert bot.sent == []

    # Long caption (>1024 UTF-16 units) -> captionless photo + text message.
    long_card = "🍜" * 600  # astral: 2 units each = 1200 units
    bot = EchoBot([photo_ok])
    telegram_bot._echo_meal_photo(bot, 12345, jpeg, long_card)
    assert bot.photo_calls == [{"caption": ""}]
    assert bot.sent[-1]["text"] == long_card

    # send_photo failing -> plain text card still arrives.
    bot = EchoBot([None])
    telegram_bot._echo_meal_photo(bot, 12345, jpeg, "short card")
    assert bot.sent[-1]["text"] == "short card"

    # Feature toggled off -> text only, no photo attempt.
    monkeypatch.setattr(telegram_bot, "ECHO_UPLOAD_PHOTOS", False)
    bot = EchoBot([])
    telegram_bot._echo_meal_photo(bot, 12345, jpeg, "short card")
    assert bot.photo_calls == [] and bot.sent[-1]["text"] == "short card"


def test_upload_food_result_arrives_as_photo_caption(mock_db, monkeypatch, tmp_path):
    """End-to-end: a phone upload's meal card now rides on the echoed photo."""
    app, bot = _upload_app(monkeypatch, tmp_path, analysis="food")
    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data=JPEG_MAGIC + b"\xe0food-photo-bytes",
                                  content_type="image/jpeg")
    assert resp.status_code == 200
    echoed = [m for m in bot.sent if m.get("photo_bytes")]
    assert len(echoed) == 1
    assert "Auto-Logged" in echoed[0]["text"]


# ---- Anti-infinite-loop: poison-update ledger + restart tripwire -----

def _poison_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)


def test_poison_update_skips_after_max_attempts(monkeypatch, tmp_path):
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()

    # Three sightings (= three fatal attempts) still process.
    for _ in range(telegram_bot.POISON_UPDATE_MAX_ATTEMPTS):
        assert telegram_bot._poison_update_should_skip(bot, 777) is False
    assert bot.sent == []

    # Fourth sighting: skipped, user notified once.
    assert telegram_bot._poison_update_should_skip(bot, 777) is True
    assert "skipped a message that crashed me repeatedly" in bot.sent[-1]["text"]

    # Fifth sighting: still skipped, but no second notice.
    assert telegram_bot._poison_update_should_skip(bot, 777) is True
    assert len(bot.sent) == 1


def test_poison_update_counts_are_per_update_id(monkeypatch, tmp_path):
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    for update_id in range(100, 100 + telegram_bot.POISON_UPDATE_MAX_ATTEMPTS + 2):
        assert telegram_bot._poison_update_should_skip(bot, update_id) is False
    assert telegram_bot._poison_update_should_skip(bot, None) is False


def test_poison_update_ledger_prunes_to_newest_twenty(monkeypatch, tmp_path):
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    for update_id in range(1000, 1030):
        telegram_bot._poison_update_should_skip(bot, update_id)
    attempts = service_health.load(tmp_path / "health.json")["update_attempts"]
    assert len(attempts) == 20
    assert "1029" in attempts and "1000" not in attempts


def test_rapid_restart_alert_fires_on_third_crash_boot_and_latches(monkeypatch, tmp_path):
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    telegram_bot._record_boot_and_maybe_alert(bot)
    telegram_bot._record_boot_and_maybe_alert(bot)
    assert bot.sent == []
    telegram_bot._record_boot_and_maybe_alert(bot)
    assert "crash-restarted 3 times in the last 10 minutes" in bot.sent[-1]["text"]
    # Latched: a live crash loop must not also spam the chat every boot.
    telegram_bot._record_boot_and_maybe_alert(bot)
    assert len(bot.sent) == 1


def test_rapid_restart_alert_ignores_clean_restarts(monkeypatch, tmp_path):
    """systemctl restart / deploy bursts stamp a clean shutdown and never
    count as crashes — the exact false alarm the review flagged."""
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    for _ in range(4):
        telegram_bot._record_clean_shutdown()
        telegram_bot._record_boot_and_maybe_alert(bot)
    assert bot.sent == []


def test_rapid_restart_alert_ignores_old_boots(monkeypatch, tmp_path):
    _poison_setup(monkeypatch, tmp_path)
    health = tmp_path / "health.json"
    stale = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    service_health.save({"recent_crash_boots": [stale, stale, "not-a-timestamp"]}, health)
    bot = FakeBot()
    telegram_bot._record_boot_and_maybe_alert(bot)
    assert bot.sent == []  # stale + junk entries pruned; this is crash-boot #1


def test_poison_clear_protects_healthy_batch_mates(monkeypatch, tmp_path):
    """A healthy update re-delivered alongside a poison one is cleared after
    its first survived pass and ring-skipped on every redelivery, so it can
    never be falsely declared poison (and its side effects never re-run)."""
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    assert telegram_bot._poison_update_should_skip(bot, 555) is False  # healthy A processes
    telegram_bot._poison_update_clear(555)                             # loop survived A
    for _ in range(telegram_bot.POISON_UPDATE_MAX_ATTEMPTS + 3):
        # Redelivered A: recognized as already processed, skipped quietly.
        assert telegram_bot._poison_update_should_skip(bot, 555) is True
        telegram_bot._poison_update_should_skip(bot, 556)              # poison B, never cleared
    # Exactly one notice, for B alone; A never accumulated a single attempt.
    assert len(bot.sent) == 1
    assert "crashed me repeatedly" in bot.sent[-1]["text"]
    attempts = service_health.load(tmp_path / "health.json")["update_attempts"]
    assert "555" not in attempts
    assert attempts["556"]["n"] > telegram_bot.POISON_UPDATE_MAX_ATTEMPTS


def test_service_health_load_tolerates_non_dict_json(tmp_path):
    """Valid-JSON-but-not-an-object must fail open like invalid JSON: a
    crash here at boot would be an unrecoverable startup loop."""
    health = tmp_path / "health.json"
    health.write_text("[1, 2, 3]")
    assert service_health.load(health) == {}
    telegram_bot.service_health.update(lambda d: d.setdefault("k", 1), health)
    assert service_health.load(health) == {"k": 1}


def test_process_update_rejects_unauthorized_chat(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    bot = FakeBot()
    telegram_bot._process_update(object(), bot, {
        "update_id": 1,
        "message": {"chat": {"id": 99999}, "from": {"first_name": "Stranger"}, "text": "hi"},
    })
    assert "not authorized" in bot.sent[-1]["text"]


# ═══ HTML-injection & formatting audit at the send boundary (hunter 4) ═══

import re as _re_h4
from utils import utf16_len as _utf16_len

_TELEGRAM_TAG_RE = _re_h4.compile(r"<(/?)(b|strong|i|em|u|ins|s|strike|del|code|pre|a|span|blockquote|tg-spoiler|tg-emoji)(\s[^<>]*)?>", _re_h4.IGNORECASE)


def _telegram_html_parse_ok(text):
    """Mimic Telegram's HTML entity parser strictly enough for these tests:
    every '<' must start a complete, supported tag, and open/close tags must
    balance. A stray '<' or an unclosed tag makes sendMessage 400."""
    stack = []
    idx = 0
    while True:
        lt = text.find("<", idx)
        if lt == -1:
            break
        m = _TELEGRAM_TAG_RE.match(text, lt)
        if not m:
            return False  # bare '<' or a tag cut in half
        name = m.group(2).lower()
        if m.group(1):
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        else:
            stack.append(name)
        idx = m.end()
    return not stack


def test_send_message_truncation_yields_parseable_telegram_html():
    """A valid long HTML message must still be valid HTML after truncation.
    Today the codepoint cut at 4000 units can land inside <b>...</b> (unclosed
    tag) or inside the tag itself (stray '<'); Telegram rejects both and the
    parse_mode-less retry shows the user 4000 chars of raw markup."""
    # Cut lands between <b> and </b> -> unclosed tag.
    bot = telegram_bot.TelegramBot("123:abc")
    bot.session = _FakeSession([{"ok": True, "result": {"message_id": 1}}])
    bot.send_message(12345, "A" * 3995 + "<b>" + "B" * 100 + "</b>")
    sent = bot.session.calls[0]["json"]["text"]
    assert _telegram_html_parse_ok(sent), f"unbalanced tail: {sent[-40:]!r}"

    # Cut lands inside the tag itself -> bare '<' (unsupported start tag).
    bot.session = _FakeSession([{"ok": True, "result": {"message_id": 1}}])
    bot.send_message(12345, "A" * 3999 + "<code>xyz</code>" + "A" * 100)
    sent = bot.session.calls[0]["json"]["text"]
    assert _telegram_html_parse_ok(sent), f"split tag around cut: {sent[3990:4010]!r}"


def test_send_message_truncation_limit_and_marker():
    """Pins the working parts of the truncation: the posted text stays under
    Telegram's 4096-unit cap, ends with the (truncated) marker, and never
    splits an astral pair (the char straddling the cut is dropped whole)."""
    bot = telegram_bot.TelegramBot("123:abc")
    bot.session = _FakeSession([{"ok": True, "result": {"message_id": 1}}])
    bot.send_message(12345, "x" * 3999 + "🍜" * 10)  # astral char straddles unit 4000
    sent = bot.session.calls[0]["json"]["text"]
    assert _utf16_len(sent) <= 4096
    assert sent.endswith("<i>(truncated)</i>")
    # The surrogate-pair char at the boundary was dropped whole, not split.
    assert sent[3998] == "x" and "🍜" not in sent

    # At exactly 4000 units nothing is truncated.
    bot.session = _FakeSession([{"ok": True, "result": {"message_id": 1}}])
    bot.send_message(12345, "y" * 4000)
    assert bot.session.calls[0]["json"]["text"] == "y" * 4000


def test_nl_correction_invalid_index_reply_escapes_model_meal_index(mock_db, monkeypatch):
    """Gemini controls meal_index; a hallucinated non-numeric value is echoed
    back verbatim. '<b>2</b>' renders as bold (model-controlled formatting),
    an unclosed '<b' 400s the send and the reply degrades to raw plain text."""
    _stable_tz(monkeypatch)
    _no_pause(monkeypatch)
    monkeypatch.setattr(telegram_bot, "get_recent_meals", _wide_recent_meals)
    today = database.user_local_now().date().isoformat()
    database.save_meal(CHAT, today, "12:00", datetime.now().isoformat(), "t", "hx", "f",
                       {"is_food": True, "meal_description": "Rice", "total_calories": 500})

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client({
        "intent": "correction", "meal_index": "<b>2</b>",
        "analysis": {"is_food": True, "meal_description": "Duck", "total_calories": 450},
    }), bot, CHAT, "fix meal <b>2</b>")

    reply = bot.sent[-1]["text"]
    assert "Invalid meal index" in reply  # the branch under test actually ran
    assert "<b>2</b>" not in reply
    assert "&lt;b&gt;2&lt;/b&gt;" in reply


def test_echo_meal_photo_caption_gate_exact_1024_boundary():
    """Pins the caption/split decision at the exact Telegram limit: 1024
    UTF-16 units ride as the caption, 1025 switch to captionless photo +
    separate text message. Counted on the FULL string the caller passes."""
    class EchoBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.photo_calls = []

        def send_photo(self, chat_id, photo_bytes, caption="", parse_mode="HTML"):
            self.photo_calls.append({"caption": caption})
            return {"message_id": 1}

    jpeg = _real_jpeg_bytes()

    at_limit = "🍜" * 512  # astral: exactly 1024 units
    assert _utf16_len(at_limit) == 1024
    bot = EchoBot()
    telegram_bot._echo_meal_photo(bot, 12345, jpeg, at_limit)
    assert bot.photo_calls == [{"caption": at_limit}]
    assert bot.sent == []

    over_limit = at_limit + "!"  # 1025 units
    bot = EchoBot()
    telegram_bot._echo_meal_photo(bot, 12345, jpeg, over_limit)
    assert bot.photo_calls == [{"caption": ""}]
    assert bot.sent[-1]["text"] == over_limit


def test_upload_echo_caption_gate_counts_auto_logged_prefix(mock_db, monkeypatch, tmp_path):
    """The 1024 gate must be applied to the caption the user actually gets —
    'Auto-Logged from <device>' prefix INCLUDED. A card that fits alone but
    overflows with the prefix must go captionless + separate text (an off-by-
    prefix here would hand Telegram an oversized caption to reject)."""
    app, bot = _upload_app(monkeypatch, tmp_path, analysis="food")
    prefix = "📲 <b>Auto-Logged from Phone:</b>\n\n"
    prefix_units = _utf16_len(prefix)
    card = "C" * (1024 - prefix_units + 1)  # fits alone, overflows with prefix
    monkeypatch.setattr(telegram_bot, "format_food_result", lambda chat_id, analysis: card)

    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data=JPEG_MAGIC + b"\xe0meal-photo-bytes",
                                  content_type="image/jpeg")
    assert resp.status_code == 200

    echoed = [m for m in bot.sent if m.get("photo_bytes")]
    assert len(echoed) == 1
    assert echoed[0]["text"] == ""  # captionless: prefix+card is 1025 units
    texts = [m for m in bot.sent if not m.get("photo_bytes")]
    assert texts[-1]["text"] == prefix + card

    # One unit shorter and the whole prefixed card rides as the caption.
    bot.sent.clear()
    card_fits = "C" * (1024 - prefix_units)
    monkeypatch.setattr(telegram_bot, "format_food_result", lambda chat_id, analysis: card_fits)
    resp = app.test_client().post("/upload", headers={"X-API-Key": "test-upload-key"},
                                  data=JPEG_MAGIC + b"\xe1other-photo-bytes",
                                  content_type="image/jpeg")
    assert resp.status_code == 200
    echoed = [m for m in bot.sent if m.get("photo_bytes")]
    assert len(echoed) == 1
    assert echoed[0]["text"] == prefix + card_fits


def test_upload_device_name_escaped_in_processing_and_caption(mock_db, monkeypatch, tmp_path):
    """X-Device-Name is client-supplied text rendered in two HTML sends (the
    'Received photo' notice and the Auto-Logged caption) — raw tags must not
    survive into either."""
    app, bot = _upload_app(monkeypatch, tmp_path, analysis="food")
    resp = app.test_client().post(
        "/upload",
        headers={"X-API-Key": "test-upload-key",
                 "X-Client-Platform": "android",
                 "X-Device-Name": "<b>Pixel & Co</b>"},
        data=JPEG_MAGIC + b"\xe0device-name-bytes",
        content_type="image/jpeg",
    )
    assert resp.status_code == 200
    all_texts = "\n".join(m["text"] for m in bot.sent)
    assert "<b>Pixel & Co</b>" not in all_texts
    assert "&lt;b&gt;Pixel &amp; Co&lt;/b&gt;" in all_texts
    # Both send sites carried the escaped name.
    assert any("Received photo from" in m["text"]
               and "&lt;b&gt;Pixel &amp; Co&lt;/b&gt;" in m["text"] for m in bot.sent)
    echoed = [m for m in bot.sent if m.get("photo_bytes")]
    assert len(echoed) == 1
    assert "Auto-Logged from &lt;b&gt;Pixel &amp; Co&lt;/b&gt;" in echoed[0]["text"]


# ---- Kill-sequence model check of the poison-update state machine ----
#
# Replays main()'s exact per-update lifecycle (guard -> process -> clear,
# batch confirmed only when a full pass survives to the next getUpdates)
# against the REAL durable ledger, cutting the "process" at scripted kill
# points. A kill is a BaseException: the loop's `except Exception` cannot
# contain it, exactly like OOM/SIGKILL.

class _KillSignal(BaseException):
    pass


class _PoisonLoopSim:
    MAX_BOOTS = 30

    def __init__(self, bot, batch, poison=()):
        self.bot = bot
        self.batch = list(batch)   # redelivered verbatim until confirmed
        self.poison = set(poison)  # updates that always kill mid-process
        self.processed = []
        self.skipped = []          # poison skips (threshold crossed)
        self.redelivered = []      # ring skips (already-processed redeliveries)
        self.sightings = {}

    def _attempts(self):
        data = service_health.load(telegram_bot.SERVICE_HEALTH_PATH, warn=lambda m: None)
        attempts = data.get("update_attempts", {})
        if not isinstance(attempts, dict):
            return {}
        # Entries are {'n': count, 'at': timestamp}; unwrap to bare counts
        # (legacy bare ints pass through).
        return {k: (v.get("n", 0) if isinstance(v, dict) else v)
                for k, v in attempts.items()}

    def _processed_ring(self):
        data = service_health.load(telegram_bot.SERVICE_HEALTH_PATH, warn=lambda m: None)
        ring = data.get("processed_updates")
        return set(ring) if isinstance(ring, list) else set()

    def boot(self, kills=()):
        """One process lifetime. kills: {(update_id, phase)} cut points, with
        phase in pre_guard/post_guard/mid_process/pre_clear/post_clear."""
        kills = set(kills)
        try:
            for uid in self.batch:
                if (uid, "pre_guard") in kills:
                    raise _KillSignal()
                self.sightings[uid] = self.sightings.get(uid, 0) + 1
                if telegram_bot._poison_update_should_skip(self.bot, uid):
                    if str(uid) in self._processed_ring():
                        # Redelivery of already-completed work: skipped
                        # quietly, no attempt counted, no notice.
                        self.redelivered.append(uid)
                        continue
                    # Cut-point invariant: a poison skip may only ever happen
                    # above the documented fatal-attempt threshold.
                    assert self._attempts().get(str(uid), 0) > telegram_bot.POISON_UPDATE_MAX_ATTEMPTS
                    if uid not in self.skipped:
                        # First skip comes after exactly MAX fatal attempts.
                        assert self.sightings[uid] == telegram_bot.POISON_UPDATE_MAX_ATTEMPTS + 1
                    self.skipped.append(uid)
                    continue
                if (uid, "post_guard") in kills:
                    raise _KillSignal()
                if uid in self.poison or (uid, "mid_process") in kills:
                    raise _KillSignal()  # _process_update dies
                self.processed.append(uid)
                if (uid, "pre_clear") in kills:
                    raise _KillSignal()
                telegram_bot._poison_update_clear(uid)
                if (uid, "post_clear") in kills:
                    raise _KillSignal()
        except _KillSignal:
            self._invariants()
            return "killed"      # offset never confirmed -> full redelivery
        self._invariants()
        self.batch = []          # next getUpdates confirms the offset
        return "confirmed"

    def run_to_confirmation(self, boot_kills=()):
        script = list(boot_kills)
        for n in range(self.MAX_BOOTS):
            kills = script[n] if n < len(script) else ()
            if self.boot(kills) == "confirmed":
                return n + 1
        raise AssertionError("batch never confirmed: the service is crash-looping")

    def _invariants(self):
        # The durable ledger stays bounded at every cut point.
        assert len(self._attempts()) <= 20


def _notices(bot):
    return [m for m in bot.sent if "crashed me repeatedly" in m["text"]]


_MAX = telegram_bot.POISON_UPDATE_MAX_ATTEMPTS


@pytest.mark.parametrize("name,batch,poison,boot_kills,expect", [
    # Two unrelated kills while a healthy update is in flight: still below
    # the threshold, so the third boot processes it and clears the count.
    ("healthy_survives_kills_below_threshold",
     [101], (), [{(101, "post_guard")}, {(101, "mid_process")}],
     {"boots": 3, "processed": [101], "skipped": [], "notices": 0,
      "final_keys": set()}),
    # Documented tradeoff pinned: the guard counts sightings, so an update
    # repeatedly killed in the tiny post-process/pre-clear window is
    # declared poison after MAX fatal-looking attempts (fail-safe beats a
    # crash loop; the update's side effects DID complete each time).
    ("kill_in_clear_window_hits_threshold_by_design",
     [102], (), [{(102, "pre_clear")}] * _MAX,
     {"boots": _MAX + 1, "processed": [102] * _MAX, "skipped": [102],
      "notices": 1, "final_keys": {"102"}}),
    # Kills AFTER the clear no longer re-run side effects: the durable
    # processed ring recognizes the redelivery, skips it quietly (no
    # attempt counted, no notice), and the batch confirms immediately.
    ("post_clear_kills_never_accumulate",
     [103], (), [{(103, "post_clear")}] * 5,
     {"boots": 2, "processed": [103], "skipped": [], "notices": 0,
      "final_keys": set(), "redelivered": [103]}),
    # The 2026-07-16 incident shape: a lone poison update crash-loops MAX
    # times, is skipped on the next delivery, and the user is told once.
    ("poison_alone_skipped_after_max_attempts",
     [201], {201}, [],
     {"boots": _MAX + 1, "processed": [], "skipped": [201], "notices": 1,
      "final_keys": {"201"}}),
    # Healthy batch-mate BEFORE the poison: processed exactly once; its
    # redeliveries during the poison crash loop are ring-skipped (the old
    # at-least-once duplication is gone), never falsely declared poison.
    ("healthy_then_poison",
     [301, 302], {302}, [],
     {"boots": _MAX + 1, "processed": [301], "skipped": [302],
      "notices": 1, "final_keys": {"302"}, "redelivered": [301] * _MAX}),
    # Healthy batch-mate AFTER the poison: starved during the crash loop,
    # then processed exactly once as soon as the poison is skipped.
    ("poison_then_healthy",
     [401, 402], {401}, [],
     {"boots": _MAX + 1, "processed": [402], "skipped": [401], "notices": 1,
      "final_keys": {"401"}}),
    # Two poisons in one batch resolve sequentially, one notice each.
    ("two_poisons_resolve_sequentially",
     [501, 502], {501, 502}, [],
     {"boots": 2 * (_MAX + 1) - 1, "processed": [],
      "skipped": [501] * (_MAX + 1) + [502], "notices": 2,
      "final_keys": {"501", "502"}}),
])
def test_poison_kill_sequences(monkeypatch, tmp_path, name, batch, poison, boot_kills, expect):
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    sim = _PoisonLoopSim(bot, batch, poison)

    boots = sim.run_to_confirmation(boot_kills)

    assert boots == expect["boots"]
    assert sim.processed == expect["processed"]
    assert sim.redelivered == expect.get("redelivered", [])
    assert sorted(sim.skipped) == sorted(expect["skipped"])
    # No update outside the expected set is ever skipped (healthy updates
    # survive every scripted kill sequence).
    assert set(sim.skipped) <= set(expect["skipped"])
    assert len(_notices(bot)) == expect["notices"]
    assert set(sim._attempts()) == expect["final_keys"]


def test_poison_kill_sequences_batch_survives_full_cycle_after_resolution(monkeypatch, tmp_path):
    """After a poison update is skipped and its batch confirmed, the NEXT
    batch starts from a clean slate: fresh healthy updates process normally
    and the stale poison entry alone remains in the bounded ledger."""
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    sim = _PoisonLoopSim(bot, [601, 602], {601})
    sim.run_to_confirmation()

    follow_up = _PoisonLoopSim(bot, [603, 604])
    follow_up.run_to_confirmation()
    assert follow_up.processed == [603, 604]
    assert follow_up.skipped == []
    assert set(follow_up._attempts()) == {"601"}
    assert len(_notices(bot)) == 1


def test_poison_ledger_survives_update_id_reset(monkeypatch, tmp_path):
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    # 20 stale entries from the pre-reset era (e.g. old skipped poisons),
    # in the legacy bare-int format the recency pruner must tolerate.
    service_health.save(
        {"update_attempts": {str(900000000 + i): 4 for i in range(20)}},
        tmp_path / "health.json")

    # Post-reset ids restart small; this update kills the process on every
    # delivery, so its count must eventually cross the threshold.
    results = [telegram_bot._poison_update_should_skip(bot, 7) for _ in range(10)]
    assert any(results)


def test_redelivered_processed_update_is_skipped_quietly(monkeypatch, tmp_path):
    """should_skip -> clear -> should_skip(same id): the durable processed
    ring recognizes the redelivery (a kill landed after the clear but before
    the offset was confirmed), skips it without counting an attempt, and
    sends the user nothing."""
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    assert telegram_bot._poison_update_should_skip(bot, 4242) is False
    telegram_bot._poison_update_clear(4242)

    assert telegram_bot._poison_update_should_skip(bot, 4242) is True
    data = service_health.load(tmp_path / "health.json")
    assert "4242" not in (data.get("update_attempts") or {})  # attempts untouched
    assert "4242" in data["processed_updates"]
    assert bot.sent == []                                     # no poison notice


def test_processed_update_ring_prunes_to_newest_256(monkeypatch, tmp_path):
    _poison_setup(monkeypatch, tmp_path)
    for update_id in range(1000, 1300):
        telegram_bot._poison_update_clear(update_id)
    ring = service_health.load(tmp_path / "health.json")["processed_updates"]
    assert len(ring) == 256
    assert "1299" in ring and "1000" not in ring


def test_clean_shutdown_stamp_consumed_exactly_once(monkeypatch, tmp_path):
    """The stamp excuses exactly ONE boot: the boot that follows it consumes
    it, and the next boot without a fresh stamp counts as a crash."""
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    health = tmp_path / "health.json"

    telegram_bot._record_clean_shutdown()
    assert "last_clean_shutdown" in service_health.load(health)

    telegram_bot._record_boot_and_maybe_alert(bot)  # boot 1: clean restart
    data = service_health.load(health)
    assert "last_clean_shutdown" not in data        # consumed
    assert data["recent_crash_boots"] == []

    telegram_bot._record_boot_and_maybe_alert(bot)  # boot 2: no stamp -> crash
    assert len(service_health.load(health)["recent_crash_boots"]) == 1
    assert bot.sent == []                           # 1 crash boot: no alert yet


def test_poison_guard_fails_open_when_disk_is_full(monkeypatch, tmp_path):
    """ENOSPC persisting the attempt count must never raise into the poll
    loop and must fail OPEN (process, don't skip): a full disk may cost the
    crash-loop protection, but it must not take the bot down."""
    import pathlib
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()

    def _enospc(self, *a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pathlib.Path, "write_text", _enospc)
    for _ in range(telegram_bot.POISON_UPDATE_MAX_ATTEMPTS + 3):
        assert telegram_bot._poison_update_should_skip(bot, 888) is False
    telegram_bot._poison_update_clear(888)  # must not raise either
    assert bot.sent == []


def test_crash_boot_tripwire_sees_boot_sweep_crashes(monkeypatch, tmp_path):
    _poison_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(telegram_bot, "BOT_TOKEN", "t")
    monkeypatch.setattr(telegram_bot, "GEMINI_API_KEY", "g")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "k")
    monkeypatch.setattr(telegram_bot, "TelegramBot", lambda token: FakeBot())
    monkeypatch.setattr(telegram_bot, "genai", SimpleNamespace(Client=lambda api_key: object()))
    monkeypatch.setattr(telegram_bot.signal, "signal", lambda *a, **k: None)

    def _sweep_crash(bot):
        raise RuntimeError("boot sweep crash")

    monkeypatch.setattr(telegram_bot, "_sweep_stranded_pending_uploads", _sweep_crash)

    for _ in range(3):  # systemd Restart=always: rapid crash-boot loop
        with pytest.raises(RuntimeError):
            telegram_bot.main()

    boots = service_health.load(tmp_path / "health.json").get("recent_crash_boots") or []
    assert len(boots) == 3  # the tripwire must see boot-time crash loops


# ─── Outbound-call hang hardening (hunter 6) ──────────────────────────

def test_gemini_hanging_call_cannot_block_past_the_deadline(monkeypatch):
    """google-genai 0.3.0 sends every Gemini HTTP request with NO socket
    timeout, and handle_text_message runs on the MAIN polling thread — a
    black-holed connection used to freeze the whole bot forever (systemd
    Restart=always never fires because the process stays alive). The
    deadline wrapper must abandon a hanging generate_content and raise a
    retryable timeout error instead."""
    import threading as real_threading
    import time as real_time

    monkeypatch.setattr(telegram_bot, "GEMINI_HTTP_DEADLINE_SECONDS", 1)
    release = real_threading.Event()

    class HangingModels:
        def generate_content(self, **kwargs):
            release.wait()  # a black-holed socket: never returns on its own
            return SimpleNamespace(text="{}")

    client = SimpleNamespace(models=HangingModels())
    start = real_time.monotonic()
    try:
        with pytest.raises(RuntimeError) as excinfo:
            telegram_bot._generate_content_with_deadline(
                client, model="m", contents=["x"])
        elapsed = real_time.monotonic() - start
    finally:
        release.set()  # unstick the abandoned daemon worker for a clean exit

    assert elapsed < 5, f"caller was blocked {elapsed:.1f}s past a 1s deadline"
    assert "timeout" in str(excinfo.value).lower()
    # The existing retry machinery must see this as retryable network trouble.
    assert telegram_bot._classify_gemini_error(excinfo.value) == "network_service"


def test_every_generate_content_call_routes_through_the_deadline_wrapper():
    """No call site may bypass the deadline: any bare client.models
    .generate_content outside the wrapper would reintroduce the unbounded
    hang on that path."""
    import ast
    import inspect

    source = inspect.getsource(telegram_bot)
    tree = ast.parse(source)
    wrapper_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_generate_content_with_deadline":
            wrapper_lines = set(range(node.lineno, node.end_lineno + 1))
    assert wrapper_lines, "expected the _generate_content_with_deadline wrapper to exist"
    offenders = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "generate_content"
        and node.lineno not in wrapper_lines
    ]
    assert offenders == [], f"generate_content called outside the deadline wrapper at lines {offenders}"


def test_get_updates_timeout_takes_the_quiet_branch(monkeypatch):
    import requests

    bot = telegram_bot.TelegramBot("test-token")

    def raise_timeout(*args, **kwargs):
        raise requests.exceptions.ConnectTimeout("connect timed out")

    monkeypatch.setattr(bot.session, "post", raise_timeout)
    slept = []
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: slept.append(s))

    assert bot.get_updates(timeout=1) == []
    assert slept == []  # the quiet-timeout branch, not the 5s penalty path


def test_get_updates_contains_network_errors_without_advancing_offset(monkeypatch):
    """Pins today's containment: a hard network error returns [] (after the
    fixed 5s backoff sleep), never raises, and never advances the long-poll
    offset — so the failed batch is re-polled, not silently skipped."""
    import requests

    bot = telegram_bot.TelegramBot("test-token")
    bot.offset = 41

    def raise_err(*args, **kwargs):
        raise requests.exceptions.ConnectionError("network down")

    monkeypatch.setattr(bot.session, "post", raise_err)
    slept = []
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: slept.append(s))

    assert bot.get_updates() == []
    assert bot.offset == 41
    assert slept == [5]  # fixed 5s today — no exponential backoff (see hunter 6 proposal)


# ─── Telegram update-shape fuzzing: _process_update / handle_callback_query (hunter 3) ───


class ShapeBot(FakeBot):
    """FakeBot + per-file_id photo bytes so distinct fuzz photos hash apart."""

    def get_file(self, file_id):
        return ("shape-bytes-" + str(file_id)).encode()

    def _redact(self, value):
        return str(value)


def _shape_setup(monkeypatch, tmp_path):
    """Route every _process_update side effect at test doubles.

    Returns the list that records (chat_id, text) calls into the (stubbed)
    NL text handler, so tests can assert what did/didn't reach it.
    """
    text_calls = []
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(database, "get_android_timezone", lambda *a, **k: "+0800")
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))
    monkeypatch.setattr(
        telegram_bot, "analyze_food_photo",
        lambda client, image_bytes: {"is_food": True, "meal_description": "Shape meal",
                                     "total_calories": 111},
    )
    monkeypatch.setattr(
        telegram_bot, "handle_text_message_safe",
        lambda gemini_client, bot, chat_id, text: text_calls.append((chat_id, text)),
    )
    # Fresh per-test dict (auto-restored) so stashed pendings never leak
    # into other tests via the module global.
    monkeypatch.setattr(telegram_bot, "_pending_nl_deletes", {})
    return text_calls


@pytest.mark.parametrize("update", [
    {"update_id": 1, "edited_message": {"chat": {"id": 12345}, "text": "edited food text"}},
    {"update_id": 2, "channel_post": {"chat": {"id": -1001}, "text": "post"}},
    {"update_id": 3, "my_chat_member": {"chat": {"id": 12345}}},
    {"update_id": 4, "message_reaction": {"chat": {"id": 12345}}},
    {},
], ids=["edited_message", "channel_post", "my_chat_member", "message_reaction", "empty-update"])
def test_process_update_ignores_non_message_update_kinds(monkeypatch, tmp_path, update):
    """Design pin: updates carrying no message/callback_query are silently
    ignored — including edited_message, so editing a food text does NOT
    re-process it (design note, not a crash)."""
    text_calls = _shape_setup(monkeypatch, tmp_path)
    bot = ShapeBot()
    telegram_bot._process_update(object(), bot, update)
    assert bot.sent == [] and bot.answered == [] and text_calls == []


def test_process_update_string_chat_id_is_unauthorized_and_inert(monkeypatch, tmp_path):
    """chat.id "12345" (str) != 12345 (int) fails closed: refused with the
    not-authorized reply, the text never reaches the NL handler, and the
    attached photo is never downloaded (FakeBot has no get_file, so touching
    the photo path would raise)."""
    text_calls = _shape_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    telegram_bot._process_update(object(), bot, {
        "update_id": 7,
        "message": {"chat": {"id": "12345"}, "from": {"first_name": "Stringy"},
                    "text": "hi", "photo": [{"file_id": "px-1"}]},
    })
    assert len(bot.sent) == 1 and "not authorized" in bot.sent[0]["text"]
    assert text_calls == []


@pytest.mark.parametrize("payload", [
    {"sticker": {"file_id": "s1"}},
    {"voice": {"file_id": "v1", "duration": 2}},
    {"video_note": {"file_id": "vn1"}},
    {"location": {"latitude": 1.0, "longitude": 2.0}},
    {"contact": {"phone_number": "+15550100", "first_name": "A"}},
    {"poll": {"id": "p1", "question": "lunch?"}},
    {"entities": [{"type": "bold", "offset": 0, "length": 2}]},
    {"document": {"file_id": "d1"}},
    {"document": {"file_id": "d2", "mime_type": "application/pdf"}},
    {"photo": []},
], ids=["sticker", "voice", "video_note", "location", "contact", "poll",
        "entities-no-text", "document-no-mime", "document-non-image", "photo-empty-list"])
def test_process_update_silently_ignores_non_text_non_image_messages(monkeypatch, tmp_path, payload):
    """Every content type the bot doesn't handle is explicitly ignored: no
    reply, no NL call, no raise. Includes document without mime_type and an
    empty photo array (neither counts as an image)."""
    text_calls = _shape_setup(monkeypatch, tmp_path)
    bot = ShapeBot()
    message = {"chat": {"id": 12345}, "from": {"first_name": "U"}}
    message.update(payload)
    telegram_bot._process_update(object(), bot, {"update_id": 10, "message": message})
    assert bot.sent == [] and text_calls == []


def test_photo_caption_is_silently_dropped_design_pin(mock_db, monkeypatch, tmp_path):
    """DESIGN NOTE (pin): a photo's caption is ignored end-to-end. Users
    naturally caption food photos ("half portion, no rice"); today the photo
    is analyzed WITHOUT that context, the caption never reaches the NL text
    handler, and nothing echoes it back. If captions ever get wired in,
    this pin should be updated deliberately."""
    text_calls = _shape_setup(monkeypatch, tmp_path)
    bot = ShapeBot()
    telegram_bot._process_update(object(), bot, {
        "update_id": 16,
        "message": {"chat": {"id": 12345}, "from": {"first_name": "U"},
                    "photo": [{"file_id": "cap-1"}],
                    "caption": "half portion, no rice"},
    })
    assert len(_wide_recent_meals(12345)) == 1   # the photo itself was processed
    assert text_calls == []                       # caption never reached the NL handler
    assert all("half portion" not in m["text"] for m in bot.sent)


def test_photo_caption_cannot_execute_commands(mock_db, monkeypatch, tmp_path):
    """Security-relevant corollary of the caption drop: a caption that LOOKS
    like a command ("/clear_failed latest confirm") is not dispatched as one —
    the message is handled purely as a photo."""
    text_calls = _shape_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(telegram_bot, "clear_failed_uploads",
                        lambda *a, **k: pytest.fail("caption must not reach command dispatch"))
    bot = ShapeBot()
    telegram_bot._process_update(object(), bot, {
        "update_id": 25,
        "message": {"chat": {"id": 12345},
                    "photo": [{"file_id": "cap-cmd"}],
                    "caption": "/clear_failed latest confirm"},
    })
    assert len(_wide_recent_meals(12345)) == 1
    assert text_calls == []


def test_photo_wins_over_text_when_both_present(mock_db, monkeypatch, tmp_path):
    """The real API never sends text and photo together, but pin dispatch
    precedence anyway: the photo is processed and the text is dropped."""
    text_calls = _shape_setup(monkeypatch, tmp_path)
    bot = ShapeBot()
    telegram_bot._process_update(object(), bot, {
        "update_id": 15,
        "message": {"chat": {"id": 12345}, "photo": [{"file_id": "both-1"}],
                    "text": "I also typed this"},
    })
    assert len(_wide_recent_meals(12345)) == 1
    assert text_calls == []


@pytest.mark.parametrize("message", [
    pytest.param({"text": "hi"}, id="chat-missing"),
    pytest.param({"chat": None, "text": "hi"}, id="chat-none"),
    pytest.param({"chat": {"id": 12345}, "text": None}, id="text-none"),
    pytest.param({"chat": {"id": 12345}, "text": 42}, id="text-int"),
])
def test_process_update_tolerates_malformed_chat_and_text_fields(monkeypatch, tmp_path, message):
    _shape_setup(monkeypatch, tmp_path)
    bot = ShapeBot()
    # Contract: structurally invalid messages should be IGNORED, not raised.
    telegram_bot._process_update(object(), bot, {"update_id": 90, "message": message})


@pytest.mark.parametrize("payload", [
    pytest.param({"photo": {"file_id": "not-a-list"}}, id="photo-dict-not-list"),
    pytest.param({"photo": [{}]}, id="photo-entry-missing-file_id"),
])
def test_malformed_photo_fields_reach_photo_error_containment(mock_db, monkeypatch, tmp_path, payload):
    _shape_setup(monkeypatch, tmp_path)
    bot = ShapeBot()
    message = {"chat": {"id": 12345}}
    message.update(payload)
    # Contract: either explicitly ignored or answered — never a raise.
    telegram_bot._process_update(object(), bot, {"update_id": 91, "message": message})


def test_callback_query_with_null_data_is_answered(monkeypatch, tmp_path):
    _shape_setup(monkeypatch, tmp_path)
    bot = ShapeBot()
    telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-null", "data": None, "message": {"chat": {"id": 12345}}})
    assert bot.answered  # post-fix expectation: answered (e.g. "Unknown action.")


def test_callback_unauthorized_answers_but_never_executes(mock_db, monkeypatch, tmp_path):
    """A callback from a non-allowed presser (no message context, from.id
    unauthorized) is answered 'Not authorized.' and must not delete meals,
    consume the pending confirmation, send chat messages, or run
    clear_failed_uploads — even with a VALID confirm token."""
    _shape_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(telegram_bot, "clear_failed_uploads",
                        lambda *a, **k: pytest.fail("unauthorized callback must not clear uploads"))
    today = database.user_local_now().date().isoformat()
    database.save_meal(12345, today, "12:00", datetime.now().isoformat(), "t", "h-auth", "f-auth",
                       {"is_food": True, "meal_description": "Guarded meal", "total_calories": 100})
    meal_id = _wide_recent_meals(12345)[0]["id"]
    telegram_bot._pending_nl_deletes[12345] = {
        "ids": [meal_id], "labels": ["Guarded meal"], "at": datetime.now(), "token": "tok-auth1"}

    bot = ShapeBot()
    for data in ("nl_delete_confirm:tok-auth1", "quota_discard:latest", "nl_delete_cancel:tok-auth1"):
        handled = telegram_bot.handle_callback_query(object(), bot, {
            "id": "cb-%s" % data.split(":", 1)[0], "data": data, "from": {"id": 666}})
        assert handled is True

    assert [text for _, text in bot.answered] == ["Not authorized."] * 3
    assert bot.sent == []
    assert len(_wide_recent_meals(12345)) == 1
    assert telegram_bot._pending_nl_deletes[12345]["token"] == "tok-auth1"


@pytest.mark.parametrize("data", [
    "nl_delete_confirm", "nl_delete_confirm:", "nl_delete_cancel", "nl_delete_cancel:",
    "nl_delete_confirm:wrong-token", ":", "bogus:stuff", "x" * 100000,
    "nl_delete_confirm:中文\U0001f35c",
], ids=["confirm-no-colon", "confirm-empty-token", "cancel-no-colon", "cancel-empty-token",
        "confirm-wrong-token", "colon-only", "wrong-prefix", "huge-data", "unicode-token"])
def test_callback_garbage_data_never_deletes_or_raises(mock_db, monkeypatch, tmp_path, data):
    """With a live pending delete, every garbage callback data shape is
    answered without deleting anything and without consuming the pending
    confirmation (its own real buttons must keep working)."""
    _shape_setup(monkeypatch, tmp_path)
    today = database.user_local_now().date().isoformat()
    database.save_meal(12345, today, "12:00", datetime.now().isoformat(), "t", "h-garb", "f-garb",
                       {"is_food": True, "meal_description": "Sturdy meal", "total_calories": 100})
    meal_id = _wide_recent_meals(12345)[0]["id"]
    telegram_bot._pending_nl_deletes[12345] = {
        "ids": [meal_id], "labels": ["Sturdy meal"], "at": datetime.now(), "token": "tokgarb1"}

    bot = ShapeBot()
    handled = telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-garbage", "data": data, "message": {"chat": {"id": 12345}}})

    assert isinstance(handled, bool)
    assert len(bot.answered) == 1  # always acknowledged, spinner never hangs
    assert len(_wide_recent_meals(12345)) == 1
    assert telegram_bot._pending_nl_deletes[12345]["token"] == "tokgarb1"


def test_replayed_confirm_token_cannot_double_delete(mock_db, monkeypatch, tmp_path):
    """Pressing the same Delete button twice deletes once: the second press
    finds no pending confirmation and answers 'Nothing to delete.'"""
    _shape_setup(monkeypatch, tmp_path)
    today = database.user_local_now().date().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_meal(12345, today, "12:00", now_iso, "t", "h-r1", "f-r1",
                       {"is_food": True, "meal_description": "Doomed meal", "total_calories": 100})
    database.save_meal(12345, today, "13:00", now_iso, "t", "h-r2", "f-r2",
                       {"is_food": True, "meal_description": "Safe meal", "total_calories": 200})
    doomed = next(m for m in _wide_recent_meals(12345)
                  if m["analysis"]["meal_description"] == "Doomed meal")
    telegram_bot._pending_nl_deletes[12345] = {
        "ids": [doomed["id"]], "labels": ["Doomed meal"], "at": datetime.now(), "token": "tok-replay1"}
    cb = {"id": "cb-replay", "data": "nl_delete_confirm:tok-replay1",
          "message": {"chat": {"id": 12345}}}

    bot = ShapeBot()
    telegram_bot.handle_callback_query(object(), bot, cb)
    assert [m["analysis"]["meal_description"] for m in _wide_recent_meals(12345)] == ["Safe meal"]

    telegram_bot.handle_callback_query(object(), bot, dict(cb))  # replay
    assert bot.answered[-1][1] == "Nothing to delete."
    assert [m["analysis"]["meal_description"] for m in _wide_recent_meals(12345)] == ["Safe meal"]


def test_stale_buttons_cannot_fire_or_cancel_newer_pending(mock_db, monkeypatch, tmp_path):
    """Buttons from an older confirmation (stale token) neither execute nor
    discard the newer pending set; only the matching token cancels it."""
    _shape_setup(monkeypatch, tmp_path)
    today = database.user_local_now().date().isoformat()
    database.save_meal(12345, today, "12:00", datetime.now().isoformat(), "t", "h-s1", "f-s1",
                       {"is_food": True, "meal_description": "Kept meal", "total_calories": 100})
    meal_id = _wide_recent_meals(12345)[0]["id"]
    telegram_bot._pending_nl_deletes[12345] = {
        "ids": [meal_id], "labels": ["Kept meal"], "at": datetime.now(), "token": "new-tok-1"}
    bot = ShapeBot()

    telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-old-c", "data": "nl_delete_confirm:old-tok-0",
        "message": {"chat": {"id": 12345}}})
    assert "superseded" in bot.answered[-1][1]
    assert len(_wide_recent_meals(12345)) == 1
    assert telegram_bot._pending_nl_deletes[12345]["token"] == "new-tok-1"

    telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-old-x", "data": "nl_delete_cancel:old-tok-0",
        "message": {"chat": {"id": 12345}}})
    assert telegram_bot._pending_nl_deletes[12345]["token"] == "new-tok-1"

    telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-new-x", "data": "nl_delete_cancel:new-tok-1",
        "message": {"chat": {"id": 12345}}})
    assert 12345 not in telegram_bot._pending_nl_deletes
    assert len(_wide_recent_meals(12345)) == 1


def test_expired_confirm_is_discarded_without_deleting(mock_db, monkeypatch, tmp_path):
    """A confirm pressed after the TTL (even with the CORRECT token) deletes
    nothing and drops the pending set."""
    _shape_setup(monkeypatch, tmp_path)
    today = database.user_local_now().date().isoformat()
    database.save_meal(12345, today, "12:00", datetime.now().isoformat(), "t", "h-e1", "f-e1",
                       {"is_food": True, "meal_description": "Aged meal", "total_calories": 100})
    meal_id = _wide_recent_meals(12345)[0]["id"]
    stale_at = datetime.now() - timedelta(seconds=telegram_bot.NL_DELETE_CONFIRM_TTL_SECONDS + 1)
    telegram_bot._pending_nl_deletes[12345] = {
        "ids": [meal_id], "labels": ["Aged meal"], "at": stale_at, "token": "tok-exp1"}

    bot = ShapeBot()
    telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-exp", "data": "nl_delete_confirm:tok-exp1",
        "message": {"chat": {"id": 12345}}})

    assert len(_wide_recent_meals(12345)) == 1
    assert 12345 not in telegram_bot._pending_nl_deletes
    assert any("expired" in m["text"] for m in bot.sent)


def _build_shape_corpus(seed, n):
    """Deterministic fuzz corpus over realistic Telegram update shapes.

    Replay a failure with: update, meta = _build_shape_corpus(seed, n)[i]
    (seed, n, and i are printed in the assertion message).
    Returns (update_dict, meta) pairs; meta drives the invariant checks.
    """
    import random
    rng = random.Random(seed)
    allowed, outsider = 12345, 999999
    texts = ["hello", "ate rice 好吃 \U0001f35a", "   x   ", "/", "/nonexistent",
             "/today", "/recent abc", "a" * 4096, "\u200b\u202enoodles", "42"]
    file_ids = ["fz-%d" % k for k in range(6)]
    cb_data = ["quota_keep:latest", "quota_discard:latest", "nl_delete_confirm:deadbeef",
               "nl_delete_cancel:deadbeef", "nl_delete_confirm:", "nl_delete_confirm",
               "bogus", "y" * 4096, "nl_delete_confirm:中文\U0001f35c", ""]
    kinds = ["text", "photo", "photo_caption", "document_image", "document_other",
             "sticker", "voice", "location", "contact", "poll", "entities",
             "edited_message", "channel_post", "my_chat_member", "message_reaction",
             "callback"]
    corpus = []
    for i in range(n):
        chat_id = allowed if rng.random() < 0.7 else outsider
        kind = rng.choice(kinds)
        update = {}
        if rng.random() < 0.9:
            update["update_id"] = 100000 + i
        meta = {"kind": kind, "authorized": chat_id == allowed,
                "is_callback": False, "is_message": False, "photo_bearing": False}
        if kind == "callback":
            cb = {"id": "cb-%d" % i, "data": rng.choice(cb_data)}
            if rng.random() < 0.5:
                cb["message"] = {"chat": {"id": chat_id}}
            else:
                cb["from"] = {"id": chat_id}
            update["callback_query"] = cb
            meta["is_callback"] = True
        elif kind in ("edited_message", "channel_post", "my_chat_member", "message_reaction"):
            update[kind] = {"chat": {"id": chat_id}, "text": rng.choice(texts)}
        else:
            message = {"chat": {"id": chat_id}}
            if rng.random() < 0.8:
                message["from"] = {"id": chat_id, "first_name": "Fuzz"}
            if rng.random() < 0.2:
                message["forward_origin"] = {"type": "user"}
            if kind == "text":
                message["text"] = rng.choice(texts)
            elif kind in ("photo", "photo_caption"):
                message["photo"] = [{"file_id": rng.choice(file_ids),
                                     "width": 90 * (j + 1), "height": 90 * (j + 1)}
                                    for j in range(rng.randint(1, 3))]
                if kind == "photo_caption":
                    message["caption"] = rng.choice(texts)
                meta["photo_bearing"] = True
            elif kind == "document_image":
                message["document"] = {"file_id": rng.choice(file_ids), "mime_type": "image/jpeg"}
                meta["photo_bearing"] = True
            elif kind == "document_other":
                message["document"] = {"file_id": rng.choice(file_ids),
                                       "mime_type": rng.choice(["application/pdf", ""])}
            elif kind == "sticker":
                message["sticker"] = {"file_id": "st-%d" % i}
            elif kind == "voice":
                message["voice"] = {"file_id": "vc-%d" % i, "duration": 3}
            elif kind == "location":
                message["location"] = {"latitude": 1.5, "longitude": 2.5}
            elif kind == "contact":
                message["contact"] = {"phone_number": "+15550100"}
            elif kind == "poll":
                message["poll"] = {"id": "p-%d" % i, "question": "lunch?"}
            elif kind == "entities":
                message["entities"] = [{"type": "bold", "offset": 0, "length": 2}]
            update["message"] = message
            meta["is_message"] = True
        corpus.append((update, meta))
    return corpus


def test_update_shape_fuzz_realistic_shapes_hold_invariants(mock_db, monkeypatch, tmp_path):
    """Seeded fuzz over realistic Telegram update shapes. Invariants:
    (1) _process_update never raises for shapes the real API can send;
    (2) an unauthorized chat gets exactly the not-authorized reply and its
        text/photos never reach the NL handler or the database;
    (3) every authorized photo-bearing message produces at least one reply
        (processed, duplicate notice, or already-logged notice);
    (4) every callback query is answered exactly once;
    (5) non-message update kinds are complete no-ops."""
    seed, n = 20260716, 250
    text_calls = _shape_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(telegram_bot, "clear_failed_uploads",
                        lambda selector, confirmed=False: "cleared (fuzz stub)")
    corpus = _build_shape_corpus(seed, n)
    for i, (update, meta) in enumerate(corpus):
        bot = ShapeBot()
        calls_before = len(text_calls)
        try:
            telegram_bot._process_update(object(), bot, update)
        except Exception as e:
            pytest.fail("update-shape fuzz raised %r; replay with "
                        "_build_shape_corpus(%d, %d)[%d]; update=%r" % (e, seed, n, i, update))
        context = (seed, i, update)
        if meta["is_callback"]:
            assert len(bot.answered) == 1, context
        elif not meta["is_message"]:
            assert bot.sent == [] and bot.answered == [], context
            assert len(text_calls) == calls_before, context
        elif not meta["authorized"]:
            assert len(bot.sent) == 1 and "not authorized" in bot.sent[0]["text"], context
            assert len(text_calls) == calls_before, context
        elif meta["photo_bearing"]:
            assert bot.sent, context
    assert _wide_recent_meals(999999) == []          # outsider never wrote a meal
    assert all(chat == 12345 for chat, _ in text_calls)


def test_poison_ledger_helpers_tolerate_missing_update_id(monkeypatch, tmp_path):
    """An update without update_id is processed normally: the poison guard
    passes it through and the post-survival clear is a no-op, not a crash."""
    _poison_setup(monkeypatch, tmp_path)
    bot = FakeBot()
    assert telegram_bot._poison_update_should_skip(bot, None) is False
    telegram_bot._poison_update_clear(None)
    assert service_health.load(tmp_path / "health.json").get("update_attempts") in (None, {})
    assert bot.sent == []


# ═══ Hunter 2: adversarial NL-pipeline fuzzing ══════════════════════
# Seeded random corpus of malformed Gemini responses driven through
# handle_text_message, plus targeted probes. Replay a failing corpus
# entry deterministically: payload = _nl_fuzz_payloads()[<index>].
import random

_NL_FUZZ_SEED = 0x5EED2026
_NL_FUZZ_RANDOM_COUNT = 200


def _nl_fuzz_scalar(rng):
    """One JSON-serializable hostile value (json.dumps round-trips them all)."""
    pool = [
        None, True, False, 0, 1, -1, 2, 999999, -999999, 10 ** 400, -(10 ** 400),
        1.5, -2.75, 1e308, float("inf"), float("-inf"), float("nan"),
        "", "0", "2", "1.5", "-1", "999999999999", "abc",
        "<b>粗体</b>", "🍜🍜🍜", "\u200d\u200d\u200d", "\u202egnp.stluser",
        "多" * 64, [], {}, [[]], [0, "1", None], {"名": 1}, [{"a": 1}],
    ]
    return rng.choice(pool)


def _nl_fuzz_analysis(rng):
    if rng.random() < 0.3:
        return _nl_fuzz_scalar(rng)          # analysis as scalar/list/None
    analysis = {"is_food": rng.choice([True, True, False, "yes", 1, None, [True]])}
    if rng.random() < 0.8:
        analysis["meal_description"] = rng.choice(
            ["fuzz 餐", "<i>x</i>", "🍔" * 20, "", 42, None, {"d": 1}])
    if rng.random() < 0.8:
        analysis["total_calories"] = _nl_fuzz_scalar(rng)
    if rng.random() < 0.7:
        analysis["food_items"] = rng.choice([
            "chips", 7, {"name": "x"},
            [{"name": "面", "estimated_calories": _nl_fuzz_scalar(rng)}],
            ["a", {"name": None}, 3],
            [{"name": "x", "estimated_calories": 5}] * 40,
        ])
    return analysis


_NL_FUZZ_INTENTS = [
    "correction", "delete", "new_meal", "log_weight", "log_activity", "chat",
    "multi", "", "CORRECTION", "意图", None, 3, ["delete"], {"i": 1}, True,
]


def _nl_fuzz_action(rng, depth):
    action = {"intent": rng.choice(_NL_FUZZ_INTENTS)}
    if rng.random() < 0.7:
        action["meal_index"] = _nl_fuzz_scalar(rng)
    if rng.random() < 0.7:
        action["meal_indices"] = rng.choice([
            _nl_fuzz_scalar(rng),
            [_nl_fuzz_scalar(rng) for _ in range(rng.randint(0, 4))],
            [0, 0, 0], [{"i": 0}], [True, False],
        ])
    if rng.random() < 0.7:
        action["analysis"] = _nl_fuzz_analysis(rng)
    if rng.random() < 0.5:
        action["reason"] = _nl_fuzz_scalar(rng)
    if rng.random() < 0.5:
        action["reply"] = rng.choice(["ok 好", "", None, {"r": 1}, ["a"], 42])
    for key in ("weight_kg", "active_calories", "steps", "distance_km"):
        if rng.random() < 0.4:
            action[key] = _nl_fuzz_scalar(rng)
    # actions nested up to 3 deep — the executor must not recurse/explode.
    if depth < 3 and rng.random() < 0.35:
        action["actions"] = [
            _nl_fuzz_action(rng, depth + 1) if rng.random() < 0.7 else _nl_fuzz_scalar(rng)
            for _ in range(rng.randint(0, 6))
        ]
    if rng.random() < 0.1:
        action["键%d" % rng.randint(0, 9)] = _nl_fuzz_scalar(rng)   # unicode keys
    return action


def _nl_fuzz_payloads(seed=_NL_FUZZ_SEED, count=_NL_FUZZ_RANDOM_COUNT):
    """Deterministic corpus: handcrafted probes first, then `count` seeded
    random payloads. Same seed → identical corpus, so any failure reproduces
    by index."""
    handcrafted = [
        {"intent": ["correction"], "meal_index": 0},                    # intent as list
        {"intent": {"i": 1}},                                           # intent as dict
        {"intent": 3},                                                  # intent as int
        {"intent": "correction", "meal_index": True,
         "analysis": {"is_food": True, "total_calories": 9}},           # bool index
        {"intent": "correction", "meal_index": "999999999999999999",
         "analysis": {"is_food": True, "total_calories": 9}},           # huge str index
        {"intent": "correction", "meal_index": 0,
         "analysis": {"is_food": True, "total_calories": "780"}},       # str calories
        {"intent": "correction", "meal_index": "<i>9</i>",
         "analysis": {"is_food": True, "total_calories": 9}},           # HTML junk index
        {"intent": "delete", "meal_indices": [{"i": 0}, {"j": 1}]},     # dict indices
        {"intent": "delete", "meal_indices": [0, 0, 0]},                # triplicate index
        {"intent": "multi", "actions": [{"intent": "multi", "actions": [
            {"intent": "multi", "actions": [{"intent": "chat", "reply": "deep"}]}]}]},
        {"intent": "log_weight", "weight_kg": "72.5"},                  # str weight
        {"intent": "log_weight", "weight_kg": [72.5]},                  # list weight
        {"intent": "chat", "reply": {"text": "hi"}},                    # reply as dict
        {"intent": "multi", "actions": [                                # duplicate intents
            {"intent": "log_weight", "weight_kg": 72.5},
            {"intent": "log_weight", "weight_kg": 73.5}]},
        [{"intent": "correction", "meal_index": 0,                      # mixed valid+junk
          "analysis": {"is_food": True, "total_calories": 123,
                       "meal_description": "ok"}}, "junk", 42],
    ]
    rng = random.Random(seed)
    payloads = list(handcrafted)
    for _ in range(count):
        roll = rng.random()
        if roll < 0.15:
            payloads.append(_nl_fuzz_scalar(rng))            # junk top-level type
        elif roll < 0.35:
            payloads.append([                                 # bare JSON array
                _nl_fuzz_action(rng, 1) if rng.random() < 0.8 else _nl_fuzz_scalar(rng)
                for _ in range(rng.randint(0, 6))
            ])
        else:
            payloads.append(_nl_fuzz_action(rng, 0))          # single/multi object
    return payloads


def _count_new_meal_dicts(node):
    """Upper bound on rows _nl_new_meal could insert for this payload."""
    if isinstance(node, dict):
        own = 1 if node.get("intent") == "new_meal" else 0
        return own + sum(_count_new_meal_dicts(v) for v in node.values())
    if isinstance(node, list):
        return sum(_count_new_meal_dicts(v) for v in node)
    return 0


def test_nl_pipeline_seeded_fuzz_never_crashes_and_guards_the_db(mock_db, monkeypatch, tmp_path):
    """~215 malformed Gemini responses (wrong types at every key, nested
    actions, mixed valid+invalid) must each: never raise out of
    handle_text_message, always send the user SOMETHING, never delete meal
    rows (deletes require confirmation), and only grow the meals table via
    new_meal actions. DB state is re-seeded per corpus entry so every entry
    replays standalone: _nl_fuzz_payloads()[<failing index>]."""
    _compound_nl_setup(monkeypatch, tmp_path)
    failures = []
    for i, payload in enumerate(_nl_fuzz_payloads()):
        with database._connect() as conn:
            conn.execute("DELETE FROM meals")
            conn.commit()
        telegram_bot._pending_nl_deletes.clear()
        _seed_three_meals()
        before = len(database.get_meals(CHAT, "1970-01-01", "9999-12-31"))

        bot = PhotoBot()  # the executor's per-action failure path uses bot._redact
        try:
            telegram_bot.handle_text_message(
                _intent_client(payload), bot, CHAT, "fuzz probe %d" % i)
        except Exception as e:  # noqa: BLE001 - the invariant under test
            failures.append((i, "raised %s: %s" % (type(e).__name__, e), payload))
            continue

        after = len(database.get_meals(CHAT, "1970-01-01", "9999-12-31"))
        if not bot.sent:
            failures.append((i, "sent nothing to the user", payload))
        if after < before:
            failures.append((i, "rows deleted without confirmation (%d -> %d)"
                             % (before, after), payload))
        allowed = min(_count_new_meal_dicts(payload), telegram_bot.NL_MAX_ACTIONS)
        if after - before > allowed:
            failures.append((i, "rows grew by %d but only %d new_meal action(s) present"
                             % (after - before, allowed), payload))
        if len(telegram_bot._pending_nl_deletes) > 1:
            failures.append((i, "more than one pending-delete slot", payload))

    detail = "; ".join("[corpus %d] %s payload=%r" % (i, why, p)
                       for i, why, p in failures[:5])
    assert not failures, ("%d corpus entries violated invariants "
                          "(replay: _nl_fuzz_payloads()[i]): %s"
                          % (len(failures), detail))


# ── Targeted probes: correct behavior pinned as passing tests ────────

def test_nl_delete_triplicate_index_dedupes_to_one_meal(mock_db, monkeypatch, tmp_path):
    """meal_indices [0, 0, 0] must stage exactly ONE meal for deletion (the
    handler dedupes via a set), with a single confirmation prompt and no row
    touched before confirm."""
    _compound_nl_setup(monkeypatch, tmp_path)
    snapshot = _seed_three_meals()

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "delete", "meal_indices": [0, 0, 0], "reason": "dup"}),
        bot, 12345, "delete the first meal")

    pending = telegram_bot._pending_nl_deletes[12345]
    assert pending["ids"] == [snapshot[0]["id"]]
    confirms = [m for m in bot.sent if m.get("reply_markup")]
    assert len(confirms) == 1
    assert "Delete 1 meal(s)?" in confirms[0]["text"]
    assert len(_wide_recent_meals(12345)) == 3


def test_nl_compound_five_corrections_same_meal_last_wins(mock_db, monkeypatch, tmp_path):
    """Five corrections aimed at the SAME meal all execute in order (each
    with its own reply); the row ends at the last correction's values."""
    _compound_nl_setup(monkeypatch, tmp_path)
    snapshot = _seed_three_meals()
    actions = [
        {"intent": "correction", "meal_index": 0,
         "analysis": {"is_food": True, "meal_description": "v%d" % i,
                      "total_calories": 200 + i}}
        for i in range(5)
    ]

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "multi", "actions": actions}), bot, 12345, "fix meal 1 five ways")

    after = {m["id"]: m for m in _wide_recent_meals(12345)}
    assert len(after) == 3
    assert after[snapshot[0]["id"]]["analysis"]["meal_description"] == "v4"
    assert after[snapshot[0]["id"]]["analysis"]["total_calories"] == 204
    assert sum("Corrected meal 1" in m["text"] for m in bot.sent) == 5
    assert not any("failed" in m["text"] for m in bot.sent)


def test_nl_nested_multi_does_not_recurse_or_execute_inner_actions(mock_db, monkeypatch, tmp_path):
    """{"intent":"multi","actions":[{"intent":"multi",...}]} must not recurse:
    the inner wrapper falls through to the chat fallback (its nested actions
    are NOT executed), something is sent, and no rows change."""
    _compound_nl_setup(monkeypatch, tmp_path)
    _seed_three_meals()
    payload = {"intent": "multi", "actions": [
        {"intent": "multi", "actions": [
            {"intent": "multi", "actions": [
                {"intent": "new_meal",
                 "analysis": {"is_food": True, "meal_description": "deep burger",
                              "total_calories": 900}},
                {"intent": "chat", "reply": "deep"},
            ]}]}]}

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(payload), bot, 12345, "nested request")

    assert bot.sent, "the user must still get a reply"
    assert not any("deep" in m["text"] for m in bot.sent), \
        "actions nested below one wrapper level must not execute"
    assert len(_wide_recent_meals(12345)) == 3
    assert 12345 not in telegram_bot._pending_nl_deletes


def test_nl_correction_reply_escapes_hostile_description(mock_db, monkeypatch, tmp_path):
    """A corrected meal_description carrying HTML/emoji/RTL/zero-width chars
    is stored verbatim but always HTML-escaped in the Telegram reply."""
    _compound_nl_setup(monkeypatch, tmp_path)
    snapshot = _seed_three_meals()
    evil = "<script>alert(1)</script>\u200d\u202e\U0001F35C"

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "correction", "meal_index": 0,
         "analysis": {"is_food": True, "meal_description": evil,
                      "total_calories": 300}}), bot, 12345, "fix meal 1")

    assert not any("<script>" in m["text"] for m in bot.sent)
    assert any("&lt;script&gt;" in m["text"] for m in bot.sent)
    after = {m["id"]: m for m in _wide_recent_meals(12345)}
    assert after[snapshot[0]["id"]]["analysis"]["meal_description"] == evil


def test_nl_new_meal_ten_thousand_food_items_completes(mock_db, monkeypatch, tmp_path):
    """A hallucinated 10k-item food_items list must neither crash nor hang:
    the meal saves and the (huge) confirmation is produced — the real
    send_message truncates it to Telegram's limit."""
    _compound_nl_setup(monkeypatch, tmp_path)
    items = [{"name": "item%d" % i, "estimated_calories": 1,
              "protein_g": 0, "carbs_g": 0, "fat_g": 0} for i in range(10000)]

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "new_meal",
         "analysis": {"is_food": True, "meal_description": "mega buffet",
                      "total_calories": 10000, "food_items": items}}),
        bot, 12345, "logged a buffet")

    assert len(_wide_recent_meals(12345)) == 1
    assert any("Added new manual meal" in m["text"] for m in bot.sent)


@pytest.mark.parametrize("weird_text", [
    "／today",                       # fullwidth-slash command lookalike
    "饭" * 4096,                     # 4096-char CJK message
    "\u200d" * 16,                  # nothing but zero-width joiners
    "<b>bold</b> & [link](x) 100%",  # Telegram HTML/markdown metacharacters
])
def test_process_update_routes_weird_texts_to_nl_pipeline(mock_db, monkeypatch, tmp_path, weird_text):
    """Command lookalikes and hostile plain text are NOT commands: they take
    exactly one trip through the Gemini NL pipeline and get a reply."""
    _compound_nl_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    client, calls = _counting_client({"intent": "chat", "reply": "回复 ok"})

    bot = FakeBot()
    telegram_bot._process_update(client, bot, {"message": {
        "chat": {"id": 12345}, "from": {"first_name": "R"}, "text": weird_text}})

    assert len(calls) == 1, "expected exactly one Gemini classification"
    assert any("回复 ok" in m["text"] for m in bot.sent)


# ── Hunter-confirmed defects, fixed in C2 and pinned as regressions ──

@pytest.mark.parametrize("hostile_items", [
    "chips", ["a", "b"], [{"name": 123}], {"name": "x"}, 7,
])
def test_nl_saved_hostile_food_items_must_not_poison_every_later_text(
        mock_db, monkeypatch, tmp_path, hostile_items):
    _compound_nl_setup(monkeypatch, tmp_path)

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "new_meal",
         "analysis": {"is_food": True, "meal_description": "fuzz snack",
                      "food_items": hostile_items}}), bot, 12345, "ate a snack")
    assert bot.sent

    bot2 = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "chat", "reply": "still alive"}), bot2, 12345, "hello again")
    assert any("still alive" in m["text"] for m in bot2.sent)


def test_nl_correction_hostile_food_items_must_not_poison_every_later_text(
        mock_db, monkeypatch, tmp_path):
    _compound_nl_setup(monkeypatch, tmp_path)
    _seed_three_meals()

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "correction", "meal_index": 0,
         "analysis": {"is_food": True, "meal_description": "corrected",
                      "total_calories": 500, "food_items": "just soup"}}),
        bot, 12345, "meal 1 was soup")
    assert bot.sent

    bot2 = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "chat", "reply": "still alive"}), bot2, 12345, "hello again")
    assert any("still alive" in m["text"] for m in bot2.sent)


def test_nl_correction_string_calories_must_not_claim_failure_after_writing(
        mock_db, monkeypatch, tmp_path):
    _compound_nl_setup(monkeypatch, tmp_path)
    snapshot = _seed_three_meals()
    original = snapshot[0]["analysis"]["total_calories"]

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "correction", "meal_index": 0,
         "analysis": {"is_food": True, "meal_description": "烧鸭饭",
                      "total_calories": "780"}}), bot, 12345, "meal 1 was 780 kcal duck")

    after = {m["id"]: m for m in _wide_recent_meals(12345)}
    row_changed = after[snapshot[0]["id"]]["analysis"].get("total_calories") != original
    told_failed = any("That request failed" in m["text"] for m in bot.sent)
    assert not (row_changed and told_failed), \
        "the row was rewritten yet the user was told the request failed"


@pytest.mark.parametrize("empty_reply", [None, ""])
def test_nl_chat_null_or_empty_reply_must_still_say_something(
        mock_db, monkeypatch, tmp_path, empty_reply):
    _compound_nl_setup(monkeypatch, tmp_path)

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "chat", "reply": empty_reply}), bot, 12345, "hello there")

    assert bot.sent and all(m["text"].strip() for m in bot.sent), \
        "an empty-text message is unsendable on real Telegram - the user hears nothing"


def test_nl_invalid_index_reply_escapes_model_supplied_value(mock_db, monkeypatch, tmp_path):
    _compound_nl_setup(monkeypatch, tmp_path)
    _seed_three_meals()

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "correction", "meal_index": "<i>9</i>",
         "analysis": {"is_food": True, "total_calories": 9}}), bot, 12345, "fix meal 9")

    assert any("Invalid meal index" in m["text"] for m in bot.sent)
    assert not any("<i>9</i>" in m["text"] for m in bot.sent), \
        "raw model output must be HTML-escaped before interpolation"


def test_nl_delete_scalar_string_indices_must_not_explode_per_character(
        mock_db, monkeypatch, tmp_path):
    _compound_nl_setup(monkeypatch, tmp_path)
    _seed_three_meals()

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": "delete", "meal_indices": "12", "reason": "remove meal 12"}),
        bot, 12345, "delete meal 12")

    assert 12345 not in telegram_bot._pending_nl_deletes, \
        "a scalar '12' must not stage meals 1 and 2 for deletion"


# ─── Loops, livelocks & admission control (hunter 1) ─────────────────


def test_get_updates_persistent_conflict_backs_off_with_a_cap(monkeypatch):
    import requests

    bot = telegram_bot.TelegramBot("test-token")

    class Conflict:
        status_code = 409

        def raise_for_status(self):
            raise requests.HTTPError("409 Conflict", response=self)

    monkeypatch.setattr(bot.session, "post", lambda *a, **k: Conflict())
    slept = []
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: slept.append(s))

    for _ in range(12):
        assert bot.get_updates(timeout=1) == []  # containment must stay

    assert len(slept) == 12
    assert slept == sorted(slept), "backoff must grow monotonically to its cap"
    assert max(slept) >= 60, f"12 consecutive failures never escalated: {slept}"
    assert max(slept) <= 300, "backoff must stay capped (~5 min) so recovery is quick"


def test_gemini_retry_loop_is_bounded_with_clamped_sleeps(monkeypatch, tmp_path):
    """Pins that the Gemini retry machinery cannot spin: a persistently
    retryable 429 runs exactly max_attempts attempts, and a hostile/huge
    server-advertised retryDelay is clamped to GEMINI_RETRY_MAX_DELAY_SECONDS
    per sleep (the background thread holds the photo bytes during these
    sleeps, so the total stall is bounded too)."""
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot.claude_analyzer, "is_configured", lambda: False)

    attempts = []

    def always_429(client, image_bytes):
        attempts.append(1)
        raise RuntimeError("429 RESOURCE_EXHAUSTED; retryDelay: '86400s'")

    monkeypatch.setattr(telegram_bot, "_analyze_food_photo_once", always_429)
    slept = []
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: slept.append(s))

    result = telegram_bot.analyze_food_photo_with_retries(object(), b"img", max_attempts=3)

    assert result is None
    assert len(attempts) == 3  # bounded: no infinite retry
    assert slept == [telegram_bot.GEMINI_RETRY_MAX_DELAY_SECONDS] * 2  # 86400s clamped


def _photo_fanout_rig(monkeypatch):
    """Telegram-photo fan-out rig with REAL (recorded) threads and a gated
    analyzer, so tests can observe how many analyses are in flight at once."""
    import threading as real_threading

    state = {"inflight": 0, "peak": 0, "entered": 0}
    lock = real_threading.Lock()
    gate = real_threading.Event()
    spawned = []

    class RecordingThread(real_threading.Thread):
        def start(self):
            spawned.append(self)
            super().start()

    def gated_analyze(client, image_bytes):
        with lock:
            state["inflight"] += 1
            state["entered"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        gate.wait(timeout=15)  # simulates the _CLI_LOCK convoy / a slow CLI run
        with lock:
            state["inflight"] -= 1
        return {"is_food": False}

    marks = []
    monkeypatch.setattr(telegram_bot, "threading",
                        SimpleNamespace(Thread=RecordingThread))
    monkeypatch.setattr(telegram_bot, "analyze_food_photo", gated_analyze)
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda *a: False)
    monkeypatch.setattr(telegram_bot, "_stage_api_upload", lambda *a, **k: None)
    monkeypatch.setattr(database, "reserve_photo_hash", lambda *a, **k: True)
    monkeypatch.setattr(database, "release_photo_hash", lambda *a, **k: None)
    monkeypatch.setattr(database, "mark_photo_hash_status",
                        lambda *a, **k: marks.append(a))

    class AlbumBot(FakeBot):
        def get_file(self, file_id):
            return b"\xff\xd8\xff" + file_id.encode()

    return AlbumBot(), state, gate, spawned, marks


def _wait_until(predicate, deadline_seconds=5.0):
    import time as real_time

    end = real_time.monotonic() + deadline_seconds
    while real_time.monotonic() < end:
        if predicate():
            return True
        real_time.sleep(0.01)
    return predicate()


def test_photo_message_returns_before_analysis_completes(monkeypatch):
    """Pins the non-blocking design: handle_photo_message returns as soon as
    the analysis thread is spawned, so a slow Claude/Gemini run can never
    stall the long-poll loop; the background thread finishes the job."""
    bot, state, gate, spawned, marks = _photo_fanout_rig(monkeypatch)
    try:
        handled = telegram_bot.handle_photo_message(
            object(), bot, 12345, {"photo": [{"file_id": "p0"}]})
        assert handled is True                       # returned while gated
        assert _wait_until(lambda: state["entered"] == 1)
        assert state["inflight"] == 1                # analysis still in flight
    finally:
        gate.set()
        for t in spawned:
            t.join(timeout=10)
    assert not any(t.is_alive() for t in spawned)
    assert any(m[2] == "skipped" for m in marks)     # background path completed


def test_photo_fanout_concurrency_is_capped(monkeypatch):
    bot, state, gate, spawned, _marks = _photo_fanout_rig(monkeypatch)
    try:
        for i in range(10):
            telegram_bot.handle_photo_message(
                object(), bot, 12345, {"photo": [{"file_id": f"album{i}"}]})
        # Wait for the fan-out to settle: either all 10 enter (today's
        # unbounded behavior) or a capped pool stalls the rest at the gate.
        _wait_until(lambda: state["entered"] == 10, deadline_seconds=3.0)
        peak = state["peak"]
    finally:
        gate.set()
        for t in spawned:
            t.join(timeout=10)
    assert not any(t.is_alive() for t in spawned)
    assert peak <= 4, (
        f"{peak} photo analyses were in flight at once, each pinning its full "
        f"image bytes on a 1GB host — fan-out must be admission-controlled"
    )


def test_upload_raw_body_chunked_buffering_is_capped(mock_db, monkeypatch, tmp_path):
    class CountingBody(io.RawIOBase):
        """Counts how much of the body the server actually pulls into memory."""

        def __init__(self, total):
            self.total = total
            self.pos = 0

        def readable(self):
            return True

        def readinto(self, b):
            n = min(len(b), self.total - self.pos)
            if n <= 0:
                return 0
            head = b"\xff\xd8\xff" if self.pos == 0 else b""
            b[:n] = (head + b"A" * n)[:n]
            self.pos += n
            return n

    app, bot = _upload_app(monkeypatch, tmp_path, analysis="forbidden")
    monkeypatch.setattr(telegram_bot, "MAX_API_UPLOAD_BYTES", 8192)

    builder = _EnvironBuilder(path="/upload", method="POST", data=b"",
                              content_type="application/octet-stream",
                              headers={"X-API-Key": "test-upload-key",
                                       "Transfer-Encoding": "chunked"})
    environ = builder.get_environ()
    environ.pop("CONTENT_LENGTH", None)
    environ.pop("HTTP_CONTENT_LENGTH", None)
    environ["wsgi.input_terminated"] = True
    body = CountingBody(1024 * 1024)  # 1MB body against an 8KB cap
    environ["wsgi.input"] = io.BufferedReader(body)

    app_iter, status, _headers = _run_wsgi_app(app, environ, buffered=True)
    payload = json.loads(b"".join(app_iter))

    assert int(status.split()[0]) == 413            # the OUTCOME is already right
    assert payload == {"error": "Photo too large"}
    assert bot.sent == []
    # ... but the whole body was pulled into RAM first. Allow generous
    # BufferedReader slack over MAX+1; 1MB read means unbounded buffering.
    assert body.pos <= 3 * 8192, (
        f"server buffered {body.pos} bytes of a chunked body against an "
        f"8192-byte cap — reads must be capped, not sliced after the fact"
    )


# ── Hunter 2 (continued): defect found BY the seeded fuzzer ──────────

@pytest.mark.parametrize("unhashable_intent", [["delete"], {"i": 1}])
def test_nl_unhashable_intent_with_actions_must_not_raise(
        mock_db, monkeypatch, tmp_path, unhashable_intent):
    _compound_nl_setup(monkeypatch, tmp_path)
    _seed_three_meals()

    bot = FakeBot()
    telegram_bot.handle_text_message(_intent_client(
        {"intent": unhashable_intent,
         "actions": [{"intent": "chat", "reply": "compound ok"}]}),
        bot, 12345, "please do both things")

    # Fix-agnostic core invariant: no exception, and the user hears back.
    # (Design intent: an unrecognized wrapper intent defers to its actions,
    # so ideally the chat action's "compound ok" is what gets sent.)
    assert bot.sent, "the user must receive a reply, not a contained crash"
