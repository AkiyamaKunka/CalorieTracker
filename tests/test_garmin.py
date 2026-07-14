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
