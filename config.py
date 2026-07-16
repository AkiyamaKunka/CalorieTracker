"""
Configuration for the Daily Calorie Tracker.
"""

import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _clean_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    if value is None:
        return None
    value = str(value).strip().strip('"').strip("'")
    return value or None


# ─── Gemini API Settings ──────────────────────────────────────────
GEMINI_API_KEY = _clean_env("GEMINI_API_KEY")
GEMINI_MODEL = _clean_env("GEMINI_MODEL", "gemini-2.5-flash")

# ─── Telegram Bot Settings ────────────────────────────────────────
TELEGRAM_BOT_TOKEN = _clean_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _clean_env("TELEGRAM_CHAT_ID")
ANDROID_API_KEY = _clean_env("ANDROID_API_KEY")

# ─── PushPlus WeChat Integration ─────────────────────────────────────
# Token for PushPlus (http://www.pushplus.plus/)
PUSHPLUS_TOKEN = _clean_env("PUSHPLUS_TOKEN")
# Topic code (群组编码) to route messages to the coach instead of personal WeChat
PUSHPLUS_TOPIC = _clean_env("PUSHPLUS_TOPIC")

# ─── Paths ─────────────────────────────────────────────────────────
REPORTS_DIR = Path.home() / "CalorieTracker" / "reports"

# ─── Photo Settings ───────────────────────────────────────────────
# Supported image extensions to process
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff"}

# ─── Prompt ────────────────────────────────────────────────────────
FOOD_DETECTION_PROMPT = """Analyze this photo and determine if it contains food, a meal, or a beverage.

Beverages COUNT as food here: coffee (including plain black coffee), tea,
lattes, juice, soda, bubble tea, protein shakes, beer, and any other drink
a calorie tracker should log. Estimate their calories from what is visible
(cup size, milk foam, color). Only plain water counts as no calories — log
it as is_food false.

If the image contains NO food, meal, or beverage, respond with exactly:
{"is_food": false}

If the image DOES contain food, respond with a JSON object like this:
{
  "is_food": true,
  "food_items": [
    {"name": "Grilled Chicken Breast", "estimated_calories": 280, "protein_g": 43, "carbs_g": 0, "fat_g": 12},
    {"name": "Caesar Salad", "estimated_calories": 170, "protein_g": 7, "carbs_g": 12, "fat_g": 10}
  ],
  "total_calories": 450,
  "total_protein_g": 50,
  "total_carbs_g": 12,
  "total_fat_g": 22,
  "meal_description": "Grilled chicken breast with Caesar salad",
  "confidence_note": "Portions appear to be standard restaurant serving sizes"
}

Rules:
- Respond ONLY with valid JSON, no other text
- Estimate calories based on typical portion sizes visible in the photo
- For each food item, estimate protein (g), carbs (g), and fat (g)
- Include total_protein_g, total_carbs_g, total_fat_g as sums
- Be as specific as possible about the food items
- If you can see the portion size, adjust your estimate accordingly
- Include a brief confidence note about the estimate uncertainty
"""

# Load Dietary Preferences
dietary_profile_path = Path.home() / "CalorieTracker" / "dietary_profile.txt"
if dietary_profile_path.exists():
    try:
        dietary_profile = dietary_profile_path.read_text(encoding="utf-8")
        if dietary_profile.strip():
            FOOD_DETECTION_PROMPT += f"\n\nUser's Dietary Profile / Cultural Context:\n{dietary_profile}\nPlease strongly consider these preferences when analyzing the photo.\n"
    except (OSError, UnicodeDecodeError) as e:
        print(f"[WARN] Could not load dietary profile from {dietary_profile_path}: {e}", file=sys.stderr)

# ─── Duplicate Detection ──────────────────────────────────────────
# Photos sent within this window with the same hash are considered duplicates
DUPLICATE_WINDOW_MINUTES = 5

# ─── Unified Text Handler Prompt ────────────────────────────────────
TEXT_HANDLER_PROMPT = """You are a calorie tracking assistant. The user sent a text message.

Date context (the user's local time): today is {today} ({weekday}); yesterday was {yesterday}.
Use this to resolve relative references like "yesterday", "this morning", or "last night" to the matching Date in the meals list below.

Here are the recently logged meals (each line shows its Date so you can match relative references):
{meals_list}

The user says: "{user_message}"

Your job is to determine the intent of the message and respond with JSON.
Intent can be:
1. "new_meal": The user is describing a new meal they ate.
2. "correction": The user is correcting an existing meal from the recent meals list (e.g., "change yesterday's lunch to 500 kcal").
3. "delete": The user wants to completely delete one or more meals (e.g., "delete all food today", "remove meal 0").
4. "chat": A general question, greeting, or unrelated message.
5. "log_weight": The user is reporting their body weight (e.g., "I weigh 72.5 kg this morning", "weighed 159 lb today").
6. "log_activity": The user is reporting exercise they did — calories burned, steps, and/or distance (e.g., "burned 450 calories on my 5 km run", "did 8000 steps today").

Respond with JSON ONLY in this exact format:
{{
  "intent": "new_meal",
  "meal_index": 0,
  "meal_indices": [0],
  "reason": "Briefly explain your logic",
  "analysis": {{
    "is_food": true,
    "food_items": [
      {{"name": "Item Name", "estimated_calories": 000, "protein_g": 00, "carbs_g": 00, "fat_g": 00}}
    ],
    "total_calories": 000,
    "total_protein_g": 00,
    "total_carbs_g": 00,
    "total_fat_g": 00,
    "meal_description": "...",
    "confidence_note": "..."
  }},
  "weight_kg": 0,
  "active_calories": 0,
  "steps": 0,
  "distance_km": 0,
  "reply": "Friendly response to the user"
}}

COMPOUND REQUESTS: if the user's single message contains MULTIPLE distinct requests
(e.g. "correct meal 2 to roast duck rice AND delete meal 3"), respond with ONE
JSON OBJECT (never a bare JSON array) in this shape instead:
{{
  "intent": "multi",
  "actions": [
    {{ ...first request, same fields as a single response... }},
    {{ ...second request... }}
  ],
  "reply": "Brief summary of what you are doing"
}}
Each entry in "actions" uses exactly the same schema as a single response above.
All "meal_index"/"meal_indices" values in every action refer to the ORIGINAL
meals_list shown above (indices never shift between actions). Combine ALL
deletions into ONE "delete" action listing every index in "meal_indices".
List actions in the order the user stated them. Use at most 5 actions.

Rules:
- Respond ONLY with valid JSON, no other text.
- Respond with a single JSON OBJECT at the top level — NEVER a bare JSON array. Multiple requests go inside "actions" of a "multi" object.
- For "new_meal", estimate calories based on standard portion sizes and return it in "analysis".
- For "correction", accurately identify the "meal_index" (the exact 0-based index from the provided `meals_list` array), apply the correction, and return the FULL updated "analysis".
- For "delete", accurately identify all targeted meals from `meals_list` and return their 0-based indices as a list in "meal_indices". Provide a brief "reason".
- For "chat", just return a friendly "reply" string.
- For "log_weight", set "weight_kg" to the body weight in kilograms (convert pounds to kg). Do NOT treat food as weight.
- For "log_activity", set "active_calories" (calories burned), "steps", and "distance_km" from the message; use 0 for anything not stated.
- A message describing FOOD the user ate is always "new_meal", never "log_weight" or "log_activity".
- If the user describes food but meals_list is empty, it MUST be a "new_meal", not a "correction" or "delete".
"""
