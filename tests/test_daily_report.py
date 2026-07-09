from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import daily_report


def test_clean_env_strips_shell_quotes(monkeypatch):
    import config

    monkeypatch.setenv("X_TEST_TOKEN", '"1234567890"')
    assert config._clean_env("X_TEST_TOKEN") == "1234567890"
    monkeypatch.setenv("X_TEST_TOKEN", "'token'")
    assert config._clean_env("X_TEST_TOKEN") == "token"
    monkeypatch.setenv("X_TEST_TOKEN", '  ""  ')
    assert config._clean_env("X_TEST_TOKEN", "fallback") is None
    monkeypatch.delenv("X_TEST_TOKEN")
    assert config._clean_env("X_TEST_TOKEN", "fallback") == "fallback"


def test_generate_report_escapes_dynamic_html(monkeypatch):
    monkeypatch.setattr(daily_report, "CHAT_ID", "12345")

    def fake_get_meals(chat_id, start_date, end_date):
        return [
            {
                "time": "20:30 <late>",
                "corrected": False,
                "analysis": {
                    "is_food": True,
                    "meal_description": "Fish & <rice>",
                    "total_calories": 500,
                    "total_protein_g": 30,
                    "total_carbs_g": 55,
                    "total_fat_g": 12,
                    "food_items": [
                        {
                            "name": "Fish & sauce <hot>",
                            "estimated_calories": 300,
                            "protein_g": 28,
                            "carbs_g": 5,
                            "fat_g": 10,
                        }
                    ],
                },
            }
        ]

    monkeypatch.setattr(daily_report.database, "get_meals", fake_get_meals)

    report = daily_report.generate_report("2026-06-25")

    assert "Fish &amp; &lt;rice&gt;" in report
    assert "20:30 &lt;late&gt;" in report
    assert "Fish &amp; sauce &lt;hot&gt;" in report


def _meal(description="Meal", calories=500, protein=30, carbs=55, fat=12,
          items=None, time="12:00", corrected=False, **extra):
    meal = {
        "time": time,
        "corrected": corrected,
        "analysis": {
            "is_food": True,
            "meal_description": description,
            "total_calories": calories,
            "total_protein_g": protein,
            "total_carbs_g": carbs,
            "total_fat_g": fat,
            "food_items": items or [],
        },
    }
    meal.update(extra)
    return meal


def _patch_meals(monkeypatch, today_meals, prior_meals=None):
    monkeypatch.setattr(daily_report, "CHAT_ID", "12345")

    def fake_get_meals(chat_id, start_date, end_date):
        if start_date == end_date:
            return today_meals
        return prior_meals or []

    monkeypatch.setattr(daily_report.database, "get_meals", fake_get_meals)


def test_generate_report_warns_on_item_total_mismatch(monkeypatch):
    # Reproduces the June-20 production shape: items 0+0+135 under total 1335.
    items = [
        {"name": "Soup", "estimated_calories": 0},
        {"name": "Greens", "estimated_calories": 0},
        {"name": "Rice", "estimated_calories": 135},
    ]
    _patch_meals(monkeypatch, [_meal(calories=1335, items=items)])

    report = daily_report.generate_report("2026-06-20")

    assert (
        "⚠️ Item calories sum to ~135 kcal but the meal total is ~1335 kcal"
        in report
    )
    assert "this entry may be wrong" in report


def test_generate_report_no_mismatch_warning_when_consistent(monkeypatch):
    items = [
        {"name": "Fish", "estimated_calories": 300},
        {"name": "Rice", "estimated_calories": 200},
    ]
    _patch_meals(monkeypatch, [_meal(calories=500, items=items)])

    report = daily_report.generate_report("2026-06-20")

    assert "Item calories sum to" not in report


def test_generate_report_no_mismatch_warning_for_corrected_meal(monkeypatch):
    items = [{"name": "Rice", "estimated_calories": 135}]
    _patch_meals(monkeypatch, [_meal(calories=1335, items=items, corrected=True)])

    report = daily_report.generate_report("2026-06-20")

    assert "Item calories sum to" not in report


