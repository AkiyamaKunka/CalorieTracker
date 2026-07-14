"""Jack Daniels' Running Formula math for the marathon training plan.

Pure module: stdlib ``math`` / ``datetime`` only, no database or network
imports. Everything here is deterministic given its arguments so the bot,
the daily report and the tests can share one source of truth for VDOT and
training paces.

The core is the Daniels-Gilbert model that maps a race result to a VDOT
("VO2max equivalent") and a VDOT to the five classic training paces. The
inputs (race distance / time, a stored VDOT) may originate from user or
model supplied fields, so every entry point coerces defensively and never
raises on junk -- callers get ``None`` or a sensible default instead.
"""

import math
from datetime import date, datetime, timedelta

# --- constants ------------------------------------------------------------

DEFAULT_VDOT = 45
VDOT_MIN = 20.0
VDOT_MAX = 85.0
KM_PER_MILE = 1.609344

# Quadratic coefficients of Daniels' VO2 = f(v) curve (v in m/min):
#   VO2 = _VO2_C + _VO2_B * v + _VO2_A * v^2
_VO2_A = 0.000104
_VO2_B = 0.182258
_VO2_C = -4.60

# %VO2max intensities for each training pace. Easy carries a 62-72% range;
# the single "E" value uses 0.70. These are Daniels' canonical intensities;
# Repetition (1.06) is an approximation of the very short/fast R work.
PCT_EASY = 0.70
PCT_EASY_SLOW = 0.62
PCT_EASY_FAST = 0.72
PCT_MARATHON = 0.84
PCT_THRESHOLD = 0.88
PCT_INTERVAL = 0.98
PCT_REPETITION = 1.06

DEFAULT_LONG_RUN_DAY = 6  # Sunday (Monday=0 .. Sunday=6, matching date.weekday())

# Two weekly quality sessions (Tuesday primary, Thursday secondary).
_QUALITY_PRIMARY = 1   # Tuesday
_QUALITY_SECONDARY = 3  # Thursday

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]


# --- small defensive coercions -------------------------------------------

def _coerce_float(value):
    """Finite float, or None for bools / non-numerics / inf / NaN."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _as_date(value):
    """A ``date`` from a date/datetime/'YYYY-MM-DD' string, else None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None
    return None


# --- VDOT <-> pace core ---------------------------------------------------

def vdot_from_race(distance_m, seconds):
    """VDOT for a race of ``distance_m`` metres run in ``seconds``.

    Implements the Daniels-Gilbert model: velocity v = distance / (t minutes),
    VO2 demand of that velocity, and the %VO2max sustainable for a race of
    that duration. VDOT = VO2 / pct, clamped to [20, 85].

    Returns ``None`` for non-numeric, non-finite or non-positive inputs
    (guards divide-by-zero) rather than raising.
    """
    distance_m = _coerce_float(distance_m)
    seconds = _coerce_float(seconds)
    if distance_m is None or seconds is None:
        return None
    if distance_m <= 0 or seconds <= 0:
        return None

    minutes = seconds / 60.0
    v = distance_m / minutes  # m/min
    vo2 = _VO2_C + _VO2_B * v + _VO2_A * v * v
    pct = (0.8
           + 0.1894393 * math.exp(-0.012778 * minutes)
           + 0.2989558 * math.exp(-0.1932605 * minutes))
    if pct <= 0:
        return None

    vdot = vo2 / pct
    return max(VDOT_MIN, min(VDOT_MAX, vdot))


def _velocity_for_vo2(target_vo2):
    """Positive root v (m/min) of _VO2_A v^2 + _VO2_B v - (|_VO2_C| + target).

    Inverts VO2 = f(v). Returns None if no positive real velocity exists.
    """
    a = _VO2_A
    b = _VO2_B
    c = _VO2_C - target_vo2  # _VO2_C is negative, so this is -(4.60 + target)
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    v = (-b + math.sqrt(disc)) / (2 * a)
    if v <= 0:
        return None
    return v


