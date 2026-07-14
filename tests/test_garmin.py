"""Tests for the optional, import-guarded Garmin Connect pull.

These run WITHOUT the ``garminconnect`` package installed: the module is
imported with its ``Garmin`` symbol possibly ``None``, and every test that
needs a client injects a fake via monkeypatch.
"""

import garmin


# ─── Fakes ────────────────────────────────────────────────────────
class _FakeGarmin:
    """Minimal stand-in for garminconnect.Garmin, token-only login."""

    def __init__(self, stats=None, activities=None):
        self._stats = stats if stats is not None else {}
        self._activities = activities if activities is not None else []
        self.logged_in_with = None

    def login(self, token_dir=None):
        self.logged_in_with = token_dir
        return True

    def get_stats(self, date_str):
        return self._stats

    def get_activities_by_date(self, start, end):
        return self._activities


def _enable(monkeypatch, token_dir="/tmp/garmin-tokens"):
    monkeypatch.setenv(garmin.GARMIN_ENABLED_ENV, "1")
    monkeypatch.setenv(garmin.GARMIN_TOKEN_DIR_ENV, token_dir)


# ─── is_configured / gating ───────────────────────────────────────
def test_is_configured_false_when_lib_absent(monkeypatch):
    monkeypatch.setattr(garmin, "Garmin", None)
    _enable(monkeypatch)
    assert garmin.is_configured() is False
    # Fully gated: fetch degrades to None without touching any client.
    assert garmin.fetch_daily_activity("2026-07-14") is None


def test_is_configured_false_when_disabled(monkeypatch):
    monkeypatch.setattr(garmin, "Garmin", _FakeGarmin)
    monkeypatch.delenv(garmin.GARMIN_ENABLED_ENV, raising=False)
    monkeypatch.setenv(garmin.GARMIN_TOKEN_DIR_ENV, "/tmp/garmin-tokens")
    assert garmin.is_configured() is False
    assert garmin.fetch_daily_activity("2026-07-14") is None


def test_is_configured_false_when_no_token_dir(monkeypatch):
    monkeypatch.setattr(garmin, "Garmin", _FakeGarmin)
    monkeypatch.setenv(garmin.GARMIN_ENABLED_ENV, "yes")
    monkeypatch.delenv(garmin.GARMIN_TOKEN_DIR_ENV, raising=False)
    assert garmin.is_configured() is False


def test_is_configured_true_when_enabled_and_lib_present(monkeypatch):
    monkeypatch.setattr(garmin, "Garmin", _FakeGarmin)
    _enable(monkeypatch)
    assert garmin.is_configured() is True


# ─── fetch_daily_activity ─────────────────────────────────────────
def test_fetch_coerces_stats_via_safe_number(monkeypatch):
    stats = {
        "activeKilocalories": 640,
        "totalKilocalories": "2200",   # numeric-looking string -> junk -> default 0
        "totalSteps": 11000,
        "totalDistanceMeters": float("inf"),  # hostile -> safe_number default 0
    }
    activities = [{"activityId": 1, "distance": 8000, "calories": 500}]
    fake = _FakeGarmin(stats=stats, activities=activities)
    monkeypatch.setattr(garmin, "Garmin", lambda: fake)
    _enable(monkeypatch, token_dir="/tokens/dir")

    daily = garmin.fetch_daily_activity("2026-07-14")

    assert isinstance(daily, garmin.DailyActivity)
    assert daily.active_calories == 640
    assert daily.steps == 11000
    # safe_number rejects non-numeric strings and inf -> default 0
    assert daily.total_calories == 0
    assert daily.distance_m == 0
    assert daily.activities == activities
    # Token-only resume: login received the configured token dir.
    assert fake.logged_in_with == "/tokens/dir"


def test_fetch_tolerates_nondict_stats_and_activities(monkeypatch):
    fake = _FakeGarmin(stats="unexpected", activities="also-bad")
    monkeypatch.setattr(garmin, "Garmin", lambda: fake)
    _enable(monkeypatch)

    daily = garmin.fetch_daily_activity("2026-07-14")

    assert isinstance(daily, garmin.DailyActivity)
    assert daily.active_calories == 0
    assert daily.activities == []


def test_fetch_returns_none_when_client_raises(monkeypatch):
    def boom():
        raise RuntimeError("garth session expired")

    monkeypatch.setattr(garmin, "Garmin", boom)
    _enable(monkeypatch)

    assert garmin.fetch_daily_activity("2026-07-14") is None


