"""Pins for the pure Daniels-Gilbert running-formula math in fitness_plan.

Anchors chosen from Jack Daniels' Running Formula so the model can't drift:
a 5k in 19:57 is a canonical VDOT ~50 result, and VDOT 50 threshold pace is
~4:15/km (6:51/mi) in the published tables. The plan/periodisation helpers
must also survive missing or hostile profiles without ever raising.
"""

from datetime import date, datetime, timedelta

import fitness_plan as fp


# --- VDOT from race -------------------------------------------------------

def test_vdot_anchor_5k_1957_is_about_50():
    vdot = fp.vdot_from_race(5000, 1197)  # 19:57
    assert vdot is not None
    assert abs(vdot - 50.0) <= 0.6


def test_vdot_marathon_anchor_in_range():
    # 42.195 km in 2:03:00 is elite -> high but still within the clamp band.
    vdot = fp.vdot_from_race(42195, 2 * 3600 + 3 * 60)
    assert vdot is not None
    assert 80.0 <= vdot <= fp.VDOT_MAX


def test_vdot_clamped_to_bounds():
    fast = fp.vdot_from_race(5000, 600)     # 10:00 5k -> off the top
    slow = fp.vdot_from_race(5000, 3600)    # 60:00 5k -> off the bottom
    assert fast == fp.VDOT_MAX
    assert slow == fp.VDOT_MIN


def test_vdot_bad_inputs_return_none():
    assert fp.vdot_from_race(5000, 0) is None
    assert fp.vdot_from_race(0, 1197) is None
    assert fp.vdot_from_race(-100, 1197) is None
    assert fp.vdot_from_race(5000, -5) is None
    assert fp.vdot_from_race(None, None) is None
    assert fp.vdot_from_race("x", 1197) is None
    assert fp.vdot_from_race(5000, float("inf")) is None
    assert fp.vdot_from_race(5000, float("nan")) is None
    assert fp.vdot_from_race(True, 1197) is None  # bool is not a distance


# --- paces from VDOT ------------------------------------------------------

def test_threshold_pace_matches_daniels_table():
    # Inverting VO2=f(v) at 88% VO2max for VDOT 50 gives ~255 s/km, i.e.
    # 4:15/km == 6:51/mi, which is Daniels' published VDOT-50 T pace.
    t = fp.paces_from_vdot(50)["T"]
    assert abs(t - 255.0) <= 6.0
    assert fp.format_pace(t) == "4:15/km"


def test_paces_are_ordered_fast_to_slow():
    paces = fp.paces_from_vdot(50)
    assert paces["E"] > paces["M"] > paces["T"] > paces["I"] > paces["R"]


def test_easy_range_brackets_easy_pace():
    paces = fp.paces_from_vdot(50)
    fast, slow = paces["E_range"]
    assert fast < paces["E"] < slow  # ascending seconds/km => fast end first


def test_paces_junk_vdot_falls_back_to_default():
    junk = fp.paces_from_vdot("not-a-number")
    default = fp.paces_from_vdot(fp.DEFAULT_VDOT)
    assert junk == default


def test_higher_vdot_is_faster():
    slow = fp.paces_from_vdot(40)["T"]
    fast = fp.paces_from_vdot(58)["T"]
    assert fast < slow


# --- formatting -----------------------------------------------------------

def test_format_pace_km_and_mi():
    assert fp.format_pace(255.0) == "4:15/km"
    # 255 s/km * 1.609344 ~= 410.4 s/mi -> 6:50/mi
    assert fp.format_pace_mi(255.0) == "6:50/mi"


def test_format_pace_handles_junk():
    assert fp.format_pace(None).endswith("/km")
    assert fp.format_pace(0) == "-:--/km"
    assert fp.format_pace_mi(-3) == "-:--/mi"


def test_per_mile_conversion():
    assert abs(fp.per_mile(300.0) - 300.0 * fp.KM_PER_MILE) < 1e-9
    assert fp.per_mile("junk") == 0.0


