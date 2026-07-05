#!/usr/bin/env python3
"""
CalorieTracker Daily Report

Generates a detailed daily calorie & macro report and sends it to Telegram.
Designed to be triggered by launchd at 11:30 PM daily.

Usage:
    python3 daily_report.py              # Report for today
    python3 daily_report.py 2026-03-28   # Report for a specific date
"""

import re
import sys
import traceback
from datetime import datetime, timezone, timedelta
from html import escape, unescape
from pathlib import Path
from typing import Optional

import requests

# Add the project directory to path
sys.path.insert(0, str(Path(__file__).parent))
import database
import service_health
from config import (
    PUSHPLUS_TOKEN,
    PUSHPLUS_TOPIC,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    REPORTS_DIR,
)
from utils import telegram_message_chunks as _telegram_chunks

# ─── Config ────────────────────────────────────────────────────────
BOT_TOKEN = TELEGRAM_BOT_TOKEN or ""
CHAT_ID = TELEGRAM_CHAT_ID or ""
SERVICE_HEALTH_PATH = service_health.DEFAULT_PATH


def _warn(message):
    print(f"[WARN] {message}")


def _load_service_health():
    return service_health.load(SERVICE_HEALTH_PATH, warn=_warn)


def _save_service_health(data):
    service_health.save(data, SERVICE_HEALTH_PATH, warn=_warn)


def _record_daily_report_health(ok: bool, target_date: str, source: str = "cron", report_path: str = "", error_summary: str = ""):
    data = _load_service_health()
    service_health.apply_report_health(
        data, ok, target_date,
        source=source, report_path=report_path, error_summary=error_summary,
    )
    _save_service_health(data)


_TELEGRAM_HTML_TAG_RE = re.compile(
    r"</?(?:b|i|u|s|code|pre|a)(?:\s[^>]*)?>", re.IGNORECASE
)


def _html_to_plain(text: str) -> str:
    """Convert Telegram-HTML report text to plain text for files and WeChat."""
    return unescape(_TELEGRAM_HTML_TAG_RE.sub("", text))


def send_wechat(text, target_date):
    """Send a plain-text version of the report to WeChat via PushPlus."""
    if not PUSHPLUS_TOKEN:
        print("[INFO] PUSHPLUS_TOKEN not set. Skipping WeChat notification.")
        return

    url = "https://www.pushplus.plus/send"
    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": f"📊 Daily Calorie Report ({target_date})",
        "content": _html_to_plain(text),
        "template": "markdown",
        "topic": PUSHPLUS_TOPIC,
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        if result.get("code") == 200:
            print("[INFO] WeChat report sent via PushPlus successfully!")
        else:
            print(f"[ERROR] PushPlus API error: {result}")
    except Exception as e:
        print(f"[ERROR] Failed to send WeChat message: {e}")


def generate_report(target_date: str) -> str:
    """Generate a detailed daily report."""
    if not CHAT_ID:
        print("[ERROR] TELEGRAM_CHAT_ID is not set in config.")
        return ""

    try:
        chat_id = int(CHAT_ID)
    except ValueError:
        print(f"[ERROR] TELEGRAM_CHAT_ID is not a valid integer: {CHAT_ID!r}")
        return ""

    meals = database.get_meals(chat_id, target_date, target_date)
    food_meals = [m for m in meals if m.get("analysis", {}).get("is_food")]

    # Header
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    day_name = dt.strftime("%A, %B %d, %Y")
    lines = [
        f"📊 <b>Daily Calorie Report</b>",
        f"📅 {day_name}",
        "",
    ]

    if not food_meals:
        lines.append("No meals logged today. 🍽️")
        lines.append("")
        lines.append("<i>Send food photos to @junjia_calorie_bot to start tracking!</i>")
        return "\n".join(lines)

    # ─── Per-meal breakdown ────────────────────────────────────
    lines.append("<b>🍽️ Meals:</b>\n")

    grand_cal = 0
    grand_p = 0
    grand_c = 0
    grand_f = 0

    for i, meal in enumerate(food_meals, 1):
        a = meal["analysis"]
        desc = escape(str(a.get("meal_description", "Unknown")))
        cal = a.get("total_calories") or 0
        p = a.get("total_protein_g") or 0
        c = a.get("total_carbs_g") or 0
        f = a.get("total_fat_g") or 0
        time_str = escape(str(meal.get("time", "??:??")))
        corrected = " ✏️" if meal.get("corrected") else ""

        grand_cal += cal
        grand_p += p
        grand_c += c
        grand_f += f

        lines.append(f"<b>{i}. {desc}</b> — {time_str}{corrected}")

        # Per-item breakdown
        for item in a.get("food_items", []):
            item_name = escape(str(item.get("name", "?")))
            item_cal = item.get("estimated_calories") or 0
            item_p = item.get("protein_g") or 0
            item_c = item.get("carbs_g") or 0
            item_f = item.get("fat_g") or 0
            lines.append(f"  • {item_name}: {item_cal} kcal")
            lines.append(f"    P:{item_p}g | C:{item_c}g | F:{item_f}g")

        lines.append(f"  📊 Subtotal: ~{cal} kcal | P:{p}g C:{c}g F:{f}g")
        lines.append("")

    # ─── Daily totals ──────────────────────────────────────────
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"<b>📊 Daily Summary</b>\n")
    lines.append(f"🔥 <b>Total Calories: ~{grand_cal:,} kcal</b>")
    lines.append(f"🥩 <b>Protein:</b> {grand_p}g")
    lines.append(f"🍞 <b>Carbs:</b> {grand_c}g")
    lines.append(f"🧈 <b>Fat:</b> {grand_f}g")
    lines.append(f"📸 <b>Meals logged:</b> {len(food_meals)}")

    # ─── Macro percentages ─────────────────────────────────────
    total_macro_cal = (grand_p * 4) + (grand_c * 4) + (grand_f * 9)
    if total_macro_cal > 0:
        p_pct = round((grand_p * 4) / total_macro_cal * 100)
        c_pct = round((grand_c * 4) / total_macro_cal * 100)
        f_pct = round((grand_f * 9) / total_macro_cal * 100)
        lines.append("")
        lines.append(f"<b>Macro Split:</b>")
        lines.append(f"  🥩 Protein: {p_pct}%")
        lines.append(f"  🍞 Carbs: {c_pct}%")
        lines.append(f"  🧈 Fat: {f_pct}%")

    return "\n".join(lines)


