import sqlite3
import json
import re
from datetime import date, datetime, timedelta, timezone
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
        # Index for the per-upload duplicate check inside reserve_photo_hash
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_hash ON meals(chat_id, image_hash)")
        
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

        # ─── Fitness expansion tables (all additive) ──────────────
        # One canonical weigh-in per user-local day; re-logging overwrites.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS body_weight (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                weight_kg REAL NOT NULL,
                source TEXT DEFAULT 'manual',
                note TEXT DEFAULT '',
                logged_at TEXT NOT NULL,
                UNIQUE(chat_id, date)
            )
        """)

        # Strength/lifting sessions; append-only, multiple per day allowed.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                workout_type TEXT DEFAULT 'strength',
                muscle_groups TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                duration_min REAL,
                source TEXT DEFAULT 'manual',
                details TEXT,
                logged_at TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_workouts_chat_date ON workouts(chat_id, date)")

        # Cardio/activity aggregate; upsert by (source, external_id) so a
        # re-sync of the same Garmin activity updates rather than duplicates.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                activity_type TEXT DEFAULT '',
                source TEXT DEFAULT 'manual',
                active_calories REAL,
                distance_km REAL,
                duration_min REAL,
                avg_hr_bpm INTEGER,
                elevation_gain_m REAL,
                start_time TEXT,
                external_id TEXT,
                notes TEXT DEFAULT '',
                raw TEXT,
                logged_at TEXT NOT NULL,
                UNIQUE(chat_id, source, external_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activities_chat_date ON activities(chat_id, date)")

        # Single config row per user: diet + macro targets + goals + running/VDOT.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fitness_profile (
                chat_id INTEGER PRIMARY KEY,
                diet_mode TEXT,
                target_calories INTEGER,
                target_protein_g INTEGER,
                target_carbs_g INTEGER,
                target_fat_g INTEGER,
                protein_g_per_kg REAL,
                goal_weight_kg REAL,
                height_cm REAL,
                vdot REAL,
                race_distance_km REAL,
                race_time_seconds INTEGER,
                race_label TEXT,
                goal_race_date TEXT,
                plan_start_date TEXT,
                long_run_day INTEGER DEFAULT 6,
                extra TEXT,
                updated_at TEXT
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
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO meals (chat_id, date, time, timestamp, source, image_hash, file_id, analysis, corrected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, date_str, time_str, timestamp_str, source, _normalize_image_hash(image_hash),
              file_id, json.dumps(analysis), False))
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


def discard_failed_photo_hashes_by_prefix(chat_id: int, hash_prefix: str) -> int:
    """Tombstone 'failed' reservations whose hash starts with the given prefix.

    Used when the user explicitly clears a failed upload (/clear_failed). The
    row is kept with status 'deleted' (matching delete_meal's tombstone) so
    the nightly /reconcile keeps suppressing the photo instead of
    auto-resurrecting it, while a deliberate Telegram re-send can still
    reclaim the hash via reserve_photo_hash(reclaim_statuses={'deleted'}).
    Returns the number of rows tombstoned.
    """
    prefix = _normalize_image_hash(hash_prefix)
    if not prefix:
        return 0
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE photo_ingestions "
            "SET status = 'deleted', meal_id = NULL, last_seen_at = ? "
            "WHERE chat_id = ? AND status = 'failed' AND image_hash LIKE ? || '%'",
            (datetime.now().isoformat(), chat_id, prefix),
        )
        conn.commit()
        return cursor.rowcount


def find_photo_hash_by_prefix(chat_id: int, hash_prefix: str) -> Optional[str]:
    """Resolve a filename's 12-char hash prefix to the full ledger hash.

    Staged/failed upload filenames embed only a hash prefix; when the phone
    declares an original-file hash for a recompressed upload, the staged
    bytes no longer hash to the ledger key, so callers must resolve through
    the ledger instead of rehashing. Returns None when the prefix is
    missing or ambiguous — callers fall back to hashing the bytes.
    """
    prefix = _normalize_image_hash(hash_prefix)
    if not prefix:
        return None
    with _connect() as conn:
        rows = conn.execute(
            "SELECT image_hash FROM photo_ingestions "
            "WHERE chat_id = ? AND image_hash LIKE ? || '%' LIMIT 2",
            (chat_id, prefix),
        ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return None


def get_processing_photo_hashes(chat_id: int) -> List[str]:
    """Return hashes whose reservations are still marked 'processing'."""
    with _connect() as conn:
        cursor = conn.execute(
            "SELECT image_hash FROM photo_ingestions "
            "WHERE chat_id = ? AND status = 'processing' AND image_hash != ''",
            (chat_id,),
        )
        return [row[0] for row in cursor.fetchall() if row[0]]


def get_reserved_photo_hashes(chat_id: int) -> List[str]:
    """Return image hashes already claimed by the ingestion guard.

    Stale 'processing' rows are skipped so a crash mid-analysis does not
    permanently block /reconcile from recovering the photo.
    """
    now = datetime.now()
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT image_hash, status, last_seen_at FROM photo_ingestions "
            "WHERE chat_id = ? AND image_hash != ''",
            (chat_id,),
        )
        hashes = []
        for image_hash, status, last_seen_at in cursor.fetchall():
            if not image_hash:
                continue
            if status == "processing" and _processing_reservation_is_stale(
                last_seen_at, now, PHOTO_RESERVATION_STALE_SECONDS
            ):
                continue
            hashes.append(image_hash)
        return hashes


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
    """Helper to get meals from the last N calendar days, including today."""
    today = user_local_today()
    end_date = today.isoformat()
    start_date = (today - timedelta(days=max(days - 1, 0))).isoformat()
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
        cursor.execute(
            "SELECT image_hash FROM meals WHERE id = ? AND chat_id = ?",
            (meal_id, chat_id),
        )
        row = cursor.fetchone()
        cursor.execute("DELETE FROM meals WHERE id = ? AND chat_id = ?", (meal_id, chat_id))
        image_hash = _normalize_image_hash(row[0]) if row else ""
        if image_hash:
            # Keep the ingestion row (as 'deleted') so /reconcile does not
            # auto re-log the photo; an explicit re-send reclaims it.
            cursor.execute("""
                UPDATE photo_ingestions
                SET status = 'deleted', meal_id = NULL, last_seen_at = ?
                WHERE chat_id = ? AND image_hash = ?
            """, (datetime.now().isoformat(), chat_id, image_hash))
        conn.commit()

# Ensure tables are created when imported
init_db()

def update_android_heartbeat(device_name: str = "android_watcher", timezone: Optional[str] = None):
    """Record that the device is online and optionally save its timezone.

    When timezone is None (a bare liveness ping), the previously stored
    device-reported offset is preserved; only an explicit non-None value
    overwrites it. First-ever inserts without a timezone default to '+0800'.
    """
    from datetime import datetime
    now_str = datetime.now().isoformat()
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO heartbeats (device_name, last_ping_time, timezone)
            VALUES (?, ?, COALESCE(?, '+0800'))
            ON CONFLICT(device_name) DO UPDATE SET
                last_ping_time=excluded.last_ping_time,
                timezone=COALESCE(?, heartbeats.timezone)
        """, (device_name, now_str, timezone, timezone))
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


