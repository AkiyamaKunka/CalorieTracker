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


# ─── Fitness sections ──────────────────────────────────────────────

def _no_fitness_data(monkeypatch):
    """Force every fitness accessor to report an empty footprint."""
    monkeypatch.setattr(daily_report.database, "get_fitness_profile", lambda cid: None)
    monkeypatch.setattr(daily_report.database, "get_latest_body_weight", lambda cid: None)
    monkeypatch.setattr(daily_report.database, "get_activities", lambda *a, **k: [])
    monkeypatch.setattr(daily_report.database, "get_workouts", lambda *a, **k: [])
    monkeypatch.setattr(daily_report.database, "get_body_weights", lambda *a, **k: [])


def test_report_byte_identical_without_fitness_data(monkeypatch):
    # A user with no profile/weigh-in/activity/workout must get the exact
    # pre-fitness report: the whole fitness block collapses to nothing.
    items = [{"name": "Fish", "estimated_calories": 300}, {"name": "Rice", "estimated_calories": 200}]
    _patch_meals(monkeypatch, [_meal(calories=500, items=items)])
    _no_fitness_data(monkeypatch)

    with_guard = daily_report.generate_report("2026-06-25")

    # Baseline: same inputs but the fitness helper forced to contribute nothing.
    monkeypatch.setattr(daily_report, "_fitness_sections", lambda *a, **k: [])
    baseline = daily_report.generate_report("2026-06-25")

    assert with_guard == baseline
    for marker in ("Macro check", "Energy Balance", "Today's Training", "kg/wk"):
        assert marker not in with_guard


def test_report_byte_identical_without_fitness_data_no_food(monkeypatch):
    # The no-meals early-return branch must also stay byte-identical.
    _patch_meals(monkeypatch, [])
    _no_fitness_data(monkeypatch)

    with_guard = daily_report.generate_report("2026-06-25")

    monkeypatch.setattr(daily_report, "_fitness_sections", lambda *a, **k: [])
    baseline = daily_report.generate_report("2026-06-25")

    assert with_guard == baseline
    assert "No meals logged today" in with_guard
    assert "Today's Training" not in with_guard