# --- default profile ------------------------------------------------------

def test_default_profile_shape():
    prof = fp.default_profile(4242, date(2026, 7, 14))
    assert prof["chat_id"] == 4242
    assert prof["vdot"] == fp.DEFAULT_VDOT
    assert prof["long_run_day"] == 6
    assert prof["goal_race_date"] is None
    assert prof["plan_start_date"] == "2026-07-14"


def test_default_profile_tolerates_bad_today():
    prof = fp.default_profile(1, None)
    assert prof["plan_start_date"] is None


# --- phase_for_date -------------------------------------------------------

def _race_profile(day, days_out):
    return {"vdot": 50, "goal_race_date": (day + timedelta(days=days_out)).isoformat()}


def test_phase_boundaries():
    day = date(2026, 7, 14)
    assert fp.phase_for_date(_race_profile(day, 14), day) == "taper"    # 2 wks
    assert fp.phase_for_date(_race_profile(day, 35), day) == "peak"     # 5 wks
    assert fp.phase_for_date(_race_profile(day, 84), day) == "quality"  # 12 wks
    assert fp.phase_for_date(_race_profile(day, 140), day) == "base"    # 20 wks


def test_phase_no_or_past_race_is_base():
    day = date(2026, 7, 14)
    assert fp.phase_for_date(None, day) == "base"
    assert fp.phase_for_date({}, day) == "base"
    assert fp.phase_for_date(_race_profile(day, -7), day) == "base"


def test_phase_never_raises_on_partial_profile():
    day = date(2026, 7, 14)
    for profile in (None, {}, {"goal_race_date": ""}, {"goal_race_date": "garbage"},
                    {"goal_race_date": 12345}, {"vdot": "x", "goal_race_date": None}):
        assert fp.phase_for_date(profile, day) in {"base", "quality", "peak", "taper"}
    # a non-date "day" must not raise either
    assert fp.phase_for_date(_race_profile(day, 84), "not-a-date") == "base"


# --- todays_workout / week_plan ------------------------------------------

def test_quality_tuesday_uses_threshold():
    # 2026-07-14 is a Tuesday, 12 weeks out => quality phase, primary day.
    tuesday = date(2026, 7, 14)
    assert tuesday.weekday() == 1
    workout = fp.todays_workout(_race_profile(tuesday, 84), tuesday)
    assert "T pace" in workout
    assert "Threshold" in workout


def test_long_run_on_configured_day():
    sunday = date(2026, 7, 19)
    assert sunday.weekday() == 6
    profile = {"vdot": 50, "long_run_day": 6}
    assert fp.todays_workout(profile, sunday).startswith("Long run")


def test_todays_workout_never_raises_on_partial():
    day = date(2026, 7, 14)
    for profile in (None, {}, {"vdot": "junk"}, {"long_run_day": 99},
                    {"long_run_day": "x", "goal_race_date": "bad"}):
        result = fp.todays_workout(profile, day)
        assert isinstance(result, str) and result


def test_todays_workout_accepts_datetime_and_string():
    day = datetime(2026, 7, 14, 8, 30)
    assert isinstance(fp.todays_workout(None, day), str)
    assert isinstance(fp.todays_workout(None, "2026-07-14"), str)
    # unparseable date still yields a usable string, not an exception
    assert isinstance(fp.todays_workout(None, "not-a-date"), str)


def test_week_plan_is_monday_to_sunday():
    plan = fp.week_plan(None, date(2026, 7, 14))
    assert len(plan) == 7
    names = [name for name, _ in plan]
    assert names == ["Monday", "Tuesday", "Wednesday", "Thursday",
                     "Friday", "Saturday", "Sunday"]
    for _, workout in plan:
        assert isinstance(workout, str) and workout


def test_week_plan_never_raises_on_partial():
    for profile in (None, {}, {"vdot": "x"}, {"long_run_day": None}):
        plan = fp.week_plan(profile, date(2026, 7, 14))
        assert len(plan) == 7
