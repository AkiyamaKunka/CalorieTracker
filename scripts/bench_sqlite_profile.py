#!/usr/bin/env python3
"""SQLite layer profiler for CalorieTracker (measure-only; no product code touched).

Builds throwaway DBs at configurable meal counts (default 1k and 5k), then measures:

  S1  connection-per-call overhead (_connect + PRAGMA foreign_keys)
  S2  EXPLAIN QUERY PLAN for every query in database.py (at the largest size)
  S3  timing for every database.py query at each size
  S4  the iteration-2 deferred composite index idx_chat_date_ts(chat_id, date,
      timestamp): with/without, plus the ORDER-BY-(date,timestamp) variant that
      the range predicate actually allows SQLite to satisfy from the index
  S5  json.loads share of get_meals (cProfile) at the largest size
  S6  init_db() cost on an already-initialized DB (boot / import cost)
  S7  connection + BEGIN/COMMIT count for the Telegram-photo happy path
      (DB-call sequence mirrored from telegram_bot.py:
       is_duplicate_photo -> reserve_photo_hash -> user_local_now + save_meal
       -> mark_photo_hash_status -> meal-card daily totals)
  S8  WAL behavior: -wal size after seeding, checkpoint cost, and per-commit
      fsync cost under synchronous=FULL (the app's effective default) vs NORMAL
  S9  batching experiment: save_meal + mark_photo_hash_status as today's two
      connections/commits vs one shared transaction

Deterministic: fixed synthetic data (analysis blobs sized to match the real
DB's ~590-byte average), no network, no reliance on the real meals.db.
Timings are medians over repeats; absolute numbers are machine-dependent
(this is expected to run on the dev Mac — the e2-micro VM is slower).

Usage: python3 scripts/bench_sqlite_profile.py [n1 n2 ...]   (default: 1000 5000)
"""
import cProfile
import io
import json
import pstats
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import database  # noqa: E402  (import runs init_db() on the real DB path, same as app boot)

CHAT = 12345
MEAL_TIMES = ["08:12", "10:30", "12:45", "16:20", "19:05"]

# Analysis payload sized to match production (real DB avg 590B, max 1381B).
ANALYSIS_TEMPLATE = {
    "is_food": True,
    "meal_description": "Beef noodle soup with bok choy and a soft-boiled egg",
    "food_items": [
        {"name": "beef noodle soup", "estimated_calories": 520,
         "protein_g": 32, "carbs_g": 58, "fat_g": 16},
        {"name": "bok choy", "estimated_calories": 25,
         "protein_g": 2, "carbs_g": 4, "fat_g": 0},
        {"name": "soft-boiled egg", "estimated_calories": 70,
         "protein_g": 6, "carbs_g": 1, "fat_g": 5},
    ],
    "total_calories": 615, "total_protein_g": 40,
    "total_carbs_g": 63, "total_fat_g": 21,
}


def _analysis_blob(i):
    d = dict(ANALYSIS_TEMPLATE)
    d["meal_description"] = f"meal {i:05d} " + d["meal_description"]
    d["total_calories"] = 400 + (i % 500)
    return d


def med_ms(fn, reps, cleanup=None):
    """Median + p95 wall time of fn() over reps, in ms. cleanup() untimed."""
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000.0)
        if cleanup:
            cleanup()
    times.sort()
    p95 = times[min(len(times) - 1, int(round(0.95 * len(times))) - 1)]
    return statistics.median(times), p95


def fmt(label, median, p95, extra=""):
    print(f"  {label:<62s} med={median:8.3f}ms  p95={p95:8.3f}ms  {extra}")


