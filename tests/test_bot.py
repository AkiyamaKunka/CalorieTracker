import io
import pytest
import json
import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import requests

from telegram_bot import format_food_result, format_daily_totals, is_duplicate_photo, analyze_food_photo
import telegram_bot
import database
from utils import parse_ai_json

def test_format_food_result():
    analysis = {
        "is_food": True,
        "meal_description": "Grilled Chicken Salad",
        "total_calories": 350,
        "total_protein_g": 40,
        "total_carbs_g": 15,
        "total_fat_g": 10
    }
    
    result = format_food_result(12345, analysis)
    
    assert "Grilled Chicken Salad" in result
    assert "350 kcal" in result
    assert "P: 40g" in result
    assert "C: 15g" in result
    assert "F: 10g" in result

def test_parse_ai_json_accepts_wrapped_json():
    result = parse_ai_json("Here is the result:\n```json\n{\"is_food\": true}\n```\nThanks")

    assert result == {"is_food": True}

def test_format_food_result_escapes_dynamic_html(monkeypatch):
    monkeypatch.setattr(telegram_bot, "format_daily_totals", lambda chat_id: "")
    analysis = {
        "is_food": True,
        "meal_description": "Fish <rice>",
        "food_items": [{"name": "Sauce & spice", "estimated_calories": "10 < 20"}],
        "total_calories": "100 < 200",
    }

    result = format_food_result(12345, analysis)

    assert "Fish &lt;rice&gt;" in result
    assert "Sauce &amp; spice" in result
    assert "10 &lt; 20" in result

def test_format_food_result_missing_fields():
    # If the LLM misses some fields, it shouldn't crash
    analysis = {
        "is_food": True,
        "meal_description": "Apple",
        "total_calories": 95,
        # missing protein, carbs, fat
    }
    
    result = format_food_result(12345, analysis)

    assert "Apple" in result
    assert "95 kcal" in result
    assert "P: 0g" in result  # defaults to 0 for missing macros

def test_format_food_result_warns_on_item_total_mismatch(monkeypatch):
    """Item calories contradicting the meal total get a correction hint."""
    monkeypatch.setattr(telegram_bot, "format_daily_totals", lambda chat_id: "")
    analysis = {
        "is_food": True,
        "meal_description": "Big bowl",
        "food_items": [
            {"name": "rice", "estimated_calories": 100},
            {"name": "egg", "estimated_calories": 35},
        ],
        "total_calories": 1335,
    }

    result = format_food_result(12345, analysis)

    assert "⚠️ Item calories sum to ~135 kcal" in result
    assert "meal total is ~1335 kcal" in result
    assert "reply with a correction" in result

def test_format_food_result_no_mismatch_warning_when_consistent(monkeypatch):
    monkeypatch.setattr(telegram_bot, "format_daily_totals", lambda chat_id: "")
    analysis = {
        "is_food": True,
        "meal_description": "Bowl",
        "food_items": [
            {"name": "rice", "estimated_calories": 300},
            {"name": "egg", "estimated_calories": 100},
        ],
        "total_calories": 420,
    }

    result = format_food_result(12345, analysis)

    assert "Item calories sum" not in result

def test_format_daily_totals(monkeypatch):
    # Mock the database.get_meals function to return controlled data
    def mock_get_meals(chat_id):
        return [
            {
                "analysis": {
                    "is_food": True,
                    "total_calories": 300, 
                    "total_protein_g": 20, 
                    "total_carbs_g": 30, 
                    "total_fat_g": 10
                }
            },
            {
                "analysis": {
                    "is_food": True,
                    "total_calories": 500, 
                    "total_protein_g": 30, 
                    "total_carbs_g": 40, 
                    "total_fat_g": 20
                }
            }
        ]
        
    monkeypatch.setattr(telegram_bot, "get_todays_meals", mock_get_meals)
    
    result = format_daily_totals(12345)
    
    assert "800 kcal" in result
    assert "P: 50g" in result
    assert "C: 70g" in result
    assert "F: 30g" in result

# --- NEW EDGE CASE TESTS ---

def test_is_duplicate_photo(monkeypatch):
    # Test duplicate within 5 minute window
    now = datetime.now()
    recent = now - timedelta(minutes=2)
    
    def mock_get_todays_meals(chat_id):
        return [
            {
                "image_hash": "hash_xyz",
                "timestamp": recent.isoformat(),
            }
        ]
    monkeypatch.setattr(telegram_bot, "get_todays_meals", mock_get_todays_meals)
    
    assert is_duplicate_photo(12345, "hash_xyz") is True
    # Different hash should return False
    assert is_duplicate_photo(12345, "hash_abc") is False

def test_is_not_duplicate_photo(monkeypatch):
    # Test duplicate outside 5 minute window
    now = datetime.now()
    old = now - timedelta(minutes=10)
    
    def mock_get_todays_meals(chat_id):
        return [
            {
                "image_hash": "hash_xyz",
                "timestamp": old.isoformat(),
            }
        ]
    monkeypatch.setattr(telegram_bot, "get_todays_meals", mock_get_todays_meals)
    
    assert is_duplicate_photo(12345, "hash_xyz") is False

def test_analyze_food_photo_invalid_json(monkeypatch):
    # Mock the Gemini client
    class MockResponse:
        text = "I am sorry, I can't do that."
    
    class MockModels:
        def generate_content(self, **kwargs):
            return MockResponse()
    
    class MockClient:
        models = MockModels()
        
    # Analyze an invalid image bytes representation
    result = analyze_food_photo(MockClient(), b"fake_image_bytes")
    # Should gracefully return None instead of crashing on JSON decode
    assert result is None

def test_analyze_food_photo_passes_native_json_config(monkeypatch, tmp_path):
    """The photo call site must request Gemini's native JSON mode."""
    captured = {}

    class MockModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text='{"is_food": false}')

    class MockClient:
        models = MockModels()

    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "service_health.json")
    monkeypatch.setattr(telegram_bot, "_prepare_image_for_gemini", lambda image_bytes: object())

    result = analyze_food_photo(MockClient(), b"fake_image_bytes")

    assert result == {"is_food": False}
    assert captured["config"].response_mime_type == "application/json"

def test_handle_text_message_passes_native_json_config(monkeypatch, tmp_path):
    """The text-handler call site must request Gemini's native JSON mode."""
    captured = {}
    sent = []

    class FakeBot:
        def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None):
            sent.append(text)

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text=json.dumps({"intent": "chat", "reply": "hello"}))

    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "get_recent_meals", lambda chat_id, days: [])

    telegram_bot.handle_text_message(SimpleNamespace(models=FakeModels()), FakeBot(), 12345, "hi")

    assert captured["config"].response_mime_type == "application/json"
    assert sent

def test_prepare_image_for_gemini_downscales_and_applies_exif():
    """Real bytes: a large EXIF-rotated JPEG comes back <=1024px, RGB, upright."""
    src = telegram_bot.Image.new("RGB", (2000, 1500), (200, 120, 80))
    exif = src.getexif()
    exif[0x0112] = 6  # EXIF orientation 6: rotate 90° CW to display upright
    buf = io.BytesIO()
    src.save(buf, format="JPEG", exif=exif)

    prepared = telegram_bot._prepare_image_for_gemini(buf.getvalue())

    assert max(prepared.size) <= 1024
    assert prepared.mode == "RGB"
    # Orientation applied: the rotated landscape source displays as portrait.
    assert prepared.size == (768, 1024)

def test_prepare_image_for_gemini_raises_original_error_for_broken_images():
    """Unreadable bytes must raise what the old un-resized path raised."""
    with pytest.raises(telegram_bot.Image.UnidentifiedImageError):
        telegram_bot._prepare_image_for_gemini(b"definitely not an image")

def test_analyze_food_photo_retries_retryable_errors(monkeypatch):
    class MockResponse:
        text = '{"is_food": false}'

    class MockModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise Exception("429 RESOURCE_EXHAUSTED retryDelay: '0s'")
            return MockResponse()

    class MockClient:
        def __init__(self):
            self.models = MockModels()

    client = MockClient()
    monkeypatch.setattr(telegram_bot, "_prepare_image_for_gemini", lambda image_bytes: object())
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda seconds: None)

    result = telegram_bot.analyze_food_photo_with_retries(client, b"fake_image_bytes")

    assert result == {"is_food": False}
    assert client.models.calls == 2

def test_classify_gemini_quota_error():
    error = Exception("429 RESOURCE_EXHAUSTED quota exceeded")

    assert telegram_bot._classify_gemini_error(error) == "quota_rate_limit"