_TZ_OFFSET_RE = re.compile(r"^([+-])(\d{2})(\d{2})$")


def parse_timezone_offset(tz_str: str) -> Optional[timedelta]:
    """Parse a '+0800'-style offset string; return None when malformed."""
    match = _TZ_OFFSET_RE.match(str(tz_str or "").strip())
    if not match:
        return None
    hours = int(match.group(2))
    minutes = int(match.group(3))
    if hours > 14 or minutes > 59:
        return None
    sign = 1 if match.group(1) == "+" else -1
    return sign * timedelta(hours=hours, minutes=minutes)


def user_local_now(device_name: str = "android_watcher") -> datetime:
    """Current naive datetime in the user's timezone (last device-reported offset).

    The server may run in a different timezone (e.g. a UTC GCP VM), so meal
    dates and "today" windows must use this clock, not the host clock. Falls
    back to server-local time when the stored offset is malformed.
    """
    offset = parse_timezone_offset(get_android_timezone(device_name))
    if offset is None:
        return datetime.now()
    return datetime.now(timezone.utc).replace(tzinfo=None) + offset


def user_local_today(device_name: str = "android_watcher") -> date:
    """Today's date on the user's clock (see user_local_now)."""
    return user_local_now(device_name).date()


def get_today_hashes(chat_id: int) -> List[str]:
    """Return a list of image_hashes recorded today (user-local) for the user."""
    today = user_local_today().isoformat()
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT image_hash FROM meals WHERE chat_id = ? AND date = ? AND image_hash != ''",
            (chat_id, today)
        )
        return [row[0] for row in cursor.fetchall() if row[0]]


