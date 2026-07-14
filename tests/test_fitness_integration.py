"""End-to-end simulated fitness day against a real temp SQLite DB.

Drives the REAL command handlers (/diet, /workout, /activity, /train_run),
the real NL text-intent handler with a stubbed Gemini, and the real
database accessors — then generates the nightly report and cross-checks
every fitness number against what the day's inputs imply. This is the
only place the whole fitness write→report pipeline is exercised without
stubbing the storage layer.
"""

import os
import sys
import json
from datetime import datetime, timezone
from html import escape
from types import SimpleNamespace

import pytest

# Add root dir to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import daily_report
import database
import fitness_plan
import nutrition
import telegram_bot

CHAT = 12345

# 04:00 UTC == 12:00 noon at the +0800 device offset -> user-local
# 2026-07-14, a Tuesday. Freezing the clock keeps every step of the
# simulated day on ONE deterministic date (no midnight flake) and makes
# the weekday-dependent training line reproducible.
_FIXED_UTC = datetime(2026, 7, 14, 4, 0, 0)
TODAY = "2026-07-14"
NEXT_DAY = "2026-07-15"


class _FrozenUTC(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return _FIXED_UTC.replace(tzinfo=timezone.utc).astimezone(tz)
        return _FIXED_UTC


class FakeBot:
    def __init__(self):
        self.sent = []
        self.answered = []
        self.deleted = []

    def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, callback_query_id, text=""):
        self.answered.append((callback_query_id, text))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _intent_client(payload):
    """A Gemini stub whose generate_content always returns ``payload`` as JSON."""

    class FakeModels:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text=json.dumps(payload))

    return SimpleNamespace(models=FakeModels())


def _save_meal(date_str, time_str, timestamp, image_hash, description,
               calories, protein, carbs, fat):
    database.save_meal(
        CHAT, date_str, time_str, timestamp, "test", image_hash, "file",
        {
            "is_food": True,
            "meal_description": description,
            "total_calories": calories,
            "total_protein_g": protein,
            "total_carbs_g": carbs,
            "total_fat_g": fat,
            "food_items": [],
        },
    )