def _pace_sec_per_km(vdot, pct):
    """Seconds per km for running at ``pct`` of ``vdot``'s VO2max, or None."""
    v = _velocity_for_vo2(pct * vdot)
    if v is None:
        return None
    return 60000.0 / v  # 60 s/min * 1000 m/km, divided by m/min


def _clamp_vdot(vdot):
    v = _coerce_float(vdot)
    if v is None:
        v = float(DEFAULT_VDOT)
    return max(VDOT_MIN, min(VDOT_MAX, v))


def paces_from_vdot(vdot):
    """The five Daniels training paces (seconds per km) for ``vdot``.

    Returns a dict with keys ``E`` (easy, 70%), ``M`` (marathon, 84%),
    ``T`` (threshold, 88%), ``I`` (interval, 98%), ``R`` (repetition, 106%),
    plus ``E_range`` -- a ``(fast, slow)`` tuple (ascending seconds/km) for
    the 72%..62% easy band. Use :func:`per_mile` / :func:`format_pace_mi`
    for the sec/mi view.

    This reproduces Daniels' published pace tables to within a few sec/mi for
    VDOT ~40-58 (e.g. VDOT 50 -> T pace ~4:15/km == 6:51/mi, matching the
    book) and drifts slightly fast above VDOT ~60.
    """
    v = _clamp_vdot(vdot)
    return {
        "E": _pace_sec_per_km(v, PCT_EASY),
        "M": _pace_sec_per_km(v, PCT_MARATHON),
        "T": _pace_sec_per_km(v, PCT_THRESHOLD),
        "I": _pace_sec_per_km(v, PCT_INTERVAL),
        "R": _pace_sec_per_km(v, PCT_REPETITION),
        "E_range": (_pace_sec_per_km(v, PCT_EASY_FAST),
                    _pace_sec_per_km(v, PCT_EASY_SLOW)),
    }


# --- pace formatting ------------------------------------------------------

def per_mile(sec_per_km):
    """Convert seconds-per-km to seconds-per-mile (0.0 for junk input)."""
    value = _coerce_float(sec_per_km)
    if value is None:
        return 0.0
    return value * KM_PER_MILE