def test_fetch_returns_none_when_a_call_raises(monkeypatch):
    class _Explodes(_FakeGarmin):
        def get_stats(self, date_str):
            raise ValueError("500 from Garmin")

    monkeypatch.setattr(garmin, "Garmin", lambda: _Explodes())
    _enable(monkeypatch)

    # ANY exception below the config gate -> None, never propagated.
    assert garmin.fetch_daily_activity("2026-07-14") is None


# ─── to_activity_kwargs ───────────────────────────────────────────
def test_to_activity_kwargs_maps_primary_run():
    daily = garmin.DailyActivity(
        active_calories=640,
        total_calories=2200,
        steps=11000,
        distance_m=8050,
        activities=[
            {"activityId": 99, "activityType": {"typeKey": "running"},
             "distance": 8050, "duration": 2400, "averageHR": 152,
             "elevationGain": 42, "startTimeLocal": "2026-07-14 06:30:00"},
            {"activityId": 12, "activityType": {"typeKey": "walking"},
             "distance": 500, "duration": 300},
        ],
    )

    kwargs = garmin.to_activity_kwargs(daily)

    assert kwargs["source"] == "garmin"
    assert kwargs["external_id"] == "garmin-99"       # longest distance wins
    assert kwargs["activity_type"] == "running"
    assert kwargs["active_calories"] == 640
    assert kwargs["distance_km"] == 8.05              # from day aggregate, meters/1000
    assert kwargs["duration_min"] == 40.0             # 2400s / 60
    assert kwargs["avg_hr_bpm"] == 152
    assert kwargs["elevation_gain_m"] == 42
    assert kwargs["start_time"] == "2026-07-14 06:30:00"


def test_to_activity_kwargs_handles_empty_day():
    daily = garmin.DailyActivity()  # no calories, no activities

    kwargs = garmin.to_activity_kwargs(daily)

    assert kwargs["source"] == "garmin"
    assert kwargs["external_id"] is None
    assert kwargs["activity_type"] == ""
    # Zeroed numerics collapse to None so save_activity stores NULL, not 0.
    assert kwargs["active_calories"] is None
    assert kwargs["distance_km"] is None
    assert kwargs["duration_min"] is None
    assert kwargs["avg_hr_bpm"] is None
    assert kwargs["raw"] is None  # nothing to persist -> NULL, not '{}'


def test_to_activity_kwargs_coerces_hostile_activity_fields():
    daily = garmin.DailyActivity(
        active_calories="not-a-number",
        distance_m=None,
        activities=[
            {"activityId": "abc", "activityType": "trail_running",
             "distance": "far", "duration": "long", "averageHR": float("nan")},
        ],
    )

    kwargs = garmin.to_activity_kwargs(daily)

    # Hostile strings/NaN never raise; they coerce to safe defaults.
    assert kwargs["external_id"] == "garmin-abc"
    assert kwargs["activity_type"] == "trail_running"
    assert kwargs["active_calories"] is None
    assert kwargs["distance_km"] is None
    assert kwargs["duration_min"] is None
    assert kwargs["avg_hr_bpm"] is None


# ═══ Appended coverage: library drift, payload realism, env hygiene ═══
import pytest

import database


class _SummaryOnlyGarmin:
    """Client shaped like a newer garminconnect release: it exposes ONLY
    get_user_summary (no get_stats) and has no login() at all — both drifts
    must be tolerated by the name-probing resolver."""

    def __init__(self, stats=None, activities=None):
        self._stats = stats if stats is not None else {}
        self._activities = activities if activities is not None else []

    def get_user_summary(self, date_str):
        return self._stats

    def get_activities_by_date(self, start, end):
        return self._activities


def test_fetch_falls_back_to_get_user_summary_when_get_stats_absent(monkeypatch):
    # garminconnect has renamed its summary method across releases; a pip
    # upgrade on the VM must not silently zero out the day's burn.
    fake = _SummaryOnlyGarmin(stats={"activeKilocalories": 480, "totalSteps": 7600})
    monkeypatch.setattr(garmin, "Garmin", lambda: fake)
    _enable(monkeypatch)

    daily = garmin.fetch_daily_activity("2026-07-13")

    assert isinstance(daily, garmin.DailyActivity)
    assert daily.active_calories == 480
    assert daily.steps == 7600


