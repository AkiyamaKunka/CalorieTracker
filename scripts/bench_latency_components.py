#!/usr/bin/env python3
"""Deterministic micro-benchmark of the CPU/disk components of user-visible flows.

Measures ONLY local compute — no network, no Gemini, no Claude CLI, no
Telegram. The point is to separate "time the box spends working" from "time
the user spends waiting on model calls and Telegram round-trips" so the
latency budget in the profiling report can attribute every second.

Components timed (median of --repeats runs, perf_counter):
  synth_photo_bytes     size of the synthetic phone photo used as input
  md5_hash              hashlib.md5 over the photo bytes (dup detection)
  stage_write           write photo bytes to disk (crash-recovery staging)
  stage_read            read them back (the /upload worker re-read)
  prepare_gemini        PIL decode + EXIF transpose + 1024px thumbnail + RGB
  compress_echo         PIL decode + 1280px thumbnail + JPEG q82 encode
  echo_bytes            output size of the echo JPEG (upload payload)
  db_today_summary      /today text build (get_todays_meals + totals + median)
  db_food_card          meal-card text build (incl. daily totals re-query)
  db_dup_check          is_duplicate_photo negative path
  nl_context_build      recent-meals fetch + TEXT_HANDLER_PROMPT build
  truncate_html         _truncate_telegram_html on a 12k-char message

Deterministic: fixed-seed noise, fixed DB contents, scratch dirs only.
Run:  python scripts/bench_latency_components.py [--repeats 5] [--json]
"""

import argparse
import hashlib
import json
import random
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHAT_ID = 777000111


def build_synthetic_photo(size=(4032, 3024)) -> bytes:
    """A phone-photo-shaped JPEG: smooth base + fixed-seed blocky detail."""
    from PIL import Image

    w, h = size
    base = Image.linear_gradient("L").resize((w, h)).convert("RGB")
    rng = random.Random(42)
    nw, nh = max(w // 8, 1), max(h // 8, 1)
    noise_small = Image.frombytes("RGB", (nw, nh), rng.randbytes(nw * nh * 3))
    noise = noise_small.resize((w, h), Image.NEAREST)
    img = Image.blend(base, noise, 0.35)
    import io

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _meal_analysis(i: int) -> dict:
    return {
        "is_food": True,
        "food_items": [
            {"name": f"Rice bowl {i}", "estimated_calories": 320, "protein_g": 8, "carbs_g": 60, "fat_g": 4},
            {"name": "Chicken thigh", "estimated_calories": 260, "protein_g": 24, "carbs_g": 1, "fat_g": 17},
            {"name": "Greens", "estimated_calories": 60, "protein_g": 3, "carbs_g": 8, "fat_g": 2},
        ],
        "total_calories": 640,
        "total_protein_g": 35,
        "total_carbs_g": 69,
        "total_fat_g": 23,
        "meal_description": f"Chicken rice bowl with greens #{i}",
        "confidence_note": "Standard portions",
    }


def seed_scratch_db(database) -> None:
    database.init_db()
    today = database.user_local_today()
    # 4 meals today + 3 meals/day for the prior 7 days = 25 rows: a realistic
    # heavy-usage window for /today and the NL prompt's 7-day meal list.
    n = 0
    for day_offset in range(0, 8):
        d = today - timedelta(days=day_offset)
        for m in range(4 if day_offset == 0 else 3):
            n += 1
            ts = datetime(d.year, d.month, d.day, 8 + 4 * m, 15, 0)
            database.save_meal(
                CHAT_ID, d.isoformat(), ts.strftime("%H:%M"), ts.isoformat(),
                "telegram", f"{n:032x}", f"file{n}", _meal_analysis(n),
            )


def _time(fn, repeats: int):
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return {
        "median_ms": round(statistics.median(samples), 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
    }


def run_benchmarks(scratch: Path, repeats: int = 5, photo_size=(4032, 3024)) -> dict:
    import database

    database.DB_PATH = scratch / "bench_meals.db"

    import telegram_bot as tb

    tb.SERVICE_HEALTH_PATH = scratch / "bench_service_health.json"

    seed_scratch_db(database)
    photo = build_synthetic_photo(photo_size)
    results = {"synth_photo_bytes": len(photo)}

    results["md5_hash"] = _time(lambda: hashlib.md5(photo).hexdigest(), repeats)

    stage_path = scratch / "staged.jpg"
    results["stage_write"] = _time(lambda: stage_path.write_bytes(photo), repeats)
    results["stage_read"] = _time(lambda: stage_path.read_bytes(), repeats)
    stage_path.unlink()

    results["prepare_gemini"] = _time(lambda: tb._prepare_image_for_gemini(photo), repeats)
    results["compress_echo"] = _time(lambda: tb._compress_photo_for_echo(photo), repeats)
    results["echo_bytes"] = len(tb._compress_photo_for_echo(photo) or b"")

    results["db_today_summary"] = _time(lambda: tb.format_today_summary(CHAT_ID), repeats)
    analysis = _meal_analysis(99)
    results["db_food_card"] = _time(lambda: tb.format_food_result(CHAT_ID, analysis), repeats)
    results["db_dup_check"] = _time(
        lambda: tb.is_duplicate_photo(CHAT_ID, "f" * 32), repeats
    )

    def nl_context():
        meals = tb.get_recent_meals(CHAT_ID, days=tb.TEXT_EDIT_WINDOW_DAYS)
        parts = []
        for i, meal in enumerate(meals):
            a = meal["analysis"]
            items = ", ".join(str(it.get("name", "?")) for it in a.get("food_items", []))
            parts.append(
                f"[{i}] Date: {meal.get('date', '?')} | Meal: {a.get('meal_description')} "
                f"(~{a.get('total_calories')} kcal) — Items: {items}"
            )
        from config import TEXT_HANDLER_PROMPT

        local_now = database.user_local_now()
        today_local = local_now.date()
        return TEXT_HANDLER_PROMPT.format(
            meals_list="\n".join(parts),
            user_message="change meal 2 to roast duck rice",
            today=today_local.isoformat(),
            weekday=today_local.strftime("%A"),
            yesterday=(today_local - timedelta(days=1)).isoformat(),
        )

    results["nl_context_build"] = _time(nl_context, repeats)

    long_msg = ("<b>line of meal text</b> with details 0123456789\n" * 260)
    results["truncate_html"] = _time(lambda: tb._truncate_telegram_html(long_msg), repeats)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="ct_bench_") as tmp:
        results = run_benchmarks(Path(tmp), repeats=max(1, args.repeats))

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"{'component':<20} {'median ms':>10} {'min ms':>10} {'max ms':>10}")
    for name, val in results.items():
        if isinstance(val, dict):
            print(f"{name:<20} {val['median_ms']:>10} {val['min_ms']:>10} {val['max_ms']:>10}")
        else:
            print(f"{name:<20} {val:>10} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