def seed(db_path, n_meals):
    """Seed a DB with n_meals meals plus matching ingestion/fitness rows.

    Returns (dates_used, save_meal_latencies_ms) — the seed inserts go through
    database.save_meal so the latency distribution doubles as the WAL/commit
    stall sample (S8).
    """
    database.DB_PATH = db_path
    database.init_db()

    today = database.user_local_today()
    d0 = date(2024, 1, 1)
    latencies = []
    with database._connect() as conn:
        cur = conn.cursor()
        rows = []
        ing = []
        for i in range(n_meals - 5):
            day = d0 + timedelta(days=i // 5)
            slot = MEAL_TIMES[i % 5]
            ts = f"{day.isoformat()}T{slot}:00"
            h = f"{i:032x}"
            rows.append((CHAT, day.isoformat(), slot, ts, "telegram", h, "file",
                         json.dumps(_analysis_blob(i)), False))
            status = ("processing" if i % 997 == 0 else
                      "failed" if i % 499 == 0 else
                      "deleted" if i % 991 == 0 else "saved")
            ing.append((CHAT, h, ts, ts, "telegram", status, i + 1))
        cur.executemany(
            "INSERT INTO meals (chat_id,date,time,timestamp,source,image_hash,"
            "file_id,analysis,corrected) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        cur.executemany(
            "INSERT INTO photo_ingestions (chat_id,image_hash,first_seen_at,"
            "last_seen_at,source,status,meal_id) VALUES (?,?,?,?,?,?,?)", ing)
        # fitness data: 180 weigh-ins, 150 workouts, 200 activities, 1 profile
        cur.executemany(
            "INSERT INTO body_weight (chat_id,date,weight_kg,source,note,logged_at)"
            " VALUES (?,?,?,?,?,?)",
            [(CHAT, (d0 + timedelta(days=i)).isoformat(), 80 - i * 0.01, "manual",
              "", f"{(d0 + timedelta(days=i)).isoformat()}T07:00:00") for i in range(180)])
        cur.executemany(
            "INSERT INTO workouts (chat_id,date,workout_type,muscle_groups,notes,"
            "duration_min,source,details,logged_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(CHAT, (d0 + timedelta(days=i * 2)).isoformat(), "strength", "push",
              "", 60.0, "manual", json.dumps({"sets": 12}),
              f"{(d0 + timedelta(days=i * 2)).isoformat()}T18:00:00") for i in range(150)])
        cur.executemany(
            "INSERT INTO activities (chat_id,date,activity_type,source,active_calories,"
            "distance_km,duration_min,avg_hr_bpm,elevation_gain_m,start_time,external_id,"
            "notes,raw,logged_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(CHAT, (d0 + timedelta(days=i * 2)).isoformat(), "running", "garmin",
              450.0, 8.0, 45.0, 150, 40.0,
              f"{(d0 + timedelta(days=i * 2)).isoformat()}T06:30:00", f"g{i}",
              "", json.dumps({"laps": 8}), f"{(d0 + timedelta(days=i * 2)).isoformat()}T08:00:00")
             for i in range(200)])
        conn.commit()
    database.save_fitness_profile(CHAT, diet_mode="cut", target_calories=1900,
                                  target_protein_g=150, protein_g_per_kg=1.9,
                                  goal_weight_kg=75.0, height_cm=178.0)
    database.update_android_heartbeat(timezone="+0800")
    # Last 5 meals through the real API on today's user-local date so
    # today-window functions return rows; latencies feed S8.
    for i in range(5):
        t0 = time.perf_counter()
        database.save_meal(CHAT, today.isoformat(), MEAL_TIMES[i],
                           f"{today.isoformat()}T{MEAL_TIMES[i]}:00", "telegram",
                           f"{90000 + i:032x}", "file", _analysis_blob(90000 + i))
        latencies.append((time.perf_counter() - t0) * 1000.0)
        database.mark_photo_hash_status(CHAT, f"{90000 + i:032x}", "saved", source="telegram")
    return today, latencies