def test_fetch_survives_client_with_no_summary_method(monkeypatch):
    # Worst-case drift: NO summary method under either name. Must degrade to a
    # zeroed day (never raise) while still returning the parsed workout list.
    class _NoSummary:
        def get_activities_by_date(self, start, end):
            return [{"activityId": 5, "distance": 5000}]

    monkeypatch.setattr(garmin, "Garmin", _NoSummary)
    _enable(monkeypatch)

    daily = garmin.fetch_daily_activity("2026-07-13")

    assert isinstance(daily, garmin.DailyActivity)
    assert daily.active_calories == 0
    assert daily.total_calories == 0
    assert daily.steps == 0
    assert daily.activities == [{"activityId": 5, "distance": 5000}]


def test_fetch_survives_client_without_activities_method(monkeypatch):
    # Losing the per-activity endpoint must not take the day summary with it.
    class _StatsOnly:
        def login(self, token_dir=None):
            return True

        def get_stats(self, date_str):
            return {"activeKilocalories": 512, "totalDistanceMeters": 8000}

    monkeypatch.setattr(garmin, "Garmin", _StatsOnly)
    _enable(monkeypatch)

    daily = garmin.fetch_daily_activity("2026-07-13")

    assert isinstance(daily, garmin.DailyActivity)
    assert daily.active_calories == 512
    assert daily.distance_m == 8000
    assert daily.activities == []


# A real-shaped Garmin Connect day (field names and value shapes as the live
# API returns them) used by the realism + contract tests below.
_REAL_STATS = {
    "userProfileId": 88231144,
    "calendarDate": "2026-07-13",
    "activeKilocalories": 612,
    "totalKilocalories": 2214,
    "totalSteps": 12340,
    "totalDistanceMeters": 9240,
    "restingHeartRate": 46,
}

_REAL_RUN = {
    "activityId": 19283746,
    "activityName": "Shanghai Morning Run",
    "activityType": {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
    "distance": 9240.0,
    "duration": 2892.5,
    "averageHR": 148.0,
    "maxHR": 171.0,
    "calories": 601.0,
    "elevationGain": 57.0,
    "startTimeLocal": "2026-07-13 06:12:04",
}


def test_realistic_garmin_day_parses_field_for_field(monkeypatch):
    # Pin the exact wire-format -> DailyActivity -> save_activity-kwargs
    # mapping for a genuine-looking morning run so any field rename shows up
    # as a precise diff, not a silent zero in the energy-balance line.
    fake = _FakeGarmin(stats=dict(_REAL_STATS), activities=[dict(_REAL_RUN)])
    monkeypatch.setattr(garmin, "Garmin", lambda: fake)
    _enable(monkeypatch)

    daily = garmin.fetch_daily_activity("2026-07-13")

    assert daily.active_calories == 612
    assert daily.total_calories == 2214
    assert daily.steps == 12340
    assert daily.distance_m == 9240
    assert daily.activities == [_REAL_RUN]

    assert garmin.to_activity_kwargs(daily) == {
        "source": "garmin",
        "external_id": "garmin-19283746",
        "activity_type": "running",
        "active_calories": 612,
        "distance_km": 9.24,
        "duration_min": 2892.5 / 60,
        "avg_hr_bpm": 148,
        "elevation_gain_m": 57.0,
        "start_time": "2026-07-13 06:12:04",
        # The primary-activity payload plus the day-level steps/total burn the
        # report reads exclusively from raw.
        "raw": {**_REAL_RUN, "steps": 12340, "total_calories": 2214},
    }
    assert daily.activities == [_REAL_RUN]  # the merge never mutates the payload


def test_to_activity_kwargs_feeds_save_activity_end_to_end(monkeypatch, tmp_path):
    # THE contract seam: fetch -> to_activity_kwargs -> database.save_activity
    # must hold with zero adapter glue, and a same-day re-sync must upsert in
    # place instead of double-counting the run.
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "contract.db")
    database.init_db()
    fake = _FakeGarmin(stats=dict(_REAL_STATS), activities=[dict(_REAL_RUN)])
    monkeypatch.setattr(garmin, "Garmin", lambda: fake)
    _enable(monkeypatch)

    daily = garmin.fetch_daily_activity("2026-07-13")
    database.save_activity(7, "2026-07-13", **garmin.to_activity_kwargs(daily))

    rows = database.get_activities(7, "2026-07-13", "2026-07-13")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "garmin"
    assert row["external_id"] == "garmin-19283746"
    assert row["activity_type"] == "running"
    assert row["active_calories"] == 612
    assert row["distance_km"] == 9.24
    assert row["duration_min"] == pytest.approx(2892.5 / 60)
    assert row["avg_hr_bpm"] == 148
    assert row["elevation_gain_m"] == 57.0
    assert row["start_time"] == "2026-07-13 06:12:04"
    # The raw payload (primary run + merged day-level steps/total burn)
    # survives the JSON round-trip.
    assert row["raw"] == {**_REAL_RUN, "steps": 12340, "total_calories": 2214}

    # Evening re-sync of the same day (calories grew): still exactly one row.
    fake2 = _FakeGarmin(stats=dict(_REAL_STATS, activeKilocalories=655),
                        activities=[dict(_REAL_RUN)])
    monkeypatch.setattr(garmin, "Garmin", lambda: fake2)
    daily2 = garmin.fetch_daily_activity("2026-07-13")
    database.save_activity(7, "2026-07-13", **garmin.to_activity_kwargs(daily2))

    rows = database.get_activities(7, "2026-07-13", "2026-07-13")
    assert len(rows) == 1
    assert rows[0]["active_calories"] == 655