# ─── Fitness expansion accessors ──────────────────────────────────
_FITNESS_PROFILE_COLUMNS = (
    "diet_mode", "target_calories", "target_protein_g", "target_carbs_g",
    "target_fat_g", "protein_g_per_kg", "goal_weight_kg", "height_cm", "vdot",
    "race_distance_km", "race_time_seconds", "race_label", "goal_race_date",
    "plan_start_date", "long_run_day", "extra",
)


def _rows_to_dicts(cursor) -> List[Dict]:
    cursor.row_factory = sqlite3.Row
    return [dict(row) for row in cursor.fetchall()]


def save_body_weight(chat_id: int, date_str: str, weight_kg: float,
                     source: str = "manual", note: str = "") -> int:
    """Record one weigh-in for a user-local day; re-logging the day overwrites."""
    now_iso = datetime.now().isoformat()
    with _connect() as conn:
        conn.execute("""
            INSERT INTO body_weight (chat_id, date, weight_kg, source, note, logged_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, date) DO UPDATE SET
                weight_kg = excluded.weight_kg,
                source = excluded.source,
                note = excluded.note,
                logged_at = excluded.logged_at
        """, (chat_id, date_str, float(weight_kg), source, note, now_iso))
        conn.commit()
        row = conn.execute(
            "SELECT id FROM body_weight WHERE chat_id = ? AND date = ?",
            (chat_id, date_str)).fetchone()
        return row[0] if row else 0


def get_body_weights(chat_id: int, start_date: str, end_date: str) -> List[Dict]:
    with _connect() as conn:
        cursor = conn.execute(
            "SELECT * FROM body_weight WHERE chat_id = ? AND date >= ? AND date <= ? "
            "ORDER BY date ASC", (chat_id, start_date, end_date))
        return _rows_to_dicts(cursor)


def get_latest_body_weight(chat_id: int) -> Optional[Dict]:
    with _connect() as conn:
        cursor = conn.execute(
            "SELECT * FROM body_weight WHERE chat_id = ? ORDER BY date DESC LIMIT 1",
            (chat_id,))
        rows = _rows_to_dicts(cursor)
        return rows[0] if rows else None


