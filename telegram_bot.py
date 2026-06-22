#!/usr/bin/env python3
"""
CalorieTracker Telegram Bot

Handles:
- Direct photo analysis (for Android / manual sends)
- AI-powered meal corrections via natural language
- Commands: /meals, /today, /history, /help

Photo scanning from macOS Photos library is done separately
by photo_scanner.py.

Usage:
    export TELEGRAM_BOT_TOKEN='your-token'
    export GEMINI_API_KEY='your-key'
    python3 telegram_bot.py
"""

import io
import json
import hashlib
import logging
import os
import shutil
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests
from google import genai
from PIL import Image

import database
from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    TELEGRAM_BOT_TOKEN,
    TEXT_HANDLER_PROMPT,
    FOOD_DETECTION_PROMPT,
    DUPLICATE_WINDOW_MINUTES,
)

# ─── Constants ─────────────────────────────────────────────────────
ALLOWED_CHAT_ID = 8675416366

BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', TELEGRAM_BOT_TOKEN or 'REDACTED-TELEGRAM-TOKEN-REVOKED')

HELP_TEXT = """🍽️ <b>CalorieTracker Bot</b>

I track your food automatically from your Photos library (iPhone/iCloud).
You can also send food photos directly here (Android)!

<b>Commands:</b>
/meals — View today's meals (numbered list)
/today — View today's calorie & macro summary
/history — View your calorie totals for the last 7 days
/help — Show this help message

<b>Photo:</b>
📸 Send a food photo for instant calorie analysis

<b>Corrections:</b>
Just type a correction in natural language! I remember your meals from the last 3 days.
• "change yesterday's lunch to 400 kcal"
• "the lunch today was pad thai not fried rice"
• "I didn't eat the rice on Tuesday"
"""

# ─── Logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("calorie_bot")


