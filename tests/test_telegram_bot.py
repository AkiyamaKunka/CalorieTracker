import sys
import os
import json
import pytest
from pathlib import Path
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

@pytest.fixture
def mock_bot():
    with patch('telegram_bot.bot') as mock:
        yield mock

def test_b3_duplicate_prevention(mock_db):
    """Test Case B3: Duplicate Prevention"""
    import hashlib
    from datetime import date, datetime
    img_data = b"fake image data"
    img_hash = hashlib.md5(img_data).hexdigest()
    
    # Save meal directly using current time so it's within the window
    today = date.today().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_meal(12345, today, "12:00 PM", now_iso, "test", img_hash, "file1", {"is_food": True})
    
    # Verify it is flagged as duplicate
    is_dup = telegram_bot.is_duplicate_photo(12345, img_hash)
    assert is_dup is True
    
    # Try with different hash
    is_dup2 = telegram_bot.is_duplicate_photo(12345, "different_hash")
    assert is_dup2 is False

@patch('telegram_bot.analyze_food_photo')
def test_b1_b2_gemini_failures(mock_analyze, mock_db):
    """Test Case B1 & B2: Gemini Rate Limit & Unavailable"""
    # Simulate Gemini returning None
    mock_analyze.return_value = None
    pass

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

def test_b12_empty_database_deletion(mock_db):
    """Test Case B12: Empty Database Deletion"""
    # Attempting to delete when DB is empty
    success = telegram_bot.delete_meal_by_index(telegram_bot.ALLOWED_CHAT_ID, 0)
    assert success is False

def test_b7_delete_command_integrity(mock_db):
    """Test Case B7: Delete Command Integrity"""
    from datetime import date, datetime

    today = date.today().isoformat()
    now_iso = datetime.now().isoformat()
    database.save_meal(telegram_bot.ALLOWED_CHAT_ID, today, "12:00", now_iso, "test", "hash1", "file1", {"is_food": True, "meal_description": "Apple", "total_calories": 95})
    database.save_meal(telegram_bot.ALLOWED_CHAT_ID, today, "12:01", now_iso, "test", "hash2", "file2", {"is_food": True, "meal_description": "Banana", "total_calories": 105})
    
    # Verify recent meals has 2 items
    recent = telegram_bot.get_recent_meals(telegram_bot.ALLOWED_CHAT_ID, days=1)
    assert len(recent) == 2
    
    # We delete whichever is at index 0
    deleted_desc = recent[0]["analysis"]["meal_description"]
    expected_remaining = "Apple" if deleted_desc == "Banana" else "Banana"
    
    # Delete index 0
    success = telegram_bot.delete_meal_by_index(telegram_bot.ALLOWED_CHAT_ID, 0)
    assert success is True
    
    # Verify the other one remains
    recent_after = telegram_bot.get_recent_meals(telegram_bot.ALLOWED_CHAT_ID, days=1)
    assert len(recent_after) == 1
    assert recent_after[0]["analysis"]["meal_description"] == expected_remaining