def test_generate_report_flags_duplicate_hashless_meals(monkeypatch):
    meals = [
        _meal(description="Noodles", calories=600, time="12:00", image_hash=""),
        _meal(description="Noodles", calories=600, time="12:00", image_hash=""),
    ]
    _patch_meals(monkeypatch, meals)

    report = daily_report.generate_report("2026-06-20")

    assert report.count("Possible duplicate of meal") == 1
    assert "⚠️ Possible duplicate of meal 1." in report


def test_generate_report_does_not_flag_distinct_meals(monkeypatch):
    meals = [
        _meal(description="Noodles", calories=600, time="12:00"),
        _meal(description="Salad", calories=300, time="18:30"),
    ]
    _patch_meals(monkeypatch, meals)

    report = daily_report.generate_report("2026-06-20")

    assert "Possible duplicate" not in report


def test_generate_report_flags_duplicate_by_image_hash(monkeypatch):
    meals = [
        _meal(description="Lunch photo", calories=700, time="12:00", image_hash="abc123"),
        _meal(description="Lunch photo (again)", calories=650, time="12:05", image_hash="abc123"),
    ]
    _patch_meals(monkeypatch, meals)

    report = daily_report.generate_report("2026-06-20")

    assert "⚠️ Possible duplicate of meal 1." in report


def test_generate_report_seven_day_average_and_delta(monkeypatch):
    prior = [
        _meal(calories=1800, date="2026-06-18"),
        _meal(calories=2200, date="2026-06-18"),
        _meal(calories=1000, date="2026-06-19"),
    ]
    _patch_meals(monkeypatch, [_meal(calories=3000)], prior_meals=prior)

    report = daily_report.generate_report("2026-06-20")

    # Two distinct prior days: 4000 and 1000 kcal -> avg 2500.
    assert "📈 <b>7-day avg:</b> ~2,500 kcal" in report
    assert "Today vs avg: +500 kcal (+20%)" in report


def test_generate_report_seven_day_average_suppressed_for_single_day(monkeypatch):
    prior = [
        _meal(calories=1800, date="2026-06-19"),
        _meal(calories=200, date="2026-06-19"),
    ]
    _patch_meals(monkeypatch, [_meal(calories=3000)], prior_meals=prior)

    report = daily_report.generate_report("2026-06-20")

    assert "7-day avg" not in report
    assert "Today vs avg" not in report


def test_generate_report_seven_day_average_ignores_absurd_calories(monkeypatch):
    # json.loads turns 1e400 into float inf, and Gemini has hallucinated
    # absurdly huge integer totals; neither may crash the average
    # arithmetic (OverflowError) nor poison it.
    inf_cal = json.loads("1e400")
    huge_int = int("9" * 400)
    prior = [
        _meal(calories=inf_cal, date="2026-06-17"),
        _meal(calories=huge_int, date="2026-06-18"),
        _meal(calories=1800, date="2026-06-18"),
        _meal(calories=2200, date="2026-06-19"),
    ]
    _patch_meals(monkeypatch, [_meal(calories=3000)], prior_meals=prior)

    report = daily_report.generate_report("2026-06-20")

    # Renders without raising; the average uses only the sane meals:
    # 06-18 -> 1800, 06-19 -> 2200 => avg 2000.
    assert "📈 <b>7-day avg:</b> ~2,000 kcal" in report
    assert "Today vs avg: +1,000 kcal (+50%)" in report


def test_generate_report_seven_day_average_suppressed_when_absurd_days_leave_one(monkeypatch):
    # If discarding absurd totals leaves fewer than two sane prior days,
    # the block is suppressed rather than computed from garbage.
    prior = [
        _meal(calories=json.loads("1e400"), date="2026-06-17"),
        _meal(calories=int("9" * 400), date="2026-06-18"),
        _meal(calories=1800, date="2026-06-19"),
    ]
    _patch_meals(monkeypatch, [_meal(calories=3000)], prior_meals=prior)

    report = daily_report.generate_report("2026-06-20")

    assert "7-day avg" not in report
    assert "Today vs avg" not in report


