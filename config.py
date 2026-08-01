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
# Grace-window slot for key rotation: the OLD key stays accepted while
# phones migrate to the new one. Remove from .env once every client has
# the new key — a retiring key is still a valid credential.
ANDROID_API_KEY_RETIRING = _clean_env("ANDROID_API_KEY_RETIRING")

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

# ─── Prompts ───────────────────────────────────────────────────────
# Single source of truth: shared/prompts/*.txt, generated into
# shared_generated.py by scripts/sync_shared.py (drift-gated in tests).
# The mobile app consumes the SAME sources via its generated Dart binding.
from shared_generated import (  # noqa: E402
    FOOD_DETECTION_PROMPT_RAW,
    TEXT_HANDLER_PROMPT_TEMPLATE,
)

FOOD_DETECTION_PROMPT = FOOD_DETECTION_PROMPT_RAW

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
TEXT_HANDLER_PROMPT = TEXT_HANDLER_PROMPT_TEMPLATE
