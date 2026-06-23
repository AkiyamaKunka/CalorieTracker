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
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS heartbeats (
                device_name TEXT PRIMARY KEY,
                last_ping_time TEXT NOT NULL,
                timezone TEXT DEFAULT '+0800'
            )
        """)
        
        # Migration: Add timezone column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE heartbeats ADD COLUMN timezone TEXT DEFAULT '+0800'")
        except sqlite3.OperationalError:
            pass # Column already exists
            
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

def update_android_heartbeat(device_name: str = "android_watcher", timezone: str = "+0800"):
    """Record that the device is online and save its timezone."""
    from datetime import datetime
    now_str = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO heartbeats (device_name, last_ping_time, timezone)
            VALUES (?, ?, ?)
            ON CONFLICT(device_name) DO UPDATE SET last_ping_time=excluded.last_ping_time, timezone=excluded.timezone
        """, (device_name, now_str, timezone))
        conn.commit()

def get_last_android_heartbeat(device_name: str = "android_watcher") -> Optional[str]:
    """Get the last ping time for a specific device."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT last_ping_time FROM heartbeats WHERE device_name = ?", (device_name,))
        row = cursor.fetchone()
        return row[0] if row else None

def get_android_timezone(device_name: str = "android_watcher") -> str:
    """Get the last reported timezone for a specific device. Defaults to +0800."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT timezone FROM heartbeats WHERE device_name = ?", (device_name,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else "+0800"

def get_today_hashes(chat_id: int) -> List[str]:
    """Return a list of image_hashes recorded today for the user."""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT image_hash FROM meals WHERE chat_id = ? AND date(timestamp) = ? AND image_hash != ''",
            (chat_id, today)
        )
        return [row[0] for row in cursor.fetchall() if row[0]]
