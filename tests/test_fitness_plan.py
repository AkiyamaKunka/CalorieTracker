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


# ===========================================================================
# Deep-dive additions (append-only): mathematical soundness sweeps, anchors
# from Daniels' published tables, schedule integrity across every
# configuration, and exact phase fence-posts. A marathoner trains off these
# numbers, so the math must be monotone, ordered, and table-faithful.
# ===========================================================================

import pytest


# --- monotonicity sweeps ----------------------------------------------------

def test_vdot_strictly_increases_as_5k_time_drops():
    # A faster 5k must NEVER map to an equal-or-lower fitness score, else the
    # bot could tell Robert he got slower after a PR. Sweep 16:00..30:00.
    times = list(range(16 * 60, 30 * 60 + 1, 15))
    vdots = [fp.vdot_from_race(5000, t) for t in times]
    assert all(v is not None for v in vdots)
    # sweep stays inside the clamp band so monotonicity is genuinely strict
    assert all(fp.VDOT_MIN < v < fp.VDOT_MAX for v in vdots)
    for slower, faster in zip(vdots, vdots[1:]):
        assert faster < slower  # more seconds -> strictly lower VDOT


def test_every_pace_strictly_speeds_up_with_vdot():
    # Each +1 VDOT must strictly speed up all five paces and both easy-band
    # ends across the whole supported 20..85 band (incl. clamp endpoints).
    prev = None
    for vdot in range(int(fp.VDOT_MIN), int(fp.VDOT_MAX) + 1):
        paces = fp.paces_from_vdot(vdot)
        flat = [paces[k] for k in ("E", "M", "T", "I", "R")] + list(paces["E_range"])
        assert all(isinstance(p, float) and p > 0 for p in flat)
        if prev is not None:
            for faster, slower in zip(flat, prev):
                assert faster < slower
        prev = flat


def test_pace_order_holds_at_every_vdot():
    # R < I < T < M < E (sec/km) and E_range brackets E at EVERY fitness
    # level, not just the single VDOT-50 point already pinned above.
    for vdot in range(int(fp.VDOT_MIN), int(fp.VDOT_MAX) + 1):
        paces = fp.paces_from_vdot(vdot)
        assert paces["R"] < paces["I"] < paces["T"] < paces["M"] < paces["E"]
        fast, slow = paces["E_range"]
        assert fast < paces["E"] < slow


# --- anchors from Daniels' published tables ---------------------------------

@pytest.mark.parametrize("label, distance_m, seconds, book_vdot", [
    # Values from Jack Daniels' Running Formula VDOT tables (2% tolerance):
    ("5k 25:00", 5000, 25 * 60, 38.3),
    ("10k 41:21", 10000, 41 * 60 + 21, 50.0),
    ("marathon 3:28:26", 42195, 3 * 3600 + 28 * 60 + 26, 45.0),
    ("half 1:24:00", 21097.5, 1 * 3600 + 24 * 60, 55.0),
])
def test_vdot_anchors_from_daniels_tables(label, distance_m, seconds, book_vdot):
    vdot = fp.vdot_from_race(distance_m, seconds)
    assert vdot is not None
    assert abs(vdot - book_vdot) / book_vdot <= 0.02, (
        "{}: got VDOT {:.2f}, book says ~{}".format(label, vdot, book_vdot))


@pytest.mark.parametrize("seconds", [16 * 60, 18 * 60, 20 * 60, 22 * 60,
                                     25 * 60, 28 * 60, 30 * 60])
def test_interval_pace_tracks_5k_race_pace(seconds):
    # Daniels property: I pace ~= current 5k race pace. If the round-trip
    # race -> VDOT -> I pace drifts, interval workouts are mis-prescribed.
    vdot = fp.vdot_from_race(5000, seconds)
    i_pace = fp.paces_from_vdot(vdot)["I"]
    race_pace = seconds / 5.0
    assert abs(i_pace - race_pace) / race_pace <= 0.05


def test_numeric_strings_accepted_for_race_inputs():
    # Race fields arrive from Telegram text / Gemini JSON as strings; a
    # stringly-typed "5000"/"1197" must score the same as the numbers.
    assert fp.vdot_from_race("5000", "1197") == fp.vdot_from_race(5000, 1197)


