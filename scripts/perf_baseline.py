#!/usr/bin/env python3
"""Reusable performance baseline for the CalorieTracker Python hot paths.

Seeds a throwaway SQLite DB (never the real ~/CalorieTracker/meals.db) at
1k and 5k meals with realistic analysis JSON (3-6 food items, CJK + emoji
descriptions, corrected rows, non-food rows, hash-less manual rows, a
photo-ingestion ledger, body weights, Garmin-style activities, workouts and
a fitness profile), then times each hot path with time.perf_counter.

Methodology: 1 discarded warmup, then N timed runs (default 7, --quick 3);
the table reports p50/p95 in milliseconds. Meal density is held constant at
5 meals/day at both sizes, so a window query (1/7/30 days) returns the same
rows at 1k and 5k — any growth between the columns is pure DB-size scaling.

Deterministic and network-free: Gemini is stubbed, Telegram is stubbed, the
service-health ledger is redirected to the temp dir, and PushPlus is nulled.

Usage:
    python3 scripts/perf_baseline.py            # 1k + 5k, 7 runs, cProfile
    python3 scripts/perf_baseline.py --quick    # 1k only, 3 runs, no profile
"""

import argparse
import cProfile
import io
import json
import logging
import math
import pstats
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database  # noqa: E402

CHAT = 77001
MEALS_PER_DAY = 5
RUNS_FULL = 7
RUNS_QUICK = 3

_DESCRIPTIONS = [
    "红烧牛肉面 🍜 with extra 香菜",
    "寿司の盛り合わせ 🍣 (salmon, tuna, tamago)",
    "Grilled chicken breast with 藜麦 quinoa salad 🥗",
    "韩式石锅拌饭 🍚 bibimbap with fried egg",
    "Protein shake 🥤 + banana 🍌",
    "麻婆豆腐 with steamed rice 🌶️",
    "Avocado toast 🥑 with poached eggs",
    "广式早茶: 虾饺 🥟, 烧卖, 凤爪",
]
_ITEM_NAMES = [
    "牛肉 beef slices", "拉面 noodles 🍜", "香菜 cilantro", "溏心蛋 egg 🥚",
    "三文鱼 salmon 🍣", "金枪鱼 tuna", "鸡胸肉 chicken breast", "藜麦 quinoa",
    "米饭 rice 🍚", "豆腐 tofu", "牛油果 avocado 🥑", "全麦吐司 toast 🍞",
    "虾饺 har gow 🥟", "烧卖 siu mai", "蛋白粉 whey", "香蕉 banana 🍌",
]

COMPOUND_NL_JSON = json.dumps({
    "intent": "multi",
    "actions": [
        {"intent": "correction", "meal_index": 2, "reason": "改成烤鸭饭",
         "analysis": {"is_food": True, "meal_description": "烤鸭饭 🦆",
                      "total_calories": 780, "total_protein_g": 42,
                      "total_carbs_g": 88, "total_fat_g": 28,
                      "food_items": [{"name": "烤鸭 roast duck",
                                      "estimated_calories": 480,
                                      "protein_g": 34, "carbs_g": 2, "fat_g": 30}]},
         "reply": "ok"},
        {"intent": "delete", "meal_indices": [0, 1], "reason": "duplicates"},
        {"intent": "new_meal",
         "analysis": {"is_food": True, "meal_description": "下午茶 🍩 donut",
                      "total_calories": 310, "total_protein_g": 4,
                      "total_carbs_g": 36, "total_fat_g": 17,
                      "food_items": [{"name": "巧克力甜甜圈 🍩",
                                      "estimated_calories": 310,
                                      "protein_g": 4, "carbs_g": 36, "fat_g": 17}]},
         "reply": "logged"},
        {"intent": "delete", "meal_indices": [4], "reason": "wrong photo"},
        {"intent": "log_activity", "active_calories": 450, "steps": 8000,
         "distance_km": 5.2, "reply": "nice run"},
    ],
    "reply": "Doing 5 things",
}, ensure_ascii=False)

NL_TEXT = ("change yesterday's 红烧牛肉面 to 650 kcal, and I also had a "
           "🍩 donut at 3pm — about 310 kcal")


class StubBot:
    """Collects sends; API-compatible with what the timed paths touch."""

    def send_chat_action(self, chat_id, action):
        return None

    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        return {"ok": True}

    def _redact(self, e):
        return str(e)


