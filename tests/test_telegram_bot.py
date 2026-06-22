import unittest
import json
from unittest.mock import patch
import sys
from pathlib import Path
from datetime import datetime, date

# Add parent dir to path so we can import from CalorieTracker
sys.path.append(str(Path(__file__).parent.parent))

from telegram_bot import (
    parse_ai_json,
    format_food_result,
    format_today_summary,
    format_meals_list,
    format_daily_totals,
    is_duplicate_photo
)

class TestTelegramBot(unittest.TestCase):
    def test_parse_ai_json_standard(self):
        """Test parsing standard JSON without markdown fences."""
        json_str = '{"intent": "new_meal", "reason": "Test"}'
        result = parse_ai_json(json_str)
        self.assertEqual(result["intent"], "new_meal")

    def test_parse_ai_json_with_markdown(self):
        """Test parsing JSON that is wrapped in markdown code blocks."""
        json_str = "```json\n{\n  \"intent\": \"new_meal\"\n}\n```"
        result = parse_ai_json(json_str)
        self.assertEqual(result["intent"], "new_meal")

    def test_format_food_result_with_nulls(self):
        """Test formatting a meal where protein/carbs/fat are missing or null."""
        analysis = {
            "is_food": True,
            "meal_description": "Apple",
            "food_items": [
                {"name": "Apple", "estimated_calories": 95}
                # Missing macros entirely
            ],
            "total_calories": 95,
            # null macros (simulating json 'null')
            "total_protein_g": None,
            "total_carbs_g": None,
            "total_fat_g": None
        }
        result = format_food_result(analysis)
        self.assertIn("P:0g | C:0g | F:0g", result)
        self.assertIn("~95 kcal", result)

    @patch('telegram_bot.get_todays_meals')
    def test_format_today_summary_with_nulls(self, mock_get_meals):
        """Test the daily summary doesn't crash when meals have null macros."""
        mock_get_meals.return_value = [
            {
                "analysis": {
                    "is_food": True,
                    "total_calories": 500,
                    "total_protein_g": 20,
                    "total_carbs_g": 50,
                    "total_fat_g": 20
                }
            },
            {
                "analysis": {
                    "is_food": True,
                    "total_calories": 100,
                    "total_protein_g": None,  # Bug trigger!
                    "total_carbs_g": None,
                    "total_fat_g": None
                }
            }
        ]
        result = format_today_summary()
        self.assertIn("600 kcal", result)
        self.assertIn("Protein: 20g", result)
        self.assertIn("Carbs: 50g", result)

    @patch('telegram_bot.get_todays_meals')
    def test_format_meals_list_with_nulls(self, mock_get_meals):
        """Test the meals list handles null macros."""
        mock_get_meals.return_value = [
            {
                "time": "12:00 PM",
                "analysis": {
                    "is_food": True,
                    "meal_description": "Test Meal",
                    "total_calories": 100,
                    "total_protein_g": None
                }
            }
        ]
        result = format_meals_list()
        self.assertIn("~100 kcal", result)
        self.assertIn("P:0g", result)

    @patch('telegram_bot.get_todays_meals')
    def test_is_duplicate_photo(self, mock_get_meals):
        """Test that duplicate hashes within the window are rejected."""
        mock_get_meals.return_value = [
            {
                "image_hash": "hash123",
                "timestamp": datetime.now().isoformat(),
                "analysis": {"is_food": True}
            }
        ]
        self.assertTrue(is_duplicate_photo("hash123"))
        self.assertFalse(is_duplicate_photo("hash456"))

if __name__ == '__main__':
    unittest.main()