# --- schedule integrity -----------------------------------------------------

@pytest.mark.parametrize("days_out", [None, 84, 35, 14])  # base/quality/peak/taper
@pytest.mark.parametrize("long_run_day", range(7))
def test_week_plan_long_run_placement_every_config(long_run_day, days_out):
    # For every phase x long-run-day combo: 7 Mon..Sun entries, exactly ONE
    # long run and it lands on the configured day (quality never steals it),
    # and the following day is always recovery.
    day = date(2026, 7, 15)  # a Wednesday
    profile = {"vdot": 50, "long_run_day": long_run_day}
    if days_out is not None:
        profile["goal_race_date"] = (day + timedelta(days=days_out)).isoformat()
    plan = fp.week_plan(profile, day)
    assert [name for name, _ in plan] == fp.DAY_NAMES
    workouts = [w for _, w in plan]
    long_idxs = [i for i, w in enumerate(workouts) if w.startswith("Long run")]
    assert long_idxs == [long_run_day]
    assert workouts[(long_run_day + 1) % 7].startswith("Rest")


def test_rest_day_takes_precedence_over_quality():
    # Monday long run makes Tuesday the recovery day; Robert must never be
    # prescribed a threshold session the day after his long run.
    profile = {"vdot": 50, "long_run_day": 0, "goal_race_date": "2026-10-11"}
    plan = fp.week_plan(profile, date(2026, 7, 15))
    assert plan[1][1].startswith("Rest")


def test_todays_workout_agrees_with_week_plan_every_day():
    # /today and /week must never disagree -- even across a phase boundary
    # falling mid-week (race 2026-08-05: Mon 7/13 is 23 days out = peak,
    # Wed 7/15 is 21 days out = taper).
    profile = {"vdot": 52, "long_run_day": 6, "goal_race_date": "2026-08-05"}
    monday = date(2026, 7, 13)
    for offset in range(7):
        d = monday + timedelta(days=offset)
        assert fp.todays_workout(profile, d) == fp.week_plan(profile, d)[d.weekday()][1]


def test_week_plan_uses_each_days_own_phase():
    # Each day is planned with its OWN date's phase: in the 7/13 week above,
    # Tuesday (22 days out) is still peak intervals while Thursday (20 days
    # out) has already switched to the taper's race-pace sharpener.
    profile = {"vdot": 52, "long_run_day": 6, "goal_race_date": "2026-08-05"}
    plan = fp.week_plan(profile, date(2026, 7, 13))
    assert plan[1][1].startswith("Intervals")   # Tue: peak primary quality
    assert plan[3][1].startswith("Race pace")   # Thu: taper secondary quality


def test_taper_long_run_is_shortened():
    # In taper the long run must drop to 60-75min relaxed -- a 2h long run
    # three weeks before the marathon would sabotage the race.
    sunday = date(2026, 7, 19)
    taper = {"vdot": 50, "long_run_day": 6,
             "goal_race_date": (sunday + timedelta(days=14)).isoformat()}
    peak = {"vdot": 50, "long_run_day": 6,
            "goal_race_date": (sunday + timedelta(days=35)).isoformat()}
    assert "60-75min" in fp.todays_workout(taper, sunday)
    assert "M pace" in fp.todays_workout(peak, sunday)  # peak: finish @ M


def test_week_plan_structure_survives_garbage_day():
    # An unparseable "day" still yields a full, structurally valid week
    # (content falls back to the current week, so only structure is pinned).
    plan = fp.week_plan(None, "garbage")
    assert [name for name, _ in plan] == fp.DAY_NAMES
    assert all(isinstance(w, str) and w for _, w in plan)


# --- hostile profile fields -------------------------------------------------

@pytest.mark.parametrize("bad_vdot", [1e9, 5, -50, 0, True])
def test_out_of_band_profile_vdot_falls_back_to_default_paces(bad_vdot):
    # A corrupted or model-invented stored VDOT (1e9, negative, bool) must
    # not leak absurd paces into workouts -- it falls back to the default.
    day = date(2026, 7, 17)  # Friday: plain easy run, pace shown in text
    assert (fp.todays_workout({"vdot": bad_vdot}, day)
            == fp.todays_workout({"vdot": fp.DEFAULT_VDOT}, day))