def test_daily_quota_error_stops_retry_and_sets_pause(monkeypatch, tmp_path):
    class MockModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            raise Exception(
                "429 RESOURCE_EXHAUSTED quota exceeded for "
                "generativelanguage.googleapis.com/generate_content_free_tier_requests; "
                "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"
            )

    class MockClient:
        def __init__(self):
            self.models = MockModels()

    client = MockClient()
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "service_health.json")
    monkeypatch.setattr(telegram_bot, "_prepare_image_for_gemini", lambda image_bytes: object())
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda seconds: pytest.fail("daily quota should not retry"))

    result = telegram_bot.analyze_food_photo_with_retries(client, b"fake_image_bytes")
    health = json.loads((tmp_path / "service_health.json").read_text())

    assert result is None
    assert client.models.calls == 1
    assert health["gemini"]["last_error_type"] == "daily_quota_exhausted"
    assert health["gemini"]["quota_pause_until"]

def test_retry_failed_upload_waits_when_daily_quota_paused(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed_uploads"
    failed_dir.mkdir()
    failed_path = failed_dir / "20260628T232700_deadbeefcafe.jpg"
    failed_path.write_bytes(b"fake image")
    health_path = tmp_path / "service_health.json"
    health_path.write_text(json.dumps({
        "gemini": {
            "quota_pause_until": (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds"),
            "quota_pause_reason": "daily free-tier quota exhausted",
        }
    }))

    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", health_path)
    monkeypatch.setattr(
        telegram_bot,
        "analyze_food_photo_with_retries",
        lambda *args, **kwargs: pytest.fail("quota pause should prevent Gemini retry"),
    )

    result = telegram_bot.retry_failed_upload(object(), "deadbeefcafe")

    assert "Retry held" in result
    assert failed_path.exists()

def test_reconcile_ignores_failed_saved_hash_prefix(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed_uploads"
    failed_dir.mkdir()
    full_hash = "deadbeefcafe1234567890abcdef1234"
    other_hash = "0123456789abcdef0123456789abcdef"
    (failed_dir / "20260628T232700_deadbeefcafe.jpg").write_bytes(b"fake image")

    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)

    missing = telegram_bot._reconcile_missing_hashes([full_hash, other_hash], set())

    assert full_hash not in missing
    assert other_hash in missing

def test_reconcile_ignores_reserved_hashes(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed_uploads"
    failed_dir.mkdir()
    reserved_hash = "abcdef1234567890abcdef1234567890"
    other_hash = "0123456789abcdef0123456789abcdef"

    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)

    missing = telegram_bot._reconcile_missing_hashes(
        [reserved_hash, other_hash],
        set(),
        reserved_hashes={reserved_hash},
    )

    assert reserved_hash not in missing
    assert other_hash in missing

def test_saved_upload_decision_message_and_buttons(tmp_path, monkeypatch):
    health_path = tmp_path / "service_health.json"
    health_path.write_text(json.dumps({
        "gemini": {
            "quota_pause_until": (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"),
            "quota_pause_reason": "daily free-tier quota exhausted",
        }
    }))
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", health_path)
    failed_path = tmp_path / "20260628T232700_deadbeefcafe.jpg"

    message = telegram_bot._format_saved_upload_decision_message(
        failed_path,
        "deadbeefcafe1234567890abcdef1234",
        source_label="Android",
    )
    markup = telegram_bot._saved_upload_decision_markup("deadbeefcafe")

    assert "Wait and analyze later" in message
    assert "Give up/delete" in message
    assert "/retry_failed deadbeefcafe" in message
    assert "/clear_failed deadbeefcafe confirm" in message
    assert markup["inline_keyboard"][0][0]["callback_data"] == "quota_keep:deadbeefcafe"
    assert markup["inline_keyboard"][1][0]["callback_data"] == "quota_discard:deadbeefcafe"

def test_quota_discard_callback_deletes_failed_upload(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed_uploads"
    failed_dir.mkdir()
    failed_path = failed_dir / "20260628T232700_deadbeefcafe.jpg"
    failed_path.write_bytes(b"fake image")
    sent = []
    callbacks = []

    class FakeBot:
        def answer_callback_query(self, callback_query_id, text=""):
            callbacks.append((callback_query_id, text))

        def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None):
            sent.append(text)

    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    # Keep the test hermetic: don't tombstone rows in the real database.
    monkeypatch.setattr(
        telegram_bot.database, "discard_failed_photo_hashes_by_prefix",
        lambda chat_id, prefix: 0,
    )

    handled = telegram_bot.handle_callback_query(
        object(),
        FakeBot(),
        {
            "id": "callback-id",
            "data": "quota_discard:deadbeefcafe",
            "message": {"chat": {"id": 12345}},
        },
    )

    assert handled is True
    assert callbacks
    assert sent
    assert not failed_path.exists()

def test_run_gemini_probe_success(monkeypatch, tmp_path):
    class MockResponse:
        text = "OK"

    class MockModels:
        def generate_content(self, **kwargs):
            return MockResponse()

    class MockClient:
        models = MockModels()

    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "service_health.json")

    result = telegram_bot.run_gemini_probe(MockClient())

    assert "Gemini probe OK" in result
    assert "OK" in result

def test_format_failed_uploads(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed_uploads"
    failed_dir.mkdir()
    (failed_dir / "20260627T010101_deadbeefcafe.jpg").write_bytes(b"img")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)

    result = telegram_bot.format_failed_uploads()

    assert "Failed Saved Uploads" in result
    assert "deadbeefcafe" in result

def test_retry_failed_upload_not_food_removes_file(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed_uploads"
    failed_dir.mkdir()
    failed_path = failed_dir / "20260627T010101_deadbeefcafe.jpg"
    failed_path.write_bytes(b"fake image")
    marked = []
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot.database, "meal_image_hash_exists", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: True)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status", lambda *args, **kwargs: marked.append((args, kwargs)))
    monkeypatch.setattr(telegram_bot, "analyze_food_photo_with_retries", lambda client, image_bytes: {"is_food": False})

    result = telegram_bot.retry_failed_upload(object(), "latest")

    assert "not food" in result
    assert not failed_path.exists()
    assert marked
    assert marked[0][0][2] == "skipped"

def test_retry_failed_upload_save_error_keeps_file(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed_uploads"
    failed_dir.mkdir()
    failed_path = failed_dir / "20260627T010101_deadbeefcafe.jpg"
    failed_path.write_bytes(b"fake image")
    marked = []
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot.database, "meal_image_hash_exists", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: True)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status", lambda *args, **kwargs: marked.append((args, kwargs)))
    monkeypatch.setattr(telegram_bot, "analyze_food_photo_with_retries", lambda client, image_bytes: {
        "is_food": True,
        "meal_description": "Soup",
        "total_calories": 100,
    })
    monkeypatch.setattr(telegram_bot, "save_meal", lambda *args: (_ for _ in ()).throw(RuntimeError("db locked")))

    result = telegram_bot.retry_failed_upload(object(), "latest")

    assert "saving failed" in result
    assert failed_path.exists()
    assert marked
    assert marked[0][0][2] == "failed"

def test_retry_failed_upload_duplicate_removes_file_without_analysis(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed_uploads"
    failed_dir.mkdir()
    failed_path = failed_dir / "20260627T010101_deadbeefcafe.jpg"
    failed_path.write_bytes(b"fake image")
    marked = []
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot.database, "meal_image_hash_exists", lambda chat_id, image_hash: True)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status", lambda *args, **kwargs: marked.append((args, kwargs)))
    monkeypatch.setattr(
        telegram_bot,
        "analyze_food_photo_with_retries",
        lambda *args, **kwargs: pytest.fail("duplicates should not hit Gemini"),
    )

    result = telegram_bot.retry_failed_upload(object(), "latest")

    assert "already logged" in result
    assert not failed_path.exists()
    assert marked
    assert marked[0][0][2] == "saved"

def test_retry_failed_upload_already_reserved_keeps_file(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed_uploads"
    failed_dir.mkdir()
    failed_path = failed_dir / "20260627T010101_deadbeefcafe.jpg"
    failed_path.write_bytes(b"fake image")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot.database, "meal_image_hash_exists", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        telegram_bot,
        "analyze_food_photo_with_retries",
        lambda *args, **kwargs: pytest.fail("reserved retries should not hit Gemini"),
    )

    result = telegram_bot.retry_failed_upload(object(), "latest")

    assert "already being processed" in result
    assert failed_path.exists()

def test_retry_failed_upload_logged_marks_hash_saved(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed_uploads"
    failed_dir.mkdir()
    failed_path = failed_dir / "20260627T010101_deadbeefcafe.jpg"
    failed_path.write_bytes(b"fake image")
    marked = []
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot.database, "meal_image_hash_exists", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: True)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status", lambda *args, **kwargs: marked.append((args, kwargs)))
    monkeypatch.setattr(telegram_bot, "analyze_food_photo_with_retries", lambda client, image_bytes: {
        "is_food": True,
        "meal_description": "Latte",
        "total_calories": 120,
    })
    monkeypatch.setattr(telegram_bot, "save_meal", lambda *args: 42)

    result = telegram_bot.retry_failed_upload(object(), "latest")

    assert "Latte" in result
    assert not failed_path.exists()
    assert marked
    assert marked[0][0][2] == "saved"
    assert marked[0][0][3] == 42

def test_android_vpn_status_from_request_headers():
    from flask import Flask

    app = Flask(__name__)
    with app.test_request_context(
        "/ping",
        method="POST",
        headers={"X-VPN-Active": "false", "X-VPN-Check": "no_vpn_interface"},
    ):
        vpn_active, vpn_check = telegram_bot._android_vpn_status_from_request()

    assert vpn_active is False
    assert vpn_check == "no_vpn_interface"

def test_authorized_api_request_requires_configured_key(monkeypatch):
    from flask import Flask

    app = Flask(__name__)
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "")
    with app.test_request_context("/ping", method="POST", headers={"X-API-Key": "anything"}):
        assert telegram_bot._authorized_api_request() is False

    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "test-upload-key")
    with app.test_request_context("/ping", method="POST", headers={"X-API-Key": "test-upload-key"}):
        assert telegram_bot._authorized_api_request() is True

    with app.test_request_context("/ping", method="POST", headers={"X-API-Key": "wrong"}):
        assert telegram_bot._authorized_api_request() is False