def test_diet_targets_section_appears_when_diet_mode_set(monkeypatch):
    _patch_meals(monkeypatch, [_meal(calories=500, protein=30, carbs=40, fat=15)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(daily_report.database, "get_fitness_profile", lambda cid: {"diet_mode": "keto"})
    monkeypatch.setattr(
        daily_report.database, "get_latest_body_weight",
        lambda cid: {"weight_kg": 80.0, "date": "2026-06-25"},
    )

    report = daily_report.generate_report("2026-06-25")

    assert "🥗 Macro check — Keto" in report
    assert "Informational only — not medical advice." in report


def test_energy_balance_section_appears_with_activity(monkeypatch):
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_activities",
        lambda *a, **k: [{"active_calories": 450, "distance_km": 8.0, "raw": {"steps": 9000}}],
    )

    report = daily_report.generate_report("2026-06-25")

    assert "⚡ Energy Balance" in report
    assert "Active burn: ~450 kcal" in report
    assert "Steps: 9,000" in report
    assert "Distance: 8.0 km" in report
    # Net defaults to consumed − ACTIVE burn: 500 − 450 = 50.
    assert "Net: ~50 kcal (consumed − active burn)" in report


def test_energy_balance_net_uses_total_when_env_set(monkeypatch):
    # The row is built via the REAL producer (garmin.to_activity_kwargs) so
    # this exercises the exact persisted shape a Garmin pull writes.
    import garmin

    monkeypatch.setenv("GARMIN_NET_USE_TOTAL", "1")
    _patch_meals(monkeypatch, [_meal(calories=2000)])
    _no_fitness_data(monkeypatch)
    row = garmin.to_activity_kwargs(
        garmin.DailyActivity(active_calories=450, total_calories=2600, activities=[])
    )
    monkeypatch.setattr(
        daily_report.database, "get_activities", lambda *a, **k: [row]
    )

    report = daily_report.generate_report("2026-06-25")

    # With total basis: 2000 − 2600 = -600, and the basis word is "total".
    assert "Net: ~-600 kcal (consumed − total burn)" in report


def test_energy_balance_net_uses_total_from_hand_crafted_raw(monkeypatch):
    # Hand-crafted variant kept on purpose: documents that the reader
    # tolerates a bare row whose raw blob carries only total_calories.
    monkeypatch.setenv("GARMIN_NET_USE_TOTAL", "1")
    _patch_meals(monkeypatch, [_meal(calories=2000)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_activities",
        lambda *a, **k: [{"active_calories": 450, "raw": {"total_calories": 2600}}],
    )

    report = daily_report.generate_report("2026-06-25")

    assert "Net: ~-600 kcal (consumed − total burn)" in report


def test_energy_balance_falls_back_to_active_when_total_absent(monkeypatch):
    import garmin

    monkeypatch.setenv("GARMIN_NET_USE_TOTAL", "1")
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)
    # Env asks for total, but Garmin reported none: the real producer then
    # persists no total_calories and the active basis is used.
    row = garmin.to_activity_kwargs(
        garmin.DailyActivity(active_calories=450, activities=[])
    )
    monkeypatch.setattr(
        daily_report.database, "get_activities", lambda *a, **k: [row]
    )

    report = daily_report.generate_report("2026-06-25")

    assert "Net: ~50 kcal (consumed − active burn)" in report


def test_energy_balance_distance_only_day_renders_distance_without_net(monkeypatch):
    # Critic #9 (report side): "ran 9.2 km" logged without a kcal figure
    # persists active_calories=NULL (the exact shape /activity's distance-only
    # form writes); the old active-burn-only gate dropped the whole section,
    # so the run never appeared in the nightly report. New intended behavior:
    # the section renders with the distance line, but the burn and Net lines
    # still require a real active burn — no net math off a burn never logged.
    monkeypatch.delenv("GARMIN_NET_USE_TOTAL", raising=False)
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_activities",
        lambda *a, **k: [{"active_calories": None, "distance_km": 9.2, "raw": None}],
    )

    report = daily_report.generate_report("2026-06-25")

    assert "⚡ Energy Balance" in report
    assert "📏 Distance: 9.2 km" in report
    assert "🔥 Active burn" not in report
    assert "⚖️ Net" not in report


def test_energy_balance_steps_only_day_renders_steps_without_net(monkeypatch):
    # Same gate, steps flavor: steps ride inside the raw blob (as /activity
    # persists them) and must surface even when no kcal figure exists.
    monkeypatch.delenv("GARMIN_NET_USE_TOTAL", raising=False)
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_activities",
        lambda *a, **k: [{"active_calories": None, "distance_km": None,
                          "raw": {"steps": 12000}}],
    )

    report = daily_report.generate_report("2026-06-25")

    assert "⚡ Energy Balance" in report
    assert "👟 Steps: 12,000" in report
    assert "🔥 Active burn" not in report
    assert "⚖️ Net" not in report