@pytest.fixture
def fitness_env(monkeypatch, tmp_path):
    """Real temp DB + frozen user-local clock + offline report config."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fitness_day.db")
    database.init_db()
    monkeypatch.setattr(
        database, "get_android_timezone", lambda device_name="android_watcher": "+0800"
    )
    # Freeze only database's clock: every user-local "today" decision in the
    # fitness commands and the NL handler flows through database.user_local_now.
    monkeypatch.setattr(database, "datetime", _FrozenUTC)
    # Empty health file: no Gemini quota pause blocks the NL handler.
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(daily_report, "CHAT_ID", str(CHAT))
    monkeypatch.delenv("GARMIN_NET_USE_TOTAL", raising=False)
    yield


def test_full_simulated_fitness_day_report(fitness_env):
    bot = FakeBot()
    assert database.user_local_now().date().isoformat() == TODAY  # clock frozen

    # ── 1. Morning: NL weigh-in through the real text handler ──────
    client = _intent_client({"intent": "log_weight", "weight_kg": 71.8, "reply": "ok"})
    telegram_bot.handle_text_message(client, bot, CHAT, "weighed 71.8 kg this morning")
    weigh = database.get_latest_body_weight(CHAT)
    assert weigh is not None
    assert weigh["weight_kg"] == 71.8
    assert weigh["date"] == TODAY                      # user-local date, not UTC
    assert any("71.8 kg" in m["text"] for m in bot.sent)

    # ── 2. Diet mode + two meals on today ──────────────────────────
    telegram_bot._cmd_diet(bot, CHAT, ["high_protein"])
    assert database.get_fitness_profile(CHAT)["diet_mode"] == "high_protein"
    _save_meal(TODAY, "08:30 AM", f"{TODAY}T08:30:00", "aa" * 16,
               "Eggs and yogurt", 620, 50, 30, 20)
    _save_meal(TODAY, "07:00 PM", f"{TODAY}T19:00:00", "bb" * 16,
               "Steak with potatoes", 1230, 75, 110, 45)

    # ── 3. Strength session + manual cardio numbers ────────────────
    telegram_bot._cmd_workout(bot, CHAT, ["legs", "45min", "squats"])
    workouts = database.get_workouts(CHAT, TODAY, TODAY)
    assert len(workouts) == 1 and workouts[0]["muscle_groups"] == "legs"
    telegram_bot._cmd_activity(bot, CHAT, ["640", "12000", "9.2"])
    activities = database.get_activities(CHAT, TODAY, TODAY)
    assert len(activities) == 1 and activities[0]["active_calories"] == 640

    # ── 4. Race result -> stored VDOT ~50 ──────────────────────────
    telegram_bot._cmd_train_run(bot, CHAT, ["5k", "19:57"])
    profile = database.get_fitness_profile(CHAT)
    assert 49.0 <= profile["vdot"] <= 51.0

    # ── 5. The nightly report, cross-checked against the day ───────
    report = daily_report.generate_report(TODAY)

    assert "Tuesday" in report                          # frozen weekday
    assert "🔥 <b>Total Calories: ~1,850 kcal</b>" in report   # 620 + 1230
    assert "📸 <b>Meals logged:</b> 2" in report

    # Diet targets: protein target derives from the morning weigh-in,
    # 2.0 g/kg * 71.8 kg rounded = 144 g; consumed 50 + 75 = 125 g (short).
    assert "🥗 Macro check — High-protein" in report
    expected_protein_target = round(2.0 * weigh["weight_kg"])
    assert expected_protein_target == 144
    assert f"Protein: 125g / {expected_protein_target}g ⬇️" in report
    assert "Calories: <b>1,850</b> kcal" in report

    # Energy balance: manual activity feeds burn/steps/distance and the net
    # is exactly meals_total − active burn = 1850 − 640 = 1210.
    assert "⚡ Energy Balance" in report
    assert "🔥 Active burn: ~640 kcal" in report
    assert "👟 Steps: 12,000" in report
    assert "📏 Distance: 9.2 km" in report
    assert "⚖️ Net: ~1,210 kcal (consumed − active burn)" in report

    # Weight: the morning weigh-in; one point -> no fabricated trend.
    assert "⚖️ Weight" in report
    assert "Latest: <b>71.8 kg</b>" in report
    assert "kg/wk" not in report

    # Training: the line must match the pure planner's prescription for the
    # stored profile on this exact weekday (Tuesday = quality day).
    assert "🏃 Today's Training" in report
    expected_workout = fitness_plan.todays_workout(profile, TODAY)
    assert f"• {escape(str(expected_workout))}" in report

    # Exactly one disclaimer for the whole report.
    assert report.count(nutrition.DISCLAIMER) == 1

    # ── 6. Next day: yesterday's burn must not leak forward ────────
    next_report = daily_report.generate_report(NEXT_DAY)

    assert "No meals logged today" in next_report
    assert "Energy Balance" not in next_report          # no activity on the 15th
    assert "640" not in next_report
    assert "12,000" not in next_report
    # Meals don't leak into the macro check either.
    assert "Calories: <b>0</b> kcal" in next_report
    # But the weigh-in is still inside the trailing 7-day weight window …
    assert "Latest: <b>71.8 kg</b>" in next_report
    # … and the plan says what Wednesday holds.
    assert "🏃 Today's Training" in next_report
    expected_next = fitness_plan.todays_workout(database.get_fitness_profile(CHAT), NEXT_DAY)
    assert f"• {escape(str(expected_next))}" in next_report


def test_meals_only_day_is_byte_identical_to_pre_fitness_report(fitness_env, monkeypatch):
    # Same real-DB flow but Robert never touches a fitness feature: the report
    # must be byte-identical to the pre-fitness output (real accessors return
    # their true empties — no stubs, unlike the unit-level guard test).
    _save_meal(TODAY, "08:30 AM", f"{TODAY}T08:30:00", "cc" * 16,
               "Eggs and yogurt", 620, 50, 30, 20)
    _save_meal(TODAY, "07:00 PM", f"{TODAY}T19:00:00", "dd" * 16,
               "Steak with potatoes", 1230, 75, 110, 45)

    with_fitness_code = daily_report.generate_report(TODAY)

    monkeypatch.setattr(daily_report, "_fitness_sections", lambda *a, **k: [])
    golden = daily_report.generate_report(TODAY)

    assert with_fitness_code == golden
    for marker in ("Macro check", "Energy Balance", "Today's Training", "⚖️ Weight", "kg/wk"):
        assert marker not in with_fitness_code
    assert "Total Calories: ~1,850 kcal" in with_fitness_code