# ── query catalog: every SQL statement in database.py ──────────────────────
def eqp_catalog(db_path, today):
    print("\nS2. EXPLAIN QUERY PLAN — every query in database.py"
          f" (db={db_path.name})")
    week_ago = (today - timedelta(days=6)).isoformat()
    t = today.isoformat()
    queries = [
        ("get_meals window",
         "SELECT * FROM meals WHERE chat_id=? AND date>=? AND date<=? ORDER BY timestamp ASC",
         (CHAT, week_ago, t)),
        ("reserve: meals dup check",
         "SELECT id FROM meals WHERE chat_id=? AND image_hash=? LIMIT 1", (CHAT, "x")),
        ("reserve: reservation lookup",
         "SELECT status, last_seen_at FROM photo_ingestions WHERE chat_id=? AND image_hash=? LIMIT 1",
         (CHAT, "x")),
        ("mark_photo_hash_status UPDATE",
         "UPDATE photo_ingestions SET last_seen_at=?, status=?, meal_id=COALESCE(?, meal_id),"
         " source=CASE WHEN ? != '' THEN ? ELSE source END WHERE chat_id=? AND image_hash=?",
         ("t", "saved", None, "", "", CHAT, "x")),
        ("release_photo_hash DELETE",
         "DELETE FROM photo_ingestions WHERE chat_id=? AND image_hash=? AND status='processing'",
         (CHAT, "x")),
        ("discard_failed by prefix",
         "UPDATE photo_ingestions SET status='deleted', meal_id=NULL, last_seen_at=?"
         " WHERE chat_id=? AND status='failed' AND image_hash LIKE ? || '%'",
         ("t", CHAT, "abc")),
        ("find_photo_hash_by_prefix",
         "SELECT image_hash FROM photo_ingestions WHERE chat_id=? AND image_hash LIKE ? || '%' LIMIT 2",
         (CHAT, "abc")),
        ("get_processing_photo_hashes",
         "SELECT image_hash FROM photo_ingestions WHERE chat_id=? AND status='processing' AND image_hash != ''",
         (CHAT,)),
        ("get_reserved_photo_hashes",
         "SELECT image_hash, status, last_seen_at FROM photo_ingestions WHERE chat_id=? AND image_hash != ''",
         (CHAT,)),
        ("meal_image_hash_exists",
         "SELECT 1 FROM meals WHERE chat_id=? AND image_hash=? LIMIT 1", (CHAT, "x")),
        ("get_today_hashes",
         "SELECT image_hash FROM meals WHERE chat_id=? AND date=? AND image_hash != ''", (CHAT, t)),
        ("heartbeat SELECT ping",
         "SELECT last_ping_time FROM heartbeats WHERE device_name=?", ("android_watcher",)),
        ("heartbeat SELECT tz",
         "SELECT timezone FROM heartbeats WHERE device_name=?", ("android_watcher",)),
        ("body_weight range",
         "SELECT * FROM body_weight WHERE chat_id=? AND date>=? AND date<=? ORDER BY date ASC",
         (CHAT, week_ago, t)),
        ("body_weight latest",
         "SELECT * FROM body_weight WHERE chat_id=? ORDER BY date DESC LIMIT 1", (CHAT,)),
        ("workouts range",
         "SELECT * FROM workouts WHERE chat_id=? AND date>=? AND date<=? ORDER BY date ASC, logged_at ASC",
         (CHAT, week_ago, t)),
        ("activities range",
         "SELECT * FROM activities WHERE chat_id=? AND date>=? AND date<=?"
         " ORDER BY date ASC, start_time ASC, logged_at ASC", (CHAT, week_ago, t)),
        ("activities id after upsert",
         "SELECT id FROM activities WHERE chat_id=? AND source=? AND (external_id IS ? OR external_id=?)"
         " ORDER BY id DESC LIMIT 1", (CHAT, "garmin", "g1", "g1")),
        ("fitness_profile SELECT",
         "SELECT * FROM fitness_profile WHERE chat_id=?", (CHAT,)),
        ("update_meal_analysis",
         "UPDATE meals SET analysis=?, corrected=1 WHERE id=? AND chat_id=?", ("{}", 1, CHAT)),
        ("delete_meal SELECT hash",
         "SELECT image_hash FROM meals WHERE id=? AND chat_id=?", (1, CHAT)),
        ("delete_meal DELETE",
         "DELETE FROM meals WHERE id=? AND chat_id=?", (0, CHAT)),  # id 0: no-op
    ]
    with database._connect() as conn:
        for label, sql, params in queries:
            plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            detail = " | ".join(str(r[-1]) for r in plan)
            print(f"  {label:<32s} {detail}")
        conn.rollback()  # discard the no-op write EQPs' side effects, if any