def test_energy_balance_full_day_block_pinned_exactly(monkeypatch):
    # Pins the EXACT full-day block (burn + steps + distance + net, in that
    # order) so the distance/steps-only gate change provably did not drift
    # the common burn-present rendering by a single byte.
    monkeypatch.delenv("GARMIN_NET_USE_TOTAL", raising=False)
    _patch_meals(monkeypatch, [_meal(calories=1850)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_activities",
        lambda *a, **k: [{"active_calories": 640, "distance_km": 9.2, "raw": {"steps": 12000}}],
    )

    report = daily_report.generate_report("2026-07-14")

    assert (
        "<b>⚡ Energy Balance</b>\n"
        "🔥 Active burn: ~640 kcal\n"
        "👟 Steps: 12,000\n"
        "📏 Distance: 9.2 km\n"
        "⚖️ Net: ~1,210 kcal (consumed − active burn)"
    ) in report


def test_weight_section_appears_with_body_weight(monkeypatch):
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_latest_body_weight",
        lambda cid: {"weight_kg": 79.3, "date": "2026-06-25"},
    )
    monkeypatch.setattr(
        daily_report.database, "get_body_weights",
        lambda *a, **k: [
            {"date": "2026-06-18", "weight_kg": 80.0},
            {"date": "2026-06-25", "weight_kg": 79.3},
        ],
    )

    report = daily_report.generate_report("2026-06-25")

    assert "⚖️ Weight" in report
    assert "Latest: <b>79.3 kg</b>" in report
    # 0.7 kg drop over 7 days -> -0.70 kg/wk, trending down.
    assert "7-day trend: ⬇️ -0.70 kg/wk" in report


def test_training_section_always_renders_for_fitness_user(monkeypatch):
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)
    # A bare weigh-in is enough to make this a fitness user; training still
    # renders off default_profile even without a running profile.
    monkeypatch.setattr(
        daily_report.database, "get_latest_body_weight",
        lambda cid: {"weight_kg": 79.3, "date": "2026-06-25"},
    )

    report = daily_report.generate_report("2026-06-25")

    assert "🏃 Today's Training" in report


def test_training_section_shows_weeks_to_race(monkeypatch):
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_fitness_profile",
        lambda cid: {"goal_race_date": "2026-08-11", "race_label": "Marathon <PB>"},
    )

    report = daily_report.generate_report("2026-07-14")

    # 2026-07-14 -> 2026-08-11 is 28 days == 4.0 weeks; label is HTML-escaped.
    assert "🎯 4.0 weeks to Marathon &lt;PB&gt;" in report


def test_generate_report_stays_offline(monkeypatch):
    import garmin

    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)

    def explode(*a, **k):
        raise AssertionError("generate_report must not touch the network")

    monkeypatch.setattr(daily_report.requests, "post", explode)
    pulls = []
    monkeypatch.setattr(garmin, "fetch_daily_activity", lambda d: pulls.append(d))

    report = daily_report.generate_report("2026-06-25")

    assert "Daily Calorie Report" in report
    assert pulls == []  # no Garmin network pull happened inside generate_report


def test_fitness_sections_never_raise_on_hostile_rows(monkeypatch):
    monkeypatch.delenv("GARMIN_NET_USE_TOTAL", raising=False)
    monkeypatch.setattr(daily_report.database, "get_fitness_profile", lambda cid: {"diet_mode": "keto", "goal_race_date": ["nope"]})
    monkeypatch.setattr(daily_report.database, "get_latest_body_weight", lambda cid: {"weight_kg": "heavy"})
    monkeypatch.setattr(
        daily_report.database, "get_activities",
        lambda *a, **k: ["junk", {"active_calories": "lots", "distance_km": None}, {"active_calories": 300}],
    )
    monkeypatch.setattr(daily_report.database, "get_workouts", lambda *a, **k: [])
    monkeypatch.setattr(
        daily_report.database, "get_body_weights",
        lambda *a, **k: [{"date": "bad", "weight_kg": None}, {"date": "2026-06-25", "weight_kg": 80.0}],
    )
    monkeypatch.setattr(daily_report.database, "get_meals", lambda *a, **k: -1)

    # Must not raise despite hostile shapes throughout.
    lines = daily_report._fitness_sections(12345, "2026-06-25", grand_cal="not-a-number")

    assert isinstance(lines, list)
    # Hostile siblings must degrade, not destroy: the one valid 300-kcal
    # activity row still yields its Energy Balance content, and the one
    # parseable weigh-in still renders.
    joined = "\n".join(lines)
    assert "<b>⚡ Energy Balance</b>" in joined
    assert "🔥 Active burn: ~300 kcal" in joined
    assert "⚖️ Net: ~-300 kcal (consumed − active burn)" in joined
    assert "Latest: <b>80.0 kg</b>" in joined


