import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Optional

DB_PATH = Path.home() / "CalorieTracker" / "meals.db"

def init_db():
    """Initialize the SQLite database and create tables if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT,
                image_hash TEXT,
                file_id TEXT,
                analysis TEXT NOT NULL,
                corrected BOOLEAN DEFAULT 0
            )
        """)
        # Index to speed up daily queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_date ON meals(chat_id, date)")
        conn.commit()

def save_meal(chat_id: int, date_str: str, time_str: str, timestamp_str: str, 
              source: str, image_hash: str, file_id: str, analysis: Dict) -> int:
    """Save a new meal to the database and return its row ID."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO meals (chat_id, date, time, timestamp, source, image_hash, file_id, analysis, corrected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, date_str, time_str, timestamp_str, source, image_hash, file_id, json.dumps(analysis), False))
        conn.commit()
        return cursor.lastrowid

def get_meals(chat_id: int, start_date: str, end_date: str) -> List[Dict]:
    """Retrieve meals for a specific chat_id within a date range (inclusive)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM meals 
            WHERE chat_id = ? AND date >= ? AND date <= ?
            ORDER BY timestamp ASC
        """, (chat_id, start_date, end_date))
        
        results = []
        for row in cursor.fetchall():
            meal = dict(row)
            meal["analysis"] = json.loads(meal["analysis"])
            meal["corrected"] = bool(meal["corrected"])
            results.append(meal)
        return results

def get_recent_meals(chat_id: int, days: int = 3) -> List[Dict]:
    """Helper to get meals from the last N days."""
    from datetime import date, timedelta
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=days)).isoformat()
    return get_meals(chat_id, start_date, end_date)

def update_meal_analysis(meal_id: int, chat_id: int, new_analysis: Dict):
    """Update a specific meal's analysis by its database ID. Validates chat_id for security."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE meals 
            SET analysis = ?, corrected = 1 
            WHERE id = ? AND chat_id = ?
        """, (json.dumps(new_analysis), meal_id, chat_id))
        conn.commit()

# Ensure tables are created when imported
init_db()
