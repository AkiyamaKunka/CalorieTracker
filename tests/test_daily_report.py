import unittest
import json
import sys
from pathlib import Path
from unittest.mock import patch, mock_open

# Add parent dir to path so we can import from CalorieTracker
sys.path.append(str(Path(__file__).parent.parent))

from daily_report import generate_report

class TestDailyReport(unittest.TestCase):
    def test_generate_report(self):
        """Test macro aggregation and markdown generation."""
        mock_meals = [
            {
                "time": "08:00 AM",
                "analysis": {
                    "is_food": True,
                    "meal_description": "Breakfast",
                    "total_calories": 300,
                    "total_protein_g": 10,
                    "total_carbs_g": 40,
                    "total_fat_g": 5,
                    "food_items": [
                        {"name": "Oatmeal", "estimated_calories": 300, "protein_g": 10, "carbs_g": 40, "fat_g": 5}
                    ]
                }
            },
            {
                "time": "01:00 PM",
                "analysis": {
                    "is_food": True,
                    "meal_description": "Lunch",
                    "total_calories": 600,
                    "total_protein_g": 30,
                    "total_carbs_g": 60,
                    "total_fat_g": 20,
                    "food_items": [
                        {"name": "Chicken Sandwich", "estimated_calories": 600, "protein_g": 30, "carbs_g": 60, "fat_g": 20}
                    ]
                }
            }
        ]
        
        with patch('daily_report.get_meals_for_date', return_value=mock_meals):
            report = generate_report("2026-06-20")
        
        # Verify meal listing
        self.assertIn("1. Breakfast", report)
        self.assertIn("2. Lunch", report)
        
        # Verify macro aggregations (300 + 600 = 900)
        self.assertIn("~900 kcal", report)
        self.assertIn("40g", report)  # Protein
        self.assertIn("100g", report) # Carbs
        self.assertIn("25g", report)    # Fat
        self.assertIn("2", report) # Meals logged

    def test_generate_report_with_nulls(self):
        """Test report generation when macros are None."""
        mock_meals = [
            {
                "time": "08:00 AM",
                "analysis": {
                    "is_food": True,
                    "meal_description": "Snack",
                    "total_calories": 150,
                    "total_protein_g": None,
                    "total_carbs_g": None,
                    "total_fat_g": None,
                    "food_items": []
                }
            }
        ]
        
        with patch('daily_report.get_meals_for_date', return_value=mock_meals):
            report = generate_report("2026-06-20")
            
        self.assertIn("~150 kcal", report)
        self.assertIn("0g", report) # Protein
        self.assertIn("0g", report) # Carbs
        self.assertIn("0g", report) # Fat

if __name__ == '__main__':
    unittest.main()