def test_remote_ip_matches_known_vpn():
    assert telegram_bot._remote_ip_matches_known_vpn("79.127.245.213") is True
    assert telegram_bot._remote_ip_matches_known_vpn("203.0.113.10") is False

def test_remote_vpn_evidence_accepts_any_non_off_country(monkeypatch):
    monkeypatch.setattr(telegram_bot, "_remote_ip_country_code", lambda remote_ip: "US")

    evidence, detail = telegram_bot._remote_vpn_evidence("203.0.113.10")

    assert evidence == "vpn"
    assert detail == "country:US"

def test_remote_ip_country_cache_prunes_expired_entries(monkeypatch):
    """A full cache of expired entries is emptied before the new entry lands."""
    # NB: documentation IPs like 203.0.113.x are ipaddress.is_private and get
    # rejected before the cache, so use a genuinely public IP here.
    monkeypatch.setattr(telegram_bot, "VPN_GEO_LOOKUP_ENABLED", True)
    monkeypatch.setattr(
        telegram_bot.requests, "get",
        lambda url, params=None, timeout=None: SimpleNamespace(
            json=lambda: {"status": "success", "countryCode": "jp", "query": "93.184.216.34"},
        ),
    )
    expired_at = telegram_bot.time.time() - telegram_bot.VPN_GEO_CACHE_TTL_SECONDS - 60
    telegram_bot._remote_ip_country_cache.clear()
    for i in range(300):
        telegram_bot._remote_ip_country_cache[f"expired-{i}"] = {"country": "US", "time": expired_at}

    try:
        country = telegram_bot._remote_ip_country_code("93.184.216.34")

        assert country == "JP"
        assert len(telegram_bot._remote_ip_country_cache) <= telegram_bot.GEO_CACHE_MAX_ENTRIES
        assert telegram_bot._remote_ip_country_cache["93.184.216.34"]["country"] == "JP"
    finally:
        telegram_bot._remote_ip_country_cache.clear()

def test_remote_ip_country_cache_drops_oldest_fresh_entries_when_full(monkeypatch):
    """With no expired entries to prune, the oldest fresh entries are evicted."""
    monkeypatch.setattr(telegram_bot, "VPN_GEO_LOOKUP_ENABLED", True)
    monkeypatch.setattr(
        telegram_bot.requests, "get",
        lambda url, params=None, timeout=None: SimpleNamespace(
            json=lambda: {"status": "success", "countryCode": "jp", "query": "93.184.216.34"},
        ),
    )
    now_ts = telegram_bot.time.time()
    telegram_bot._remote_ip_country_cache.clear()
    for i in range(300):
        # fresh-0 is the newest entry, fresh-299 the oldest.
        telegram_bot._remote_ip_country_cache[f"fresh-{i}"] = {"country": "US", "time": now_ts - i}

    try:
        country = telegram_bot._remote_ip_country_code("93.184.216.34")

        assert country == "JP"
        assert len(telegram_bot._remote_ip_country_cache) <= telegram_bot.GEO_CACHE_MAX_ENTRIES
        assert "93.184.216.34" in telegram_bot._remote_ip_country_cache
        assert "fresh-0" in telegram_bot._remote_ip_country_cache
        assert "fresh-299" not in telegram_bot._remote_ip_country_cache
    finally:
        telegram_bot._remote_ip_country_cache.clear()

def test_android_unreliable_vpn_check_does_not_warn_without_direct_evidence(monkeypatch):
    from flask import Flask

    sent_messages = []

    class FakeBot:
        def send_message(self, chat_id, text):
            sent_messages.append((chat_id, text))

    app = Flask(__name__)
    monkeypatch.setattr(telegram_bot, "TRUSTED_PROXY_ENABLED", True)
    monkeypatch.setattr(telegram_bot, "_last_android_vpn_warning_at", None)
    monkeypatch.setattr(telegram_bot, "_remote_ip_country_code", lambda remote_ip: None)

    with app.test_request_context(
        "/ping",
        method="POST",
        headers={
            "X-VPN-Active": "false",
            "X-VPN-Check": "no_vpn_interface",
            "X-VPN-Check-Reliable": "false",
            "X-Forwarded-For": "203.0.113.10",
        },
    ):
        telegram_bot.maybe_warn_android_vpn_inactive(FakeBot(), "/ping")

    assert sent_messages == []

def test_android_direct_country_still_warns(monkeypatch):
    from flask import Flask

    sent_messages = []

    class FakeBot:
        def send_message(self, chat_id, text):
            sent_messages.append((chat_id, text))

    app = Flask(__name__)
    monkeypatch.setattr(telegram_bot, "TRUSTED_PROXY_ENABLED", True)
    monkeypatch.setattr(telegram_bot, "_last_android_vpn_warning_at", None)
    monkeypatch.setattr(telegram_bot, "_remote_ip_country_code", lambda remote_ip: "CN")

    with app.test_request_context(
        "/ping",
        method="POST",
        headers={
            "X-VPN-Active": "false",
            "X-VPN-Check": "no_vpn_interface",
            "X-VPN-Check-Reliable": "false",
            "X-Forwarded-For": "203.0.113.10",
        },
    ):
        telegram_bot.maybe_warn_android_vpn_inactive(FakeBot(), "/ping")

    assert sent_messages
    assert "Android VPN appears OFF" in sent_messages[0][1]

def test_api_upload_device_name_from_ios_headers():
    from flask import Flask

    app = Flask(__name__)
    with app.test_request_context(
        "/upload",
        method="POST",
        headers={
            "X-Client-Platform": "iOS",
            "X-Device-Name": "Robert iPhone",
            "User-Agent": "Shortcuts/1 CFNetwork",
        },
    ):
        assert telegram_bot._api_upload_device_name_from_request() == "Robert iPhone"

def test_api_upload_processing_hash_reservation():
    image_hash = "hash_for_ios_double_fire"
    telegram_bot._finish_api_upload_processing(image_hash)

    assert telegram_bot._begin_api_upload_processing(image_hash) is True
    assert telegram_bot._begin_api_upload_processing(image_hash) is False

    telegram_bot._finish_api_upload_processing(image_hash)
    assert telegram_bot._begin_api_upload_processing(image_hash) is True
    telegram_bot._finish_api_upload_processing(image_hash)

def test_ios_vpn_required_warning(monkeypatch):
    from flask import Flask

    sent_messages = []

    class FakeBot:
        def send_message(self, chat_id, text):
            sent_messages.append((chat_id, text))

    app = Flask(__name__)
    monkeypatch.setattr(telegram_bot, "TRUSTED_PROXY_ENABLED", True)
    monkeypatch.setattr(telegram_bot, "_last_ios_vpn_warning_at", None)
    monkeypatch.setattr(telegram_bot, "_remote_ip_country_code", lambda remote_ip: "CN")

    with app.test_request_context(
        "/upload",
        method="POST",
        headers={
            "X-VPN-Required": "true",
            "X-Forwarded-For": "203.0.113.10",
        },
    ):
        telegram_bot.maybe_warn_ios_vpn_unverified(FakeBot(), "/upload")

    assert sent_messages
    assert "iPhone VPN may be OFF" in sent_messages[0][1]

