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
