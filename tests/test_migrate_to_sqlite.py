import json
import pytest
import database
import migrate_to_sqlite

TEST_CHAT_ID = 12345


@pytest.fixture(autouse=True)
def mock_db_path(monkeypatch, tmp_path):
    """Override the DB_PATH to use a temporary file for tests."""
    temp_db = tmp_path / "test_meals.db"
    monkeypatch.setattr(database, "DB_PATH", temp_db)
    database.init_db()
    yield temp_db


@pytest.fixture
def legacy_meals_file(monkeypatch, tmp_path):
    entries = [
        {
            "date": "2026-07-01",
            "time": "12:00:00",
            "timestamp": "2026-07-01T12:00:00",
            "source": "telegram",
            "image_hash": "hash_one",
            "file_id": "f1",
            "analysis": {"is_food": True, "total_calories": 500},
        },
        {
            # Legacy entry missing date/time; hash needs normalizing
            "timestamp": "2026-07-02T08:30:00",
            "source": "telegram",
            "image_hash": "HASH_TWO",
            "file_id": "f2",
            "analysis": {"is_food": True, "total_calories": 300},
        },
        {
            "date": "2026-07-02",
            "time": "09:00:00",
            "timestamp": "2026-07-02T09:00:00",
            "source": "telegram",
            "image_hash": "hash_three",
            "file_id": "f3",
            "analysis": {"is_food": False},
        },
        {
            # Oldest legacy shape: ONLY analysis/date/filename/time keys —
            # no timestamp and no image_hash. Analysis keys deliberately
            # unsorted so dedupe must compare with sort_keys=True.
            "analysis": {"total_calories": 250, "is_food": True, "meal_description": "Greek yogurt"},
            "date": "2026-07-03",
            "filename": "IMG_1234.jpg",
            "time": "07:45:00",
        },
    ]
    meals_file = tmp_path / "telegram_meals.json"
    meals_file.write_text(json.dumps(entries))
    monkeypatch.setattr(migrate_to_sqlite, "MEALS_FILE", meals_file)
    monkeypatch.setattr(migrate_to_sqlite, "ALLOWED_CHAT_ID", TEST_CHAT_ID)
    return meals_file


def test_migrate_skips_non_food_and_fills_missing_fields(legacy_meals_file):
    migrate_to_sqlite.migrate()

    meals = database.get_meals(TEST_CHAT_ID, "2026-07-01", "2026-07-02")
    assert len(meals) == 2

    by_hash = {meal["image_hash"]: meal for meal in meals}
    assert set(by_hash) == {"hash_one", "hash_two"}
    assert by_hash["hash_two"]["date"] == "2026-07-02"
    assert by_hash["hash_two"]["time"] == "08:30:00"


def test_migrate_twice_does_not_duplicate(legacy_meals_file, capsys):
    migrate_to_sqlite.migrate()
    migrate_to_sqlite.migrate()

    meals = database.get_meals(TEST_CHAT_ID, "2026-07-01", "2026-07-03")
    assert len(meals) == 3

    output = capsys.readouterr().out
    assert "Migrated 3" in output
    assert "skipped 3" in output


def test_migrate_twice_keeps_single_copy_of_timestampless_hashless_entry(legacy_meals_file):
    migrate_to_sqlite.migrate()
    migrate_to_sqlite.migrate()

    meals = database.get_meals(TEST_CHAT_ID, "2026-07-03", "2026-07-03")
    assert len(meals) == 1

    meal = meals[0]
    assert meal["timestamp"] == ""
    assert meal["image_hash"] == ""
    assert meal["time"] == "07:45:00"
    assert meal["analysis"] == {
        "total_calories": 250,
        "is_food": True,
        "meal_description": "Greek yogurt",
    }


def test_migrate_missing_file_is_noop(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(migrate_to_sqlite, "MEALS_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(migrate_to_sqlite, "ALLOWED_CHAT_ID", TEST_CHAT_ID)

    migrate_to_sqlite.migrate()

    assert "No json file found." in capsys.readouterr().out
    assert database.get_meals(TEST_CHAT_ID, "2026-01-01", "2026-12-31") == []
