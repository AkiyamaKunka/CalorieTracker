import json
from datetime import datetime
from pathlib import Path
from config import TELEGRAM_CHAT_ID
import database
from database import init_db

ALLOWED_CHAT_ID = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else 0
MEALS_FILE = Path.home() / "CalorieTracker" / "logs" / "telegram_meals.json"

def migrate():
    if not ALLOWED_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID must be set before running migration.")

    init_db()
    if not MEALS_FILE.exists():
        print("No json file found.")
        return

    try:
        with open(MEALS_FILE, 'r') as f:
            meals = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SystemExit(f"ERROR: {MEALS_FILE} is not valid JSON ({e}); nothing migrated.")
    if not isinstance(meals, list):
        raise SystemExit(f"ERROR: {MEALS_FILE} must contain a JSON array of meals; nothing migrated.")

    migrated = 0
    skipped = 0
    skipped_bad = 0
    with database._connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT timestamp, image_hash, date, time, analysis FROM meals WHERE chat_id = ?",
            (ALLOWED_CHAT_ID,),
        )
        existing_timestamps = set()
        existing_hashes = set()
        # Oldest legacy entries carry only analysis/date/filename/time (no
        # timestamp, no image_hash), so dedupe those on content instead.
        existing_fallback_keys = set()
        for timestamp, image_hash, date_val, time_val, analysis_json in cursor.fetchall():
            if timestamp:
                existing_timestamps.add(timestamp)
            if image_hash:
                existing_hashes.add(image_hash)
            if not timestamp and not image_hash:
                try:
                    analysis_key = json.dumps(json.loads(analysis_json or "{}"), sort_keys=True)
                except (TypeError, ValueError):
                    analysis_key = str(analysis_json)
                existing_fallback_keys.add((date_val or "", time_val or "", analysis_key))

        for meal in meals:
            # Legacy files can contain malformed entries; skip rather than
            # letting one bad element abort the whole migration.
            if not isinstance(meal, dict) or not isinstance(meal.get("analysis"), dict):
                skipped_bad += 1
                continue
            # Avoid migrating completely non-food items
            if not meal.get("analysis", {}).get("is_food"):
                continue

            timestamp_str = meal.get("timestamp", "")
            image_hash = database._normalize_image_hash(meal.get("image_hash", ""))
            fallback_key = None
            if timestamp_str:
                already_present = timestamp_str in existing_timestamps
            elif image_hash:
                already_present = image_hash in existing_hashes
            else:
                fallback_key = (
                    meal.get("date", ""),
                    meal.get("time", ""),
                    json.dumps(meal.get("analysis", {}), sort_keys=True),
                )
                already_present = fallback_key in existing_fallback_keys
            if already_present:
                skipped += 1
                continue

            # Legacy entries may lack date/time; derive them from the timestamp
            date_str = meal.get("date", "")
            time_str = meal.get("time", "")
            if timestamp_str and (not date_str or not time_str):
                try:
                    ts = datetime.fromisoformat(timestamp_str)
                    date_str = date_str or ts.date().isoformat()
                    time_str = time_str or ts.strftime("%H:%M:%S")
                except ValueError:
                    pass

            cursor.execute("""
                INSERT INTO meals (chat_id, date, time, timestamp, source, image_hash, file_id, analysis, corrected)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ALLOWED_CHAT_ID,
                date_str,
                time_str,
                timestamp_str,
                meal.get("source", "telegram"),
                image_hash,
                meal.get("file_id", ""),
                json.dumps(meal.get("analysis", {})),
                False,
            ))
            if timestamp_str:
                existing_timestamps.add(timestamp_str)
            if image_hash:
                existing_hashes.add(image_hash)
            if fallback_key is not None:
                existing_fallback_keys.add(fallback_key)
            migrated += 1

        conn.commit()

    summary = f"Migrated {migrated} meals to SQLite; skipped {skipped} already-migrated entries."
    if skipped_bad:
        summary += f" Ignored {skipped_bad} malformed entr{'y' if skipped_bad == 1 else 'ies'}."
    print(summary)

if __name__ == "__main__":
    migrate()