# ─── Garmin main() network hook ────────────────────────────────────

def _stub_main_send(monkeypatch, health_path, report_path):
    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", health_path)
    monkeypatch.setattr(daily_report.sys, "argv", ["daily_report.py", "2026-07-14"])
    monkeypatch.setattr(daily_report, "generate_report", lambda target_date: "<b>Report</b>")
    monkeypatch.setattr(daily_report, "save_report_file", lambda target_date, report: report_path)
    monkeypatch.setattr(daily_report, "send_telegram", lambda report: True)
    monkeypatch.setattr(daily_report, "send_wechat", lambda report, target_date: None)


def test_main_garmin_hook_skipped_when_not_configured(monkeypatch, tmp_path):
    import garmin

    _stub_main_send(monkeypatch, tmp_path / "health.json", tmp_path / "report.md")
    monkeypatch.setattr(garmin, "is_configured", lambda: False)
    pulls = []
    monkeypatch.setattr(garmin, "fetch_daily_activity", lambda d: pulls.append(d))

    assert daily_report.main() == 0
    assert pulls == []
    # No activity record is written when the feature is off.
    health = json.loads((tmp_path / "health.json").read_text())
    assert "activity" not in health


def test_main_garmin_hook_syncs_and_records_when_configured(monkeypatch, tmp_path):
    import garmin

    _stub_main_send(monkeypatch, tmp_path / "health.json", tmp_path / "report.md")
    monkeypatch.setattr(daily_report, "CHAT_ID", "12345")
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    monkeypatch.setattr(
        garmin, "fetch_daily_activity",
        lambda d: garmin.DailyActivity(active_calories=450, total_calories=2600,
                                       steps=9000, distance_m=8000),
    )
    saved = []
    monkeypatch.setattr(
        daily_report.database, "save_activity",
        lambda chat_id, date_str, **kw: saved.append((chat_id, date_str, kw)) or 1,
    )

    assert daily_report.main() == 0
    assert len(saved) == 1
    chat_id, date_str, kw = saved[0]
    assert (chat_id, date_str) == (12345, "2026-07-14")
    assert kw["source"] == "garmin"
    assert kw["active_calories"] == 450

    health = json.loads((tmp_path / "health.json").read_text())
    assert health["activity"]["last_ok"] is True
    assert health["activity"]["last_target_date"] == "2026-07-14"
    assert health["activity"]["last_source"] == "garmin"


def test_main_garmin_failure_never_blocks_report(monkeypatch, tmp_path):
    import garmin

    _stub_main_send(monkeypatch, tmp_path / "health.json", tmp_path / "report.md")
    monkeypatch.setattr(daily_report, "CHAT_ID", "12345")
    monkeypatch.setattr(garmin, "is_configured", lambda: True)

    def boom(date_str):
        raise RuntimeError("garmin exploded")

    monkeypatch.setattr(garmin, "fetch_daily_activity", boom)

    # The report still succeeds; the Garmin failure is recorded, not raised.
    assert daily_report.main() == 0
    health = json.loads((tmp_path / "health.json").read_text())
    assert health["activity"]["last_ok"] is False
    assert "garmin exploded" in health["activity"]["last_error_summary"]


def test_record_activity_health_wrapper_writes_file(monkeypatch, tmp_path):
    health_path = tmp_path / "service_health.json"
    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", health_path)

    daily_report._record_activity_health(True, "2026-07-14")

    data = json.loads(health_path.read_text())
    assert data["activity"]["last_ok"] is True
    assert data["activity"]["last_source"] == "garmin"


