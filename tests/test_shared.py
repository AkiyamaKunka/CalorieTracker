"""Pins for the shared modules so the deduplicated helpers cannot re-diverge."""

from datetime import datetime, timedelta, timezone

import config
import daily_report
import database
import service_health
import telegram_bot
import utils


def test_telegram_chunker_is_shared():
    assert telegram_bot.telegram_message_chunks is utils.telegram_message_chunks
    assert daily_report._telegram_chunks is utils.telegram_message_chunks


def test_reports_dir_single_source_of_truth():
    assert telegram_bot.REPORTS_DIR is config.REPORTS_DIR
    assert daily_report.REPORTS_DIR is config.REPORTS_DIR


def test_service_health_path_single_source_of_truth():
    assert telegram_bot.SERVICE_HEALTH_PATH is service_health.DEFAULT_PATH
    assert daily_report.SERVICE_HEALTH_PATH is service_health.DEFAULT_PATH


def test_apply_report_health_schema():
    data = {}
    service_health.apply_report_health(data, True, "2026-07-01", source="cron")
    report = data["daily_report"]
    assert report["last_ok"] is True
    assert report["last_target_date"] == "2026-07-01"
    assert report["consecutive_failures"] == 0
    assert report["events"][-1]["source"] == "cron"

    service_health.apply_report_health(data, False, "2026-07-02", source="telegram_command",
                                       error_summary="boom")
    report = data["daily_report"]
    assert report["last_ok"] is False
    assert report["consecutive_failures"] == 1
    assert report["last_error_summary"] == "boom"
    assert len(report["events"]) == 2


def test_report_health_wrappers_share_schema(tmp_path, monkeypatch):
    bot_path = tmp_path / "bot_health.json"
    report_path = tmp_path / "report_health.json"
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", bot_path)
    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", report_path)

    telegram_bot._record_report_health(True, "2026-07-01")
    daily_report._record_daily_report_health(True, "2026-07-01")

    bot_data = service_health.load(bot_path)["daily_report"]
    report_data = service_health.load(report_path)["daily_report"]
    assert bot_data["last_source"] == "telegram_command"
    assert report_data["last_source"] == "cron"
    assert set(bot_data) == set(report_data)


def test_service_health_update_persists_mutation(tmp_path):
    path = tmp_path / "health.json"

    result = service_health.update(lambda d: d.setdefault("gemini", {}).update({"x": 1}), path)

    assert result["gemini"]["x"] == 1
    assert service_health.load(path)["gemini"]["x"] == 1


