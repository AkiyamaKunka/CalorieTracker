import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

DB_PATH = Path.home() / "CalorieTracker" / "meals.db"
PHOTO_RESERVATION_STALE_SECONDS = 6 * 3600


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Initialize the SQLite database and create tables if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
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

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS photo_ingestions (
                chat_id INTEGER NOT NULL,
                image_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                source TEXT,
                status TEXT NOT NULL DEFAULT 'processing',
                meal_id INTEGER,
                PRIMARY KEY (chat_id, image_hash)
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_photo_ingestions_status "
            "ON photo_ingestions(chat_id, status)"
        )
        
        # Migration: Add timezone column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE heartbeats ADD COLUMN timezone TEXT DEFAULT '+0800'")
        except sqlite3.OperationalError:
            pass # Column already exists
            
        conn.commit()

def save_meal(chat_id: int, date_str: str, time_str: str, timestamp_str: str, 
              source: str, image_hash: str, file_id: str, analysis: Dict) -> int:
    """Save a new meal to the database and return its row ID."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO meals (chat_id, date, time, timestamp, source, image_hash, file_id, analysis, corrected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, date_str, time_str, timestamp_str, source, image_hash, file_id, json.dumps(analysis), False))
        conn.commit()
        return cursor.lastrowid


def _normalize_image_hash(image_hash: str) -> str:
    return str(image_hash or "").strip().lower()


def _processing_reservation_is_stale(last_seen_at: str, now: datetime,
                                     stale_after_seconds: int) -> bool:
    try:
        seen_at = datetime.fromisoformat(str(last_seen_at or ""))
        return (now - seen_at).total_seconds() > stale_after_seconds
    except (TypeError, ValueError):
        return False


def reserve_photo_hash(chat_id: int, image_hash: str, source: str = "",
                       timestamp_str: Optional[str] = None,
                       stale_after_seconds: int = PHOTO_RESERVATION_STALE_SECONDS,
                       reclaim_statuses: Optional[set] = None) -> bool:
    """
    Atomically reserve a photo hash before Gemini analysis.

    This prevents duplicate Telegram updates, Android queue retries, iOS double
    fires, or overlapping bot processes from analyzing/logging the same image
    while the first copy is still in flight.
    """
    normalized_hash = _normalize_image_hash(image_hash)
    if not normalized_hash:
        return True

    reclaim_statuses = set(reclaim_statuses or [])
    now = datetime.now()
    now_str = timestamp_str or now.isoformat()
    with _connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing_meal = conn.execute(
                "SELECT id FROM meals WHERE chat_id = ? AND image_hash = ? LIMIT 1",
                (chat_id, normalized_hash),
            ).fetchone()
            if existing_meal:
                conn.commit()
                return False

            existing_reservation = conn.execute("""
                SELECT status, last_seen_at
                FROM photo_ingestions
                WHERE chat_id = ? AND image_hash = ?
                LIMIT 1
            """, (chat_id, normalized_hash)).fetchone()
            if existing_reservation:
                status, last_seen_at = existing_reservation
                can_reclaim_processing = status == "processing" and _processing_reservation_is_stale(
                    last_seen_at,
                    now,
                    stale_after_seconds,
                )
                if can_reclaim_processing or status in reclaim_statuses:
                    conn.execute("""
                        UPDATE photo_ingestions
                        SET last_seen_at = ?, source = ?, status = 'processing', meal_id = NULL
                        WHERE chat_id = ? AND image_hash = ?
                    """, (now_str, source, chat_id, normalized_hash))
                    conn.commit()
                    return True

                conn.commit()
                return False

            cursor = conn.execute("""
                INSERT OR IGNORE INTO photo_ingestions
                    (chat_id, image_hash, first_seen_at, last_seen_at, source, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, normalized_hash, now_str, now_str, source, "processing"))
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise


