import pytest
import sqlite3
from datetime import datetime, date, timedelta
import database


def test_find_photo_hash_by_prefix():
    full = "ab" * 16
    assert database.reserve_photo_hash(1, full, "test") is True
    assert database.find_photo_hash_by_prefix(1, full[:12]) == full
    assert database.find_photo_hash_by_prefix(1, "no such prefix") is None
    assert database.find_photo_hash_by_prefix(1, "") is None
    # Ambiguous prefixes resolve to None so callers fall back to rehashing.
    sibling = full[:12] + "f" * 20
    assert database.reserve_photo_hash(1, sibling, "test") is True
    assert database.find_photo_hash_by_prefix(1, full[:12]) is None

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

    # Update heartbeat. Sandwich the write between two host-clock reads so
    # the date assertion cannot straddle midnight (write at 23:59:59.9,
    # assert at 00:00:00.1 used to fail once a night).
    date_before = date.today()
    database.update_android_heartbeat("android_watcher")
    date_after = date.today()

    last_ping = database.get_last_android_heartbeat("android_watcher")
    assert last_ping is not None

    # Ensure it's a valid ISO string
    dt = datetime.fromisoformat(last_ping)
    assert dt.date() in (date_before, date_after)


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
    # Freeze the clock: get_today_hashes recomputes user_local_today()
    # internally, so a midnight rollover between this read and the query
    # would empty the expected list.
    frozen_now = database.user_local_now()
    monkeypatch.setattr(
        database, "user_local_now", lambda device_name="android_watcher": frozen_now
    )
    user_today = frozen_now.date()
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
    # Freeze the clock: get_recent_meals recomputes its user-local window
    # internally, so a midnight rollover mid-test would shift the expected
    # date sets by one day.
    frozen_now = database.user_local_now()
    monkeypatch.setattr(
        database, "user_local_now", lambda device_name="android_watcher": frozen_now
    )
    today = frozen_now.date()
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


def test_body_weight_upsert_one_per_day():
    assert database.save_body_weight(1, "2026-07-10", 72.5, note="am")
    database.save_body_weight(1, "2026-07-10", 72.1, source="telegram")  # same day overwrites
    database.save_body_weight(1, "2026-07-11", 71.9)

    weights = database.get_body_weights(1, "2026-07-01", "2026-07-31")
    assert len(weights) == 2                          # one row per day
    by_date = {w["date"]: w for w in weights}
    assert by_date["2026-07-10"]["weight_kg"] == 72.1  # last write wins
    assert by_date["2026-07-10"]["source"] == "telegram"
    assert database.get_latest_body_weight(1)["date"] == "2026-07-11"
    assert database.get_latest_body_weight(999) is None


def test_workouts_append_and_range():
    database.save_workout(1, "2026-07-10", "strength", muscle_groups="legs", duration_min=45)
    database.save_workout(1, "2026-07-10", "mobility", notes="stretch",
                          details={"exercises": ["squat"]})
    database.save_workout(1, "2026-07-09", "strength", muscle_groups="chest")

    same_day = database.get_workouts(1, "2026-07-10", "2026-07-10")
    assert len(same_day) == 2                          # append-only, both kept
    assert same_day[1]["details"] == {"exercises": ["squat"]}  # JSON round-trips
    assert len(database.get_workouts(1, "2026-07-01", "2026-07-31")) == 3


def test_activities_upsert_by_external_id_and_manual_inserts():
    # A re-synced Garmin activity (same source+external_id) updates in place.
    database.save_activity(1, "2026-07-10", "run", source="garmin",
                           active_calories=600, distance_km=9.0, external_id="g123")
    database.save_activity(1, "2026-07-10", "run", source="garmin",
                           active_calories=640, distance_km=9.2, external_id="g123")
    garmin_rows = database.get_activities(1, "2026-07-10", "2026-07-10")
    assert len(garmin_rows) == 1
    assert garmin_rows[0]["active_calories"] == 640

    # Manual rows (external_id NULL) always insert — no false collision.
    database.save_activity(1, "2026-07-10", "walk", source="manual", active_calories=100)
    database.save_activity(1, "2026-07-10", "walk", source="manual", active_calories=120)
    assert len(database.get_activities(1, "2026-07-10", "2026-07-10")) == 3


