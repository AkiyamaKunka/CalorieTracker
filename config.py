"""
Configuration for the Daily Calorie Tracker.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ─── Gemini API Settings ──────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"

# ─── Telegram Bot Settings ────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8675416366")

# ─── PushPlus WeChat Integration ─────────────────────────────────────
# Token for PushPlus (http://www.pushplus.plus/)
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN")
# Topic code (群组编码) to route messages to the coach instead of personal WeChat
PUSHPLUS_TOPIC = os.environ.get("PUSHPLUS_TOPIC", "coach123")

# ─── Paths ─────────────────────────────────────────────────────────
REPORTS_DIR = Path.home() / "CalorieTracker" / "reports"
MEALS_LOG = Path.home() / "CalorieTracker" / "logs" / "telegram_meals.json"
PROCESSED_PHOTOS_LOG = Path.home() / "CalorieTracker" / "logs" / "processed_photos.json"

# ─── Photo Settings ───────────────────────────────────────────────
# Supported image extensions to process
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff"}

# ─── Prompt ────────────────────────────────────────────────────────
FOOD_DETECTION_PROMPT = """Analyze this photo and determine if it contains food or a meal.

If the image does NOT contain food, respond with exactly:
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

# ─── Correction Prompt ─────────────────────────────────────────────
CORRECTION_PROMPT = """I previously analyzed a food photo and got this result:
{previous_analysis}

The user says this correction: "{user_correction}"

Please re-analyze the photo with this correction in mind. Apply the user's feedback
to produce a more accurate estimate.

Respond with the corrected JSON in the same format:
{{
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
}}

Rules:
- Respond ONLY with valid JSON, no other text
- Apply the user's correction to adjust the specific items mentioned
- Keep other items unchanged unless the correction affects them
- Recalculate all totals
"""

# ─── Text-Only Meal Prompt ─────────────────────────────────────────
TEXT_MEAL_PROMPT = """The user didn't take a photo but described what they ate:

"{meal_description}"

Based on this description, estimate the calories and macros as accurately as possible.

Respond with JSON in this exact format:
{{
  "is_food": true,
  "food_items": [
    {{"name": "Item Name", "estimated_calories": 000, "protein_g": 00, "carbs_g": 00, "fat_g": 00}}
  ],
  "total_calories": 000,
  "total_protein_g": 00,
  "total_carbs_g": 00,
  "total_fat_g": 00,
  "meal_description": "...",
  "confidence_note": "Estimated from text description only"
}}

Rules:
- Respond ONLY with valid JSON, no other text
- Use typical portion sizes unless the user specifies
- Be as specific as possible with your estimates
- If the description is vague, make reasonable assumptions on portion size
"""

# ─── Duplicate Detection ──────────────────────────────────────────
# Photos sent within this window with the same hash are considered duplicates
DUPLICATE_WINDOW_MINUTES = 5

# ─── Unified Text Handler Prompt ────────────────────────────────────
TEXT_HANDLER_PROMPT = """You are a calorie tracking assistant. The user sent a text message.

Here are the recently logged meals (across the last few days):
{meals_list}

The user says: "{user_message}"

Your job is to determine the intent of the message and respond with JSON.
Intent can be:
1. "new_meal": The user is describing a new meal they ate.
2. "correction": The user is correcting an existing meal from the recent meals list (e.g., "change yesterday's lunch to 500 kcal").
3. "chat": A general question, greeting, or unrelated message.

Respond with JSON ONLY in this exact format:
{{
  "intent": "new_meal",
  "meal_index": 0,
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
  "reply": "Friendly response to the user"
}}

Rules:
- Respond ONLY with valid JSON, no other text
- For "new_meal", estimate calories based on standard portion sizes and return it in "analysis".
- For "correction", accurately identify the "meal_index" (the exact 0-based index from the provided `meals_list` array), apply the correction, and return the FULL updated "analysis".
- For "chat", just return a friendly "reply" string.
- If the user describes food but meals_list is empty, it MUST be a "new_meal", not a "correction".
"""