def test_generate_report_seven_day_window_bounds(monkeypatch):
    monkeypatch.setattr(daily_report, "CHAT_ID", "12345")
    windows = []

    def fake_get_meals(chat_id, start_date, end_date):
        windows.append((start_date, end_date))
        if start_date == end_date:
            return [_meal(calories=500)]
        return []

    monkeypatch.setattr(daily_report.database, "get_meals", fake_get_meals)

    daily_report.generate_report("2026-06-20")

    assert ("2026-06-20", "2026-06-20") in windows
    assert ("2026-06-13", "2026-06-19") in windows


def test_send_telegram_retries_plain_text_and_reports_failure(monkeypatch):
    calls = []
    responses = [
        {"ok": False, "description": "can't parse entities"},
        {"ok": False, "description": "chat not found"},
    ]

    def fake_post(url, json, timeout):
        calls.append(json)
        return SimpleNamespace(
            status_code=400,
            text="bad request",
            json=lambda: responses.pop(0),
        )

    monkeypatch.setattr(daily_report, "BOT_TOKEN", "token")
    monkeypatch.setattr(daily_report, "CHAT_ID", "12345")
    monkeypatch.setattr(daily_report.requests, "post", fake_post)

    assert daily_report.send_telegram("<b>Report</b>") is False
    assert calls[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in calls[1]


def test_record_daily_report_health_success_and_failure(monkeypatch, tmp_path):
    health_path = tmp_path / "service_health.json"
    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", health_path)

    daily_report._record_daily_report_health(True, "2026-06-27", report_path="/tmp/report.md")
    success_health = json.loads(health_path.read_text())
    assert success_health["daily_report"]["last_ok"] is True
    assert success_health["daily_report"]["consecutive_failures"] == 0

    daily_report._record_daily_report_health(False, "2026-06-28", error_summary="Telegram failed")
    failure_health = json.loads(health_path.read_text())
    assert failure_health["daily_report"]["last_ok"] is False
    assert failure_health["daily_report"]["consecutive_failures"] == 1
    assert failure_health["daily_report"]["last_error_summary"] == "Telegram failed"


def test_main_records_telegram_failure(monkeypatch, tmp_path):
    health_path = tmp_path / "service_health.json"
    report_path = tmp_path / "report.md"

    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", health_path)
    monkeypatch.setattr(daily_report.sys, "argv", ["daily_report.py", "2026-06-27"])
    monkeypatch.setattr(daily_report, "generate_report", lambda target_date: "<b>Report</b>")
    monkeypatch.setattr(daily_report, "save_report_file", lambda target_date, report: report_path)
    monkeypatch.setattr(daily_report, "send_telegram", lambda report: False)
    monkeypatch.setattr(daily_report, "send_wechat", lambda report, target_date: None)

    assert daily_report.main() == 1

    health = json.loads(health_path.read_text())
    assert health["daily_report"]["last_ok"] is False
    assert health["daily_report"]["last_target_date"] == "2026-06-27"
    assert "Telegram send failed" in health["daily_report"]["last_error_summary"]


def test_telegram_chunks_splits_without_losing_text():
    chunks = daily_report._telegram_chunks("abc\ndefghijk", limit=5)

    assert "".join(chunks).replace("\n", "") == "abcdefghijk"
    assert all(len(chunk) <= 5 for chunk in chunks)


def test_resolve_auto_target_date_late_night_targets_today():
    local_time = datetime(2026, 7, 4, 23, 45)

    assert daily_report.resolve_auto_target_date(local_time, {}) == "2026-07-04"


def test_resolve_auto_target_date_morning_catches_up_yesterday():
    local_time = datetime(2026, 7, 5, 7, 40)

    assert daily_report.resolve_auto_target_date(local_time, {}) == "2026-07-04"


def test_resolve_auto_target_date_skips_when_already_sent():
    local_time = datetime(2026, 7, 5, 7, 40)
    health = {
        "daily_report": {
            "last_target_date": "2026-07-04",
            "last_ok": True,
            "last_source": "cron",
        }
    }

    assert daily_report.resolve_auto_target_date(local_time, health) is None


def test_resolve_auto_target_date_manual_success_does_not_suppress_auto_run():
    # A successful /report (telegram_command) or CLI run for the same date
    # writes the same ledger record, but must NOT skip the nightly cron run.
    local_time = datetime(2026, 7, 5, 7, 40)
    for manual_source in ("telegram_command", "manual"):
        health = {
            "daily_report": {
                "last_target_date": "2026-07-04",
                "last_ok": True,
                "last_source": manual_source,
            }
        }

        assert daily_report.resolve_auto_target_date(local_time, health) == "2026-07-04"


def test_resolve_auto_target_date_retries_after_failed_send():
    local_time = datetime(2026, 7, 5, 7, 40)
    health = {"daily_report": {"last_target_date": "2026-07-04", "last_ok": False}}

    assert daily_report.resolve_auto_target_date(local_time, health) == "2026-07-04"


def test_generate_report_rejects_non_numeric_chat_id(monkeypatch):
    monkeypatch.setattr(daily_report, "CHAT_ID", "not-a-number")

    calls = []
    monkeypatch.setattr(
        daily_report.database, "get_meals", lambda *args: calls.append(args) or []
    )

    assert daily_report.generate_report("2026-06-25") == ""
    assert calls == []


def test_save_report_file_strips_telegram_html(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_report, "REPORTS_DIR", tmp_path)

    report = '<b>Daily</b>\n<i>note</i> &amp; <a href="https://x">link</a>'
    filepath = daily_report.save_report_file("2026-06-25", report)
    content = filepath.read_text()

    assert "<b>" not in content
    assert "<i>" not in content
    assert "&amp;" not in content
    assert "Daily" in content
    assert "note & link" in content


def test_send_wechat_uses_https_and_plain_text(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["payload"] = json
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"code": 200})

    monkeypatch.setattr(daily_report, "PUSHPLUS_TOKEN", "token")
    monkeypatch.setattr(daily_report.requests, "post", fake_post)

    daily_report.send_wechat("<b>Report</b> &amp; more", "2026-06-25")

    assert captured["url"].startswith("https://")
    assert captured["payload"]["content"] == "Report & more"
    assert captured["payload"]["template"] == "txt"