def test_sync_garmin_resync_of_day_aggregate_does_not_duplicate(monkeypatch, tmp_path):
    # A Garmin day with no discrete activity has no activityId; before the
    # date-derived fallback external_id, its NULL never conflicted in
    # UNIQUE(chat_id, source, external_id), so every re-sync (cron retry,
    # /report) inserted a fresh row and doubled the day's burn.
    import garmin

    monkeypatch.delenv("GARMIN_NET_USE_TOTAL", raising=False)
    monkeypatch.setattr(daily_report.database, "DB_PATH", tmp_path / "resync.db")
    daily_report.database.init_db()
    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(daily_report, "CHAT_ID", "12345")
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    monkeypatch.setattr(
        garmin, "fetch_daily_activity",
        lambda d: garmin.DailyActivity(active_calories=450, activities=[]),
    )

    daily_report._sync_garmin_activity("2026-07-14")
    daily_report._sync_garmin_activity("2026-07-14")  # cron retry / manual /report

    rows = daily_report.database.get_activities(12345, "2026-07-14", "2026-07-14")
    assert len(rows) == 1
    assert rows[0]["external_id"] == "garmin-day-2026-07-14"
    assert rows[0]["active_calories"] == 450

    report = daily_report.generate_report("2026-07-14")
    assert "🔥 Active burn: ~450 kcal" in report
    assert "~900" not in report


# ─── Weight slope arithmetic ───────────────────────────────────────
import re as _re
from html import unescape as _unescape

import pytest


def _seed_weights(monkeypatch, rows):
    """Point both weight accessors at a fixed weigh-in series."""
    monkeypatch.setattr(
        daily_report.database, "get_latest_body_weight",
        lambda cid: rows[-1] if rows else None,
    )
    monkeypatch.setattr(daily_report.database, "get_body_weights", lambda *a, **k: list(rows))


def test_weight_trend_matches_hand_computed_linear_series(monkeypatch):
    # Robert drops exactly 0.1 kg/day for a week; the least-squares fit over
    # all 7 points must print the hand-computed -0.70 kg/wk, not just the
    # endpoint delta.
    series = [
        {"date": f"2026-06-{19 + i}", "weight_kg": round(72.0 - 0.1 * i, 1)}
        for i in range(7)
    ]
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)
    _seed_weights(monkeypatch, series)

    report = daily_report.generate_report("2026-06-25")

    assert "Latest: <b>71.4 kg</b>" in report
    assert "7-day trend: ⬇️ -0.70 kg/wk" in report


def test_weight_section_single_weigh_in_shows_latest_without_trend(monkeypatch):
    # One weigh-in cannot define a slope: the section renders the weight but
    # must not fabricate a kg/wk line.
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)
    _seed_weights(monkeypatch, [{"date": "2026-06-25", "weight_kg": 71.8}])

    report = daily_report.generate_report("2026-06-25")

    assert "⚖️ Weight" in report
    assert "Latest: <b>71.8 kg</b>" in report
    assert "kg/wk" not in report


def test_weight_trend_pins_outlier_arithmetic_at_300kg_boundary(monkeypatch):
    # PINS current behavior: one 300 kg entry (the exact upper bound the
    # /weight parser accepts, i.e. a plausible fat-finger) swings the fitted
    # trend to a physically impossible -57.75 kg/wk. Least squares:
    # mean_y = 730.1/7, slope/day = -231.0/28 = -8.25 -> *7 = -57.75.
    series = [
        {"date": f"2026-06-{19 + i}", "weight_kg": round(72.0 - 0.1 * i, 1)}
        for i in range(7)
    ]
    series[2] = {"date": "2026-06-21", "weight_kg": 300.0}
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)
    _seed_weights(monkeypatch, series)

    report = daily_report.generate_report("2026-06-25")

    assert "7-day trend: ⬇️ -57.75 kg/wk" in report


