import json
from pathlib import Path
from database import save_meal, init_db

ALLOWED_CHAT_ID = 8675416366

def migrate():
    init_db()
    meals_file = Path.home() / "CalorieTracker" / "logs" / "telegram_meals.json"
    if not meals_file.exists():
        print("No json file found.")
        return

    with open(meals_file, 'r') as f:
        meals = json.load(f)

    count = 0
    for meal in meals:
        # Avoid migrating completely non-food items
        if not meal.get("analysis", {}).get("is_food"):
            continue

        save_meal(
            chat_id=ALLOWED_CHAT_ID,
            date_str=meal.get("date", ""),
            time_str=meal.get("time", ""),
            timestamp_str=meal.get("timestamp", ""),
            source=meal.get("source", "telegram"),
            image_hash=meal.get("image_hash", ""),
            file_id=meal.get("file_id", ""),
            analysis=meal.get("analysis", {})
        )
        count += 1
    
    print(f"Successfully migrated {count} meals to SQLite.")

if __name__ == "__main__":
    migrate()