def bench_all_queries(db_path, today, n):
    print(f"\nS3. Query timings at n={n} (db={db_path.name})")
    t = today.isoformat()
    week_ago = (today - timedelta(days=6)).isoformat()
    month_ago = (today - timedelta(days=29)).isoformat()

    fmt("get_meals 1-day window", *med_ms(lambda: database.get_meals(CHAT, t, t), 200),
        extra=f"rows={len(database.get_meals(CHAT, t, t))}")
    fmt("get_meals 7-day window", *med_ms(lambda: database.get_meals(CHAT, week_ago, t), 100),
        extra=f"rows={len(database.get_meals(CHAT, week_ago, t))}")
    fmt("get_meals 30-day window", *med_ms(lambda: database.get_meals(CHAT, month_ago, t), 50),
        extra=f"rows={len(database.get_meals(CHAT, month_ago, t))}")
    fmt("get_meals all-time ('1970-01-01'..today, /stats path)",
        *med_ms(lambda: database.get_meals(CHAT, "1970-01-01", t), 20),
        extra=f"rows={len(database.get_meals(CHAT, '1970-01-01', t))}")
    fmt("get_recent_meals(3) [2 conns: tz + query]",
        *med_ms(lambda: database.get_recent_meals(CHAT, 3), 100))
    fmt("get_today_hashes [2 conns: tz + query]",
        *med_ms(lambda: database.get_today_hashes(CHAT), 200))
    fmt("meal_image_hash_exists (miss)",
        *med_ms(lambda: database.meal_image_hash_exists(CHAT, "nothere"), 200))
    fmt("reserve_photo_hash (already-logged hash -> False)",
        *med_ms(lambda: database.reserve_photo_hash(CHAT, f"{1:032x}", "t"), 200))
    seq = {"i": 0}

    def _reserve_new():
        seq["i"] += 1
        database.reserve_photo_hash(CHAT, f"{700000 + seq['i']:032x}", "t")

    def _release_last():
        database.release_photo_hash(CHAT, f"{700000 + seq['i']:032x}")

    fmt("reserve_photo_hash (new hash, BEGIN IMMEDIATE+INSERT+COMMIT)",
        *med_ms(_reserve_new, 50, cleanup=_release_last))
    fmt("mark_photo_hash_status (existing row UPDATE+COMMIT)",
        *med_ms(lambda: database.mark_photo_hash_status(CHAT, f"{90001:032x}", "saved",
                                                        source="telegram"), 50))
    saved_ids = []

    def _save():
        saved_ids.append(database.save_meal(
            CHAT, t, "12:00", f"{t}T12:00:00", "bench", "", "f", _analysis_blob(1)))

    def _undo():
        database.delete_meal(saved_ids.pop(), CHAT)

    fmt("save_meal (INSERT+COMMIT)", *med_ms(_save, 50, cleanup=_undo))
    fmt("update_meal_analysis", *med_ms(
        lambda: database.update_meal_analysis(1, CHAT, _analysis_blob(1)), 50))
    fmt("get_processing_photo_hashes",
        *med_ms(lambda: database.get_processing_photo_hashes(CHAT), 200))
    fmt("get_reserved_photo_hashes (full ledger scan + py filter)",
        *med_ms(lambda: database.get_reserved_photo_hashes(CHAT), 50),
        extra=f"rows={len(database.get_reserved_photo_hashes(CHAT))}")
    fmt("find_photo_hash_by_prefix",
        *med_ms(lambda: database.find_photo_hash_by_prefix(CHAT, "00000000000"), 200))
    fmt("update_android_heartbeat (UPSERT+COMMIT)",
        *med_ms(lambda: database.update_android_heartbeat(timezone="+0800"), 50))
    fmt("get_last_android_heartbeat", *med_ms(database.get_last_android_heartbeat, 200))
    fmt("get_android_timezone", *med_ms(database.get_android_timezone, 200))
    fmt("user_local_now (tz read + math)", *med_ms(database.user_local_now, 200))
    fmt("get_body_weights 7d", *med_ms(lambda: database.get_body_weights(CHAT, week_ago, t), 200))
    fmt("get_latest_body_weight", *med_ms(lambda: database.get_latest_body_weight(CHAT), 200))
    fmt("get_workouts 7d", *med_ms(lambda: database.get_workouts(CHAT, week_ago, t), 200))
    fmt("get_activities 7d", *med_ms(lambda: database.get_activities(CHAT, week_ago, t), 200))
    fmt("get_fitness_profile", *med_ms(lambda: database.get_fitness_profile(CHAT), 200))
    fmt("save_body_weight (UPSERT+COMMIT+SELECT)", *med_ms(
        lambda: database.save_body_weight(CHAT, "2024-01-01", 80.0), 50))
    fmt("save_activity (UPSERT+COMMIT+SELECT)", *med_ms(
        lambda: database.save_activity(CHAT, "2024-01-01", "running", "garmin",
                                       450, 8, 45, 150, 40, None, "g1"), 50))
    fmt("save_fitness_profile (partial upsert)", *med_ms(
        lambda: database.save_fitness_profile(CHAT, target_calories=1900), 50))

    # daily_report's exact query set (get_meals(target,target) runs twice in
    # generate_report: _section_diet_targets + the main body; plus 7-day prior)
    def daily_report_queries():
        database.get_android_timezone()
        database.get_fitness_profile(CHAT)
        database.get_latest_body_weight(CHAT)
        database.get_meals(CHAT, t, t)          # _section_diet_targets
        database.get_meals(CHAT, t, t)          # main body
        database.get_meals(CHAT, week_ago, t)   # 7-day comparison
        database.get_body_weights(CHAT, week_ago, t)
        database.get_activities(CHAT, t, t)
        database.get_workouts(CHAT, t, t)
    fmt("daily_report DB bundle (9 queries/9 conns)", *med_ms(daily_report_queries, 50))