def test_send_wechat_user_markup_is_sent_as_literal_text(monkeypatch):
    # generate_report escapes user text for Telegram, but _html_to_plain
    # unescapes it again, so user-typed markup like <script> reaches the
    # PushPlus payload verbatim. Template "txt" makes PushPlus render it
    # literally instead of as HTML/markdown.
    captured = {}

    def fake_post(url, json, timeout):
        captured["payload"] = json
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"code": 200})

    monkeypatch.setattr(daily_report, "PUSHPLUS_TOKEN", "token")
    monkeypatch.setattr(daily_report.requests, "post", fake_post)

    daily_report.send_wechat(
        "<b>1. Meal</b> — &lt;script&gt;alert(1)&lt;/script&gt;", "2026-06-25"
    )

    assert "<script>alert(1)</script>" in captured["payload"]["content"]
    assert captured["payload"]["template"] == "txt"


def test_main_exception_records_health_failure(monkeypatch, tmp_path):
    health_path = tmp_path / "service_health.json"
    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", health_path)
    monkeypatch.setattr(daily_report.sys, "argv", ["daily_report.py", "2026-06-27"])

    def boom(target_date):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(daily_report, "generate_report", boom)

    alerts = []
    monkeypatch.setattr(daily_report, "BOT_TOKEN", "token")
    monkeypatch.setattr(daily_report, "CHAT_ID", "12345")
    monkeypatch.setattr(
        daily_report,
        "_post_telegram_message",
        lambda text, parse_mode=None: alerts.append(text) or True,
    )

    assert daily_report.main() == 1

    health = json.loads(health_path.read_text())
    assert health["daily_report"]["last_ok"] is False
    assert health["daily_report"]["last_target_date"] == "2026-06-27"
    assert "RuntimeError: db exploded" in health["daily_report"]["last_error_summary"]
    assert len(alerts) == 1
    assert "2026-06-27" in alerts[0]