def save_report_file(target_date: str, report: str):
    """Save report as a markdown file."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = REPORTS_DIR / f"report_{target_date}.md"
    filepath.write_text(_html_to_plain(report))
    return filepath


def _post_telegram_message(text, parse_mode=None):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json=payload,
        timeout=15,
    )

    try:
        result = resp.json()
    except ValueError:
        result = {"ok": False, "description": resp.text[:300]}

    if result.get("ok"):
        return True

    print(f"[ERROR] Telegram API error ({resp.status_code}): {result}")
    return False


def send_telegram(text):
    """Send a report to Telegram and return whether every chunk succeeded."""
    if not BOT_TOKEN:
        print("[ERROR] TELEGRAM_BOT_TOKEN is not set.")
        return False
    if not CHAT_ID:
        print("[ERROR] TELEGRAM_CHAT_ID is not set.")
        return False

    all_sent = True
    try:
        for chunk in _telegram_chunks(text):
            if _post_telegram_message(chunk, parse_mode="HTML"):
                continue

            print("⚠️ HTML send failed, retrying plain text...")
            if not _post_telegram_message(chunk):
                all_sent = False

        if all_sent:
            print("✅ Report sent to Telegram")
        else:
            print("❌ One or more Telegram report chunks failed.")
    except Exception as e:
        print(f"❌ Failed to send: {e}")
        return False

    return all_sent



def get_local_time(tz_str: str) -> datetime:
    """Convert current UTC time to user's local time based on +0800 string.

    Delegates offset parsing to database.parse_timezone_offset so the report
    clock and the meal-date clock can never disagree on what counts as a
    valid offset (e.g. '+2500' is rejected by both, not just one).
    """
    offset = database.parse_timezone_offset(tz_str)
    if offset is None:
        return datetime.now()  # Fallback to server local time
    return datetime.now(timezone.utc).replace(tzinfo=None) + offset

def _auto_candidate_date(local_time: datetime) -> str:
    """The date an auto run should report on: today at 23:xx, else yesterday."""
    if local_time.hour >= 23:
        return local_time.strftime("%Y-%m-%d")
    return (local_time - timedelta(days=1)).strftime("%Y-%m-%d")


def resolve_auto_target_date(local_time: datetime, health_data: dict) -> Optional[str]:
    """Pick the report date for an auto run, or None if it was already sent.

    Runs before 23:00 act as catch-up for the previous day (e.g. a launchd
    job fired late because the Mac was asleep at 23:30).
    """
    target = _auto_candidate_date(local_time)
    report = (health_data or {}).get("daily_report") or {}
    # Only a successful CRON run for this date counts as "already sent".
    # A manual /report or CLI run writes the same ledger record but must
    # not suppress that night's automatic report.
    if (
        report.get("last_target_date") == target
        and report.get("last_ok")
        and report.get("last_source") == "cron"
    ):
        return None
    return target


def main():
    is_auto = len(sys.argv) == 1
    source = "cron" if is_auto else "manual"

    if is_auto:
        local_time = get_local_time(database.get_android_timezone())
        target_date = resolve_auto_target_date(local_time, _load_service_health())
        if target_date is None:
            print(f"[SKIP] report for {_auto_candidate_date(local_time)} already sent")
            return 0
    else:
        target_date = sys.argv[1]

    print(f"📊 Generating daily report for {target_date}...")

    try:
        # Generate report
        report = generate_report(target_date)
        if not report:
            print("[ERROR] Report generation returned empty content. Aborting send.")
            _record_daily_report_health(False, target_date, source=source, error_summary="Report generation returned empty content.")
            return 1

        # Save to file
        filepath = save_report_file(target_date, report)
        print(f"💾 Saved to {filepath}")

        print("Sending reports...")

        # 1. Send to Telegram
        telegram_ok = send_telegram(report)
        _record_daily_report_health(
            telegram_ok,
            target_date,
            source=source,
            report_path=str(filepath),
            error_summary="" if telegram_ok else "Telegram send failed.",
        )

        # 2. Send to WeChat via PushPlus
        send_wechat(report, target_date)
    except Exception as e:
        traceback.print_exc()
        _record_daily_report_health(False, target_date, source=source, error_summary=f"{type(e).__name__}: {e}")
        if BOT_TOKEN and CHAT_ID:
            try:
                _post_telegram_message(
                    f"⚠️ Daily report for {target_date} failed: {type(e).__name__}: {e}"
                )
            except Exception:
                pass
        return 1

    print("Daily reporting process complete.")
    return 0 if telegram_ok else 1


if __name__ == "__main__":
    sys.exit(main())
