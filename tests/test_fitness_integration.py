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


# ═══ Appended: mid-day device-offset change (write-time dating contract) ═══
#
# The frozen instant 2026-07-14 04:00 UTC is 12:00 on the 14th at '+0800'
# but 21:00 on the 13th at '-0700'. CONTRACT: every fitness write is stamped
# with the device-local date in effect AT THE MOMENT of that write, so a
# heartbeat offset change between two writes legitimately lands them on two
# different calendar days — that is correct behavior, not a duplicate bug.

PREV_DAY = "2026-07-13"  # the same frozen UTC instant, seen from '-0700'


@pytest.fixture
def tz_shift_env(monkeypatch, tmp_path):
    """Real temp DB + frozen UTC clock + the REAL heartbeat-driven offset.

    Unlike fitness_env, get_android_timezone is NOT stubbed: these tests
    exercise the actual heartbeats row so the device-reported offset can
    change between writes, exactly like a phone crossing timezones mid-day.
    """
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "tz_shift.db")
    database.init_db()
    monkeypatch.setattr(database, "datetime", _FrozenUTC)
    monkeypatch.setattr(daily_report, "CHAT_ID", str(CHAT))
    monkeypatch.delenv("GARMIN_NET_USE_TOTAL", raising=False)
    yield


def test_offset_change_mid_day_dates_each_weigh_in_at_write_time(tz_shift_env):
    bot = FakeBot()

    database.update_android_heartbeat("android_watcher", timezone="+0800")
    assert database.user_local_today().isoformat() == TODAY
    telegram_bot._cmd_weight(bot, CHAT, ["71.8"])

    # Device flies east→west: SAME frozen UTC instant, new local date.
    database.update_android_heartbeat("android_watcher", timezone="-0700")
    assert database.user_local_today().isoformat() == PREV_DAY
    telegram_bot._cmd_weight(bot, CHAT, ["72.4"])

    # Two rows on two dates is CORRECT: the one-weigh-in-per-day upsert is
    # keyed on the user-local calendar day in effect at each write, so the
    # second write must not overwrite (or be swallowed by) the first.
    weights = database.get_body_weights(CHAT, PREV_DAY, TODAY)
    assert [(w["date"], w["weight_kg"]) for w in weights] == [
        (PREV_DAY, 72.4),
        (TODAY, 71.8),
    ]
    # "Latest" keys off the max calendar date: the chronologically later
    # write (dated the 13th) does not displace the 14th's weigh-in.
    assert database.get_latest_body_weight(CHAT)["date"] == TODAY
    # Each confirmation echoes its own write-time date to the user.
    assert TODAY in bot.sent[0]["text"] and "71.8" in bot.sent[0]["text"]
    assert PREV_DAY in bot.sent[1]["text"] and "72.4" in bot.sent[1]["text"]


def test_offset_change_mid_day_dates_each_workout_at_write_time(tz_shift_env):
    bot = FakeBot()

    database.update_android_heartbeat("android_watcher", timezone="+0800")
    telegram_bot._cmd_workout(bot, CHAT, ["legs"])

    database.update_android_heartbeat("android_watcher", timezone="-0700")
    telegram_bot._cmd_workout(bot, CHAT, ["chest"])

    # Same write-time contract as weigh-ins: one session lands on each
    # local date, and neither day's query sees the other's session.
    assert [w["muscle_groups"] for w in database.get_workouts(CHAT, TODAY, TODAY)] == ["legs"]
    assert [w["muscle_groups"] for w in database.get_workouts(CHAT, PREV_DAY, PREV_DAY)] == ["chest"]
    assert len(database.get_workouts(CHAT, PREV_DAY, TODAY)) == 2


def test_activity_after_offset_change_feeds_new_local_dates_report(tz_shift_env):
    bot = FakeBot()

    database.update_android_heartbeat("android_watcher", timezone="+0800")
    assert database.user_local_today().isoformat() == TODAY
    database.update_android_heartbeat("android_watcher", timezone="-0700")

    telegram_bot._cmd_activity(bot, CHAT, ["640", "12000", "9.2"])

    rows = database.get_activities(CHAT, PREV_DAY, PREV_DAY)
    assert len(rows) == 1 and rows[0]["active_calories"] == 640
    assert database.get_activities(CHAT, TODAY, TODAY) == []

    # A just-written activity is picked up immediately by the report for
    # ITS OWN (new-offset) local date...
    report_prev = daily_report.generate_report(PREV_DAY)
    assert "⚡ Energy Balance" in report_prev
    assert "🔥 Active burn: ~640 kcal" in report_prev
    assert "👟 Steps: 12,000" in report_prev
    assert "📏 Distance: 9.2 km" in report_prev
    # ...netted against that same date's meals (none logged on the 13th).
    assert "⚖️ Net: ~-640 kcal (consumed − active burn)" in report_prev

    # ...and never leaks into the pre-shift date's report.
    report_today = daily_report.generate_report(TODAY)
    assert "Energy Balance" not in report_today
    assert "640" not in report_today


@pytest.mark.parametrize(
    "junk_day",
    ["not-a-date", "", None, 12345, "2026-99-99"],
    ids=["text", "empty", "none", "int", "bad-month"],
)
def test_week_plan_junk_day_falls_back_to_server_clock(junk_day):
    # week_plan's fallback for an unparseable day is the SERVER clock
    # (date.today()), so only the shape is pinned here: WHICH week comes
    # back is wall-clock-dependent by design and deliberately not asserted.
    plan = fitness_plan.week_plan({"vdot": 50.0}, junk_day)  # must not raise
    assert len(plan) == 7
    assert [day for day, _ in plan] == fitness_plan.DAY_NAMES
    assert all(isinstance(workout, str) and workout for _, workout in plan)