def bench_connection_overhead(db_path):
    print("\nS1. Connection-per-call overhead")
    m, p = med_ms(lambda: database._connect().close(), 500)
    fmt("_connect() + close  [connect + PRAGMA foreign_keys]", m, p)
    raw = sqlite3.connect(database.DB_PATH, timeout=30)

    def cached_tz():
        raw.execute("SELECT timezone FROM heartbeats WHERE device_name=?",
                    ("android_watcher",)).fetchone()
    m2, p2 = med_ms(cached_tz, 500)
    fmt("same SELECT tz on a cached connection", m2, p2)
    m3, p3 = med_ms(database.get_android_timezone, 500)
    fmt("get_android_timezone() as shipped (fresh conn each call)", m3, p3)
    raw.close()
    print(f"  -> connection setup is {max(m3 - m2, 0):.3f}ms of the "
          f"{m3:.3f}ms call ({(max(m3 - m2, 0) / m3 * 100 if m3 else 0):.0f}%)")


def bench_composite_index(db_path, today, n):
    print(f"\nS4. Deferred composite index idx_chat_date_ts(chat_id,date,timestamp) at n={n}")
    t = today.isoformat()
    week_ago = (today - timedelta(days=6)).isoformat()
    sql = ("SELECT * FROM meals WHERE chat_id=? AND date>=? AND date<=? "
           "ORDER BY timestamp ASC")
    sql_dts = ("SELECT * FROM meals WHERE chat_id=? AND date>=? AND date<=? "
               "ORDER BY date ASC, timestamp ASC")

    def timed_fetch(conn, q, params, reps):
        return med_ms(lambda: conn.execute(q, params).fetchall(), reps)

    for label, create in [("WITHOUT composite (stock idx_chat_date)", None),
                          ("WITH idx_chat_date_ts", "CREATE INDEX idx_chat_date_ts ON meals(chat_id, date, timestamp)")]:
        with database._connect() as conn:
            if create:
                p0 = conn.execute("PRAGMA page_count").fetchone()[0]
                t0 = time.perf_counter()
                conn.execute(create)
                conn.commit()
                p1 = conn.execute("PRAGMA page_count").fetchone()[0]
                print(f"  CREATE INDEX cost: {(time.perf_counter() - t0) * 1000:.1f}ms, "
                      f"+{(p1 - p0) * 4096:,} bytes")
            for qlabel, q, params, reps in [
                    ("1-day  ORDER BY timestamp", sql, (CHAT, t, t), 200),
                    ("7-day  ORDER BY timestamp", sql, (CHAT, week_ago, t), 100),
                    ("all    ORDER BY timestamp", sql, (CHAT, "1970-01-01", t), 20),
                    ("7-day  ORDER BY date,timestamp", sql_dts, (CHAT, week_ago, t), 100),
                    ("all    ORDER BY date,timestamp", sql_dts, (CHAT, "1970-01-01", t), 20)]:
                plan = " | ".join(str(r[-1]) for r in
                                  conn.execute("EXPLAIN QUERY PLAN " + q, params).fetchall())
                m, p = timed_fetch(conn, q, params, reps)
                fmt(f"[{label[:7]}] {qlabel}", m, p, extra=f"plan: {plan}")
            if create:
                conn.execute("DROP INDEX idx_chat_date_ts")
            conn.commit()