def test_fitness_profile_partial_upsert_preserves_unset_columns():
    assert database.get_fitness_profile(1) is None
    database.save_fitness_profile(1, diet_mode="keto", target_protein_g=140)
    database.save_fitness_profile(1, vdot=50.0, goal_race_date="2026-11-01")  # different columns

    profile = database.get_fitness_profile(1)
    assert profile["diet_mode"] == "keto"             # not clobbered by the second upsert
    assert profile["target_protein_g"] == 140
    assert profile["vdot"] == 50.0
    assert profile["goal_race_date"] == "2026-11-01"

    database.save_fitness_profile(1, extra={"note": "cutting"})
    assert database.get_fitness_profile(1)["extra"] == {"note": "cutting"}  # JSON round-trips
    # Non-whitelisted keys are ignored, not injected.
    database.save_fitness_profile(1, evil="DROP TABLE meals")
    assert "evil" not in database.get_fitness_profile(1)


# ═══ Appended fitness-table coverage: isolation, boundaries, migration ═══
import json


def test_fitness_tables_cross_chat_isolation():
    # Defense-in-depth: if a stranger ever messages the bot, no fitness getter
    # may leak the owner's weight, workouts, activities or profile.
    database.save_body_weight(1, "2026-07-10", 72.5)
    database.save_workout(1, "2026-07-10", "strength", muscle_groups="legs")
    database.save_activity(1, "2026-07-10", "run", source="garmin",
                           distance_km=9.0, external_id="g1")
    database.save_fitness_profile(1, diet_mode="keto", vdot=50.0)

    assert database.get_body_weights(2, "2026-01-01", "2026-12-31") == []
    assert database.get_latest_body_weight(2) is None
    assert database.get_workouts(2, "2026-01-01", "2026-12-31") == []
    assert database.get_activities(2, "2026-01-01", "2026-12-31") == []
    assert database.get_fitness_profile(2) is None

    # And the owner still sees every row they wrote.
    assert len(database.get_body_weights(1, "2026-01-01", "2026-12-31")) == 1
    assert len(database.get_workouts(1, "2026-01-01", "2026-12-31")) == 1
    assert len(database.get_activities(1, "2026-01-01", "2026-12-31")) == 1
    assert database.get_fitness_profile(1)["diet_mode"] == "keto"


@pytest.mark.parametrize("save_row,get_rows", [
    (lambda d: database.save_body_weight(1, d, 72.0),
     lambda s, e: database.get_body_weights(1, s, e)),
    (lambda d: database.save_workout(1, d, "strength"),
     lambda s, e: database.get_workouts(1, s, e)),
    (lambda d: database.save_activity(1, d, "run", source="manual"),
     lambda s, e: database.get_activities(1, s, e)),
], ids=["body_weight", "workouts", "activities"])
def test_fitness_date_ranges_include_both_boundary_days(save_row, get_rows):
    # An off-by-one here silently drops the first or last day of every weekly
    # report window — assert both edges included, both neighbours excluded.
    for day in ("2026-07-09", "2026-07-10", "2026-07-12", "2026-07-13"):
        save_row(day)

    rows = get_rows("2026-07-10", "2026-07-12")
    assert [r["date"] for r in rows] == ["2026-07-10", "2026-07-12"]