def _mmss(seconds):
    value = _coerce_float(seconds)
    if value is None or value <= 0:
        return "-:--"
    total = int(round(value))
    return "{}:{:02d}".format(total // 60, total % 60)


def format_pace(sec_per_km):
    """Format a seconds-per-km pace as ``"m:ss/km"``."""
    return _mmss(sec_per_km) + "/km"


def format_pace_mi(sec_per_km):
    """Format a seconds-per-km pace as ``"m:ss/mi"``."""
    return _mmss(per_mile(sec_per_km)) + "/mi"


# --- profile helpers ------------------------------------------------------

def _profile_vdot(profile):
    if isinstance(profile, dict):
        v = _coerce_float(profile.get("vdot"))
        if v is not None and VDOT_MIN <= v <= VDOT_MAX:
            return v
    return float(DEFAULT_VDOT)


def _long_run_day(profile):
    if isinstance(profile, dict):
        v = _coerce_float(profile.get("long_run_day"))
        if v is not None:
            day = int(v)
            if 0 <= day <= 6:
                return day
    return DEFAULT_LONG_RUN_DAY


def _goal_race_date(profile):
    if not isinstance(profile, dict):
        return None
    return _as_date(profile.get("goal_race_date"))


def default_profile(chat_id, today):
    """A stand-in fitness_profile dict for a user who hasn't set one.

    Mirrors the ``fitness_profile`` columns the plan actually reads: a
    default VDOT, a Sunday long run and no goal race yet.
    """
    start = _as_date(today)
    return {
        "chat_id": chat_id,
        "vdot": float(DEFAULT_VDOT),
        "race_distance_km": None,
        "race_time_seconds": None,
        "race_label": None,
        "goal_race_date": None,
        "plan_start_date": start.isoformat() if start is not None else None,
        "long_run_day": DEFAULT_LONG_RUN_DAY,
    }


# --- periodisation --------------------------------------------------------

def phase_for_date(profile, day):
    """Training phase for ``day`` given the profile's goal race.

    One of ``base`` | ``quality`` | ``peak`` | ``taper`` from weeks-to-race:
    taper in the last ~3 weeks, peak ~3-8 weeks out, quality ~8-16, and base
    earlier, with no goal race, or once the race has passed. Never raises on
    a missing or partial profile.
    """
    today = _as_date(day)
    race = _goal_race_date(profile)
    if today is None or race is None:
        return "base"

    days_to_race = (race - today).days
    if days_to_race < 0:
        return "base"

    weeks = days_to_race / 7.0
    if weeks <= 3:
        return "taper"
    if weeks <= 8:
        return "peak"
    if weeks <= 16:
        return "quality"
    return "base"


# --- workout text ---------------------------------------------------------

def _easy_text(paces):
    return "Easy run - 40-60min @ E pace ({})".format(format_pace(paces["E"]))


def _long_run_text(phase, paces):
    if phase == "peak":
        return ("Long run - up to 2h, last 20min @ M pace ({})"
                .format(format_pace(paces["M"])))
    if phase == "taper":
        return ("Long run - 60-75min relaxed @ E pace ({})"
                .format(format_pace(paces["E"])))
    return "Long run - 90-120min @ E pace ({})".format(format_pace(paces["E"]))


def _quality_text(phase, weekday, paces):
    primary = (weekday == _QUALITY_PRIMARY)
    t = format_pace(paces["T"])
    i = format_pace(paces["I"])
    m = format_pace(paces["M"])
    r = format_pace(paces["R"])

    if phase == "base":
        if primary:
            return "Strides - easy run + 8x20s @ R pace ({}), full recovery".format(r)
        return "Easy run - steady aerobic, finish with 6x20s strides"
    if phase == "quality":
        if primary:
            return "Threshold - 4x1mi @ T pace ({}), WU/CD 15min E".format(t)
        return "Intervals - 5x1000m @ I pace ({}), 3min jog rec".format(i)
    if phase == "peak":
        if primary:
            return "Intervals - 5x1km @ I pace ({}), 3min jog rec".format(i)
        return "Threshold - 20min tempo @ T pace ({}), WU/CD 15min E".format(t)
    # taper
    if primary:
        return "Sharpener - 3x1km @ T pace ({}), easy recovery".format(t)
    return "Race pace - 2x1mi @ M pace ({}), stay smooth".format(m)


def todays_workout(profile, day):
    """A short human workout string for ``day`` under the profile's plan.

    Long-run day gets a long run, the day after it is recovery, Tuesday and
    Thursday are quality sessions keyed to the current phase, and everything
    else is easy running. Never raises on a missing/partial profile.
    """
    paces = paces_from_vdot(_profile_vdot(profile))
    phase = phase_for_date(profile, day)
    long_day = _long_run_day(profile)

    today = _as_date(day)
    if today is None:
        return _easy_text(paces)

    weekday = today.weekday()
    if weekday == long_day:
        return _long_run_text(phase, paces)
    if weekday == (long_day + 1) % 7:
        return "Rest - full rest or light cross-training"
    if weekday in (_QUALITY_PRIMARY, _QUALITY_SECONDARY):
        return _quality_text(phase, weekday, paces)
    return _easy_text(paces)


def week_plan(profile, day):
    """Mon..Sun list of ``(weekday_name, workout_str)`` for ``day``'s week.

    The caller marks which entry is "today". Never raises on a missing or
    partial profile.
    """
    today = _as_date(day) or date.today()
    monday = today - timedelta(days=today.weekday())
    plan = []
    for offset in range(7):
        current = monday + timedelta(days=offset)
        plan.append((DAY_NAMES[offset], todays_workout(profile, current)))
    return plan