@pytest.mark.parametrize("rows,expected", [
    ([], None),                                                    # nothing logged
    ([{"date": "2026-06-25", "weight_kg": 71.8}], None),           # one point: no slope
    ([{"date": "2026-06-25", "weight_kg": 72.0},                   # same-day pair:
      {"date": "2026-06-25", "weight_kg": 71.0}], None),           # zero-denominator guard
    ([{"date": "junk", "weight_kg": 72.0},                         # unusable rows leave
      {"date": "2026-06-25", "weight_kg": None},                   # <2 points -> None
      {"date": "2026-06-24", "weight_kg": 71.8}], None),
    ([{"date": "2026-06-24", "weight_kg": -5},                     # non-positive kg skipped
      {"date": "2026-06-25", "weight_kg": 71.8}], None),
    ([{"date": "2026-06-18", "weight_kg": 72.0},                   # 0.7 kg over 7 days
      {"date": "2026-06-25", "weight_kg": 71.3}], -0.7),
])
def test_weekly_weight_slope_contract(rows, expected):
    # The slope helper must degrade to None (not crash, not divide by zero)
    # on every sparse/junk series a real body_weight table can contain.
    assert daily_report._weekly_weight_slope(rows) == expected


# ─── Section combinatorics ─────────────────────────────────────────
_SECTION_MARKERS = {
    "diet": "🥗 Macro check",
    "energy": "⚡ Energy Balance",
    "weight": "⚖️ Weight",
    "training": "🏃 Today's Training",
}


@pytest.mark.parametrize("seed_diet,seed_activity,seed_weight,expected", [
    (False, False, "fresh", {"weight", "training"}),
    (False, True,  None,    {"energy", "training"}),
    (True,  False, None,    {"diet", "training"}),
    # A weigh-in outside the trailing 7-day window still marks a fitness user
    # (training renders) but must not produce an empty Weight section.
    (False, False, "stale", {"training"}),
    (True,  True,  "fresh", {"diet", "energy", "weight", "training"}),
], ids=["weight-only", "activity-only", "diet-only", "stale-weight-only", "all-sections"])
def test_fitness_section_combinatorics(monkeypatch, seed_diet, seed_activity, seed_weight, expected):
    # Exactly the sections whose data exists may render; a partial fitness
    # footprint must not drag in empty sibling sections.
    monkeypatch.delenv("GARMIN_NET_USE_TOTAL", raising=False)
    _patch_meals(monkeypatch, [_meal(calories=500)])
    _no_fitness_data(monkeypatch)

    if seed_diet:
        monkeypatch.setattr(
            daily_report.database, "get_fitness_profile", lambda cid: {"diet_mode": "keto"}
        )
    if seed_activity:
        monkeypatch.setattr(
            daily_report.database, "get_activities",
            lambda *a, **k: [{"active_calories": 450, "distance_km": 8.0, "raw": {"steps": 9000}}],
        )
    if seed_weight == "fresh":
        _seed_weights(monkeypatch, [{"date": "2026-06-25", "weight_kg": 79.3}])
    elif seed_weight == "stale":
        # Latest weigh-in exists but is outside the report's 7-day window.
        monkeypatch.setattr(
            daily_report.database, "get_latest_body_weight",
            lambda cid: {"weight_kg": 79.3, "date": "2026-05-01"},
        )
        monkeypatch.setattr(daily_report.database, "get_body_weights", lambda *a, **k: [])

    report = daily_report.generate_report("2026-06-25")

    assert "Daily Summary" in report  # the core report always renders
    for key, marker in _SECTION_MARKERS.items():
        if key in expected:
            assert marker in report, f"expected section {key} missing"
        else:
            assert marker not in report, f"unexpected section {key} rendered"


# ─── Net arithmetic exactness ──────────────────────────────────────

def test_energy_balance_net_thousands_separator_and_multi_row_sum(monkeypatch):
    # Garmin sync + a manual entry on the same day must SUM (640 total burn),
    # and consumed 1850 − active 640 = 1210 renders with a thousands separator.
    monkeypatch.delenv("GARMIN_NET_USE_TOTAL", raising=False)
    _patch_meals(monkeypatch, [
        _meal(description="Breakfast", calories=620, time="08:30"),
        _meal(description="Dinner", calories=1230, time="19:00"),
    ])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_activities",
        lambda *a, **k: [
            {"active_calories": 400, "distance_km": 5.2, "raw": {"steps": 7500}},
            {"active_calories": 240, "distance_km": 4.0, "raw": {"steps": 4500}},
        ],
    )

    report = daily_report.generate_report("2026-06-25")

    assert "🔥 Active burn: ~640 kcal" in report
    assert "👟 Steps: 12,000" in report
    assert "📏 Distance: 9.2 km" in report
    assert "⚖️ Net: ~1,210 kcal (consumed − active burn)" in report