def test_ios_vpn_required_no_warning_for_non_off_country(monkeypatch):
    from flask import Flask

    sent_messages = []

    class FakeBot:
        def send_message(self, chat_id, text):
            sent_messages.append((chat_id, text))

    app = Flask(__name__)
    monkeypatch.setattr(telegram_bot, "TRUSTED_PROXY_ENABLED", True)
    monkeypatch.setattr(telegram_bot, "_last_ios_vpn_warning_at", None)
    monkeypatch.setattr(telegram_bot, "_remote_ip_country_code", lambda remote_ip: "JP")

    with app.test_request_context(
        "/upload",
        method="POST",
        headers={
            "X-VPN-Required": "true",
            "X-Forwarded-For": "203.0.113.10",
        },
    ):
        telegram_bot.maybe_warn_ios_vpn_unverified(FakeBot(), "/upload")

    assert sent_messages == []

def test_format_food_result_not_food():
    analysis = {
        "is_food": False
    }
    result = format_food_result(12345, analysis)
    assert "🚫 No food detected" in result

def test_format_daily_totals_empty(monkeypatch):
    # If the user hasn't logged anything today
    def mock_get_meals_empty(chat_id):
        return []
        
    monkeypatch.setattr(telegram_bot, "get_todays_meals", mock_get_meals_empty)
    
    result = format_daily_totals(12345)
    
    # Should not crash and should show empty string for daily totals
    assert result == ""
    
    # Let's also test format_today_summary
    from telegram_bot import format_today_summary
    summary = format_today_summary(12345)
    assert "No meals logged yet" in summary

def test_handle_text_message_correction_invalid_index(monkeypatch, tmp_path):
    sent = []

    class FakeBot:
        def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None):
            sent.append(text)

    class FakeModels:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text=json.dumps({
                "intent": "correction",
                "meal_index": 5,
                "analysis": {"is_food": True, "total_calories": 100},
            }))

    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "get_recent_meals", lambda chat_id, days: [
        {"id": 1, "analysis": {"is_food": True}},
        {"id": 2, "analysis": {"is_food": True}},
    ])
    monkeypatch.setattr(
        telegram_bot.database,
        "update_meal_analysis",
        lambda *args: pytest.fail("out-of-range index must not update anything"),
    )

    telegram_bot.handle_text_message(SimpleNamespace(models=FakeModels()), FakeBot(), 12345, "fix meal 6")

    assert any("Invalid meal index" in message for message in sent)


def test_format_queue_status_lists_pending_and_failed(monkeypatch, tmp_path):
    pending_dir = tmp_path / "pending"
    failed_dir = tmp_path / "failed"
    pending_dir.mkdir()
    failed_dir.mkdir()
    (pending_dir / "pending.jpg").write_bytes(b"pending")
    (failed_dir / "failed.jpg").write_bytes(b"failed")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", pending_dir)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)

    result = telegram_bot.format_queue_status()

    assert "Pending analysis: 1" in result
    assert "Failed saved: 1" in result
    assert "pending.jpg" in result
    assert "failed.jpg" in result


def test_clear_failed_upload_requires_confirm_and_removes_latest(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    old_file = failed_dir / "old_deadbeef.jpg"
    new_file = failed_dir / "new_deadbeef.jpg"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)

    warning = telegram_bot.clear_failed_uploads("latest", confirmed=False)
    assert "Confirmation required" in warning
    assert old_file.exists()
    assert new_file.exists()

    result = telegram_bot.clear_failed_uploads("latest", confirmed=True)

    assert "Removed 1" in result
    assert not new_file.exists()
    assert old_file.exists()


def test_retry_all_failed_uploads_logs_food_and_removes_not_food(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    first = failed_dir / "first.jpg"
    second = failed_dir / "second.jpg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)

    analyses = [
        {
            "is_food": True,
            "meal_description": "Noodles",
            "total_calories": 450,
            "total_protein_g": 20,
            "total_carbs_g": 60,
            "total_fat_g": 12,
        },
        {"is_food": False},
    ]
    saved = []
    monkeypatch.setattr(telegram_bot, "analyze_food_photo_with_retries", lambda client, image_bytes: analyses.pop(0))
    monkeypatch.setattr(telegram_bot, "save_meal", lambda *args: saved.append(args) or len(saved))
    monkeypatch.setattr(telegram_bot.database, "meal_image_hash_exists", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot.database, "reserve_photo_hash", lambda *args, **kwargs: True)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status", lambda *args, **kwargs: None)

    result = telegram_bot.retry_all_failed_uploads(object(), limit=2)

    assert "Logged: 1" in result
    assert "Not food: 1" in result
    assert len(saved) == 1
    assert not first.exists()
    assert not second.exists()


def test_format_report_status_from_health(monkeypatch, tmp_path):
    health_path = tmp_path / "service_health.json"
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", health_path)

    telegram_bot._record_report_health(True, "2026-06-27", report_path="/tmp/report.md")

    result = telegram_bot.format_report_status()
    assert "Daily Report Status" in result
    assert "2026-06-27" in result
    assert "OK" in result


def test_generate_report_for_command_records_success(monkeypatch, tmp_path):
    import daily_report

    health_path = tmp_path / "service_health.json"
    report_path = tmp_path / "report_2026-06-27.md"
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", health_path)
    monkeypatch.setattr(daily_report, "generate_report", lambda target_date: "<b>Report</b>")
    monkeypatch.setattr(daily_report, "save_report_file", lambda target_date, report: report_path)

    result = telegram_bot.generate_report_for_command("2026-06-27")
    health = json.loads(health_path.read_text())

    assert "Manual Daily Report" in result
    assert health["daily_report"]["last_ok"] is True
    assert health["daily_report"]["last_target_date"] == "2026-06-27"


def test_format_recent_logs_escapes_journal_output(monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="line with <bad> & chars", stderr="")

    monkeypatch.setattr(telegram_bot.subprocess, "run", fake_run)

    result = telegram_bot.format_recent_logs(5)

    assert "Recent Logs" in result
    assert "&lt;bad&gt;" in result
    assert " &amp; " in result


def test_format_safe_config_does_not_expose_secret_values(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "BOT_TOKEN", "dummy-telegram-token")
    monkeypatch.setattr(telegram_bot, "GEMINI_API_KEY", "dummy-gemini-key")
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "dummy-android-key")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "REPORTS_DIR", tmp_path / "reports")

    result = telegram_bot.format_safe_config()

    assert "dummy-telegram-token" not in result
    assert "dummy-gemini-key" not in result
    assert "dummy-android-key" not in result
    assert "Gemini API key set" in result


def test_telegram_message_chunks_splits_long_lines():
    chunks = telegram_bot.telegram_message_chunks("abcdefghijk", limit=5)

    assert chunks == ["abcde", "fghij", "k"]


def test_format_android_status_with_heartbeat_and_vpn(monkeypatch, tmp_path):
    health_path = tmp_path / "service_health.json"
    health_path.write_text(json.dumps({
        "vpn": {
            "android": {
                "endpoint": "/ping",
                "evidence_detail": "country:HK",
            }
        }
    }))
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", health_path)
    monkeypatch.setattr(telegram_bot.database, "get_last_android_heartbeat", lambda: datetime.now().isoformat())
    monkeypatch.setattr(telegram_bot.database, "get_android_timezone", lambda: "+0800")

    result = telegram_bot.format_android_status()

    assert "Android Watcher" in result
    assert "online" in result
    assert "country:HK" in result


def test_format_database_stats_counts_sources(monkeypatch):
    meals = [
        {
            "date": date.today().isoformat(),
            "source": "api_auto",
            "analysis": {"is_food": True, "total_calories": 300},
        },
        {
            "date": date.today().isoformat(),
            "source": "telegram",
            "analysis": {"is_food": True, "total_calories": 200},
        },
        {
            "date": date.today().isoformat(),
            "source": "api_auto",
            "analysis": {"is_food": False, "total_calories": 999},
        },
    ]
    monkeypatch.setattr(telegram_bot.database, "get_meals", lambda chat_id, start, end: meals)
    monkeypatch.setattr(telegram_bot, "get_todays_meals", lambda chat_id: meals[:2])

    result = telegram_bot.format_database_stats(12345)

    assert "Food meals all time: 2" in result
    assert "Calories all time: ~500" in result
    assert "api_auto" in result