def test_init_db_upgrades_legacy_schema_without_losing_meals(monkeypatch, tmp_path):
    # The real upgrade path on the VM: a pre-fitness meals.db must gain the
    # four fitness tables in place, with existing meal history untouched.
    legacy = tmp_path / "legacy_meals.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute("""
            CREATE TABLE meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT,
                image_hash TEXT,
                file_id TEXT,
                analysis TEXT NOT NULL,
                corrected BOOLEAN DEFAULT 0
            )
        """)
        # Oldest deployed shape: heartbeats existed before the timezone column.
        conn.execute("""
            CREATE TABLE heartbeats (
                device_name TEXT PRIMARY KEY,
                last_ping_time TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE photo_ingestions (
                chat_id INTEGER NOT NULL,
                image_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                source TEXT,
                status TEXT NOT NULL DEFAULT 'processing',
                meal_id INTEGER,
                PRIMARY KEY (chat_id, image_hash)
            )
        """)
        conn.execute(
            "INSERT INTO meals (chat_id, date, time, timestamp, source, image_hash,"
            " file_id, analysis, corrected) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (777, "2026-07-01", "12:00", "2026-07-01T12:00:00", "telegram",
             "legacyhash", "file1",
             json.dumps({"is_food": True, "total_calories": 640}), 0),
        )
        conn.commit()

    monkeypatch.setattr(database, "DB_PATH", legacy)
    database.init_db()

    with sqlite3.connect(legacy) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        heartbeat_cols = {r[1] for r in conn.execute("PRAGMA table_info(heartbeats)")}
    assert {"body_weight", "workouts", "activities", "fitness_profile"} <= tables
    assert "timezone" in heartbeat_cols  # the old ALTER migration still runs too

    meals = database.get_meals(777, "2026-07-01", "2026-07-01")
    assert len(meals) == 1
    assert meals[0]["analysis"]["total_calories"] == 640
    # The new accessors work immediately on the upgraded file.
    database.save_body_weight(777, "2026-07-14", 72.0)
    assert database.get_latest_body_weight(777)["weight_kg"] == 72.0


def test_save_activity_resync_updates_every_mutable_column_in_place():
    # A Garmin re-sync (even one correcting the activity's date across
    # midnight) must rewrite ALL mutable columns of the SAME row rather than
    # duplicating the workout or leaving stale HR/elevation behind.
    first_id = database.save_activity(
        1, "2026-07-10", "running", source="garmin", active_calories=600,
        distance_km=9.0, duration_min=48.0, avg_hr_bpm=150, elevation_gain_m=40.0,
        start_time="2026-07-10 06:00:00", external_id="garmin-777",
        notes="easy", raw={"rev": 1})
    second_id = database.save_activity(
        1, "2026-07-11", "trail_running", source="garmin", active_calories=712,
        distance_km=11.3, duration_min=63.5, avg_hr_bpm=156, elevation_gain_m=210.0,
        start_time="2026-07-11 06:30:00", external_id="garmin-777",
        notes="hills", raw={"rev": 2})

    rows = database.get_activities(1, "2026-07-01", "2026-07-31")
    assert len(rows) == 1
    assert second_id == first_id  # stable row id across re-syncs
    row = rows[0]
    assert row["date"] == "2026-07-11"
    assert row["activity_type"] == "trail_running"
    assert row["active_calories"] == 712
    assert row["distance_km"] == 11.3
    assert row["duration_min"] == 63.5
    assert row["avg_hr_bpm"] == 156
    assert row["elevation_gain_m"] == 210.0
    assert row["start_time"] == "2026-07-11 06:30:00"
    assert row["notes"] == "hills"
    assert row["raw"] == {"rev": 2}

    # A different external_id is a genuinely different workout: second row.
    database.save_activity(1, "2026-07-11", "running", source="garmin",
                           external_id="garmin-778")
    assert len(database.get_activities(1, "2026-07-01", "2026-07-31")) == 2


def test_fitness_profile_explicit_none_clears_column_and_row_shape_complete():
    # Cancelling a goal race: an explicit goal_race_date=None must CLEAR the
    # stored value (None is an update, not a skip), leaving siblings intact.
    database.save_fitness_profile(1, goal_race_date="2026-11-01",
                                  race_label="Shanghai Marathon", vdot=50.0)
    database.save_fitness_profile(1, goal_race_date=None, race_label=None)

    profile = database.get_fitness_profile(1)
    assert profile["goal_race_date"] is None
    assert profile["race_label"] is None
    assert profile["vdot"] == 50.0

    # extra accepts list payloads too (dict already covered) and round-trips.
    database.save_fitness_profile(1, extra=["cutting", "carb cycling"])
    profile = database.get_fitness_profile(1)
    assert profile["extra"] == ["cutting", "carb cycling"]

    # Repeated partial upserts never shrink the row: all 18 columns present.
    assert set(profile) == {"chat_id", "updated_at", *database._FITNESS_PROFILE_COLUMNS}
    assert len(profile) == 18


def test_get_latest_body_weight_uses_max_date_not_insertion_order():
    # Backfilling a missed weigh-in (older date, newer row id) must not
    # masquerade as the "current" weight that reports and targets key off.
    database.save_body_weight(1, "2026-07-12", 71.8)
    database.save_body_weight(1, "2026-07-10", 72.6)  # backfill: higher id, earlier date

    latest = database.get_latest_body_weight(1)
    assert latest["date"] == "2026-07-12"
    assert latest["weight_kg"] == 71.8


# ═══ Appended WAL concurrency hammer: parallel writers on one temp DB ═══
# The bot process, the nightly report cron and a Garmin re-sync can all hit
# the same meals.db at once; each accessor opens its own connection, so the
# WAL journal plus the 30s busy timeout in _connect() are what keep racing
# writers from raising or corrupting rows. These tests pin that contract.
from concurrent.futures import ThreadPoolExecutor

_HAMMER_WRITES = 60
_HAMMER_THREADS = 8
_HAMMER_TIMEOUT = 120  # generous: WAL writers serialize behind the 30s busy timeout


def _hammer(fn, args_list):
    """Run fn over args_list on 8 threads; joins all threads, re-raises the
    first worker exception (the 'no exception under contention' assertion)."""
    with ThreadPoolExecutor(max_workers=_HAMMER_THREADS) as pool:
        futures = [pool.submit(fn, *args) for args in args_list]
        return [f.result(timeout=_HAMMER_TIMEOUT) for f in futures]


def test_wal_concurrent_same_day_weigh_ins_collapse_to_one_row(mock_db_path):
    # The autouse fixture's init_db must have left the temp file in WAL
    # mode — the journal mode this concurrency contract is pinned against.
    with sqlite3.connect(mock_db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    weights = [60.0 + i for i in range(_HAMMER_WRITES)]
    ids = _hammer(database.save_body_weight,
                  [(1, "2026-07-14", w) for w in weights])

    rows = database.get_body_weights(1, "2026-07-14", "2026-07-14")
    assert len(rows) == 1                        # UNIQUE(chat_id, date) upsert held
    assert rows[0]["weight_kg"] in weights       # some writer's value, uncorrupted
    assert set(ids) == {rows[0]["id"]}           # every upsert reported the same row


def test_wal_concurrent_resyncs_of_one_external_id_collapse_to_one_row(mock_db_path):
    def resync(cal):
        return database.save_activity(1, "2026-07-14", "run", source="garmin",
                                      active_calories=cal, external_id="garmin-1")

    calories = [float(100 + i) for i in range(_HAMMER_WRITES)]
    ids = _hammer(resync, [(c,) for c in calories])

    rows = database.get_activities(1, "2026-07-14", "2026-07-14")
    assert len(rows) == 1                        # UNIQUE(chat, source, external_id) held
    assert rows[0]["active_calories"] in calories
    assert set(ids) == {rows[0]["id"]}           # stable row id across racing re-syncs


def test_wal_concurrent_workout_appends_all_survive(mock_db_path):
    def append(i):
        return database.save_workout(1, "2026-07-14",
                                     "strength" if i % 2 else "mobility",
                                     muscle_groups="legs", notes=f"set-{i}")

    ids = _hammer(append, [(i,) for i in range(_HAMMER_WRITES)])

    rows = database.get_workouts(1, "2026-07-14", "2026-07-14")
    assert len(rows) == _HAMMER_WRITES           # append-only: no lost inserts
    assert len(set(ids)) == _HAMMER_WRITES       # each append got its own row id
    assert {r["notes"] for r in rows} == {f"set-{i}" for i in range(_HAMMER_WRITES)}