def test_service_health_update_serializes_concurrent_writers(tmp_path):
    """Two overlapping read-modify-write cycles must not drop each other's
    record — this is the bot-vs-cron lost-update race."""
    import threading as _threading
    import time as _time

    path = tmp_path / "health.json"
    barrier = _threading.Barrier(2)

    def writer(name):
        def mutate(data):
            events = data.setdefault("events", [])
            _time.sleep(0.05)  # widen the race window inside the critical section
            events.append(name)
        barrier.wait()
        service_health.update(mutate, path)

    threads = [_threading.Thread(target=writer, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(service_health.load(path)["events"]) == ["w0", "w1"]


def test_record_daily_report_health_uses_locked_update(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", tmp_path / "health.json")

    daily_report._record_daily_report_health(True, "2026-07-07")

    data = service_health.load(tmp_path / "health.json")
    assert data["daily_report"]["last_ok"] is True
    assert (tmp_path / "health.json.lock").exists()


def test_get_processing_photo_hashes_filters_by_status(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "meals.db")
    database.init_db()

    assert database.reserve_photo_hash(1, "aa" * 16, "test") is True
    assert database.reserve_photo_hash(1, "bb" * 16, "test") is True
    database.mark_photo_hash_status(1, "bb" * 16, "saved", meal_id=7)

    assert database.get_processing_photo_hashes(1) == ["aa" * 16]


def test_meal_calorie_mismatch_flags_production_shape():
    # The observed 2026-06-20 report bug: items sum to 135, total says 1335.
    analysis = {
        "total_calories": 1335,
        "food_items": [
            {"estimated_calories": 0},
            {"estimated_calories": 0},
            {"estimated_calories": 135},
        ],
    }
    assert utils.meal_calorie_mismatch(analysis) == 135


def test_meal_calorie_mismatch_consistent_and_tolerant():
    consistent = {"total_calories": 640,
                  "food_items": [{"estimated_calories": 400}, {"estimated_calories": 240}]}
    assert utils.meal_calorie_mismatch(consistent) is None
    within_tolerance = {"total_calories": 550,
                        "food_items": [{"estimated_calories": 500}]}
    assert utils.meal_calorie_mismatch(within_tolerance) is None


def test_meal_calorie_mismatch_defensive_on_bad_data():
    assert utils.meal_calorie_mismatch({}) is None
    assert utils.meal_calorie_mismatch(None) is None
    assert utils.meal_calorie_mismatch({"total_calories": None, "food_items": [{"estimated_calories": 500}]}) is None
    assert utils.meal_calorie_mismatch({"total_calories": 900, "food_items": []}) is None
    assert utils.meal_calorie_mismatch(
        {"total_calories": 900, "food_items": [{"estimated_calories": "?"}, {"estimated_calories": True}]}
    ) is None
    # One bad item doesn't poison the numeric ones.
    assert utils.meal_calorie_mismatch(
        {"total_calories": 900, "food_items": [{"estimated_calories": "?"}, {"estimated_calories": 300}]}
    ) == 300


def test_as_number_rejects_absurd_magnitudes():
    # Hallucinated JSON has produced 400-digit ints and 1e400 (-> inf);
    # these overflow int/float arithmetic downstream, so the helper must
    # treat them as non-numeric instead of raising.
    assert utils._as_number(int("9" * 400)) is None
    assert utils._as_number(float("inf")) is None
    assert utils._as_number(float("nan")) is None
    assert utils._as_number(-1e12) is None
    # Sane values still pass through unchanged.
    assert utils._as_number(500) == 500
    assert utils._as_number(-3.5) == -3.5


def test_meal_calorie_mismatch_survives_absurd_values():
    huge_int = int("9" * 400)
    # 400-digit total under a float item sum used to raise OverflowError
    # in the int*float tolerance arithmetic.
    assert utils.meal_calorie_mismatch(
        {"total_calories": huge_int, "food_items": [{"estimated_calories": 135.0}]}
    ) is None
    assert utils.meal_calorie_mismatch(
        {"total_calories": float("inf"), "food_items": [{"estimated_calories": 135}]}
    ) is None
    assert utils.meal_calorie_mismatch(
        {"total_calories": 1335, "food_items": [{"estimated_calories": float("nan")}]}
    ) is None
    assert utils.meal_calorie_mismatch(
        {"total_calories": -1e12, "food_items": [{"estimated_calories": huge_int}]}
    ) is None


def test_meal_calorie_mismatch_skips_non_dict_items():
    # The docstring promises never to crash; non-dict entries (strings,
    # lists, None, numbers) must be skipped, not exploded on .get().
    analysis = {
        "total_calories": 900,
        "food_items": ["burger", ["fries"], None, 42, {"estimated_calories": 300}],
    }
    assert utils.meal_calorie_mismatch(analysis) == 300
    assert utils.meal_calorie_mismatch(
        {"total_calories": 900, "food_items": ["a", ["b"], None, 7]}
    ) is None


def test_parse_timezone_offset():
    assert database.parse_timezone_offset("+0800") == timedelta(hours=8)
    assert database.parse_timezone_offset("-0530") == timedelta(hours=-5, minutes=-30)
    assert database.parse_timezone_offset(" +0800 ") == timedelta(hours=8)
    for bad in ("", None, "0800", "+800", "+08:00", "+2500", "+0860", "garbage"):
        assert database.parse_timezone_offset(bad) is None


def test_user_local_now_applies_device_offset(monkeypatch):
    monkeypatch.setattr(database, "get_android_timezone", lambda device_name="android_watcher": "+0800")
    expected = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
    assert abs((database.user_local_now() - expected).total_seconds()) < 5


def test_user_local_now_falls_back_to_server_clock(monkeypatch):
    monkeypatch.setattr(database, "get_android_timezone", lambda device_name="android_watcher": "garbage")
    assert abs((database.user_local_now() - datetime.now()).total_seconds()) < 5


def test_safe_number_and_safe_food_items_contracts():
    assert utils.safe_number("640") == 0
    assert utils.safe_number(640) == 640
    assert utils.safe_number([], default=7) == 7
    assert utils.safe_number(float("inf")) == 0
    assert utils.safe_number(10 ** 400) == 0

    assert utils.safe_food_items("banana") == []
    assert utils.safe_food_items({"food_items": -1}) == []
    assert utils.safe_food_items({"food_items": ["rice", {"name": "ok"}, None]}) == [{"name": "ok"}]
    assert utils.safe_food_items(None) == []


def test_meal_calorie_mismatch_non_dict_analysis_is_none():
    # Fuzzer classes: truthy non-dict analysis, non-list food_items.
    assert utils.meal_calorie_mismatch("banana") is None
    assert utils.meal_calorie_mismatch(["x"]) is None
    assert utils.meal_calorie_mismatch({"food_items": -10**12, "total_calories": 500}) is None


def test_reserve_photo_hash_is_atomic_under_thread_contention(tmp_path, monkeypatch):
    """S4 regression: concurrent reservers of the same hash yield exactly one
    winner, and after mark-failed, concurrent reclaimers yield exactly one."""
    import concurrent.futures as cf

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "race.db")
    database.init_db()

    for round_no in range(8):
        h = f"{round_no:032x}"
        with cf.ThreadPoolExecutor(12) as pool:
            wins = list(pool.map(lambda _: database.reserve_photo_hash(1, h, "race"), range(12)))
        assert sum(wins) == 1, f"round {round_no}: {sum(wins)} winners"

    h = "f" * 32
    assert database.reserve_photo_hash(1, h, "race")
    database.mark_photo_hash_status(1, h, "failed")
    with cf.ThreadPoolExecutor(8) as pool:
        reclaims = list(pool.map(
            lambda _: database.reserve_photo_hash(1, h, "race", reclaim_statuses={"failed"}),
            range(8)))
    assert sum(reclaims) == 1


def test_processing_set_begin_is_atomic(monkeypatch):
    import concurrent.futures as cf
    import telegram_bot

    telegram_bot._api_upload_processing_hashes.clear()
    wins = []
    with cf.ThreadPoolExecutor(12) as pool:
        list(pool.map(lambda _: wins.append(telegram_bot._begin_api_upload_processing("cc" * 16)), range(12)))
    assert sum(1 for w in wins if w) == 1
    telegram_bot._finish_api_upload_processing("cc" * 16)
    assert not telegram_bot._api_upload_processing_hashes


def test_service_health_update_survives_thread_contention(tmp_path):
    """In-process thread hammer on the flock'd update — no lost appends."""
    import threading
    import service_health

    path = tmp_path / "health.json"

    def worker(tag):
        for i in range(25):
            service_health.update(
                lambda d, t=f"{tag}-{i}": d.setdefault("events", []).append(t),
                path, warn=lambda m: None)

    threads = [threading.Thread(target=worker, args=(f"t{k}",)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(service_health.load(path).get("events", [])) == 100