def test_format_recent_meals_escapes_html_and_keeps_indexes(monkeypatch):
    monkeypatch.setattr(telegram_bot, "get_recent_meals", lambda chat_id, days: [
        {
            "date": "2026-06-27",
            "time": "12:00",
            "corrected": False,
            "analysis": {
                "is_food": True,
                "meal_description": "Fish <rice>",
                "total_calories": 500,
            },
        }
    ])

    result = telegram_bot.format_recent_meals(12345)

    assert "[0]" in result
    assert "Fish &lt;rice&gt;" in result


def test_format_saved_reports(monkeypatch, tmp_path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "report_2026-06-27.md").write_text("report")
    monkeypatch.setattr(telegram_bot, "REPORTS_DIR", reports_dir)

    result = telegram_bot.format_saved_reports()

    assert "Saved Reports" in result
    assert "report_2026-06-27.md" in result


def test_run_doctor_success(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(telegram_bot.database, "get_meals", lambda *args: [])
    monkeypatch.setattr(telegram_bot, "run_gemini_probe", lambda client: "🟢 <b>Gemini probe OK</b>")
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "service_health.json")

    result = telegram_bot.run_doctor(object())

    assert "Database readable" in result
    assert "Gemini live probe OK" in result


def test_handle_text_message_new_meal_saves_and_replies(monkeypatch):
    sent = []
    saved = []

    class FakeBot:
        def send_message(self, chat_id, text, parse_mode="HTML"):
            sent.append(text)

    class FakeModels:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text=json.dumps({
                "intent": "new_meal",
                "analysis": {
                    "is_food": True,
                    "meal_description": "Rice bowl",
                    "total_calories": 600,
                    "total_protein_g": 30,
                    "total_carbs_g": 80,
                    "total_fat_g": 15,
                },
            }))

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(telegram_bot, "get_recent_meals", lambda chat_id, days: [])
    monkeypatch.setattr(telegram_bot, "save_meal", lambda *args: saved.append(args))
    monkeypatch.setattr(telegram_bot, "format_food_result", lambda chat_id, analysis: "formatted meal")

    telegram_bot.handle_text_message(FakeClient(), FakeBot(), 12345, "I ate a rice bowl")

    assert saved
    assert any("Added new manual meal" in message for message in sent)


def test_handle_text_message_correction_updates_meal(monkeypatch):
    sent = []
    updates = []
    meal = {
        "id": 1,
        "date": date.today().isoformat(),
        "analysis": {
            "is_food": True,
            "meal_description": "Old meal",
            "total_calories": 500,
            "food_items": [{"name": "rice"}],
        },
    }

    class FakeBot:
        def send_message(self, chat_id, text, parse_mode="HTML"):
            sent.append(text)

    class FakeModels:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text=json.dumps({
                "intent": "correction",
                "meal_index": 0,
                "reason": "portion adjusted",
                "analysis": {
                    "is_food": True,
                    "meal_description": "New meal",
                    "total_calories": 650,
                },
            }))

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(telegram_bot, "get_recent_meals", lambda chat_id, days: [meal])
    monkeypatch.setattr(
        telegram_bot.database,
        "update_meal_analysis",
        lambda meal_id, chat_id, analysis: updates.append((meal_id, chat_id, analysis)),
    )

    telegram_bot.handle_text_message(FakeClient(), FakeBot(), 12345, "make it 650 kcal")

    # Updated by the DB id from the snapshot, not by re-queried index
    assert updates[0][0] == 1
    assert updates[0][1] == 12345
    assert updates[0][2]["total_calories"] == 650
    assert any("Corrected meal" in message for message in sent)


# --- Fixes: clocks, redaction, confirm flows, proxy trust ---

class _RecordingBot:
    def __init__(self):
        self.sent = []
        self.answered = []

    def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, callback_query_id, text=""):
        self.answered.append((callback_query_id, text))


def _delete_intent_client(indices):
    class FakeModels:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text=json.dumps({
                "intent": "delete",
                "meal_indices": indices,
                "reason": "test",
            }))

    return SimpleNamespace(models=FakeModels())


def test_save_meal_stamps_user_local_date_and_server_timestamp(monkeypatch):
    user_now = datetime(2026, 1, 2, 23, 30, 0)
    server_now = datetime(2026, 1, 2, 15, 30, 0)

    class FakeDateTime:
        @staticmethod
        def now(tz=None):
            return server_now

    saved = {}

    def fake_db_save(chat_id, date_str, time_str, timestamp_str, source, image_hash, file_id, analysis):
        saved.update(date=date_str, time=time_str, timestamp=timestamp_str)
        return 7

    monkeypatch.setattr(telegram_bot.database, "user_local_now", lambda *args, **kwargs: user_now)
    monkeypatch.setattr(telegram_bot, "datetime", FakeDateTime)
    monkeypatch.setattr(telegram_bot.database, "save_meal", fake_db_save)

    meal_id = telegram_bot.save_meal(12345, {"is_food": True}, "telegram")

    assert meal_id == 7
    assert saved["date"] == "2026-01-02"
    assert saved["time"] == "11:30 PM"
    assert saved["timestamp"] == server_now.isoformat()


def test_format_history_window_is_inclusive_n_days(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_bot.database, "user_local_now",
        lambda *args, **kwargs: datetime(2026, 7, 1, 12, 0, 0),
    )
    monkeypatch.setattr(
        telegram_bot.database, "get_meals",
        lambda chat_id, start, end: calls.append((start, end)) or [],
    )

    result = telegram_bot.format_history(12345, days=7)

    # 6 days back plus today = exactly 7 days
    assert calls == [("2026-06-25", "2026-07-01")]
    assert "No meals logged" in result


def test_call_wraps_http_error_without_token(monkeypatch):
    bot = telegram_bot.TelegramBot("123:SECRETTOKEN")

    def fake_post(url, json=None, timeout=None):
        raise requests.HTTPError(
            f"404 Client Error for url: {url}",
            response=SimpleNamespace(status_code=404),
        )

    monkeypatch.setattr(bot.session, "post", fake_post)

    with pytest.raises(RuntimeError) as excinfo:
        bot._call("getUpdates")

    assert "SECRETTOKEN" not in str(excinfo.value)
    assert "Telegram 404 calling getUpdates" in str(excinfo.value)


def test_call_wraps_connection_error_without_token(monkeypatch):
    bot = telegram_bot.TelegramBot("123:SECRETTOKEN")

    def fake_post(url, json=None, timeout=None):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(bot.session, "post", fake_post)

    with pytest.raises(RuntimeError) as excinfo:
        bot._call("sendMessage")

    assert "SECRETTOKEN" not in str(excinfo.value)
    assert "Telegram network-error calling sendMessage" in str(excinfo.value)


def test_get_file_wraps_connection_error_without_token(monkeypatch):
    bot = telegram_bot.TelegramBot("123:SECRETTOKEN")
    monkeypatch.setattr(bot, "_call", lambda method, **kwargs: {"file_path": "photos/file_1.jpg"})

    def fake_get(url, timeout=None):
        raise requests.exceptions.ConnectTimeout(f"timed out connecting to {url}")

    monkeypatch.setattr(bot.session, "get", fake_get)

    with pytest.raises(RuntimeError) as excinfo:
        bot.get_file("file-id")

    assert "SECRETTOKEN" not in str(excinfo.value)
    assert "Telegram network-error calling getFile" in str(excinfo.value)


def test_get_updates_logs_are_token_free(monkeypatch, caplog):
    bot = telegram_bot.TelegramBot("123:SECRETTOKEN")

    def fake_post(url, json=None, timeout=None):
        raise requests.HTTPError(
            f"502 Server Error for url: {url}",
            response=SimpleNamespace(status_code=502),
        )

    monkeypatch.setattr(bot.session, "post", fake_post)
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda seconds: None)

    with caplog.at_level(logging.ERROR, logger="calorie_bot"):
        result = bot.get_updates(timeout=0)

    assert result == []
    assert caplog.text
    assert "SECRETTOKEN" not in caplog.text


def test_send_message_connection_error_logs_are_token_free(monkeypatch, caplog):
    # ConnectionError messages embed the token URL; _call must sanitize them
    # before they reach any generic logger.
    bot = telegram_bot.TelegramBot("123:SECRETTOKEN")

    def fake_post(url, json=None, timeout=None):
        raise requests.ConnectionError(f"connection refused for {url}")

    monkeypatch.setattr(bot.session, "post", fake_post)

    with caplog.at_level(logging.ERROR, logger="calorie_bot"):
        result = bot.send_message(1, "hello")

    assert result is None
    assert caplog.text
    assert "SECRETTOKEN" not in caplog.text
    assert "network-error" in caplog.text


