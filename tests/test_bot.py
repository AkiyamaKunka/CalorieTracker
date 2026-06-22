import pytest
from datetime import date
from telegram_bot import format_food_result, format_daily_totals

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
        
    import telegram_bot
    monkeypatch.setattr(telegram_bot, "get_todays_meals", mock_get_meals)
    
    result = format_daily_totals(12345)
    
    # The sum should be 800 kcal, 50g protein, 70g carbs, 30g fat
    assert "800 kcal" in result
    assert "P: 50g" in result
    assert "C: 70g" in result
    assert "F: 30g" in result