@pytest.mark.parametrize("env_value,basis", [
    ("true", "total"), ("YES", "total"), (" on ", "total"), ("y", "total"),
    ("0", "active"), ("false", "active"), ("off", "active"), ("", "active"),
    ("2", "active"),  # only the documented truthy spellings enable total basis
])
def test_net_basis_env_spellings(monkeypatch, env_value, basis):
    # A half-set env var ("0", "false") must NOT silently flip the net basis:
    # only documented truthy spellings switch to total burn.
    monkeypatch.setenv("GARMIN_NET_USE_TOTAL", env_value)
    _patch_meals(monkeypatch, [_meal(calories=2000)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_activities",
        lambda *a, **k: [{"active_calories": 450, "raw": {"total_calories": 2600}}],
    )

    report = daily_report.generate_report("2026-06-25")

    if basis == "total":
        assert "Net: ~-600 kcal (consumed − total burn)" in report
    else:
        assert "Net: ~1,550 kcal (consumed − active burn)" in report


# ─── WeChat / plain-text mirror ────────────────────────────────────

def test_html_to_plain_full_fitness_report_keeps_numbers_and_drops_tags(monkeypatch):
    # The WeChat mirror must carry every figure of the full fitness report
    # (calories, weight, burn, steps, distance, net, paces) and none of the
    # Telegram HTML markup.
    monkeypatch.delenv("GARMIN_NET_USE_TOTAL", raising=False)
    items = [{"name": "Chicken & rice", "estimated_calories": 1850,
              "protein_g": 125, "carbs_g": 140, "fat_g": 65}]
    _patch_meals(monkeypatch, [_meal(description="Chicken & rice", calories=1850,
                                     protein=125, carbs=140, fat=65, items=items)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_fitness_profile",
        lambda cid: {"diet_mode": "high_protein", "vdot": 50.0,
                     "goal_race_date": "2026-07-28", "race_label": "Marathon",
                     "long_run_day": 6},
    )
    _seed_weights(monkeypatch, [
        {"date": "2026-07-08", "weight_kg": 72.5},
        {"date": "2026-07-14", "weight_kg": 71.8},
    ])
    monkeypatch.setattr(
        daily_report.database, "get_activities",
        lambda *a, **k: [{"active_calories": 640, "distance_km": 9.2, "raw": {"steps": 12000}}],
    )

    report = daily_report.generate_report("2026-07-14")
    for marker in _SECTION_MARKERS.values():
        assert marker in report  # all four sections participate

    plain = daily_report._html_to_plain(report)

    # Independent oracle: strip tags generically and unescape; the number
    # stream (order included) must be identical in the plain mirror.
    num_re = _re.compile(r"\d[\d,.:]*")
    expected_numbers = num_re.findall(_unescape(_re.sub(r"<[^>]+>", "", report)))
    assert num_re.findall(plain) == expected_numbers

    # Spot-check the load-bearing figures a WeChat reader acts on.
    for figure in ("1,850", "71.8", "640", "12,000", "9.2", "1,210", "-0.82 kg/wk"):
        assert figure in plain

    # Every Telegram tag is gone and entities are back to literal text.
    assert not _re.search(r"</?(?:b|i|u|s|code|pre|a)\b", plain)
    assert "&amp;" not in plain and "&lt;" not in plain
    assert "Chicken & rice" in plain


# ─── Report vs /macros target consistency ──────────────────────────

def test_report_macro_targets_honor_stored_profile_overrides(monkeypatch):
    # Robert runs /diet protein 2.2 and /diet target 2400 …; /macros targets
    # 158 g protein and 2,400 kcal, and the nightly report must resolve the
    # SAME stored overrides instead of recomputing from bare mode defaults.
    _patch_meals(monkeypatch, [_meal(calories=500, protein=30, carbs=40, fat=15)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_fitness_profile",
        lambda cid: {"diet_mode": "high_protein", "protein_g_per_kg": 2.2,
                     "target_calories": 2400},
    )
    _seed_weights(monkeypatch, [{"weight_kg": 71.8, "date": "2026-06-25"}])

    report = daily_report.generate_report("2026-06-25")

    assert "/ 158g" in report                          # round(2.2 * 71.8)
    assert "Calories: <b>500</b> / 2,400 kcal" in report


def test_report_macro_targets_overlay_explicit_gram_targets(monkeypatch):
    # Explicit gram targets stored via '/diet target 2400 150 200 70' must
    # win over the mode-derived split in the report, exactly as in /macros.
    _patch_meals(monkeypatch, [_meal(calories=500, protein=30, carbs=40, fat=15)])
    _no_fitness_data(monkeypatch)
    monkeypatch.setattr(
        daily_report.database, "get_fitness_profile",
        lambda cid: {"diet_mode": "high_protein", "target_calories": 2400,
                     "target_protein_g": 150, "target_carbs_g": 200,
                     "target_fat_g": 70},
    )
    _seed_weights(monkeypatch, [{"weight_kg": 71.8, "date": "2026-06-25"}])

    report = daily_report.generate_report("2026-06-25")

    assert "Protein: 30g / 150g" in report             # not weight-derived 144g
    assert "Carbs: 40g / 200g" in report
    assert "Fat: 15g / 70g" in report


# ─── Weight anchor is bounded by the report date ───────────────────

def test_catch_up_report_anchors_weight_on_or_before_target_date(monkeypatch, tmp_path):
    # Robert weighs 72.0 on the 13th and 70.0 on the morning of the 14th; a
    # catch-up report FOR the 13th must show 72.0 and anchor the protein
    # target on 72.0, not on the next morning's weigh-in.
    monkeypatch.setattr(daily_report.database, "DB_PATH", tmp_path / "anchor.db")
    daily_report.database.init_db()
    monkeypatch.setattr(daily_report, "CHAT_ID", "12345")
    daily_report.database.save_fitness_profile(12345, diet_mode="high_protein")
    daily_report.database.save_body_weight(12345, "2026-07-13", 72.0)
    daily_report.database.save_body_weight(12345, "2026-07-14", 70.0)

    report = daily_report.generate_report("2026-07-13")

    assert "Latest: <b>72.0 kg</b>" in report
    # high_protein: 2.0 g/kg × 72.0 kg = 144 g, anchored on the 13th; the
    # 14th's 70.0 kg would have produced 140 g.
    assert "/ 144g" in report
    assert "/ 140g" not in report
    assert "70.0 kg" not in report


def test_report_on_weigh_in_day_unchanged_by_bounded_anchor(monkeypatch, tmp_path):
    # When every weigh-in is on or before the report date the bounded anchor
    # must behave exactly like the old unbounded latest-weight lookup.
    monkeypatch.setattr(daily_report.database, "DB_PATH", tmp_path / "anchor2.db")
    daily_report.database.init_db()
    monkeypatch.setattr(daily_report, "CHAT_ID", "12345")
    daily_report.database.save_fitness_profile(12345, diet_mode="high_protein")
    daily_report.database.save_body_weight(12345, "2026-07-13", 72.0)
    daily_report.database.save_body_weight(12345, "2026-07-14", 70.0)

    report = daily_report.generate_report("2026-07-14")

    assert "Latest: <b>70.0 kg</b>" in report
    assert "/ 140g" in report                          # 2.0 g/kg × 70.0 kg