# ─── Telegram Bot API ─────────────────────────────────────────────
class TelegramBot:
    """Simple Telegram Bot using HTTP polling (no library needed)."""

    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.offset = 0  # Track last processed update

    def _call(self, method: str, **kwargs) -> dict:
        """Make a Telegram Bot API call."""
        resp = requests.post(f"{self.base_url}/{method}", json=kwargs, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data.get("result", {})

    def get_updates(self, timeout: int = 30) -> list:
        """Long-poll for new messages."""
        try:
            result = self._call(
                "getUpdates", offset=self.offset, timeout=timeout
            )
            if result:
                self.offset = result[-1]["update_id"] + 1
            return result
        except requests.exceptions.Timeout:
            return []
        except Exception as e:
            log.error(f"Error getting updates: {e}")
            time.sleep(5)
            return []

    def send_message(self, chat_id: int, text: str, parse_mode: str = "HTML"):
        """Send a text message."""
        # Truncate if too long for Telegram (4096 char limit)
        if len(text) > 4000:
            text = text[:4000] + "\n\n<i>(truncated)</i>"
        try:
            self._call(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as e:
            log.error(f"Failed to send message: {e}")
            # Retry without parse_mode in case of formatting issues
            try:
                self._call("sendMessage", chat_id=chat_id, text=text)
            except Exception:
                pass

    def get_file(self, file_id: str) -> bytes:
        """Download a file from Telegram."""
        file_info = self._call("getFile", file_id=file_id)
        file_path = file_info.get("file_path")
        if not file_path:
            raise RuntimeError("Could not get file_path from telegram.")
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content


# ─── Meals Data Access ─────────────────────────────────────────────
def get_todays_meals(chat_id: int) -> List[Dict]:
    """Get all food meals logged today."""
    today_str = date.today().isoformat()
    meals = database.get_meals(chat_id, today_str, today_str)
    return [m for m in meals if m.get("analysis", {}).get("is_food")]


def get_recent_meals(chat_id: int, days: int = 3) -> List[Dict]:
    """Get all food meals logged in the past N days."""
    meals = database.get_recent_meals(chat_id, days)
    return [m for m in meals if m.get("analysis", {}).get("is_food")]


def update_meal_by_index(chat_id: int, meal_index: int, new_analysis: Dict) -> bool:
    """Update a specific meal in the log by its index within the recent food meals."""
    meals = get_recent_meals(chat_id, days=3)

    if meal_index < 0 or meal_index >= len(meals):
        log.warning(f"Invalid meal_index {meal_index} (recent has {len(meals)} meals)")
        return False

    meal_id = meals[meal_index]["id"]
    database.update_meal_analysis(meal_id, chat_id, new_analysis)
    log.info(f"Updated meal at index {meal_index} (DB ID {meal_id})")
    return True


# ─── Photo Analysis ────────────────────────────────────────────────
def analyze_food_photo(client: genai.Client, image_bytes: bytes) -> Optional[Dict]:
    """Analyze a food photo with Gemini."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[FOOD_DETECTION_PROMPT, img],
        )
        content = response.text.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            content = "\n".join(lines)
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.warning(f"Could not parse API response: {e}")
        return None
    except Exception as e:
        log.error(f"Error analyzing photo: {e}")
        return None


def save_meal(chat_id: int, analysis: Dict, source: str, file_id: str = "", image_hash: str = ""):
    """Save a meal analysis to the log (from direct Telegram photo)."""
    date_str = date.today().isoformat()
    time_str = datetime.now().strftime("%I:%M %p")
    timestamp_str = datetime.now().isoformat()
    database.save_meal(chat_id, date_str, time_str, timestamp_str, source, image_hash, file_id, analysis)


def is_duplicate_photo(chat_id: int, image_hash: str) -> bool:
    """Check if a photo with the same hash was logged recently."""
    if not image_hash:
        return False
    meals = get_todays_meals(chat_id)
    now = datetime.now()
    for meal in reversed(meals):
        if meal.get("image_hash") == image_hash:
            ts = meal.get("timestamp", "")
            if ts:
                try:
                    meal_time = datetime.fromisoformat(ts)
                    if (now - meal_time).total_seconds() < DUPLICATE_WINDOW_MINUTES * 60:
                        return True
                except ValueError:
                    pass
            else:
                return True
    return False


def format_food_result(chat_id: int, analysis: Dict) -> str:
    """Format a food analysis result for Telegram (HTML)."""
    if not analysis.get("is_food"):
        return "🚫 No food detected in this photo."

    lines = []
    desc = analysis.get("meal_description", "Unknown meal")
    lines.append(f"🍽️ <b>{desc}</b>\n")

    for item in analysis.get("food_items", []):
        name = item.get("name", "?")
        cals = item.get("estimated_calories", "?")
        p = item.get("protein_g") or 0
        c = item.get("carbs_g") or 0
        f = item.get("fat_g") or 0
        lines.append(f"  • {name}: ~{cals} kcal")
        lines.append(f"    P:{p}g | C:{c}g | F:{f}g")

    total = analysis.get("total_calories") or "?"
    tp = analysis.get("total_protein_g") or 0
    tc = analysis.get("total_carbs_g") or 0
    tf = analysis.get("total_fat_g") or 0
    lines.append(f"\n📊 <b>This meal: ~{total} kcal</b>")
    lines.append(f"🥩 P: {tp}g | 🍞 C: {tc}g | 🧈 F: {tf}g")

    # Daily totals
    daily = format_daily_totals(chat_id)
    if daily:
        lines.append(f"\n━━━━━━━━━━━━━━━━━━")
        lines.append(daily)

    return "\n".join(lines)


def format_daily_totals(chat_id: int) -> str:
    """Calculate and format today's running totals."""
    meals = get_todays_meals(chat_id)
    if not meals:
        return ""
    total_cal = sum((m["analysis"].get("total_calories") or 0) for m in meals)
    total_p = sum((m["analysis"].get("total_protein_g") or 0) for m in meals)
    total_c = sum((m["analysis"].get("total_carbs_g") or 0) for m in meals)
    total_f = sum((m["analysis"].get("total_fat_g") or 0) for m in meals)
    lines = [
        f"📋 <b>Today's Total ({len(meals)} meals):</b>",
        f"🔥 {total_cal:,} kcal",
        f"🥩 P: {total_p}g | 🍞 C: {total_c}g | 🧈 F: {total_f}g",
    ]
    return "\n".join(lines)


# ─── Formatting ────────────────────────────────────────────────────
def format_today_summary(chat_id: int) -> str:
    """Format today's calorie & macro summary."""
    meals = get_todays_meals(chat_id)

    if not meals:
        return "📋 <b>Today's Summary</b>\n\nNo meals logged yet today."

    total_cal = sum((m["analysis"].get("total_calories") or 0) for m in meals)
    total_p = sum((m["analysis"].get("total_protein_g") or 0) for m in meals)
    total_c = sum((m["analysis"].get("total_carbs_g") or 0) for m in meals)
    total_f = sum((m["analysis"].get("total_fat_g") or 0) for m in meals)

    lines = [
        "📋 <b>Today's Summary</b>\n",
        f"🔥 <b>{total_cal:,} kcal</b>",
        f"🥩 Protein: {total_p}g",
        f"🍞 Carbs: {total_c}g",
        f"🧈 Fat: {total_f}g",
        f"📸 Meals: {len(meals)}",
    ]
    return "\n".join(lines)


def format_meals_list(chat_id: int) -> str:
    """Format today's meals as a numbered list with per-meal calories."""
    meals = get_todays_meals(chat_id)

    if not meals:
        return "📋 <b>Today's Meals</b>\n\nNo meals logged yet today."

    lines = ["📋 <b>Today's Meals</b>\n"]
    total_cal = 0

    for i, meal in enumerate(meals):
        a = meal["analysis"]
        desc = a.get("meal_description", "Unknown")
        cal = a.get("total_calories") or 0
        p = a.get("total_protein_g") or 0
        c = a.get("total_carbs_g") or 0
        f = a.get("total_fat_g") or 0
        total_cal += cal
        corrected = " ✏️" if meal.get("corrected") else ""
        lines.append(f"{i + 1}. <b>{desc}</b> ({meal.get('time', '?')}){corrected}")
        lines.append(f"   ~{cal} kcal | P:{p}g C:{c}g F:{f}g")

    lines.append(f"\n━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔥 <b>Total: ~{total_cal:,} kcal</b> ({len(meals)} meals)")
    return "\n".join(lines)


def format_history(chat_id: int, days: int = 7) -> str:
    """Format a summary of daily calorie totals over the past week."""
    cutoff_date = (date.today() - timedelta(days=days)).isoformat()
    today_str = date.today().isoformat()
    all_meals = database.get_meals(chat_id, cutoff_date, today_str)
    meals = [m for m in all_meals if m.get("analysis", {}).get("is_food")]
    
    if not meals:
        return f"📅 <b>{days}-Day History</b>\n\nNo meals logged in the past {days} days."

    daily_cals = {}
    for m in meals:
        d = m.get("date", "")
        cal = m.get("analysis", {}).get("total_calories") or 0
        daily_cals[d] = daily_cals.get(d, 0) + cal
        
    lines = [f"📅 <b>{days}-Day History</b>\n"]
    for d in sorted(daily_cals.keys(), reverse=True):
        try:
            dt = date.fromisoformat(d)
            friendly_date = dt.strftime("%A, %b %d")
            if d == date.today().isoformat():
                friendly_date = "Today"
        except ValueError:
            friendly_date = d
        lines.append(f"• {friendly_date}: <b>~{daily_cals[d]} kcal</b>")
        
    avg = sum(daily_cals.values()) / len(daily_cals)
    lines.append(f"\n📊 <b>Average:</b> ~{int(avg)} kcal / day")
    return "\n".join(lines)


# ─── Smart Correction via Gemini ───────────────────────────────────
def parse_ai_json(text: str) -> Dict:
    """Parse JSON from Gemini response, stripping markdown fences if present."""
    content = text.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines)
    return json.loads(content)


def handle_text_message(
    gemini_client: genai.Client,
    bot: TelegramBot,
    chat_id: int,
    text: str,
):
    """Process a text message as either a new meal, correction, or chat."""
    meals = get_recent_meals(chat_id, days=3)

    meals_list_str = "No meals logged recently."
    if meals:
        meals_list_parts = []
        for i, meal in enumerate(meals):
            a = meal["analysis"]
            d = meal.get("date", "?")
            desc = a.get("meal_description", "Unknown")
            cal = a.get("total_calories", 0)
            items = a.get("food_items", [])
            items_str = ", ".join(item.get("name", "?") for item in items)
            meals_list_parts.append(
                f"[{i}] Date: {d} | Meal: {desc} (~{cal} kcal) — Items: {items_str}"
            )
        meals_list_str = "\n".join(meals_list_parts)

    # Call Gemini
    prompt = TEXT_HANDLER_PROMPT.format(
        meals_list=meals_list_str,
        user_message=text,
    )

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt],
        )
        result = parse_ai_json(response.text)
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse AI response: {e}")
        bot.send_message(chat_id, "❌ Couldn't understand the AI response. Try rephrasing.")
        return
    except Exception as e:
        log.error(f"Gemini API error: {e}")
        bot.send_message(chat_id, "❌ Error contacting AI. Please try again.")
        return

    intent = result.get("intent")

    if intent == "correction":
        meal_index = result.get("meal_index", 0)
        new_analysis = result.get("analysis", {})
        reason = result.get("reason", "")

        if not meals:
            bot.send_message(chat_id, "❌ Cannot correct because no meals are logged recently.")
            return

        if meal_index < 0 or meal_index >= len(meals):
            bot.send_message(chat_id, f"❌ Invalid meal index ({meal_index}). You have {len(meals)} recent meals.")
            return

        # Get old values for the diff
        old_analysis = meals[meal_index]["analysis"]
        old_cal = old_analysis.get("total_calories") or 0
        new_cal = new_analysis.get("total_calories") or 0
        old_desc = old_analysis.get("meal_description", "Unknown")
        new_desc = new_analysis.get("meal_description", old_desc)

        # Update the log
        success = update_meal_by_index(chat_id, meal_index, new_analysis)
        if not success:
            bot.send_message(chat_id, "❌ Failed to update the meal.")
            return

        # Format reply with diff
        diff = new_cal - old_cal
        diff_str = f"+{diff}" if diff > 0 else str(diff)
        reply_lines = [
            f"✏️ <b>Corrected meal {meal_index + 1}!</b>",
            f"",
            f"<b>{old_desc}</b> → <b>{new_desc}</b>",
            f"🔥 {old_cal} kcal → {new_cal} kcal ({diff_str})",
        ]
        if reason:
            reply_lines.append(f"\n💬 {reason}")

        bot.send_message(chat_id, "\n".join(reply_lines))
        log.info(f"  ✏️ Corrected meal {meal_index + 1}: {old_cal} → {new_cal} kcal")

    elif intent == "new_meal":
        analysis = result.get("analysis", {})
        if analysis.get("is_food"):
            save_meal(chat_id, analysis, "manual_text", "text", "")
            log.info(f"  ✅ Manual Food: {analysis.get('meal_description')} (~{analysis.get('total_calories')} kcal)")
            result_text = format_food_result(chat_id, analysis)
            bot.send_message(chat_id, "✅ Added new manual meal:\n\n" + result_text)
        else:
            bot.send_message(chat_id, "🚫 I couldn't detect food in that description.")

    else:
        # chat
        reply = result.get("reply", "I'm not sure what you mean. Try describing a meal or correction!")
        bot.send_message(chat_id, reply)
        log.info(f"  💬 Chat response sent")