def test_request_remote_ip_honors_xff_only_with_trusted_proxy(monkeypatch):
    from flask import Flask

    app = Flask(__name__)
    with app.test_request_context(
        "/ping",
        method="POST",
        headers={"X-Forwarded-For": "203.0.113.10"},
        environ_base={"REMOTE_ADDR": "10.0.0.2"},
    ):
        monkeypatch.setattr(telegram_bot, "TRUSTED_PROXY_ENABLED", False)
        assert telegram_bot._request_remote_ip() == "10.0.0.2"

        monkeypatch.setattr(telegram_bot, "TRUSTED_PROXY_ENABLED", True)
        assert telegram_bot._request_remote_ip() == "203.0.113.10"


def test_quota_keep_callback_sends_retry_instructions(monkeypatch):
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    bot = _RecordingBot()

    handled = telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-keep",
        "data": "quota_keep:deadbeefcafe",
        "message": {"chat": {"id": 12345}},
    })

    assert handled is True
    assert bot.answered
    assert any("/retry_failed deadbeefcafe" in m["text"] for m in bot.sent)
    assert any("/clear_failed deadbeefcafe confirm" in m["text"] for m in bot.sent)


def test_callback_query_from_unauthorized_chat_is_rejected(monkeypatch):
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    telegram_bot._pending_nl_deletes.clear()
    telegram_bot._pending_nl_deletes[999] = {"ids": [1], "labels": ["x"], "at": datetime.now()}
    monkeypatch.setattr(
        telegram_bot.database, "delete_meal",
        lambda *args: pytest.fail("unauthorized callback must not delete"),
    )
    bot = _RecordingBot()

    handled = telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-bad",
        "data": "nl_delete_confirm",
        "message": {"chat": {"id": 999}},
    })

    assert handled is True
    assert bot.answered == [("cb-bad", "Not authorized.")]
    assert bot.sent == []
    telegram_bot._pending_nl_deletes.clear()


def test_nl_delete_intent_stashes_pending_without_deleting(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "get_recent_meals", lambda chat_id, days: [
        {
            "id": 11,
            "date": "2026-07-04",
            "time": "12:00 PM",
            "analysis": {"is_food": True, "meal_description": "Rice", "total_calories": 300},
        }
    ])
    monkeypatch.setattr(
        telegram_bot.database, "delete_meal",
        lambda *args: pytest.fail("delete must wait for confirmation"),
    )
    telegram_bot._pending_nl_deletes.clear()
    bot = _RecordingBot()

    telegram_bot.handle_text_message(_delete_intent_client([0]), bot, 777, "delete the rice")

    pending = telegram_bot._pending_nl_deletes[777]
    assert pending["ids"] == [11]
    token = pending["token"]
    assert token
    markup = bot.sent[-1]["reply_markup"]
    callback_datas = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
    ]
    # Buttons are bound to this confirmation message via the nonce.
    assert f"nl_delete_confirm:{token}" in callback_datas
    assert f"nl_delete_cancel:{token}" in callback_datas
    telegram_bot._pending_nl_deletes.clear()


def test_nl_delete_cancel_callback_deletes_nothing(monkeypatch):
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(
        telegram_bot.database, "delete_meal",
        lambda *args: pytest.fail("cancel must not delete"),
    )
    telegram_bot._pending_nl_deletes.clear()
    telegram_bot._pending_nl_deletes[12345] = {
        "ids": [11], "labels": ["Rice"], "at": datetime.now(), "token": "cafe1234",
    }
    bot = _RecordingBot()

    handled = telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-cancel",
        "data": "nl_delete_cancel:cafe1234",
        "message": {"chat": {"id": 12345}},
    })

    assert handled is True
    assert 12345 not in telegram_bot._pending_nl_deletes
    assert any("nothing was deleted" in m["text"] for m in bot.sent)


def test_nl_delete_confirm_expired_pending_deletes_nothing(monkeypatch):
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(
        telegram_bot.database, "delete_meal",
        lambda *args: pytest.fail("expired confirmation must not delete"),
    )
    telegram_bot._pending_nl_deletes.clear()
    telegram_bot._pending_nl_deletes[12345] = {
        "ids": [11],
        "labels": ["Rice"],
        "at": datetime.now() - timedelta(seconds=telegram_bot.NL_DELETE_CONFIRM_TTL_SECONDS + 60),
        "token": "cafe1234",
    }
    bot = _RecordingBot()

    handled = telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-expired",
        "data": "nl_delete_confirm:cafe1234",
        "message": {"chat": {"id": 12345}},
    })

    assert handled is True
    assert 12345 not in telegram_bot._pending_nl_deletes
    assert any("expired" in m["text"] for m in bot.sent)


def test_nl_delete_stale_confirm_token_is_noop_then_current_token_deletes(monkeypatch, tmp_path):
    """A Delete button from an OLDER confirmation must never execute the newest
    pending set; the current message's button still deletes the right ids."""
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    meals = [
        {
            "id": 11,
            "date": "2026-07-04",
            "time": "08:00 AM",
            "analysis": {"is_food": True, "meal_description": "Toast", "total_calories": 200},
        },
        {
            "id": 22,
            "date": "2026-07-04",
            "time": "12:00 PM",
            "analysis": {"is_food": True, "meal_description": "Rice", "total_calories": 300},
        },
    ]
    monkeypatch.setattr(telegram_bot, "get_recent_meals", lambda chat_id, days: meals)
    deleted = []
    monkeypatch.setattr(
        telegram_bot.database, "delete_meal",
        lambda meal_id, chat_id: deleted.append((meal_id, chat_id)),
    )
    telegram_bot._pending_nl_deletes.clear()
    bot = _RecordingBot()

    # First confirmation message: just the toast.
    telegram_bot.handle_text_message(_delete_intent_client([0]), bot, 12345, "delete the toast")
    first_markup = bot.sent[-1]["reply_markup"]
    first_confirm = first_markup["inline_keyboard"][0][0]["callback_data"]
    first_token = telegram_bot._pending_nl_deletes[12345]["token"]
    assert first_confirm == f"nl_delete_confirm:{first_token}"

    # Second confirmation message supersedes it: everything from today.
    telegram_bot.handle_text_message(_delete_intent_client([0, 1]), bot, 12345, "delete everything today")
    second_token = telegram_bot._pending_nl_deletes[12345]["token"]
    assert second_token != first_token
    assert telegram_bot._pending_nl_deletes[12345]["ids"] == [11, 22]

    # Firing the FIRST (stale) message's Delete button deletes nothing and
    # leaves the newer pending entry intact.
    handled = telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-stale",
        "data": first_confirm,
        "message": {"chat": {"id": 12345}},
    })

    assert handled is True
    assert deleted == []
    assert telegram_bot._pending_nl_deletes[12345]["token"] == second_token
    assert telegram_bot._pending_nl_deletes[12345]["ids"] == [11, 22]
    assert any("superseded" in text for _cb, text in bot.answered)
    assert any("superseded" in m["text"] for m in bot.sent)

    # Firing the CURRENT message's Delete button deletes exactly the right ids.
    handled = telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-current",
        "data": f"nl_delete_confirm:{second_token}",
        "message": {"chat": {"id": 12345}},
    })

    assert handled is True
    assert deleted == [(11, 12345), (22, 12345)]
    assert 12345 not in telegram_bot._pending_nl_deletes
    telegram_bot._pending_nl_deletes.clear()


def test_nl_delete_stale_cancel_token_leaves_pending_intact(monkeypatch):
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(
        telegram_bot.database, "delete_meal",
        lambda *args: pytest.fail("cancel must not delete"),
    )
    telegram_bot._pending_nl_deletes.clear()
    telegram_bot._pending_nl_deletes[12345] = {
        "ids": [11], "labels": ["Rice"], "at": datetime.now(), "token": "bbbbbbbb",
    }
    bot = _RecordingBot()

    handled = telegram_bot.handle_callback_query(object(), bot, {
        "id": "cb-stale-cancel",
        "data": "nl_delete_cancel:aaaaaaaa",
        "message": {"chat": {"id": 12345}},
    })

    assert handled is True
    # The stale Cancel button must not discard the newer pending request.
    assert telegram_bot._pending_nl_deletes[12345]["ids"] == [11]
    assert telegram_bot._pending_nl_deletes[12345]["token"] == "bbbbbbbb"
    assert any("superseded" in text for _cb, text in bot.answered)
    telegram_bot._pending_nl_deletes.clear()