def test_day_aggregate_round_trip_renders_steps_and_total_net(monkeypatch, tmp_path):
    # Round-trip contract for a workout-less Garmin day: the day-level steps
    # and total burn must survive to_activity_kwargs -> save_activity -> the
    # report, so the Steps line renders and GARMIN_NET_USE_TOTAL has effect
    # from a real pull (both were silently dropped before the raw merge).
    import daily_report

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "roundtrip.db")
    database.init_db()
    monkeypatch.setattr(daily_report, "CHAT_ID", "7")
    monkeypatch.setenv("GARMIN_NET_USE_TOTAL", "1")

    daily = garmin.DailyActivity(steps=9000, total_calories=2600,
                                 active_calories=450, activities=[])
    database.save_activity(7, "2026-07-13", **garmin.to_activity_kwargs(daily))

    report = daily_report.generate_report("2026-07-13")

    assert "👟 Steps: 9,000" in report
    # Total-basis net: 0 kcal consumed − 2600 kcal whole-day burn.
    assert "⚖️ Net: ~-2,600 kcal (consumed − total burn)" in report


def test_token_dir_env_quotes_and_whitespace_are_stripped(monkeypatch):
    # Ops reality: .env files often carry GARMIN_TOKEN_DIR="/path" with quotes
    # and stray spaces — login() must receive the cleaned path, not the junk.
    fake = _FakeGarmin(stats={"activeKilocalories": 100})
    monkeypatch.setattr(garmin, "Garmin", lambda: fake)
    monkeypatch.setenv(garmin.GARMIN_ENABLED_ENV, "1")
    monkeypatch.setenv(garmin.GARMIN_TOKEN_DIR_ENV, '  "/srv/garmin/tokens"  ')

    assert garmin.is_configured() is True
    daily = garmin.fetch_daily_activity("2026-07-13")

    assert daily is not None
    assert fake.logged_in_with == "/srv/garmin/tokens"


def test_token_dir_of_only_quotes_counts_as_unconfigured(monkeypatch):
    # An empty-but-quoted value ('""') is a missing config, not a valid path —
    # the feature must gate off rather than attempt a login with junk.
    monkeypatch.setattr(garmin, "Garmin", _FakeGarmin)
    monkeypatch.setenv(garmin.GARMIN_ENABLED_ENV, "1")
    monkeypatch.setenv(garmin.GARMIN_TOKEN_DIR_ENV, '""')

    assert garmin.is_configured() is False
    assert garmin.fetch_daily_activity("2026-07-13") is None


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_falsy_garmin_enabled_values_disable_the_pull(monkeypatch, value):
    # Every conventional "off" spelling must fully gate the pull: the client
    # is never even constructed, so no network attempt can happen.
    def _must_not_construct():
        raise AssertionError("Garmin client constructed while disabled")

    monkeypatch.setattr(garmin, "Garmin", _must_not_construct)
    monkeypatch.setenv(garmin.GARMIN_ENABLED_ENV, value)
    monkeypatch.setenv(garmin.GARMIN_TOKEN_DIR_ENV, "/tmp/garmin-tokens")

    assert garmin.is_configured() is False
    assert garmin.fetch_daily_activity("2026-07-13") is None