def save_workout(chat_id: int, date_str: str, workout_type: str = "strength",
                 muscle_groups: str = "", notes: str = "",
                 duration_min: Optional[float] = None, source: str = "manual",
                 details: Optional[Dict] = None) -> int:
    now_iso = datetime.now().isoformat()
    with _connect() as conn:
        cursor = conn.execute("""
            INSERT INTO workouts (chat_id, date, workout_type, muscle_groups, notes,
                                  duration_min, source, details, logged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, date_str, workout_type, muscle_groups, notes, duration_min,
              source, json.dumps(details) if details is not None else None, now_iso))
        conn.commit()
        return cursor.lastrowid


def get_workouts(chat_id: int, start_date: str, end_date: str) -> List[Dict]:
    with _connect() as conn:
        cursor = conn.execute(
            "SELECT * FROM workouts WHERE chat_id = ? AND date >= ? AND date <= ? "
            "ORDER BY date ASC, logged_at ASC", (chat_id, start_date, end_date))
        rows = _rows_to_dicts(cursor)
    for row in rows:
        if row.get("details"):
            try:
                row["details"] = json.loads(row["details"])
            except (TypeError, ValueError):
                pass
    return rows


def save_activity(chat_id: int, date_str: str, activity_type: str = "",
                  source: str = "manual", active_calories: Optional[float] = None,
                  distance_km: Optional[float] = None, duration_min: Optional[float] = None,
                  avg_hr_bpm: Optional[int] = None, elevation_gain_m: Optional[float] = None,
                  start_time: Optional[str] = None, external_id: Optional[str] = None,
                  notes: str = "", raw: Optional[Dict] = None) -> int:
    """Insert or update a cardio activity.

    A re-synced source activity (same source + external_id) updates in place.
    Manual rows (external_id NULL) always insert, since NULLs are distinct in a
    SQLite UNIQUE index.
    """
    now_iso = datetime.now().isoformat()
    raw_json = json.dumps(raw) if raw is not None else None
    with _connect() as conn:
        conn.execute("""
            INSERT INTO activities (chat_id, date, activity_type, source, active_calories,
                                    distance_km, duration_min, avg_hr_bpm, elevation_gain_m,
                                    start_time, external_id, notes, raw, logged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, source, external_id) DO UPDATE SET
                date = excluded.date,
                activity_type = excluded.activity_type,
                active_calories = excluded.active_calories,
                distance_km = excluded.distance_km,
                duration_min = excluded.duration_min,
                avg_hr_bpm = excluded.avg_hr_bpm,
                elevation_gain_m = excluded.elevation_gain_m,
                start_time = excluded.start_time,
                notes = excluded.notes,
                raw = excluded.raw,
                logged_at = excluded.logged_at
        """, (chat_id, date_str, activity_type, source, active_calories, distance_km,
              duration_min, avg_hr_bpm, elevation_gain_m, start_time, external_id,
              notes, raw_json, now_iso))
        conn.commit()
        cursor = conn.execute(
            "SELECT id FROM activities WHERE chat_id = ? AND source = ? "
            "AND (external_id IS ? OR external_id = ?) ORDER BY id DESC LIMIT 1",
            (chat_id, source, external_id, external_id))
        row = cursor.fetchone()
        return row[0] if row else 0


def get_activities(chat_id: int, start_date: str, end_date: str) -> List[Dict]:
    with _connect() as conn:
        cursor = conn.execute(
            "SELECT * FROM activities WHERE chat_id = ? AND date >= ? AND date <= ? "
            "ORDER BY date ASC, start_time ASC, logged_at ASC",
            (chat_id, start_date, end_date))
        rows = _rows_to_dicts(cursor)
    for row in rows:
        if row.get("raw"):
            try:
                row["raw"] = json.loads(row["raw"])
            except (TypeError, ValueError):
                pass
    return rows


def save_fitness_profile(chat_id: int, **fields) -> None:
    """Partial upsert of the single fitness_profile row (only provided columns)."""
    updates = {k: v for k, v in fields.items() if k in _FITNESS_PROFILE_COLUMNS}
    if "extra" in updates and isinstance(updates["extra"], (dict, list)):
        updates["extra"] = json.dumps(updates["extra"])
    now_iso = datetime.now().isoformat()
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO fitness_profile (chat_id, updated_at) VALUES (?, ?)",
                     (chat_id, now_iso))
        if updates:
            # Column names come only from the whitelist; values are bound params.
            set_clause = ", ".join(f"{col} = ?" for col in updates)
            conn.execute(
                f"UPDATE fitness_profile SET {set_clause}, updated_at = ? WHERE chat_id = ?",
                (*updates.values(), now_iso, chat_id))
        conn.commit()


def get_fitness_profile(chat_id: int) -> Optional[Dict]:
    with _connect() as conn:
        cursor = conn.execute("SELECT * FROM fitness_profile WHERE chat_id = ?", (chat_id,))
        rows = _rows_to_dicts(cursor)
    if not rows:
        return None
    profile = rows[0]
    if profile.get("extra"):
        try:
            profile["extra"] = json.loads(profile["extra"])
        except (TypeError, ValueError):
            pass
    return profile