def test_clear_failed_uploads_discards_failed_hash_rows(monkeypatch, tmp_path):
    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    (failed_dir / "20260701T010101_deadbeefcafe.jpg").write_bytes(b"img")
    discarded = []
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(
        telegram_bot.database, "discard_failed_photo_hashes_by_prefix",
        lambda chat_id, prefix: discarded.append((chat_id, prefix)) or 1,
    )

    result = telegram_bot.clear_failed_uploads("latest", confirmed=True)

    assert "Removed 1" in result
    assert discarded == [(12345, "deadbeefcafe")]


def test_clear_failed_uploads_tombstones_ingestion_row(monkeypatch, tmp_path):
    """/clear_failed keeps the ingestion row as a 'deleted' tombstone so the
    nightly /reconcile keeps suppressing the photo instead of re-requesting it."""
    import sqlite3

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "meals.db")
    database.init_db()
    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    img_hash = "deadbeefcafe" + "0" * 20
    (failed_dir / f"20260701T010101_{img_hash[:12]}.jpg").write_bytes(b"img")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    assert database.reserve_photo_hash(12345, img_hash, source="telegram") is True
    database.mark_photo_hash_status(12345, img_hash, "failed", source="telegram")

    result = telegram_bot.clear_failed_uploads("latest", confirmed=True)

    assert "Removed 1" in result
    with sqlite3.connect(database.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT status, meal_id FROM photo_ingestions WHERE chat_id = ? AND image_hash = ?",
            (12345, img_hash),
        ).fetchall()
    # Row remains as a tombstone, not deleted.
    assert rows == [("deleted", None)]
    # And it still suppresses /reconcile from re-requesting the photo.
    reserved = set(database.get_reserved_photo_hashes(12345))
    assert img_hash in reserved
    assert telegram_bot._reconcile_missing_hashes({img_hash}, set(), reserved) == []


def test_sweep_stranded_pending_uploads_moves_files_and_notifies(monkeypatch, tmp_path):
    pending_dir = tmp_path / "pending"
    failed_dir = tmp_path / "failed"
    pending_dir.mkdir()
    (pending_dir / "20260701T010101_deadbeefcafe.jpg").write_bytes(b"stranded image")
    statuses = []
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", pending_dir)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(
        telegram_bot.database, "mark_photo_hash_status",
        lambda *args, **kwargs: statuses.append((args, kwargs)),
    )
    bot = _RecordingBot()

    telegram_bot._sweep_stranded_pending_uploads(bot)

    assert list(pending_dir.iterdir()) == []
    assert len(list(failed_dir.iterdir())) == 1
    assert statuses and statuses[0][0][2] == "failed"
    assert any("/retry_failed" in m["text"] for m in bot.sent)


def test_sweep_stranded_pending_uploads_missing_dir_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "never-created")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    bot = _RecordingBot()

    telegram_bot._sweep_stranded_pending_uploads(bot)

    assert bot.sent == []


def test_sweep_stranded_pending_uploads_skips_unreadable_file(monkeypatch, tmp_path):
    """One unreadable file must not abort the sweep; readable files still move."""
    pending_dir = tmp_path / "pending"
    failed_dir = tmp_path / "failed"
    pending_dir.mkdir()
    unreadable = pending_dir / "20260701T010101_aaaaaaaaaaaa.jpg"
    unreadable.write_bytes(b"locked image")
    unreadable.chmod(0)
    (pending_dir / "20260701T020202_bbbbbbbbbbbb.jpg").write_bytes(b"fine image")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", pending_dir)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status", lambda *a, **k: None)
    bot = _RecordingBot()

    try:
        telegram_bot._sweep_stranded_pending_uploads(bot)
    finally:
        unreadable.chmod(0o644)

    assert len(list(failed_dir.iterdir())) == 1
    assert unreadable.exists()
    assert any("Recovered 1 upload" in m["text"] for m in bot.sent)


def test_sweep_stranded_pending_uploads_truncates_long_lists(monkeypatch, tmp_path):
    pending_dir = tmp_path / "pending"
    failed_dir = tmp_path / "failed"
    pending_dir.mkdir()
    for i in range(12):
        (pending_dir / f"20260701T0101{i:02d}_{i:012x}.jpg").write_bytes(f"img-{i}".encode())
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", pending_dir)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status", lambda *a, **k: None)
    bot = _RecordingBot()

    telegram_bot._sweep_stranded_pending_uploads(bot)

    assert len(list(failed_dir.iterdir())) == 12
    message = next(m["text"] for m in bot.sent if "Recovered 12 upload" in m["text"])
    assert "...and 2 more." in message
    assert message.count("• <code>") == 10


def test_sweep_releases_orphaned_processing_reservation(monkeypatch, tmp_path):
    """A 'processing' reservation with no staged or failed file behind it is
    a crash orphan — the sweep must release it so phone retries stop getting
    'duplicate' and losing their queued copy."""
    orphan_hash = "ab" * 16
    released = []
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot.database, "get_processing_photo_hashes",
                        lambda chat_id: [orphan_hash])
    monkeypatch.setattr(telegram_bot.database, "release_photo_hash",
                        lambda chat_id, image_hash: released.append((chat_id, image_hash)))
    bot = _RecordingBot()

    telegram_bot._sweep_stranded_pending_uploads(bot)

    assert released == [(12345, orphan_hash)]
    assert bot.sent == []


def test_sweep_keeps_processing_reservation_backed_by_failed_file(monkeypatch, tmp_path):
    kept_hash = "cd" * 16
    released = []
    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    (failed_dir / f"20260707_220000_{kept_hash[:12]}.jpg").write_bytes(b"x")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot.database, "get_processing_photo_hashes",
                        lambda chat_id: [kept_hash])
    monkeypatch.setattr(telegram_bot.database, "release_photo_hash",
                        lambda chat_id, image_hash: released.append(image_hash))
    bot = _RecordingBot()

    telegram_bot._sweep_stranded_pending_uploads(bot)

    assert released == []


def test_sweep_keeps_processing_reservation_backed_by_unreadable_pending_file(monkeypatch, tmp_path):
    """A pending file whose bytes cannot be read still backs its reservation.
    The sweep must NOT release the 'processing' row — releasing it while the
    file stays in pending lets a later sweep's md5 fallback re-key the photo
    under the recompressed bytes' hash and double-process it."""
    kept_hash = "ef" * 16
    released = []
    marked = []
    pending_dir = tmp_path / "pending"
    failed_dir = tmp_path / "failed"
    pending_dir.mkdir()
    stuck = pending_dir / f"20260709T010101_{kept_hash[:12]}.jpg"
    stuck.write_bytes(b"unreadable image")
    stuck.chmod(0)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", pending_dir)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed_dir)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot.database, "get_processing_photo_hashes",
                        lambda chat_id: [kept_hash])
    monkeypatch.setattr(telegram_bot.database, "release_photo_hash",
                        lambda chat_id, image_hash: released.append(image_hash))
    monkeypatch.setattr(telegram_bot.database, "mark_photo_hash_status",
                        lambda *a, **k: marked.append((a, k)))
    bot = _RecordingBot()

    try:
        telegram_bot._sweep_stranded_pending_uploads(bot)
    finally:
        stuck.chmod(0o644)

    assert released == []  # row stays 'processing'
    assert marked == []
    assert stuck.exists()  # file still in pending
    assert not failed_dir.exists() or list(failed_dir.iterdir()) == []


def _stale_heartbeat_setup(monkeypatch, last_ping):
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot, "_last_stale_heartbeat_warning_at", None)
    monkeypatch.setattr(telegram_bot.database, "get_last_android_heartbeat", lambda: last_ping)


def test_stale_heartbeat_warns_once_with_cooldown(monkeypatch):
    stale = (datetime.now() - timedelta(hours=3)).isoformat()
    _stale_heartbeat_setup(monkeypatch, stale)
    bot = _RecordingBot()

    assert telegram_bot.maybe_warn_stale_android_heartbeat(bot) is True
    assert telegram_bot.maybe_warn_stale_android_heartbeat(bot) is False
    assert len(bot.sent) == 1
    assert "hasn't reached the server" in bot.sent[0]["text"]
    assert "/android" in bot.sent[0]["text"]
    # A DELIVERED warning keeps the full cooldown: the stamp is now, not
    # rolled back into the retry window.
    assert (datetime.now() - telegram_bot._last_stale_heartbeat_warning_at).total_seconds() < 60


