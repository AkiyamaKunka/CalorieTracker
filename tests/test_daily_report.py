from types import SimpleNamespace
import json

import daily_report


def test_clean_config_value_strips_shell_quotes():
    assert daily_report._clean_config_value('"1234567890"') == "1234567890"
    assert daily_report._clean_config_value("'token'") == "token"


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