def mark_photo_hash_status(chat_id: int, image_hash: str, status: str,
                           meal_id: Optional[int] = None, source: str = ""):
    """Update the processing status for a reserved photo hash."""
    normalized_hash = _normalize_image_hash(image_hash)
    if not normalized_hash:
        return

    now_str = datetime.now().isoformat()
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE photo_ingestions
            SET last_seen_at = ?, status = ?, meal_id = COALESCE(?, meal_id),
                source = CASE WHEN ? != '' THEN ? ELSE source END
            WHERE chat_id = ? AND image_hash = ?
        """, (now_str, status, meal_id, source, source, chat_id, normalized_hash))
        if cursor.rowcount == 0:
            cursor.execute("""
                INSERT OR IGNORE INTO photo_ingestions
                    (chat_id, image_hash, first_seen_at, last_seen_at, source, status, meal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, normalized_hash, now_str, now_str, source, status, meal_id))
        conn.commit()


def release_photo_hash(chat_id: int, image_hash: str):
    """Release an in-flight reservation when no meal or saved upload remains."""
    normalized_hash = _normalize_image_hash(image_hash)
    if not normalized_hash:
        return

    with _connect() as conn:
        conn.execute(
            "DELETE FROM photo_ingestions WHERE chat_id = ? AND image_hash = ? AND status = 'processing'",
            (chat_id, normalized_hash),
        )
        conn.commit()


def get_reserved_photo_hashes(chat_id: int) -> List[str]:
    """Return image hashes already claimed by the ingestion guard."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT image_hash FROM photo_ingestions WHERE chat_id = ? AND image_hash != ''",
            (chat_id,),
        )
        return [row[0] for row in cursor.fetchall() if row[0]]


def meal_image_hash_exists(chat_id: int, image_hash: str) -> bool:
    """Return whether a meal already exists with this exact image hash."""
    normalized_hash = _normalize_image_hash(image_hash)
    if not normalized_hash:
        return False

    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM meals WHERE chat_id = ? AND image_hash = ? LIMIT 1",
            (chat_id, normalized_hash),
        )
        return cursor.fetchone() is not None

def get_meals(chat_id: int, start_date: str, end_date: str) -> List[Dict]:
    """Retrieve meals for a specific chat_id within a date range (inclusive)."""
    with _connect() as conn:
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
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE meals 
            SET analysis = ?, corrected = 1 
            WHERE id = ? AND chat_id = ?
        """, (json.dumps(new_analysis), meal_id, chat_id))
        conn.commit()

def delete_meal(meal_id: int, chat_id: int):
    """Delete a specific meal by its database ID. Validates chat_id for security."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM meals WHERE id = ? AND chat_id = ?", (meal_id, chat_id))
        conn.commit()

# Ensure tables are created when imported
init_db()

def update_android_heartbeat(device_name: str = "android_watcher", timezone: str = "+0800"):
    """Record that the device is online and save its timezone."""
    from datetime import datetime
    now_str = datetime.now().isoformat()
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO heartbeats (device_name, last_ping_time, timezone)
            VALUES (?, ?, ?)
            ON CONFLICT(device_name) DO UPDATE SET last_ping_time=excluded.last_ping_time, timezone=excluded.timezone
        """, (device_name, now_str, timezone))
        conn.commit()

def get_last_android_heartbeat(device_name: str = "android_watcher") -> Optional[str]:
    """Get the last ping time for a specific device."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT last_ping_time FROM heartbeats WHERE device_name = ?", (device_name,))
        row = cursor.fetchone()
        return row[0] if row else None

def get_android_timezone(device_name: str = "android_watcher") -> str:
    """Get the last reported timezone for a specific device. Defaults to +0800."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT timezone FROM heartbeats WHERE device_name = ?", (device_name,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else "+0800"

def get_today_hashes(chat_id: int) -> List[str]:
    """Return a list of image_hashes recorded today for the user."""
    from datetime import date
    today = date.today().isoformat()
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT image_hash FROM meals WHERE chat_id = ? AND date = ? AND image_hash != ''",
            (chat_id, today)
        )
        return [row[0] for row in cursor.fetchall() if row[0]]
