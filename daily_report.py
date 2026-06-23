#!/usr/bin/env python3
"""
CalorieTracker Daily Report

Generates a detailed daily calorie & macro report and sends it to Telegram.
Designed to be triggered by launchd at 11:30 PM daily.

Usage:
    python3 daily_report.py              # Report for today
    python3 daily_report.py 2026-03-28   # Report for a specific date
"""

import json
import os
import shutil
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import requests

# Add the project directory to path
sys.path.insert(0, str(Path(__file__).parent))
import database
from config import PUSHPLUS_TOKEN, PUSHPLUS_TOPIC, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# ─── Config ────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
CHAT_ID = TELEGRAM_CHAT_ID
REPORTS_DIR = Path.home() / "CalorieTracker" / "reports"


def send_wechat(text, target_date):
    """Send a markdown message to WeChat via PushPlus."""
    if not PUSHPLUS_TOKEN:
        print("[INFO] PUSHPLUS_TOKEN not set. Skipping WeChat notification.")
        return
    
    url = "http://www.pushplus.plus/send"
    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": f"📊 Daily Calorie Report ({target_date})",
        "content": text,
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
        
    meals = database.get_meals(int(CHAT_ID), target_date, target_date)
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
        desc = a.get("meal_description", "Unknown")
        cal = a.get("total_calories") or 0
        p = a.get("total_protein_g") or 0
        c = a.get("total_carbs_g") or 0
        f = a.get("total_fat_g") or 0
        time_str = meal.get("time", "??:??")
        corrected = " ✏️" if meal.get("corrected") else ""

        grand_cal += cal
        grand_p += p
        grand_c += c
        grand_f += f

        lines.append(f"<b>{i}. {desc}</b> — {time_str}{corrected}")

        # Per-item breakdown
        for item in a.get("food_items", []):
            item_name = item.get("name", "?")
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
    # Strip markdown formatting for the file version
    filepath.write_text(report)
    return filepath


def send_telegram(text):
    """Send a message to Telegram."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        result = resp.json()
        if result.get("ok"):
            print(f"✅ Report sent to Telegram")
        else:
            # Retry without markdown
            print(f"⚠️ Markdown failed, retrying plain text...")
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text},
                timeout=10,
            )
            print(f"✅ Report sent (plain text)")
    except Exception as e:
        print(f"❌ Failed to send: {e}")



def get_local_time(tz_str: str) -> datetime:
    """Convert current UTC time to user's local time based on +0800 string."""
    try:
        sign = 1 if tz_str.startswith('+') else -1
        hours = int(tz_str[1:3])
        minutes = int(tz_str[3:5])
        offset = timedelta(hours=hours, minutes=minutes) * sign
        return datetime.now(timezone.utc) + offset
    except:
        return datetime.now() # Fallback to server local time

def main():
    # Check timezone if running automatically (no args)
    is_auto = len(sys.argv) == 1
    
    if is_auto:
        tz_str = database.get_android_timezone()
        local_time = get_local_time(tz_str)
        
        # We only send the report if the local hour is 23 (11:00 PM)
        if local_time.hour != 23:
            print(f"[SKIP] User local time is {local_time.strftime('%Y-%m-%d %H:%M')}. Not 11 PM.")
            return
            
        target_date = local_time.strftime('%Y-%m-%d')
    else:
        target_date = sys.argv[1]

    print(f"📊 Generating daily report for {target_date}...")

    # Generate report
    report = generate_report(target_date)

    # Save to file
    filepath = save_report_file(target_date, report)
    print(f"💾 Saved to {filepath}")

    print("Sending reports...")
    
    # 1. Send to Telegram
    send_telegram(report)
    
    # 2. Send to WeChat via PushPlus
    send_wechat(report, target_date)
    
    print("Daily reporting process complete.")


if __name__ == "__main__":
    main()
