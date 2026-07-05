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