# ─── Main Bot Loop ────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("🤖 CalorieTracker Bot (Corrections Mode)")
    log.info("=" * 50)

    # Validate config
    if not BOT_TOKEN:
        log.error(
            "TELEGRAM_BOT_TOKEN not set. "
            "Set it: export TELEGRAM_BOT_TOKEN='your-token'"
        )
        sys.exit(1)

    if not GEMINI_API_KEY:
        log.error(
            "GEMINI_API_KEY not set. "
            "Set it: export GEMINI_API_KEY='your-key'"
        )
        sys.exit(1)

    # Initialize
    bot = TelegramBot(BOT_TOKEN)
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

    log.info("Bot is running! Listening for corrections and commands.")
    log.info("Press Ctrl+C to stop.\n")

    try:
        while True:
            updates = bot.get_updates(timeout=30)

            for update in updates:
                message = update.get("message")
                if not message:
                    continue

                chat_id = message["chat"]["id"]
                user = message.get("from", {}).get("first_name", "User")

                # ─── Security Check ──────────────────────────────
                if chat_id != ALLOWED_CHAT_ID:
                    log.warning(f"Unauthorized access attempt from chat_id: {chat_id} (User: {user})")
                    bot.send_message(
                        chat_id,
                        "🚫 You are not authorized to use this bot. This is a private instance.",
                    )
                    continue

                text = message.get("text", "").strip()

                # ─── Commands ────────────────────────────────────
                if text.startswith("/start") or text.startswith("/help"):
                    log.info(f"[{user}] /help")
                    bot.send_message(chat_id, HELP_TEXT)
                    continue

                if text.startswith("/today"):
                    log.info(f"[{user}] /today")
                    bot.send_message(chat_id, format_today_summary(chat_id))
                    continue

                if text.startswith("/meals"):
                    log.info(f"[{user}] /meals")
                    bot.send_message(chat_id, format_meals_list(chat_id))
                    continue
                    
                if text.startswith("/history"):
                    log.info(f"[{user}] /history")
                    bot.send_message(chat_id, format_history(chat_id))
                    continue

                # ─── Handle photos ────────────────────────────────
                photos = message.get("photo")
                document = message.get("document")
                file_id = None

                if photos:
                    file_id = photos[-1]["file_id"]
                elif document:
                    mime = document.get("mime_type", "")
                    if mime.startswith("image/"):
                        file_id = document["file_id"]
                        log.info(f"  📎 Received image as document ({mime})")

                if file_id:
                    log.info(f"[{user}] Received photo, analyzing...")
                    try:
                        image_bytes = bot.get_file(file_id)

                        # Duplicate detection
                        img_hash = hashlib.md5(image_bytes).hexdigest()
                        if is_duplicate_photo(chat_id, img_hash):
                            log.info(f"  🔄 Duplicate photo detected, skipping")
                            bot.send_message(
                                chat_id,
                                "🔄 This looks like the same photo you already sent!\n"
                                "It won't be counted twice.",
                            )
                            continue

                        bot.send_message(chat_id, "🔍 Analyzing your food... one moment!")
                        analysis = analyze_food_photo(gemini_client, image_bytes)

                        if analysis is None:
                            bot.send_message(chat_id, "❌ Sorry, I couldn't analyze that photo. Please try again.")
                            continue

                        if analysis.get("is_food"):
                            save_meal(chat_id, analysis, "telegram", file_id, img_hash)
                            log.info(
                                f"  ✅ Food: {analysis.get('meal_description')} "
                                f"(~{analysis.get('total_calories')} kcal)"
                            )
                        else:
                            log.info("  ⏭️ Not food")

                        result_text = format_food_result(chat_id, analysis)
                        bot.send_message(chat_id, result_text)

                    except Exception as e:
                        log.error(f"Error processing photo: {e}")
                        bot.send_message(chat_id, "❌ Error processing your photo. Please try again.")
                    continue

                # ─── Ignore non-text messages ─────────────────────
                if not text or text.startswith("/"):
                    continue

                # ─── Smart Text Handler ───────────────────────
                log.info(f"[{user}] Text: {text}")
                handle_text_message(gemini_client, bot, chat_id, text)

    except KeyboardInterrupt:
        log.info("\n👋 Bot stopped.")


if __name__ == "__main__":
    main()
