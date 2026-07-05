import pytest
import sqlite3
import json
from datetime import datetime, date, timedelta
from pathlib import Path
import database

@pytest.fixture(autouse=True)
def mock_db_path(monkeypatch, tmp_path):
    """Override the DB_PATH to use a temporary file for tests."""
    temp_db = tmp_path / "test_meals.db"
    monkeypatch.setattr(database, "DB_PATH", temp_db)
    # Re-initialize the database to create tables in the temp file
    database.init_db()
    yield temp_db

def test_init_db(mock_db_path):
    """Test that the tables are created successfully."""
    assert mock_db_path.exists()
    
    with sqlite3.connect(mock_db_path) as conn:
        cursor = conn.cursor()
        
        # Check if meals table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meals'")
        assert cursor.fetchone() is not None
        
        # Check if heartbeats table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='heartbeats'")
        assert cursor.fetchone() is not None

def test_save_and_get_meals():
    chat_id = 12345
    date_str = date.today().isoformat()
    
    analysis_data = {
        "description": "A delicious burger",
        "calories": 600,
        "protein_g": 30,
        "carbs_g": 40,
        "fat_g": 20
    }
    
    # Save a meal
    meal_id = database.save_meal(
        chat_id=chat_id,
        date_str=date_str,
        time_str="12:00:00",
        timestamp_str=datetime.now().isoformat(),
        source="telegram",
        image_hash="abc123hash",
        file_id="file123",
        analysis=analysis_data
    )
    
    assert meal_id == 1
    
    # Retrieve the meal
    meals = database.get_meals(chat_id, date_str, date_str)
    assert len(meals) == 1
    
    meal = meals[0]
    assert meal["id"] == 1
    assert meal["chat_id"] == chat_id
    assert meal["date"] == date_str
    assert meal["source"] == "telegram"
    assert meal["corrected"] is False
    assert meal["analysis"] == analysis_data

def test_update_meal_analysis():
    chat_id = 12345
    date_str = date.today().isoformat()
    
    # Insert initial meal
    meal_id = database.save_meal(
        chat_id=chat_id,
        date_str=date_str,
        time_str="12:00:00",
        timestamp_str=datetime.now().isoformat(),
        source="telegram",
        image_hash="abc",
        file_id="file",
        analysis={"calories": 500}
    )
    
    # Correct the meal
    new_analysis = {"calories": 600, "protein_g": 40}
    database.update_meal_analysis(meal_id, chat_id, new_analysis)
    
    # Verify the update
    meals = database.get_meals(chat_id, date_str, date_str)
    updated_meal = meals[0]
    
    assert updated_meal["corrected"] is True
    assert updated_meal["analysis"]["calories"] == 600
    assert updated_meal["analysis"]["protein_g"] == 40

def test_android_heartbeat():
    # Initial state should be None
    assert database.get_last_android_heartbeat() is None
    
    # Update heartbeat
    database.update_android_heartbeat("android_watcher")
    
    last_ping = database.get_last_android_heartbeat("android_watcher")
    assert last_ping is not None
    
    # Ensure it's a valid ISO string
    dt = datetime.fromisoformat(last_ping)
    assert dt.date() == date.today()


def test_android_timezone_defaults_and_updates():
    assert database.get_android_timezone("missing_device") == "+0800"

    database.update_android_heartbeat("android_watcher", timezone="+0900")

    assert database.get_android_timezone("android_watcher") == "+0900"


def test_delete_meal_validates_chat_id():
    owner_chat = 111
    other_chat = 222
    date_str = date.today().isoformat()
    meal_id = database.save_meal(
        chat_id=owner_chat,
        date_str=date_str,
        time_str="12:00:00",
        timestamp_str=datetime.now().isoformat(),
        source="telegram",
        image_hash="abc",
        file_id="file",
        analysis={"is_food": True, "total_calories": 500},
    )

    database.delete_meal(meal_id, other_chat)
    assert len(database.get_meals(owner_chat, date_str, date_str)) == 1

    database.delete_meal(meal_id, owner_chat)
    assert database.get_meals(owner_chat, date_str, date_str) == []


def test_get_today_hashes_returns_only_hashes_for_today():
    chat_id = 12345
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    database.save_meal(
        chat_id,
        today,
        "12:00",
        f"{today}T12:00:00",
        "api",
        "today_hash",
        "file1",
        {"is_food": True},
    )
    database.save_meal(
        chat_id,
        yesterday,
        "12:00",
        f"{yesterday}T12:00:00",
        "api",
        "yesterday_hash",
        "file2",
        {"is_food": True},
    )
    database.save_meal(
        chat_id,
        today,
        "13:00",
        f"{today}T13:00:00",
        "api",
        "",
        "file3",
        {"is_food": True},
    )

    assert database.get_today_hashes(chat_id) == ["today_hash"]


def test_photo_hash_reservation_blocks_duplicate_until_released():
    chat_id = 12345
    image_hash = "latte_hash"

    assert database.reserve_photo_hash(chat_id, image_hash, "telegram") is True
    assert database.reserve_photo_hash(chat_id, image_hash, "api_upload") is False

    database.release_photo_hash(chat_id, image_hash)

    assert database.reserve_photo_hash(chat_id, image_hash, "api_upload") is True


def test_photo_hash_reservation_blocks_existing_meal():
    chat_id = 12345
    today = date.today().isoformat()
    image_hash = "already_logged_latte"

    database.save_meal(
        chat_id,
        today,
        "12:00",
        datetime.now().isoformat(),
        "telegram",
        image_hash,
        "file1",
        {"is_food": True, "meal_description": "Latte"},
    )

    assert database.reserve_photo_hash(chat_id, image_hash, "api_upload") is False
    assert database.meal_image_hash_exists(chat_id, image_hash) is True
    assert database.meal_image_hash_exists(chat_id, "missing_hash") is False


def test_mark_photo_hash_status_and_reserved_hashes():
    chat_id = 12345
    image_hash = "saved_latte_hash"

    assert database.reserve_photo_hash(chat_id, image_hash, "telegram") is True
    database.mark_photo_hash_status(chat_id, image_hash, "saved", meal_id=42, source="telegram")

    assert database.get_reserved_photo_hashes(chat_id) == [image_hash]
    assert database.reserve_photo_hash(chat_id, image_hash, "api_upload") is False


def test_stale_processing_photo_hash_can_be_reclaimed():
    chat_id = 12345
    image_hash = "stale_latte_hash"
    old_timestamp = (datetime.now() - timedelta(hours=7)).isoformat()

    assert database.reserve_photo_hash(chat_id, image_hash, "telegram", old_timestamp) is True

    assert database.reserve_photo_hash(chat_id, image_hash, "api_upload") is True


def test_failed_photo_hash_can_be_reclaimed_for_retry():
    chat_id = 12345
    image_hash = "failed_latte_hash"

    assert database.reserve_photo_hash(chat_id, image_hash, "api_upload") is True
    database.mark_photo_hash_status(chat_id, image_hash, "failed", source="api_upload")

    assert database.reserve_photo_hash(
        chat_id,
        image_hash,
        "api_retry",
        reclaim_statuses={"failed"},
    ) is True
    assert database.reserve_photo_hash(chat_id, image_hash, "api_upload") is False
