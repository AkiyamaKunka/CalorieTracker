import pytest
from datetime import date, datetime, timedelta
from telegram_bot import format_food_result, format_daily_totals, is_duplicate_photo, analyze_food_photo
import telegram_bot

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
