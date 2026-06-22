import unittest
from unittest.mock import patch
from datetime import date, timedelta
import telegram_bot

class TestHistory(unittest.TestCase):
    def test_format_history(self):
        today = date.today()
        yesterday = today - timedelta(days=1)
        mock_meals = [
            {"date": today.isoformat(), "analysis": {"is_food": True, "total_calories": 500}},
            {"date": today.isoformat(), "analysis": {"is_food": True, "total_calories": 400}},
            {"date": yesterday.isoformat(), "analysis": {"is_food": True, "total_calories": 1000}},
        ]
        with patch('telegram_bot.load_meals_log', return_value=mock_meals):
            result = telegram_bot.format_history(days=7)
            
        self.assertIn("~900 kcal", result)
        self.assertIn("~1000 kcal", result)
        self.assertIn("Today", result)
        self.assertIn("Average:", result)

if __name__ == '__main__':
    unittest.main()