def test_main_auto_skips_when_already_sent(monkeypatch, tmp_path):
    health_path = tmp_path / "service_health.json"
    health_path.write_text(
        json.dumps(
            {
                "daily_report": {
                    "last_target_date": "2026-07-04",
                    "last_ok": True,
                    "last_source": "cron",
                }
            }
        )
    )
    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", health_path)
    monkeypatch.setattr(daily_report.sys, "argv", ["daily_report.py"])
    monkeypatch.setattr(daily_report.database, "get_android_timezone", lambda: "+0800")
    monkeypatch.setattr(
        daily_report, "get_local_time", lambda tz_str: datetime(2026, 7, 5, 7, 40)
    )

    generated = []
    monkeypatch.setattr(
        daily_report, "generate_report", lambda target_date: generated.append(target_date) or ""
    )

    assert daily_report.main() == 0
    assert generated == []


def test_main_manual_cli_records_source_manual(monkeypatch, tmp_path):
    health_path = tmp_path / "service_health.json"
    report_path = tmp_path / "report.md"

    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", health_path)
    monkeypatch.setattr(daily_report.sys, "argv", ["daily_report.py", "2026-07-04"])
    monkeypatch.setattr(daily_report, "generate_report", lambda target_date: "<b>Report</b>")
    monkeypatch.setattr(daily_report, "save_report_file", lambda target_date, report: report_path)
    monkeypatch.setattr(daily_report, "send_telegram", lambda report: True)
    monkeypatch.setattr(daily_report, "send_wechat", lambda report, target_date: None)

    assert daily_report.main() == 0

    health = json.loads(health_path.read_text())
    assert health["daily_report"]["last_ok"] is True
    assert health["daily_report"]["last_target_date"] == "2026-07-04"
    # A manual CLI run must not masquerade as the nightly cron run,
    # otherwise resolve_auto_target_date would skip that night's report.
    assert health["daily_report"]["last_source"] == "manual"


def test_get_local_time_rejects_bogus_offset_and_falls_back_to_server_time():
    # '+2500' (25 hours) passes the old hand-rolled parser but is rejected
    # by database.parse_timezone_offset; both clocks must agree it is bogus.
    before = datetime.now()
    result = daily_report.get_local_time("+2500")
    after = datetime.now()

    assert result.tzinfo is None
    assert before - timedelta(seconds=5) <= result <= after + timedelta(seconds=5)


def test_get_local_time_applies_valid_offset():
    expected = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
    result = daily_report.get_local_time("+0800")

    assert result.tzinfo is None
    assert abs(result - expected) < timedelta(seconds=5)


def test_generate_report_survives_hostile_analysis_shapes(monkeypatch):
    """Distilled from the S3 fuzzer: string calories, huge ints, non-dict
    items, scalar food_items, and unhashable dup-key fields crashed the
    report before the safe_number/safe_food_items armor."""
    hostile = [
        _meal(calories="640"),                      # str into += (TypeError)
        _meal(protein=10 ** 400),                   # OverflowError in += 
        _meal(items=["rice 200 kcal", {"name": "ok", "estimated_calories": 200}]),
        {"time": [1, 2], "corrected": [], "image_hash": "",  # unhashable dup key
         "analysis": {"is_food": True, "food_items": -1}},
        _meal(calories=json.loads("1e400")),        # inf
    ]
    _patch_meals(monkeypatch, hostile)

    report = daily_report.generate_report("2026-07-09")

    assert "Daily Calorie Report" in report
    assert "Daily Summary" in report  # reached the totals section
