/// Database open/migration for the on-device SQLite store (spec §2).
///
/// Schema is the verbatim port of the server DDL (spec §2.1, database.py:
/// 20-156) minus the server-only `heartbeats` table (spec §2.1/§2.2: the app
/// has one clock — the device's — so heartbeats have no app role). WAL
/// journal mode and foreign keys ON match database.py:16/25.
library;

import 'package:sqflite/sqflite.dart';

/// v1: the full spec §2.1 table set. v2: meal_thumbs (app-only, spec §9 —
/// the server never stores thumbnails; the app keeps a tiny JPEG per photo
/// meal so history can show the picture even after the gallery original is
/// gone). v3: body_measurements (app-only, spec §9 — waist/chest/hip for
/// the Body page; the server tracks weight only).
const int kSchemaVersion = 3;

const String kDatabaseFileName = 'calorie_tracker.db';

/// DDL statements, executed in order on create (spec §2.1; idempotent
/// IF NOT EXISTS mirrors database.py:20-156).
const List<String> kSchemaDdl = [
  // database.py:26-43
  '''
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
)''',
  'CREATE INDEX IF NOT EXISTS idx_chat_date ON meals(chat_id, date)',
  'CREATE INDEX IF NOT EXISTS idx_chat_hash ON meals(chat_id, image_hash)',
  // database.py:53-68
  '''
CREATE TABLE IF NOT EXISTS photo_ingestions (
    chat_id INTEGER NOT NULL,
    image_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'processing',
    meal_id INTEGER,
    PRIMARY KEY (chat_id, image_hash)
)''',
  '''
CREATE INDEX IF NOT EXISTS idx_photo_ingestions_status
    ON photo_ingestions(chat_id, status)''',
  // database.py:72-83 — one canonical weigh-in per user-local day
  '''
CREATE TABLE IF NOT EXISTS body_weight (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    source TEXT DEFAULT 'manual',
    note TEXT DEFAULT '',
    logged_at TEXT NOT NULL,
    UNIQUE(chat_id, date)
)''',
  // App-only (spec §9): girth measurements for the Body page. Same
  // one-canonical-row-per-day shape as body_weight; columns nullable so a
  // day can carry any subset. The server has no equivalent table.
  '''
CREATE TABLE IF NOT EXISTS body_measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    waist_cm REAL,
    chest_cm REAL,
    hip_cm REAL,
    note TEXT DEFAULT '',
    logged_at TEXT NOT NULL,
    UNIQUE(chat_id, date)
)''',
  // database.py:86-100 — append-only, multiple per day allowed
  '''
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
)''',
  'CREATE INDEX IF NOT EXISTS idx_workouts_chat_date ON workouts(chat_id, date)',
  // database.py:104-124 — UNIQUE(chat_id, source, external_id); manual rows
  // carry NULL external_id and always insert (SQLite NULLs are distinct).
  '''
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
)''',
  'CREATE INDEX IF NOT EXISTS idx_activities_chat_date ON activities(chat_id, date)',
  // database.py:127-148 — single config row per user
  '''
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
)''',
  // App-only (spec §9): one small JPEG per photo meal, written by the
  // pipeline at save time from the ORIGINAL bytes. Deleted with the meal.
  '''
CREATE TABLE IF NOT EXISTS meal_thumbs (
    meal_id INTEGER PRIMARY KEY,
    jpeg BLOB NOT NULL
)''',
];

/// Table names included in the full export (spec §8: exportJson covers all
/// tables; order is the DDL order for a stable envelope).
const List<String> kExportTables = [
  'meals',
  'photo_ingestions',
  'body_weight',
  'body_measurements',
  'workouts',
  'activities',
  'fitness_profile',
];

/// Open (creating/migrating as needed) a database at [path], or the default
/// on-device location when null. Exposed with an explicit path so tests and
/// tools can target a temp file.
Future<Database> openAppDatabase({String? path}) async {
  final dbPath = path ?? '${await getDatabasesPath()}/$kDatabaseFileName';
  return databaseFactory.openDatabase(
    dbPath,
    options: OpenDatabaseOptions(
      version: kSchemaVersion,
      // Two isolates share this database (foreground app + the WorkManager
      // backfill isolate) through ONE static native handle (sqflite
      // singleInstance). If either isolate dies between BEGIN and COMMIT
      // (WorkManager's 10-min hard stop, EMUI kill), the handle's
      // transaction state wedges and every later operation queues forever.
      // This flag makes the NEXT open issue the healing ROLLBACK — release
      // builds default it to false, which would freeze the app on every
      // warm start after a mid-transaction kill, with no recovery path.
      rollbackActiveTransactionOnOpen: true,
      // database.py:16 — foreign keys ON.
      onConfigure: (db) => db.execute('PRAGMA foreign_keys = ON'),
      onCreate: (db, version) async {
        for (final ddl in kSchemaDdl) {
          await db.execute(ddl);
        }
      },
      // Every DDL statement is IF NOT EXISTS, so upgrade = re-run the list;
      // v(n) → v(n+1) only ever ADDS tables/indexes.
      onUpgrade: (db, oldVersion, newVersion) async {
        for (final ddl in kSchemaDdl) {
          await db.execute(ddl);
        }
      },
      // database.py:25 — WAL journal mode (rawQuery: the pragma returns a
      // row, and execute would throw on some platforms).
      onOpen: (db) => db.rawQuery('PRAGMA journal_mode = WAL'),
    ),
  );
}

Future<Database>? _singleton;

/// Process-wide singleton database handle; the memoized future prevents a
/// double-open when two callers race the first access.
Future<Database> appDatabase() => _singleton ??= openAppDatabase();

// (No reset hook: tests open temp/in-memory databases through
// openAppDatabase(path:) instead of touching the singleton. A hook whose
// name promises a test contract but has no caller invites someone to
// build on it.)