class StubGeminiResponse:
    text = json.dumps({"intent": "chat", "reply": "ok 👍"})


def _analysis(i, is_food):
    if not is_food:
        return {"is_food": False}
    n_items = 3 + (i % 4)  # 3-6 items
    items = []
    for j in range(n_items):
        cal = 80 + ((i * 37 + j * 91) % 420)
        items.append({
            "name": _ITEM_NAMES[(i + j * 5) % len(_ITEM_NAMES)],
            "estimated_calories": cal,
            "protein_g": 4 + ((i + j) % 38),
            "carbs_g": (i * 3 + j * 7) % 70,
            "fat_g": (i + j * 3) % 32,
        })
    return {
        "is_food": True,
        "food_items": items,
        "total_calories": sum(it["estimated_calories"] for it in items),
        "total_protein_g": sum(it["protein_g"] for it in items),
        "total_carbs_g": sum(it["carbs_g"] for it in items),
        "total_fat_g": sum(it["fat_g"] for it in items),
        "meal_description": _DESCRIPTIONS[i % len(_DESCRIPTIONS)],
        "confidence_note": "Standard 分量 portion sizes 📏",
    }


def seed_db(db_path: Path, n_meals: int):
    """Seed a fresh DB; returns the user-local 'today' the data ends at."""
    database.DB_PATH = db_path
    database.init_db()

    # Heartbeat with the HOST's utc offset so user_local_today() == local today.
    host_offset = datetime.now().astimezone().strftime("%z")  # e.g. +0800
    database.update_android_heartbeat(timezone=host_offset)
    today = database.user_local_today()

    times = ["08:12", "10:30", "12:45", "15:20", "19:05"]
    meal_rows, ledger_rows = [], []
    for i in range(n_meals):
        day = today - timedelta(days=i // MEALS_PER_DAY)
        t = times[i % MEALS_PER_DAY]
        ts = f"{day.isoformat()}T{t}:00"
        is_food = i % 23 != 0
        corrected = 1 if i % 11 == 0 else 0
        image_hash = "" if i % 17 == 0 else f"{i:064x}"
        analysis = json.dumps(_analysis(i, is_food), ensure_ascii=False)
        source = ("telegram", "api_upload", "manual_text")[i % 3]
        meal_rows.append((CHAT, day.isoformat(), t, ts, source, image_hash,
                          f"file{i}", analysis, corrected))
        if image_hash:
            if i % 29 == 0:
                status = "failed"
            elif i % 31 == 0:
                status = "processing"
            elif i % 37 == 0:
                status = "deleted"
            else:
                status = "done"
            ledger_rows.append((CHAT, image_hash, ts, ts, source, status,
                                None if status != "done" else i + 1))

    with database._connect() as conn:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO meals (chat_id,date,time,timestamp,source,image_hash,"
            "file_id,analysis,corrected) VALUES (?,?,?,?,?,?,?,?,?)", meal_rows)
        cur.executemany(
            "INSERT INTO photo_ingestions (chat_id,image_hash,first_seen_at,"
            "last_seen_at,source,status,meal_id) VALUES (?,?,?,?,?,?,?)",
            ledger_rows)

        n_days = max(1, n_meals // MEALS_PER_DAY)
        weight_rows = []
        for d in range(min(n_days, 400)):
            day = (today - timedelta(days=d)).isoformat()
            kg = 72.0 + math.sin(d / 9.0) * 1.5
            weight_rows.append((CHAT, day, round(kg, 1), "manual", "",
                                f"{day}T07:30:00"))
        cur.executemany(
            "INSERT INTO body_weight (chat_id,date,weight_kg,source,note,"
            "logged_at) VALUES (?,?,?,?,?,?)", weight_rows)

        activity_rows = []
        for d in range(min(n_days, 120)):
            day = (today - timedelta(days=d)).isoformat()
            raw = json.dumps({"steps": 6000 + (d * 137) % 8000,
                              "totalKilocalories": 2300 + (d * 53) % 500,
                              "activityType": "running"})
            activity_rows.append((
                CHAT, day, "running", "garmin", 300 + (d * 31) % 400,
                round(4 + (d % 7) * 1.1, 1), 30 + d % 40, 140 + d % 25,
                float(d % 90), f"{day}T18:00:00", f"garmin-{d}", "", raw,
                f"{day}T20:00:00"))
        cur.executemany(
            "INSERT INTO activities (chat_id,date,activity_type,source,"
            "active_calories,distance_km,duration_min,avg_hr_bpm,"
            "elevation_gain_m,start_time,external_id,notes,raw,logged_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", activity_rows)

        workout_rows = []
        for d in range(0, min(n_days, 60), 3):
            day = (today - timedelta(days=d)).isoformat()
            workout_rows.append((
                CHAT, day, "strength", "legs,core", "深蹲 squats 💪", 45.0,
                "manual", json.dumps({"sets": 5, "reps": 5}),
                f"{day}T18:30:00"))
        cur.executemany(
            "INSERT INTO workouts (chat_id,date,workout_type,muscle_groups,"
            "notes,duration_min,source,details,logged_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)", workout_rows)
        conn.commit()

    database.save_fitness_profile(
        CHAT, diet_mode="cut", target_calories=2000, protein_g_per_kg=1.8,
        goal_weight_kg=70.0, height_cm=178.0, long_run_day=6)
    return today


def build_20kb_text() -> str:
    """~20KB of mixed-unicode Telegram-style report text (deterministic)."""
    lines = []
    i = 0
    while sum(len(l.encode("utf-8")) for l in lines) < 19 * 1024:
        lines.append(f"{i + 1}. <b>{_DESCRIPTIONS[i % len(_DESCRIPTIONS)]}</b> "
                     f"— 12:45 ✏️ ~{500 + i % 300} kcal | P:{i % 60}g "
                     f"C:{(i * 3) % 90}g F:{i % 30}g 📊")
        i += 1
    # One overlong single line (> 3900 UTF-16 units) exercises the
    # codepoint-by-codepoint splitter, astral emoji included.
    lines.append(("菜品明细🍜数据" * 400) + "🎉")
    return "\n".join(lines)


def percentile(samples, q):
    s = sorted(samples)
    k = (len(s) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def bench(fn, runs, cleanup=None):
    fn()
    if cleanup:
        cleanup()
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
        if cleanup:
            cleanup()
    return percentile(samples, 0.50), percentile(samples, 0.95)


def make_benchmarks(tb, dr, utils_mod, today):
    """Returns [(name, fn, cleanup)] closed over the currently active DB."""
    today_s = today.isoformat()
    d7 = (today - timedelta(days=6)).isoformat()
    d30 = (today - timedelta(days=29)).isoformat()

    save_ids = []

    def do_save():
        save_ids.append(database.save_meal(
            CHAT, today_s, "13:37", f"{today_s}T13:37:00", "telegram",
            "feedbead" * 8, "file-bench", _analysis(3, True)))

    def undo_save():
        with database._connect() as conn:
            conn.execute("DELETE FROM meals WHERE id = ?", (save_ids.pop(),))
            conn.commit()

    reserve_seq = [0]

    def do_reserve():
        reserve_seq[0] += 1
        assert database.reserve_photo_hash(
            CHAT, f"beefcafe{reserve_seq[0]:056x}", "api_upload")

    def undo_reserve():
        database.release_photo_hash(CHAT, f"beefcafe{reserve_seq[0]:056x}")

    # A hash that exists today but outside the 5-minute duplicate window.
    dup_hash = f"{1:064x}"

    def do_handle_text():
        tb.handle_text_message(None, StubBot(), CHAT, NL_TEXT)

    big_text = build_20kb_text()

    return [
        ("get_meals 1d window", lambda: database.get_meals(CHAT, today_s, today_s), None),
        ("get_meals 7d window", lambda: database.get_meals(CHAT, d7, today_s), None),
        ("get_meals 30d window", lambda: database.get_meals(CHAT, d30, today_s), None),
        ("get_recent_meals 3d (bot)", lambda: tb.get_recent_meals(CHAT, days=3), None),
        ("format_today_summary", lambda: tb.format_today_summary(CHAT), None),
        ("format_meals_list", lambda: tb.format_meals_list(CHAT), None),
        ("format_recent_meals", lambda: tb.format_recent_meals(CHAT), None),
        ("format_history(30)", lambda: tb.format_history(CHAT, days=30), None),
        ("daily_report.generate_report", lambda: dr.generate_report(today_s), None),
        ("telegram_message_chunks 20KB", lambda: utils_mod.telegram_message_chunks(big_text), None),
        ("handle_text_message stub-Gemini", do_handle_text, None),
        ("parse+normalize 5-action NL", lambda: tb._normalize_nl_actions(json.loads(COMPOUND_NL_JSON)), None),
        ("save_meal", do_save, undo_save),
        ("is_duplicate_photo", lambda: tb.is_duplicate_photo(CHAT, dup_hash), None),
        ("reserve_photo_hash", do_reserve, undo_reserve),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="1k rows only, 3 runs, skip cProfile")
    parser.add_argument("--profile",
                        help="cProfile this benchmark name instead of the slowest")
    args = parser.parse_args()

    runs = RUNS_QUICK if args.quick else RUNS_FULL
    sizes = (1000,) if args.quick else (1000, 5000)

    logging.disable(logging.CRITICAL)  # silence bot logging during timing

    with tempfile.TemporaryDirectory(prefix="perf_baseline_") as tmp:
        tmp = Path(tmp)

        # Import the app modules, then wall them off from the real world.
        import telegram_bot as tb
        import daily_report as dr
        import utils as utils_mod

        tb.SERVICE_HEALTH_PATH = tmp / "service_health.json"
        dr.SERVICE_HEALTH_PATH = tmp / "service_health.json"
        utils_mod.PUSHPLUS_TOKEN = None
        dr.CHAT_ID = str(CHAT)
        tb._generate_content_with_deadline = lambda client, **kw: StubGeminiResponse()

        results = {}  # name -> {size: (p50, p95)}
        order = []
        db_stats = {}
        for n in sizes:
            db_path = tmp / f"meals_{n}.db"
            t0 = time.perf_counter()
            today = seed_db(db_path, n)
            seed_s = time.perf_counter() - t0
            db_stats[n] = (db_path.stat().st_size / 1024.0, seed_s)

            benchmarks = make_benchmarks(tb, dr, utils_mod, today)
            for name, fn, cleanup in benchmarks:
                if name not in results:
                    results[name] = {}
                    order.append(name)
                results[name][n] = bench(fn, runs, cleanup)

        # ── Table ──────────────────────────────────────────────────
        col = max(len(n) for n in order)
        hdr_sizes = "".join(f" | n={n//1000}k p50    p95   " for n in sizes)
        print(f"\nCalorieTracker perf baseline — {runs} timed runs, ms "
              f"(density {MEALS_PER_DAY} meals/day; identical window content "
              f"across sizes)")
        for n in sizes:
            kb, seed_s = db_stats[n]
            print(f"  DB n={n}: {kb:,.0f} KB on disk, seeded in {seed_s:.2f}s")
        print(f"\n{'benchmark'.ljust(col)}{hdr_sizes} | flags")
        print("-" * (col + len(hdr_sizes) + 10))
        worst_name, worst_p50 = None, -1.0
        for name in order:
            cells = ""
            flags = []
            for n in sizes:
                p50, p95 = results[name][n]
                cells += f" | {p50:8.3f} {p95:8.3f}"
            big = sizes[-1]
            p50_big = results[name][big][0]
            if p50_big > 100:
                flags.append("SLOW>100ms")
            if len(sizes) > 1:
                p50_small = results[name][sizes[0]][0]
                if p50_big > 1.0 and p50_small > 0 and p50_big / p50_small > 3:
                    flags.append(f"SCALES x{p50_big / p50_small:.1f}")
            if p50_big > worst_p50:
                worst_name, worst_p50 = name, p50_big
            print(f"{name.ljust(col)}{cells} | {' '.join(flags)}")

        # ── cProfile the worst offender at the largest size ────────
        if not args.quick:
            target = args.profile or worst_name
            fn = cleanup = None
            for name, f, c in make_benchmarks(tb, dr, utils_mod, today):
                if name == target:
                    fn, cleanup = f, c
                    break
            if fn is None:
                print(f"\n[profile] unknown benchmark {target!r}")
                return
            print(f"\ncProfile of worst offender: {target} "
                  f"(n={sizes[-1]}, 5 calls)")
            prof = cProfile.Profile()
            for _ in range(5):
                prof.enable()
                fn()
                prof.disable()
                if cleanup:
                    cleanup()
            out = io.StringIO()
            stats = pstats.Stats(prof, stream=out).sort_stats("cumulative")
            stats.print_stats(18)
            print(out.getvalue())


if __name__ == "__main__":
    main()