def bench_json_share(db_path, today, n):
    print(f"\nS5. json.loads share of get_meals all-time at n={n} (cProfile, 20 calls)")
    t = today.isoformat()
    pr = cProfile.Profile()
    pr.enable()
    for _ in range(20):
        database.get_meals(CHAT, "1970-01-01", t)
    pr.disable()
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(18)
    total = ps.total_tt
    # top-level json.loads cumtime only (nested decode/raw_decode are inside it)
    json_t = sum(ct for (f, l, name), (cc, nc, tt, ct, callers) in ps.stats.items()
                 if name == "loads" and "json/__init__" in f)
    fetch_t = sum(tt for (f, l, name), (cc, nc, tt, ct, callers) in ps.stats.items()
                  if name == "fetchall")
    exec_t = sum(tt for (f, l, name), (cc, nc, tt, ct, callers) in ps.stats.items()
                 if name == "execute")
    print(f"  total={total * 1000:.1f}ms  json.loads(cum)={json_t * 1000:.1f}ms "
          f"({json_t / total * 100:.0f}%)  fetchall={fetch_t * 1000:.1f}ms "
          f"({fetch_t / total * 100:.0f}%)  execute={exec_t * 1000:.1f}ms")
    print("  top lines:")
    for line in s.getvalue().splitlines()[4:14]:
        print("   " + line)


def bench_init_db(db_path):
    print("\nS6. init_db() on an already-initialized DB (runs at every import/boot)")
    m, p = med_ms(database.init_db, 20)
    fmt("init_db (17 DDL statements + failing ALTER)", m, p)


def bench_photo_trace(db_path, today):
    print("\nS7. Telegram-photo happy path: connections / transactions per photo")
    trace = []
    counts = {"connect": 0}
    real_connect = sqlite3.connect

    def counting_connect(*a, **k):
        counts["connect"] += 1
        conn = real_connect(*a, **k)
        conn.set_trace_callback(lambda s: trace.append(s.strip().upper().split()[0]
                                                       if s.strip() else ""))
        return conn

    sqlite3.connect = counting_connect
    h = f"{888888:032x}"
    t0 = time.perf_counter()
    try:
        phases = []

        def snap(name):
            phases.append((name, counts["connect"], trace.count("BEGIN"),
                           trace.count("COMMIT")))
        # mirrors telegram_bot.handle_photo_message + _analyze_telegram_photo_background
        today_s = database.user_local_today().isoformat()          # is_duplicate_photo
        database.get_meals(CHAT, today_s, today_s)                 #   "
        snap("is_duplicate_photo (today card read)")
        database.reserve_photo_hash(CHAT, h, "telegram",
                                    reclaim_statuses={"failed", "skipped", "deleted"})
        snap("reserve_photo_hash")
        now = database.user_local_now()                            # save_meal wrapper
        meal_id = database.save_meal(CHAT, now.date().isoformat(),
                                     now.strftime("%I:%M %p"), now.isoformat(),
                                     "telegram", h, "file", _analysis_blob(1))
        snap("user_local_now + save_meal")
        database.mark_photo_hash_status(CHAT, h, "saved", meal_id, source="telegram")
        snap("mark_photo_hash_status")
        today_s = database.user_local_today().isoformat()          # format_daily_totals
        database.get_meals(CHAT, today_s, today_s)
        snap("meal card daily totals")
        elapsed = (time.perf_counter() - t0) * 1000.0
    finally:
        sqlite3.connect = real_connect
    prev = (0, 0, 0)
    for name, c, b, cm in phases:
        print(f"  {name:<40s} conns +{c - prev[0]}  BEGIN +{b - prev[1]}  COMMIT +{cm - prev[2]}")
        prev = (c, b, cm)
    print(f"  TOTAL per photo: {counts['connect']} connections, "
          f"{trace.count('BEGIN')} BEGINs, {trace.count('COMMIT')} COMMITs, "
          f"DB wall time {elapsed:.1f}ms (excl. 22s analysis)")
    database.delete_meal(meal_id, CHAT)
    database.release_photo_hash(CHAT, h)


