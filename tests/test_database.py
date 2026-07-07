import pytest
import sqlite3
from datetime import datetime, date, timedelta
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


def test_update_android_heartbeat_without_timezone_preserves_stored_offset():
    database.update_android_heartbeat("android_watcher", timezone="+0530")
    assert database.get_android_timezone("android_watcher") == "+0530"

    # A bare liveness ping (no timezone arg) must NOT clobber the
    # phone-reported offset that drives all meal dating.
    database.update_android_heartbeat("android_watcher")
    assert database.get_android_timezone("android_watcher") == "+0530"

    # An explicit non-None offset still overwrites.
    database.update_android_heartbeat("android_watcher", timezone="+0100")
    assert database.get_android_timezone("android_watcher") == "+0100"


def test_update_android_heartbeat_first_ping_without_timezone_defaults():
    database.update_android_heartbeat("fresh_device")
    assert database.get_android_timezone("fresh_device") == "+0800"
    assert database.get_last_android_heartbeat("fresh_device") is not None


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


def test_get_today_hashes_returns_only_hashes_for_today(monkeypatch):
    monkeypatch.setattr(
        database, "get_android_timezone", lambda device_name="android_watcher": "+0800"
    )
    chat_id = 12345
    user_today = database.user_local_now().date()
    today = user_today.isoformat()
    yesterday = (user_today - timedelta(days=1)).isoformat()
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


def test_discard_failed_photo_hashes_by_prefix_tombstones(mock_db_path):
    chat_id = 12345
    failed_hash = "failed_upload_hash"
    saved_hash = "failed_upload_saved_hash"  # same prefix, but status 'saved'

    assert database.reserve_photo_hash(chat_id, failed_hash, "api_upload") is True
    database.mark_photo_hash_status(chat_id, failed_hash, "failed", source="api_upload")
    assert database.reserve_photo_hash(chat_id, saved_hash, "api_upload") is True
    database.mark_photo_hash_status(chat_id, saved_hash, "saved", meal_id=7)

    # Prefix is normalized (case/whitespace) and only 'failed' rows match
    assert database.discard_failed_photo_hashes_by_prefix(chat_id, " FAILED_UP ") == 1

    with sqlite3.connect(mock_db_path) as conn:
        row = conn.execute(
            "SELECT status, meal_id FROM photo_ingestions WHERE chat_id = ? AND image_hash = ?",
            (chat_id, failed_hash),
        ).fetchone()
        saved_row = conn.execute(
            "SELECT status, meal_id FROM photo_ingestions WHERE chat_id = ? AND image_hash = ?",
            (chat_id, saved_hash),
        ).fetchone()
    # Tombstone, not delete: the row survives with status 'deleted'
    assert row == ("deleted", None)
    assert saved_row == ("saved", 7)

    # The tombstone keeps suppressing the nightly /reconcile auto-resurrection
    assert failed_hash in database.get_reserved_photo_hashes(chat_id)
    # ...but stays reclaimable by the deliberate Telegram re-send path
    assert database.reserve_photo_hash(
        chat_id, failed_hash, "telegram", reclaim_statuses={"deleted"}
    ) is True

    # No match / empty prefix tombstone nothing
    assert database.discard_failed_photo_hashes_by_prefix(chat_id, "zzz") == 0
    assert database.discard_failed_photo_hashes_by_prefix(chat_id, "") == 0


def test_save_meal_normalizes_image_hash():
    chat_id = 12345
    database.save_meal(
        chat_id,
        "2026-07-01",
        "12:00",
        "2026-07-01T12:00:00",
        "telegram",
        "  ABC123  ",
        "file1",
        {"is_food": True},
    )

    assert database.meal_image_hash_exists(chat_id, "abc123") is True
    # The existing-meal guard must see the normalized hash and block a re-log
    assert database.reserve_photo_hash(chat_id, "ABC123", "api_upload") is False


def test_get_recent_meals_window_spans_exactly_n_user_local_days(monkeypatch):
    monkeypatch.setattr(
        database, "get_android_timezone", lambda device_name="android_watcher": "+0800"
    )
    chat_id = 12345
    today = database.user_local_now().date()
    for offset in range(4):
        day = (today - timedelta(days=offset)).isoformat()
        database.save_meal(
            chat_id,
            day,
            "12:00",
            f"{day}T12:00:00",
            "api",
            f"window_hash_{offset}",
            f"file{offset}",
            {"is_food": True},
        )

    dates_3 = {meal["date"] for meal in database.get_recent_meals(chat_id, days=3)}
    assert dates_3 == {(today - timedelta(days=o)).isoformat() for o in range(3)}

    dates_1 = {meal["date"] for meal in database.get_recent_meals(chat_id, days=1)}
    assert dates_1 == {today.isoformat()}


def test_stale_processing_hash_excluded_from_reserved_hashes(mock_db_path):
    chat_id = 12345
    fresh_hash = "fresh_processing_hash"
    stale_hash = "stale_processing_hash"
    old_saved_hash = "old_saved_hash"

    assert database.reserve_photo_hash(chat_id, fresh_hash, "telegram") is True
    assert database.reserve_photo_hash(chat_id, stale_hash, "telegram") is True
    assert database.reserve_photo_hash(chat_id, old_saved_hash, "telegram") is True
    database.mark_photo_hash_status(chat_id, old_saved_hash, "saved", meal_id=1)

    backdated = (datetime.now() - timedelta(hours=7)).isoformat()
    with sqlite3.connect(mock_db_path) as conn:
        conn.execute(
            "UPDATE photo_ingestions SET last_seen_at = ? WHERE image_hash IN (?, ?)",
            (backdated, stale_hash, old_saved_hash),
        )
        conn.commit()

    reserved = database.get_reserved_photo_hashes(chat_id)
    assert fresh_hash in reserved
    # Only 'processing' rows go stale; terminal statuses stay reserved forever
    assert old_saved_hash in reserved
    assert stale_hash not in reserved


def test_delete_meal_marks_photo_ingestion_deleted(mock_db_path):
    chat_id = 12345
    image_hash = "Deleted_Latte_Hash"
    normalized = "deleted_latte_hash"

    assert database.reserve_photo_hash(chat_id, image_hash, "telegram") is True
    meal_id = database.save_meal(
        chat_id,
        "2026-07-01",
        "12:00",
        "2026-07-01T12:00:00",
        "telegram",
        image_hash,
        "file1",
        {"is_food": True, "total_calories": 500},
    )
    database.mark_photo_hash_status(chat_id, image_hash, "saved", meal_id=meal_id)

    database.delete_meal(meal_id, chat_id)

    with sqlite3.connect(mock_db_path) as conn:
        row = conn.execute(
            "SELECT status, meal_id FROM photo_ingestions WHERE chat_id = ? AND image_hash = ?",
            (chat_id, normalized),
        ).fetchone()
    assert row == ("deleted", None)
    # The hash must stay reserved so /reconcile does not auto re-log the photo
    assert normalized in database.get_reserved_photo_hashes(chat_id)