def test_stale_heartbeat_recovery_resets_cooldown(monkeypatch):
    """A fresh heartbeat clears the cooldown so the NEXT outage warns
    promptly instead of being silenced by the previous outage's warning."""
    stale = (datetime.now() - timedelta(hours=3)).isoformat()
    _stale_heartbeat_setup(monkeypatch, stale)
    bot = _RecordingBot()

    assert telegram_bot.maybe_warn_stale_android_heartbeat(bot) is True

    # Phone recovers: fresh heartbeat resets the cooldown.
    monkeypatch.setattr(telegram_bot.database, "get_last_android_heartbeat",
                        lambda: datetime.now().isoformat())
    assert telegram_bot.maybe_warn_stale_android_heartbeat(bot) is False
    assert telegram_bot._last_stale_heartbeat_warning_at is None

    # New outage right after recovery: warns again immediately.
    monkeypatch.setattr(telegram_bot.database, "get_last_android_heartbeat",
                        lambda: stale)
    assert telegram_bot.maybe_warn_stale_android_heartbeat(bot) is True
    assert len(bot.sent) == 2


def test_stale_heartbeat_failed_send_retries_soon(monkeypatch):
    """send_message swallows errors and returns None; a failed delivery must
    not consume the 12h cooldown — the stamp rolls back to retry in ~15min."""

    class _FailingBot(_RecordingBot):
        def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None):
            super().send_message(chat_id, text, parse_mode, reply_markup)
            return None

    stale = (datetime.now() - timedelta(hours=3)).isoformat()
    _stale_heartbeat_setup(monkeypatch, stale)
    failing = _FailingBot()

    assert telegram_bot.maybe_warn_stale_android_heartbeat(failing) is False
    assert len(failing.sent) == 1  # delivery was attempted

    # The rolled-back stamp puts the next retry ~15 minutes out, not 12h.
    stamp = telegram_bot._last_stale_heartbeat_warning_at
    retry_at = stamp + timedelta(hours=telegram_bot.HEARTBEAT_STALE_WARNING_COOLDOWN_HOURS)
    wait_seconds = (retry_at - datetime.now()).total_seconds()
    assert 13 * 60 < wait_seconds <= 15 * 60

    # Still inside the retry window: no duplicate attempt yet.
    working = _RecordingBot()
    assert telegram_bot.maybe_warn_stale_android_heartbeat(working) is False
    assert working.sent == []

    # ~16 minutes later the retry fires; success stamps the full cooldown.
    telegram_bot._last_stale_heartbeat_warning_at = stamp - timedelta(minutes=16)
    assert telegram_bot.maybe_warn_stale_android_heartbeat(working) is True
    assert len(working.sent) == 1
    assert (datetime.now() - telegram_bot._last_stale_heartbeat_warning_at).total_seconds() < 60
    assert telegram_bot.maybe_warn_stale_android_heartbeat(working) is False
    assert len(working.sent) == 1


def test_fresh_heartbeat_does_not_warn(monkeypatch):
    _stale_heartbeat_setup(monkeypatch, datetime.now().isoformat())
    bot = _RecordingBot()

    assert telegram_bot.maybe_warn_stale_android_heartbeat(bot) is False
    assert bot.sent == []


def test_never_connected_heartbeat_does_not_warn(monkeypatch):
    _stale_heartbeat_setup(monkeypatch, None)
    bot = _RecordingBot()

    assert telegram_bot.maybe_warn_stale_android_heartbeat(bot) is False
    assert bot.sent == []


def test_stale_heartbeat_warning_can_be_disabled(monkeypatch):
    stale = (datetime.now() - timedelta(hours=30)).isoformat()
    _stale_heartbeat_setup(monkeypatch, stale)
    monkeypatch.setattr(telegram_bot, "HEARTBEAT_STALE_WARNING_HOURS", 0)
    bot = _RecordingBot()

    assert telegram_bot.maybe_warn_stale_android_heartbeat(bot) is False
    assert bot.sent == []


def test_format_vpn_status_renders_evidence_and_empty_state(monkeypatch, tmp_path):
    empty_path = tmp_path / "empty.json"
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", empty_path)
    empty = telegram_bot.format_vpn_status()
    assert "VPN Evidence" in empty

    health_path = tmp_path / "health.json"
    health_path.write_text(json.dumps({
        "vpn": {
            "android": {
                "at": "2026-07-08T01:00:00",
                "endpoint": "/ping",
                "remote_ip": "93.184.216.34",
                "vpn_active": True,
                "vpn_check": "tun0",
                "vpn_check_reliable": True,
                "evidence": "remote_country",
                "evidence_detail": "US exit <spicy&detail>",
            },
        },
    }))
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", health_path)

    rendered = telegram_bot.format_vpn_status()

    assert "android" in rendered.lower()
    assert "93.184.216.34" in rendered
    assert "<spicy&detail>" not in rendered  # HTML-escaped before Telegram


def test_format_meals_list_empty_and_populated(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot.database, "DB_PATH", tmp_path / "meals.db")
    telegram_bot.database.init_db()
    monkeypatch.setattr(telegram_bot.database, "get_android_timezone",
                        lambda device_name="android_watcher": "+0800")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)

    assert "No meals" in telegram_bot.format_meals_list(12345)

    now = telegram_bot.database.user_local_now()
    telegram_bot.database.save_meal(
        12345, now.date().isoformat(), "12:00 PM", datetime.now().isoformat(),
        "telegram", "aa" * 16, "f1",
        {"is_food": True, "total_calories": 640, "meal_description": "Beef <br> noodles"},
    )

    rendered = telegram_bot.format_meals_list(12345)

    assert "640" in rendered
    assert "Beef &lt;br&gt; noodles" in rendered
    assert "<br>" not in rendered


def test_run_gemini_probe_reports_classified_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")

    class ExplodingModels:
        def generate_content(self, **kwargs):
            raise Exception("429 RESOURCE_EXHAUSTED rate thing")

    probe = telegram_bot.run_gemini_probe(SimpleNamespace(models=ExplodingModels()))

    assert "❌" in probe or "failed" in probe.lower()


def test_handle_text_message_refuses_during_quota_pause(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_bot, "_gemini_quota_pause_summary",
                        lambda: "Paused until tomorrow.")
    monkeypatch.setattr(telegram_bot.database, "get_recent_meals",
                        lambda *a, **k: calls.append("db") or [])
    bot = _RecordingBot()

    telegram_bot.handle_text_message(object(), bot, 12345, "I ate a burger")

    assert len(bot.sent) == 1
    assert "paused" in bot.sent[0]["text"].lower()
    assert calls == []  # never reached the DB or Gemini


def test_format_operational_status_with_no_state(monkeypatch, tmp_path):
    """Smoke test: /status renders cleanly on a fresh install (no health file,
    no heartbeat, no upload dirs)."""
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "no-pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "no-failed")
    monkeypatch.setattr(telegram_bot.database, "get_last_android_heartbeat", lambda: None)

    status = telegram_bot.format_operational_status()

    assert "CalorieTracker Status" in status
    assert "no heartbeat yet" in status
    assert "0 pending, 0 failed" in status
    assert "Daily report:" not in status


def test_format_operational_status_with_full_state(monkeypatch, tmp_path):
    import json as _json

    health_path = tmp_path / "health.json"
    health_path.write_text(_json.dumps({
        "gemini": {"last_ok_at": datetime.now().isoformat(timespec="seconds")},
        "daily_report": {
            "last_ok": True,
            "last_target_date": "2026-07-07",
            "last_attempt_at": datetime.now().isoformat(timespec="seconds"),
        },
    }))
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", health_path)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "no-pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "no-failed")
    monkeypatch.setattr(
        telegram_bot.database, "get_last_android_heartbeat",
        lambda: datetime.now().isoformat(),
    )

    status = telegram_bot.format_operational_status()

    assert "🟢 Android: online" in status
    assert "Daily report: 🟢 target 2026-07-07" in status


def test_format_operational_status_escapes_invalid_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "no-pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "no-failed")
    monkeypatch.setattr(
        telegram_bot.database, "get_last_android_heartbeat", lambda: "corrupt<b>value",
    )

    status = telegram_bot.format_operational_status()

    assert "invalid heartbeat" in status
    assert "corrupt&lt;b&gt;value" in status
    assert "corrupt<b>value" not in status


def test_format_food_result_survives_hostile_analysis_shapes():
    """Distilled from the S3 fuzzer: non-dict food_items entries and scalar
    food_items crashed the photo reply before safe_food_items."""
    hostile = {
        "is_food": True,
        "meal_description": False,
        "total_calories": [],
        "food_items": ["rice", {"name": "ok", "estimated_calories": 200}, None],
    }
    result = telegram_bot.format_food_result(12345, hostile)
    assert isinstance(result, str)
    assert "ok" in result

    scalar_items = {"is_food": True, "meal_description": "x", "food_items": -1}
    assert isinstance(telegram_bot.format_food_result(12345, scalar_items), str)