def bench_wal(db_path, seed_latencies):
    print("\nS8. WAL behavior")
    wal = Path(str(db_path) + "-wal")
    print(f"  -wal size after seeding: {wal.stat().st_size if wal.exists() else 0:,} bytes "
          f"(db {db_path.stat().st_size:,} bytes)")
    if seed_latencies:
        print(f"  save_meal during seed: med={statistics.median(seed_latencies):.2f}ms "
              f"max={max(seed_latencies):.2f}ms (n={len(seed_latencies)})")
    with database._connect() as conn:
        t0 = time.perf_counter()
        res = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        print(f"  wal_checkpoint(TRUNCATE): {(time.perf_counter() - t0) * 1000:.2f}ms, "
              f"result={res}")
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        print(f"  effective synchronous on a fresh _connect(): {sync} "
              f"(2=FULL: fsync per commit; app never overrides)")
    # per-commit cost: FULL vs NORMAL (unique 1-row INSERT + COMMIT each rep).
    # CAVEAT: on macOS/APFS fsync() does not force media flush, so these are
    # LOWER BOUNDS for the ext4 GCP VM, where each FULL commit is a real fsync.
    for mode in ("FULL", "NORMAL"):
        conn = sqlite3.connect(database.DB_PATH, timeout=30)
        conn.execute(f"PRAGMA synchronous={mode}")
        seq = {"i": 0}

        def commit_once():
            seq["i"] += 1
            conn.execute(
                "INSERT INTO heartbeats (device_name, last_ping_time) VALUES (?, 'x')",
                (f"bench_{mode}_{seq['i']}",))
            conn.commit()
        m, p = med_ms(commit_once, 100)
        fmt(f"1-row INSERT+COMMIT with synchronous={mode} (macOS lower bound)", m, p)
        conn.execute(f"DELETE FROM heartbeats WHERE device_name LIKE 'bench_{mode}_%'")
        conn.commit()
        conn.close()


def bench_batching(db_path, today):
    print("\nS9. save_meal + mark_photo_hash_status: 2 conns/commits vs 1 shared txn")
    t = today.isoformat()
    ids = []

    def as_shipped():
        h = f"{777000 + len(ids):032x}"
        mid = database.save_meal(CHAT, t, "12:00", f"{t}T12:00:00", "bench", h, "f",
                                 _analysis_blob(2))
        database.mark_photo_hash_status(CHAT, h, "saved", mid, source="bench")
        ids.append((mid, h))

    def undo():
        mid, h = ids.pop()
        database.delete_meal(mid, CHAT)
        with database._connect() as conn:
            conn.execute("DELETE FROM photo_ingestions WHERE chat_id=? AND image_hash=?",
                         (CHAT, h))
            conn.commit()
    fmt("as shipped (2 connections, 2 commits)", *med_ms(as_shipped, 50, cleanup=undo))

    ids2 = []

    def batched():
        h = f"{778000 + len(ids2):032x}"
        now = "2025-01-01T00:00:00"
        with database._connect() as conn:
            cur = conn.execute(
                "INSERT INTO meals (chat_id,date,time,timestamp,source,image_hash,"
                "file_id,analysis,corrected) VALUES (?,?,?,?,?,?,?,?,0)",
                (CHAT, t, "12:00", f"{t}T12:00:00", "bench", h, "f",
                 json.dumps(_analysis_blob(2))))
            conn.execute(
                "UPDATE photo_ingestions SET last_seen_at=?, status='saved', meal_id=? "
                "WHERE chat_id=? AND image_hash=?", (now, cur.lastrowid, CHAT, h))
            conn.commit()
            ids2.append((cur.lastrowid, h))

    def undo2():
        mid, h = ids2.pop()
        with database._connect() as conn:
            conn.execute("DELETE FROM meals WHERE id=?", (mid,))
            conn.execute("DELETE FROM photo_ingestions WHERE chat_id=? AND image_hash=?",
                         (CHAT, h))
            conn.commit()
    fmt("batched (1 connection, 1 commit)", *med_ms(batched, 50, cleanup=undo2))


