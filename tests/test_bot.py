import pytest
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from telegram_bot import format_food_result, format_daily_totals, is_duplicate_photo, analyze_food_photo
import telegram_bot
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
    monkeypatch.setattr(telegram_bot.Image, "open", lambda image: object())
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
    monkeypatch.setattr(telegram_bot.Image, "open", lambda image: object())
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
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
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
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
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
    monkeypatch.setattr(telegram_bot.database, "meal_image_hash_exists", lambda chat_id, image_hash: False)
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: True)
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
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
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
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
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

def test_android_unreliable_vpn_check_does_not_warn_without_direct_evidence(monkeypatch):
    from flask import Flask

    sent_messages = []

    class FakeBot:
        def send_message(self, chat_id, text):
            sent_messages.append((chat_id, text))

    app = Flask(__name__)
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

def test_update_meal_invalid_index(monkeypatch):
    from telegram_bot import update_meal_by_index
    
    # User only has 2 recent meals
    def mock_get_recent_meals(chat_id, days):
        return [{"id": 1}, {"id": 2}]
        
    monkeypatch.setattr(telegram_bot, "get_recent_meals", mock_get_recent_meals)
    
    # Try to access index 5 (which doesn't exist)
    result = update_meal_by_index(12345, 5, {"is_food": True})
    
    # Should gracefully return False instead of crashing
    assert result is False
    
    # Also test negative index
    assert update_meal_by_index(12345, -1, {"is_food": True}) is False


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
    monkeypatch.setattr(telegram_bot, "is_duplicate_photo", lambda chat_id, image_hash: False)
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
    monkeypatch.setattr(telegram_bot, "update_meal_by_index", lambda chat_id, index, analysis: updates.append((index, analysis)) or True)

    telegram_bot.handle_text_message(FakeClient(), FakeBot(), 12345, "make it 650 kcal")

    assert updates[0][0] == 0
    assert updates[0][1]["total_calories"] == 650
    assert any("Corrected meal" in message for message in sent)
