"""
Garmin Connect daily-activity pull (import-guarded, config-gated).

Optional integration: pulls a day's active/total calories, steps, distance and
per-workout activities from Garmin Connect so the daily report can fold real
cardio burn into the energy-balance line.

This module and its tests run fully WITHOUT the optional ``garminconnect``
package installed and WITHOUT any credentials. When the dependency is absent or
the feature is not configured, every public entry point degrades to a no-op
(``is_configured()`` is False, ``fetch_daily_activity`` returns ``None``).

Auth is token-only: a Garmin session is minted once OFF the server (with the
password, MFA if needed) into a ``~/.garminconnect`` token directory, and that
directory is copied to the VM. The password is NEVER stored or used here — we
only resume the saved garth session, which auto-refreshes.

Config (read from the environment at call time):
    GARMIN_ENABLED    truthy (1/true/yes/on) to turn the pull on
    GARMIN_TOKEN_DIR  path to the pre-minted token directory
"""

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

from utils import parse_boolish, safe_number

try:  # optional dependency — must stay import-safe when absent
    from garminconnect import Garmin
except Exception:  # pragma: no cover - exercised via monkeypatch in tests
    Garmin = None

log = logging.getLogger("garmin")

# ─── Config env var names ─────────────────────────────────────────
GARMIN_ENABLED_ENV = "GARMIN_ENABLED"
GARMIN_TOKEN_DIR_ENV = "GARMIN_TOKEN_DIR"


def _truthy(value) -> bool:
    return parse_boolish(value) is True


def _clean(value) -> Optional[str]:
    """Mirror config._clean_env: strip whitespace/quotes, empty -> None."""
    if value is None:
        return None
    value = str(value).strip().strip('"').strip("'")
    return value or None


def _token_dir() -> Optional[str]:
    return _clean(os.environ.get(GARMIN_TOKEN_DIR_ENV))


def is_configured() -> bool:
    """True only when the library is importable AND the feature is switched on."""
    return Garmin is not None and _truthy(os.environ.get(GARMIN_ENABLED_ENV)) and bool(_token_dir())


@dataclass
class DailyActivity:
    """A day's Garmin summary. All numerics are already coerced via safe_number."""
    active_calories: float = 0
    total_calories: float = 0
    steps: float = 0
    distance_m: float = 0
    activities: List[dict] = field(default_factory=list)


def _resolve(obj, *names):
    """First present callable among ``names`` on ``obj`` (getattr only, no call).

    Garmin's client method names have drifted across releases, so we probe a few
    candidates by name. Resolving never calls the method, so it cannot raise;
    the actual call happens under fetch_daily_activity's single guard.
    """
    for name in names:
        method = getattr(obj, name, None)
        if callable(method):
            return method
    return None


def fetch_daily_activity(date_str: str) -> Optional[DailyActivity]:
    """Pull one user-local day (YYYY-MM-DD) from Garmin Connect.

    Returns ``None`` when unconfigured or on ANY failure — the caller can treat
    a ``None`` as "no Garmin data for this day" and render the report unchanged.
    """
    if not is_configured():
        return None

    try:
        client = Garmin()
        # Token-only resume (garth). No email/password is ever passed here.
        login = _resolve(client, "login")
        if login is not None:
            login(_token_dir())

        get_stats = _resolve(client, "get_stats", "get_user_summary")
        stats = get_stats(date_str) if get_stats is not None else {}
        if not isinstance(stats, dict):
            stats = {}

        get_activities = _resolve(client, "get_activities_by_date")
        raw_activities = get_activities(date_str, date_str) if get_activities is not None else []
        activities = [a for a in raw_activities if isinstance(a, dict)] \
            if isinstance(raw_activities, list) else []

        return DailyActivity(
            active_calories=safe_number(stats.get("activeKilocalories")),
            total_calories=safe_number(stats.get("totalKilocalories")),
            steps=safe_number(stats.get("totalSteps")),
            distance_m=safe_number(stats.get("totalDistanceMeters")),
            activities=activities,
        )
    except Exception as exc:  # noqa: BLE001 - never raise into the report integrator
        log.warning("Garmin fetch for %s failed: %s", date_str, exc)
        return None


def _primary_activity(daily: DailyActivity) -> Optional[dict]:
    """The day's headline workout: longest distance, then most calories, then first."""
    acts = [a for a in (daily.activities or []) if isinstance(a, dict)]
    if not acts:
        return None

    def key(a):
        return (
            safe_number(a.get("distance")),
            safe_number(a.get("calories", a.get("activeKilocalories"))),
        )

    return max(acts, key=key)


def to_activity_kwargs(daily: DailyActivity) -> dict:
    """Kwargs for ``database.save_activity`` for the primary run / day aggregate.

    The caller supplies ``chat_id`` and ``date_str`` positionally, e.g.::

        database.save_activity(chat_id, date_str, **garmin.to_activity_kwargs(daily))

    ``active_calories``/``distance_km`` come from the day summary; the primary
    activity supplies type, duration, HR, elevation, start time and a stable
    ``external_id`` so a re-sync upserts in place. When the day has no discrete
    activity, ``external_id`` is ``None`` (the caller may substitute a
    date-derived id for a clean day-aggregate upsert). Every numeric is bounded
    via safe_number so a hostile payload can never raise here.
    """
    primary = _primary_activity(daily) or {}

    external_id = None
    activity_id = primary.get("activityId")
    if activity_id not in (None, ""):
        external_id = "garmin-" + str(activity_id)

    activity_type = ""
    type_field = primary.get("activityType")
    if isinstance(type_field, dict):
        activity_type = str(type_field.get("typeKey") or "")
    elif isinstance(type_field, str):
        activity_type = type_field

    duration_min = safe_number(primary.get("duration")) / 60.0 or None
    avg_hr = safe_number(primary.get("averageHR"))
    elevation = safe_number(primary.get("elevationGain"))
    start_time = primary.get("startTimeLocal")

    # Day-level steps and whole-day total burn are not first-class activity
    # columns; the report reads them ONLY from the persisted ``raw`` blob, so
    # merge them in alongside the primary-activity payload (copied, so the
    # caller's dict is never mutated).
    raw = dict(primary)
    steps = safe_number(daily.steps)
    if steps > 0:
        raw["steps"] = steps
    total_calories = safe_number(daily.total_calories)
    if total_calories > 0:
        raw["total_calories"] = total_calories

    return {
        "source": "garmin",
        "external_id": external_id,
        "activity_type": activity_type,
        "active_calories": safe_number(daily.active_calories) or None,
        "distance_km": (safe_number(daily.distance_m) / 1000.0) or None,
        "duration_min": duration_min,
        "avg_hr_bpm": int(avg_hr) if avg_hr else None,
        "elevation_gain_m": elevation or None,
        "start_time": str(start_time) if start_time not in (None, "") else None,
        "raw": raw or None,
    }