def bench_stats_aggregate(db_path, today, n):
    """S10: /stats (format_database_stats) does get_meals('1970-01-01'..today)
    and reduces in Python. Measure the equivalent SQL aggregate (json_extract
    runs inside SQLite, no rows shipped to Python)."""
    print(f"\nS10. /stats all-time path: Python reduce vs SQL aggregate at n={n}")
    t = today.isoformat()

    def python_path():
        meals = database.get_meals(CHAT, "1970-01-01", t)
        food = [m for m in meals if m.get("analysis", {}).get("is_food")]
        total = sum((m["analysis"].get("total_calories") or 0) for m in food)
        days = len({m.get("date") for m in food if m.get("date")})
        src = {}
        for m in food:
            src[m.get("source") or "unknown"] = src.get(m.get("source") or "unknown", 0) + 1
        return total, days, len(food), len(meals), src

    def sql_path():
        with database._connect() as conn:
            head = conn.execute(
                "SELECT COUNT(*),"
                " SUM(json_extract(analysis,'$.is_food') = 1),"
                " SUM(CASE WHEN json_extract(analysis,'$.is_food') = 1"
                "     THEN COALESCE(json_extract(analysis,'$.total_calories'),0) ELSE 0 END),"
                " COUNT(DISTINCT CASE WHEN json_extract(analysis,'$.is_food') = 1"
                "     THEN date END)"
                " FROM meals WHERE chat_id=? AND date>=? AND date<=?",
                (CHAT, "1970-01-01", t)).fetchone()
            src = conn.execute(
                "SELECT COALESCE(NULLIF(source,''),'unknown'), COUNT(*) FROM meals"
                " WHERE chat_id=? AND date>=? AND date<=?"
                " AND json_extract(analysis,'$.is_food') = 1 GROUP BY 1",
                (CHAT, "1970-01-01", t)).fetchall()
        return head, dict(src)

    py = python_path()
    sql = sql_path()
    agree = (py[0] == sql[0][2] and py[2] == sql[0][1] and py[3] == sql[0][0]
             and py[4] == sql[1])
    fmt("Python reduce (as shipped in format_database_stats)", *med_ms(python_path, 20))
    fmt("SQL aggregate (json_extract, 2 queries)", *med_ms(sql_path, 20),
        extra=f"results agree: {agree}")


def main():
    sizes = [int(a) for a in sys.argv[1:]] or [1000, 5000]
    print(f"python {sys.version.split()[0]}  sqlite {sqlite3.sqlite_version}  "
          f"sizes={sizes}")
    original_db_path = database.DB_PATH
    try:
        for n in sizes:
            tmpdir = Path(tempfile.mkdtemp(prefix=f"ct_bench_{n}_"))
            db_path = tmpdir / f"bench_{n}.db"
            print(f"\n{'=' * 78}\nDATASET n={n} meals  ({db_path})\n{'=' * 78}")
            today, seed_lat = seed(db_path, n)
            with database._connect() as conn:
                pages = conn.execute("PRAGMA page_count").fetchone()[0]
                psize = conn.execute("PRAGMA page_size").fetchone()[0]
            print(f"db file: {db_path.stat().st_size:,} bytes "
                  f"({pages} pages x {psize}B)")
            bench_connection_overhead(db_path)
            if n == sizes[-1]:
                eqp_catalog(db_path, today)
            bench_all_queries(db_path, today, n)
            bench_composite_index(db_path, today, n)
            if n == sizes[-1]:
                bench_json_share(db_path, today, n)
            bench_init_db(db_path)
            bench_photo_trace(db_path, today)
            bench_wal(db_path, seed_lat)
            bench_batching(db_path, today)
            bench_stats_aggregate(db_path, today, n)
    finally:
        database.DB_PATH = original_db_path


if __name__ == "__main__":
    main()