@pytest.mark.parametrize("raw, expected_idx", [
    ("3", 3),     # numeric string from a text field is still honoured
    (5.9, 5),     # floats truncate rather than rounding out of range
    (-1, 6),      # out of range -> default Sunday
    (7, 6),
    (True, 6),    # bool is not a day index
])
def test_hostile_long_run_day_placement(raw, expected_idx):
    plan = fp.week_plan({"vdot": 50, "long_run_day": raw}, date(2026, 7, 15))
    long_idxs = [i for i, (_, w) in enumerate(plan) if w.startswith("Long run")]
    assert long_idxs == [expected_idx]


# --- phase fence-posts ------------------------------------------------------

@pytest.mark.parametrize("days_out, expected", [
    (0, "taper"),     # race day itself still reads as taper, not base
    (1, "taper"),
    (21, "taper"),    # exactly 3 weeks: fence-post belongs to taper
    (22, "peak"),
    (56, "peak"),     # exactly 8 weeks: still peak
    (57, "quality"),
    (112, "quality"),  # exactly 16 weeks: still quality
    (113, "base"),
    (-1, "base"),     # the day after the race the plan resets to base
])
def test_phase_exact_fence_posts(days_out, expected):
    day = date(2026, 7, 14)
    profile = {"vdot": 50,
               "goal_race_date": (day + timedelta(days=days_out)).isoformat()}
    assert fp.phase_for_date(profile, day) == expected


def test_leap_day_race_date_is_valid_and_phased():
    # 2028-02-29 is a real date; the parser must not junk it (and an ISO
    # timestamp with a time part must parse via its date prefix too).
    for stored in ("2028-02-29", "2028-02-29T08:00:00"):
        profile = {"vdot": 50, "goal_race_date": stored}
        assert fp.phase_for_date(profile, date(2028, 2, 8)) == "taper"  # 21 days
        assert fp.phase_for_date(profile, date(2028, 2, 7)) == "peak"   # 22 days
        race = date(2028, 2, 29)
        assert fp.phase_for_date(profile, race - timedelta(days=100)) == "quality"


@pytest.mark.parametrize("bad", ["2027-02-29",   # non-leap Feb 29
                                 "2026-13-01",   # month 13
                                 "2026-02-30",   # day 30 in Feb
                                 "2026-06-31"])  # day 31 in June
def test_invalid_calendar_dates_fall_back_to_base(bad):
    # Plausible-looking but impossible dates (typos, hostile model output)
    # must degrade to base phase, never crash the plan.
    day = date(2026, 7, 14)
    profile = {"vdot": 50, "goal_race_date": bad}
    assert fp.phase_for_date(profile, day) == "base"
    assert isinstance(fp.todays_workout(profile, day), str)
    assert len(fp.week_plan(profile, day)) == 7


# --- pace formatting --------------------------------------------------------

@pytest.mark.parametrize("sec_per_km, expected", [
    (240, "4:00/km"),      # exact round number
    (245.4, "4:05/km"),    # fractional input rounds; single-digit secs padded
    (299.6, "5:00/km"),    # rounds up cleanly across the minute boundary
    (65, "1:05/km"),
    (3661, "61:01/km"),    # no hours unit: minutes just keep counting
])
def test_format_pace_km_variants(sec_per_km, expected):
    assert fp.format_pace(sec_per_km) == expected


@pytest.mark.parametrize("sec_per_km, expected_mi", [
    (240, "6:26/mi"),   # 240 * 1.609344 = 386.24 -> 6:26
    (300, "8:03/mi"),   # 300 * 1.609344 = 482.80 -> 8:03
])
def test_format_pace_mi_consistent_with_km_factor(sec_per_km, expected_mi):
    # The /mi display must be the exact same pace scaled by KM_PER_MILE,
    # so km and mi readouts never describe two different efforts.
    assert fp.format_pace_mi(sec_per_km) == expected_mi
    assert fp.per_mile(sec_per_km) == pytest.approx(sec_per_km * fp.KM_PER_MILE)
