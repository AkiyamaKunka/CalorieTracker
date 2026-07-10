"""Scaling & index-usage guards (from the S13 performance hunt).

The hot paths were measured clean (no O(n^2); reserve_photo_hash flat at
~0.6ms from 1k to 4k meals via the covering index). These deterministic
guards pin the properties that keep them fast: the per-upload existence
check must use idx_chat_hash (not a full table scan), and /history must
stay windowed regardless of total history size.
"""
import json
from datetime import date, timedelta

import database

CHAT = 12345


def _seed(monkeypatch, tmp_path, n):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "scaling.db")
    database.init_db()
    d0 = date(2024, 1, 1)
    with database._connect() as conn:
        cur = conn.cursor()
        for i in range(n):
            analysis = json.dumps({"is_food": True, "total_calories": 500,
                                   "meal_description": f"meal {i}", "food_items": []})
            cur.execute(
                "INSERT INTO meals (chat_id,date,time,timestamp,source,image_hash,"
                "file_id,analysis,corrected) VALUES (?,?,?,?,?,?,?,?,0)",
                (CHAT, (d0 + timedelta(days=i % 900)).isoformat(), "12:00 PM",
                 f"2024-01-01T{i % 24:02d}:00:00", "t", f"{i:032x}", "f", analysis))
        conn.commit()


def test_reserve_existence_check_uses_the_hash_index(monkeypatch, tmp_path):
    """The per-upload duplicate check inside reserve_photo_hash's BEGIN
    IMMEDIATE must hit idx_chat_hash — a full scan here would slow every
    upload as history grows."""
    _seed(monkeypatch, tmp_path, 200)
    with database._connect() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM meals "
            "WHERE chat_id = ? AND image_hash = ? LIMIT 1", (CHAT, "x")).fetchall()
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "idx_chat_hash" in plan_text, plan_text
    assert "SCAN meals" not in plan_text, plan_text


def test_reserve_stays_fast_as_history_grows(monkeypatch, tmp_path):
    """Absolute smoke: reserving a new hash against a large table is cheap
    (index-backed), well under any user-perceptible threshold."""
    import time

    _seed(monkeypatch, tmp_path, 2000)
    novel = "f" * 32
    start = time.perf_counter()
    assert database.reserve_photo_hash(CHAT, novel, "t") is True
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 200, f"reserve took {elapsed_ms:.1f}ms against 2000 rows"


def test_get_meals_window_query_uses_the_date_index(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, 200)
    with database._connect() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM meals "
            "WHERE chat_id = ? AND date >= ? AND date <= ? ORDER BY timestamp ASC",
            (CHAT, "2024-01-01", "2024-12-31")).fetchall()
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "idx_chat_date" in plan_text, plan_text
    assert "SCAN meals" not in plan_text, plan_text
