#!/usr/bin/env python3
"""
CalorieTracker Telegram Bot

Handles:
- Direct photo analysis (for Android / manual sends)
- AI-powered meal corrections via natural language
- Commands: /meals, /today, /history, /help

Phone and Telegram uploads are processed through this bot and its Flask
upload API.

Usage:
    export TELEGRAM_BOT_TOKEN='your-token'
    export GEMINI_API_KEY='your-key'
    python3 telegram_bot.py
"""

import io
import ipaddress
import json
import hashlib
import hmac
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, date, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Dict, List, Optional

import threading
from flask import Flask, request, jsonify

import requests
from google import genai
from PIL import Image
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

import database
import service_health
from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TEXT_HANDLER_PROMPT,
    FOOD_DETECTION_PROMPT,
    DUPLICATE_WINDOW_MINUTES,
    ANDROID_API_KEY,
    REPORTS_DIR,
    SUPPORTED_EXTENSIONS,
)
from utils import parse_ai_json, telegram_message_chunks

# ─── Constants ─────────────────────────────────────────────────────
def _parse_chat_id(value: Optional[str]) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return 0


def _env_int(name: str, default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        logging.getLogger("calorie_bot").warning(f"Invalid integer for {name}; using {default}.")
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_float(name: str, default: float, min_value: Optional[float] = None, max_value: Optional[float] = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        logging.getLogger("calorie_bot").warning(f"Invalid float for {name}; using {default}.")
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _html(value) -> str:
    return escape(str(value if value is not None else ""))


ALLOWED_CHAT_ID = _parse_chat_id(TELEGRAM_CHAT_ID)

BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') or TELEGRAM_BOT_TOKEN
GEMINI_ANALYSIS_MAX_ATTEMPTS = _env_int("GEMINI_ANALYSIS_MAX_ATTEMPTS", 3, 1, 10)
GEMINI_RETRY_BASE_DELAY_SECONDS = _env_int("GEMINI_RETRY_BASE_DELAY_SECONDS", 5, 0, 3600)
GEMINI_RETRY_MAX_DELAY_SECONDS = _env_int("GEMINI_RETRY_MAX_DELAY_SECONDS", 60, 1, 3600)
GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS = _env_int("GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS", 12 * 3600, 60, 7 * 24 * 3600)
API_UPLOAD_PENDING_DIR = Path.home() / "CalorieTracker" / "logs" / "pending_uploads"
API_UPLOAD_FAILED_DIR = Path.home() / "CalorieTracker" / "logs" / "failed_uploads"
SERVICE_HEALTH_PATH = service_health.DEFAULT_PATH
BOT_SERVICE_NAME = os.environ.get("CALORIE_BOT_SERVICE_NAME", "caloriebot.service")
RETRY_ALL_FAILED_MAX = _env_int("RETRY_ALL_FAILED_MAX", 10, 1, 50)
MAX_API_UPLOAD_BYTES = _env_int("MAX_API_UPLOAD_BYTES", 25 * 1024 * 1024, 1024, 100 * 1024 * 1024)
ANDROID_VPN_WARNING_COOLDOWN_MINUTES = _env_int("ANDROID_VPN_WARNING_COOLDOWN_MINUTES", 30, 0, 1440)
IOS_VPN_WARNING_COOLDOWN_MINUTES = _env_int("IOS_VPN_WARNING_COOLDOWN_MINUTES", 30, 0, 1440)
HEARTBEAT_STALE_WARNING_HOURS = _env_float("HEARTBEAT_STALE_WARNING_HOURS", 2, 0, 168)
HEARTBEAT_STALE_WARNING_COOLDOWN_HOURS = _env_float("HEARTBEAT_STALE_WARNING_COOLDOWN_HOURS", 12, 0.1, 168)
VPN_GEO_LOOKUP_ENABLED = os.environ.get("VPN_GEO_LOOKUP_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
VPN_GEO_LOOKUP_TIMEOUT_SECONDS = _env_float("VPN_GEO_LOOKUP_TIMEOUT_SECONDS", 2, 0.1, 30)
VPN_GEO_CACHE_TTL_SECONDS = _env_int("VPN_GEO_CACHE_TTL_SECONDS", 86400, 60, 604800)
VPN_OFF_COUNTRY_CODES = {
    country.strip().upper()
    for country in os.environ.get("VPN_OFF_COUNTRY_CODES", "CN").split(",")
    if country.strip()
}
ANDROID_VPN_REMOTE_CIDRS = [
    cidr.strip()
    for cidr in os.environ.get(
        "VPN_REMOTE_CIDRS",
        os.environ.get("ANDROID_VPN_REMOTE_CIDRS", "79.127.245.0/24,169.150.222.0/24"),
    ).split(",")
    if cidr.strip()
]
VPN_OFF_REMOTE_CIDRS = [
    cidr.strip()
    for cidr in os.environ.get("VPN_OFF_REMOTE_CIDRS", "").split(",")
    if cidr.strip()
]
# Only trust X-Forwarded-For when a reverse proxy in front of Flask sets it;
# the Flask port is directly exposed by default, so the header is spoofable.
TRUSTED_PROXY_ENABLED = os.environ.get("TRUSTED_PROXY_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
NL_DELETE_CONFIRM_TTL_SECONDS = 600
_last_android_vpn_warning_at = None
_last_ios_vpn_warning_at = None
_last_stale_heartbeat_warning_at = None
_vpn_warning_lock = threading.Lock()
_api_upload_processing_hashes = set()
_api_upload_processing_lock = threading.Lock()
_service_health_lock = threading.Lock()
_remote_ip_country_cache = {}
# Pending natural-language deletes awaiting inline-button confirmation.
_pending_nl_deletes: Dict[int, Dict] = {}

HELP_TEXT = """🍽️ <b>CalorieTracker Bot</b>

I track your food automatically from your Photos library (iPhone/iCloud).
You can also send food photos directly here (Android)!

<b>Commands:</b>
/meals — View today's meals (numbered list)
/recent — View recent meals from the last 3 days
/today — View today's calorie & macro summary
/history — View your calorie totals for the last 7 days
/status — Bot, Android, Gemini, queue, and report health
/commands — Show the full operations command menu
/help — Show this help message

<b>Photo:</b>
📸 Send a food photo for instant calorie analysis

<b>Corrections:</b>
Just type a correction in natural language! I remember your meals from the last 3 days.
• "change yesterday's lunch to 400 kcal"
• "the lunch today was pad thai not fried rice"
• "I didn't eat the rice on Tuesday"
"""

COMMAND_MENU_TEXT = """🧰 <b>Operations Commands</b>

<b>Daily tracking</b>
/today — Today's calories and macros
/meals — Today's meals
/recent — Recent meals from the last 3 days
/history — 7-day calorie history

<b>System health</b>
/status — Overall health summary
/doctor — Run a deeper self-check
/gemini — Live Gemini probe
/android — Android heartbeat and timezone
/vpn — Android/iPhone VPN evidence

<b>Uploads</b>
/queue — Pending and failed upload files
/failed — Failed saved upload list
/retry_failed latest — Retry one failed upload
/retry_all_failed 3 — Retry a small batch
/clear_failed latest confirm — Delete saved failed upload(s)

<b>Reports and debugging</b>
/report today — Generate a report now
/report_status — Last daily report result
/reports — Saved report files
/logs 30 — Recent systemd service logs
/config — Safe runtime config, no secrets
/stats — Database totals and source counts
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
        self.session = requests.Session()

    def _redact(self, value) -> str:
        """Strip the bot token from text destined for logs or user messages."""
        text = str(value)
        return text.replace(self.token, "<token>") if self.token else text

    def _call(self, method: str, **kwargs) -> dict:
        """Make a Telegram Bot API call."""
        try:
            resp = self.session.post(f"{self.base_url}/{method}", json=kwargs, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as e:
            # requests embeds the full URL (token included) in HTTPError messages,
            # and ConnectionError/ConnectTimeout messages carry it too.
            resp = getattr(e, "response", None)
            status = resp.status_code if resp is not None else "network-error"
            raise RuntimeError(f"Telegram {status} calling {method}") from None
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
            log.error(f"Error getting updates: {self._redact(e)}")
            time.sleep(5)
            return []

    def send_message(self, chat_id: int, text: str, parse_mode: str = "HTML", reply_markup: Optional[dict] = None):
        """Send a text message. Returns the Message dict on success, or None."""
        # Truncate if too long for Telegram (4096 char limit)
        if len(text) > 4000:
            text = text[:4000] + "\n\n<i>(truncated)</i>"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            return self._call("sendMessage", **payload)
        except Exception as e:
            log.error(f"Failed to send message: {self._redact(e)}")
            # Retry without parse_mode in case of formatting issues
            try:
                fallback = {"chat_id": chat_id, "text": text}
                if reply_markup:
                    fallback["reply_markup"] = reply_markup
                return self._call("sendMessage", **fallback)
            except Exception:
                return None

    def answer_callback_query(self, callback_query_id: str, text: str = ""):
        """Acknowledge an inline-button callback."""
        try:
            return self._call("answerCallbackQuery", callback_query_id=callback_query_id, text=text[:200])
        except Exception as e:
            log.warning(f"Failed to answer callback query: {self._redact(e)}")
            return None

    def delete_message(self, chat_id: int, message_id: int):
        """Delete a message from a chat."""
        try:
            self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception as e:
            log.warning(f"Failed to delete message: {self._redact(e)}")

    def get_file(self, file_id: str) -> bytes:
        """Download a file from Telegram."""
        file_info = self._call("getFile", file_id=file_id)
        file_path = file_info.get("file_path")
        if not file_path:
            raise RuntimeError("Could not get file_path from telegram.")
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            # Network errors (ConnectionError/ConnectTimeout) embed the token URL too.
            resp = getattr(e, "response", None)
            status = resp.status_code if resp is not None else "network-error"
            raise RuntimeError(f"Telegram {status} calling getFile") from None
        return resp.content


# ─── Meals Data Access ─────────────────────────────────────────────
def get_todays_meals(chat_id: int) -> List[Dict]:
    """Get all food meals logged today (user-local date)."""
    today_str = database.user_local_now().date().isoformat()
    meals = database.get_meals(chat_id, today_str, today_str)
    return [m for m in meals if m.get("analysis", {}).get("is_food")]


def get_recent_meals(chat_id: int, days: int = 3) -> List[Dict]:
    """Get all food meals logged in the past N days."""
    meals = database.get_recent_meals(chat_id, days)
    return [m for m in meals if m.get("analysis", {}).get("is_food")]


# ─── Photo Analysis ────────────────────────────────────────────────
def _extract_retry_delay_seconds(error: Exception) -> int:
    """Extract a Gemini RetryInfo delay from an exception string."""
    error_text = str(error)
    for pattern in (
        r"retryDelay['\"]?\s*:\s*['\"](?P<value>\d+(?:\.\d+)?)s['\"]",
        r"retry in (?P<value>\d+(?:\.\d+)?)s",
    ):
        match = re.search(pattern, error_text, flags=re.IGNORECASE)
        if match:
            delay = int(float(match.group("value")) + 0.999)
            return max(1, min(delay, GEMINI_RETRY_MAX_DELAY_SECONDS))

    match = re.search(r"retry in (?P<value>\d+(?:\.\d+)?)ms", error_text, flags=re.IGNORECASE)
    if match:
        delay = int((float(match.group("value")) / 1000) + 0.999)
        return max(1, min(delay, GEMINI_RETRY_MAX_DELAY_SECONDS))

    return GEMINI_RETRY_BASE_DELAY_SECONDS


def _is_retryable_gemini_error(error: Exception) -> bool:
    """Return True for transient Gemini rate-limit/quota/service errors."""
    if _is_daily_free_tier_quota_error(error):
        return False

    error_text = str(error).upper()
    return (
        "RESOURCE_EXHAUSTED" in error_text
        or "429" in error_text
        or "UNAVAILABLE" in error_text
        or "503" in error_text
    )


def _classify_gemini_error(error: Exception) -> str:
    """Classify Gemini failures into stable user-facing health categories."""
    if isinstance(error, json.JSONDecodeError):
        return "parse_error"

    if _is_daily_free_tier_quota_error(error):
        return "daily_quota_exhausted"

    error_text = str(error).upper()
    if any(token in error_text for token in ("RESOURCE_EXHAUSTED", "429", "QUOTA", "RATE_LIMIT")):
        return "quota_rate_limit"
    if any(token in error_text for token in ("API_KEY", "UNAUTHENTICATED", "PERMISSION_DENIED", "401", "403")):
        return "auth"
    if any(token in error_text for token in ("UNAVAILABLE", "DEADLINE", "TIMEOUT", "CONNECTION", "503")):
        return "network_service"
    if any(token in error_text for token in ("INVALID_ARGUMENT", "MODEL_NOT_FOUND", "NOT_FOUND")):
        return "model_error"
    return "unknown"


def _is_daily_free_tier_quota_error(error: Exception) -> bool:
    """Return True when Gemini says the daily free-tier request quota is exhausted."""
    error_text = str(error).upper()
    return (
        "GENERATEREQUESTSPERDAYPERPROJECTPERMODEL-FREETIER" in error_text
        or (
            "GENERATE_CONTENT_FREE_TIER_REQUESTS" in error_text
            and ("PERDAY" in error_text or "PER DAY" in error_text or "LIMIT: 20" in error_text)
        )
    )


def _load_service_health() -> Dict:
    return service_health.load(SERVICE_HEALTH_PATH, warn=log.warning)


def _save_service_health(data: Dict):
    service_health.save(data, SERVICE_HEALTH_PATH, warn=log.warning)


def _health_timestamp() -> str:
    return service_health.timestamp()


def _parse_health_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _gemini_quota_pause() -> Optional[Dict]:
    gemini = _load_service_health().get("gemini", {})
    pause_until = _parse_health_datetime(gemini.get("quota_pause_until"))
    if not pause_until or pause_until <= datetime.now():
        return None
    return {
        "until": pause_until,
        "reason": gemini.get("quota_pause_reason", "daily free-tier quota exhausted"),
        "set_at": gemini.get("quota_pause_set_at", ""),
    }


def _gemini_quota_pause_summary() -> str:
    pause = _gemini_quota_pause()
    if not pause:
        return ""
    until = pause["until"].isoformat(timespec="minutes")
    remaining_seconds = max(0, int((pause["until"] - datetime.now()).total_seconds()))
    if remaining_seconds < 60:
        remaining = f"{remaining_seconds}s"
    elif remaining_seconds < 3600:
        remaining = f"{remaining_seconds // 60}m"
    else:
        remaining = f"{remaining_seconds // 3600}h{(remaining_seconds % 3600) // 60:02d}m"
    return (
        f"Gemini daily free-tier quota is paused until <code>{escape(until)}</code> "
        f"(about {remaining} remaining)."
    )


def _set_gemini_daily_quota_pause(error: Exception) -> str:
    pause_until = datetime.now() + timedelta(seconds=GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS)
    with _service_health_lock:
        data = _load_service_health()
        gemini = data.setdefault("gemini", {})
        gemini["quota_pause_set_at"] = _health_timestamp()
        gemini["quota_pause_until"] = pause_until.isoformat(timespec="seconds")
        gemini["quota_pause_reason"] = str(error)[:500]
        _save_service_health(data)
    return pause_until.isoformat(timespec="seconds")


def _record_gemini_health(
    ok: bool,
    *,
    error_type: str = "",
    error_summary: str = "",
    latency_seconds: Optional[float] = None,
    probe: bool = False,
):
    with _service_health_lock:
        data = _load_service_health()
        gemini = data.setdefault("gemini", {})
        now = _health_timestamp()

        if ok:
            gemini["last_ok_at"] = now
            gemini["consecutive_failures"] = 0
            gemini.pop("quota_pause_until", None)
            gemini.pop("quota_pause_set_at", None)
            gemini.pop("quota_pause_reason", None)
        else:
            gemini["last_error_at"] = now
            gemini["last_error_type"] = error_type or "unknown"
            gemini["last_error_summary"] = str(error_summary or "")[:500]
            gemini["consecutive_failures"] = int(gemini.get("consecutive_failures", 0)) + 1

        if latency_seconds is not None:
            gemini["last_latency_seconds"] = round(latency_seconds, 2)

        if probe:
            gemini["last_probe_at"] = now
            gemini["last_probe_ok"] = ok
            if not ok:
                gemini["last_probe_error_type"] = error_type or "unknown"

        events = gemini.setdefault("events", [])
        events.append({
            "at": now,
            "ok": ok,
            "type": "" if ok else (error_type or "unknown"),
            "probe": probe,
        })
        gemini["events"] = events[-50:]
        _save_service_health(data)


def _record_vpn_observation(
    client: str,
    endpoint: str,
    remote_ip: str,
    vpn_active: Optional[bool],
    vpn_check: str,
    reliable: bool,
    evidence: str,
    evidence_detail: str,
):
    with _service_health_lock:
        data = _load_service_health()
        vpn = data.setdefault("vpn", {})
        vpn[client] = {
            "at": _health_timestamp(),
            "endpoint": endpoint,
            "remote_ip": remote_ip,
            "vpn_active": vpn_active,
            "vpn_check": vpn_check or "",
            "vpn_check_reliable": reliable,
            "evidence": evidence,
            "evidence_detail": evidence_detail,
        }
        _save_service_health(data)


def _record_report_health(
    ok: bool,
    target_date: str,
    *,
    source: str = "telegram_command",
    report_path: str = "",
    error_summary: str = "",
):
    with _service_health_lock:
        data = _load_service_health()
        service_health.apply_report_health(
            data, ok, target_date,
            source=source, report_path=report_path, error_summary=error_summary,
        )
        _save_service_health(data)


def _gemini_recent_counts(hours: int = 24) -> tuple[int, int]:
    data = _load_service_health()
    events = data.get("gemini", {}).get("events", [])
    cutoff = datetime.now() - timedelta(hours=hours)
    ok_count = 0
    fail_count = 0

    for event in events:
        try:
            event_at = datetime.fromisoformat(event.get("at", ""))
        except ValueError:
            continue
        if event_at < cutoff:
            continue
        if event.get("ok"):
            ok_count += 1
        else:
            fail_count += 1

    return ok_count, fail_count


def _gemini_health_label_from(gemini: Dict) -> str:
    consecutive = int(gemini.get("consecutive_failures", 0))
    last_ok = gemini.get("last_ok_at")
    last_error = gemini.get("last_error_at")
    last_error_type = gemini.get("last_error_type", "unknown")
    pause_until = _parse_health_datetime(gemini.get("quota_pause_until"))

    if pause_until and pause_until > datetime.now():
        return f"🟠 daily quota paused until {pause_until.isoformat(timespec='minutes')}"

    if consecutive == 0 and last_ok:
        return f"🟢 OK; last success {last_ok}"
    if consecutive:
        return f"🔴 {last_error_type}; {consecutive} consecutive failure(s), last error {last_error or 'unknown'}"
    return "⚪ No Gemini health data yet"


def _gemini_failure_context() -> str:
    gemini = _load_service_health().get("gemini", {})
    ok_24h, fail_24h = _gemini_recent_counts(24)
    error_type = gemini.get("last_error_type", "unknown")
    summary = gemini.get("last_error_summary", "")

    lines = [
        f"Gemini status: <code>{escape(error_type)}</code>",
        f"Recent Gemini health: {ok_24h} success(es), {fail_24h} failure(s) in the last 24h",
    ]
    if summary:
        lines.append(f"Last error: <code>{escape(summary[:220])}</code>")
    pause_summary = _gemini_quota_pause_summary()
    if pause_summary:
        lines.append(pause_summary)
    return "\n".join(lines)


def _analyze_food_photo_once(client: genai.Client, image_bytes: bytes) -> Dict:
    """Analyze a food photo once with Gemini, letting callers handle failures."""
    img = Image.open(io.BytesIO(image_bytes))
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[FOOD_DETECTION_PROMPT, img],
    )
    return parse_ai_json(response.text)


def analyze_food_photo(client: genai.Client, image_bytes: bytes) -> Optional[Dict]:
    """Analyze a food photo with Gemini (single attempt for the interactive path)."""
    return analyze_food_photo_with_retries(client, image_bytes, max_attempts=1)


def analyze_food_photo_with_retries(
    client: genai.Client,
    image_bytes: bytes,
    max_attempts: int = GEMINI_ANALYSIS_MAX_ATTEMPTS,
) -> Optional[Dict]:
    """Analyze a photo with bounded retry/backoff for retryable Gemini errors."""
    pause = _gemini_quota_pause()
    if pause:
        log.warning("Skipping Gemini photo analysis because daily quota pause is active.")
        return None

    max_attempts = max(1, max_attempts)
    start = time.time()

    for attempt in range(1, max_attempts + 1):
        try:
            result = _analyze_food_photo_once(client, image_bytes)
            _record_gemini_health(True, latency_seconds=time.time() - start)
            return result
        except json.JSONDecodeError as e:
            log.warning(f"Could not parse API response: {e}")
            _record_gemini_health(
                False,
                error_type=_classify_gemini_error(e),
                error_summary=str(e),
                latency_seconds=time.time() - start,
            )
            return None
        except Exception as e:
            error_type = _classify_gemini_error(e)
            if error_type == "daily_quota_exhausted":
                pause_until = _set_gemini_daily_quota_pause(e)
                log.error(
                    f"Gemini daily free-tier quota exhausted on attempt {attempt}; "
                    f"pausing automatic analysis until {pause_until}."
                )
                _record_gemini_health(
                    False,
                    error_type=error_type,
                    error_summary=str(e),
                    latency_seconds=time.time() - start,
                )
                return None

            if not _is_retryable_gemini_error(e) or attempt == max_attempts:
                log.error(f"Error analyzing photo after {attempt} attempt(s): {e}")
                _record_gemini_health(
                    False,
                    error_type=error_type,
                    error_summary=str(e),
                    latency_seconds=time.time() - start,
                )
                return None

            delay = _extract_retry_delay_seconds(e)
            log.warning(
                f"Gemini analysis attempt {attempt}/{max_attempts} failed with a "
                f"retryable error. Retrying in {delay}s."
            )
            time.sleep(delay)

    return None


def _api_upload_extension(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext if ext in SUPPORTED_EXTENSIONS else ".jpg"


def _stage_api_upload(image_bytes: bytes, image_hash: str, filename: str) -> Path:
    """Persist an API upload while background analysis is pending."""
    API_UPLOAD_PENDING_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    upload_path = API_UPLOAD_PENDING_DIR / f"{timestamp}_{image_hash[:12]}{_api_upload_extension(filename)}"
    upload_path.write_bytes(image_bytes)
    return upload_path


def _discard_api_upload(upload_path: Path):
    try:
        upload_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f"Could not remove staged upload {upload_path}: {e}")


def _image_hash_prefix(image_hash: str) -> str:
    normalized = re.sub(r"[^0-9a-fA-F]", "", str(image_hash or "")).lower()
    return normalized[:12]


def _failed_upload_hash_prefixes() -> set:
    prefixes = set()
    for path in _failed_upload_items():
        match = re.search(r"_([0-9a-fA-F]{12})(?:\.[^.]+)?$", path.name)
        if match:
            prefixes.add(match.group(1).lower())
    return prefixes


def _find_failed_upload_by_hash(image_hash: str) -> Optional[Path]:
    prefix = _image_hash_prefix(image_hash)
    if not prefix:
        return None
    for path in _failed_upload_items():
        if prefix in path.name.lower():
            return path
    return None


def _failed_upload_selector(path: Path, image_hash: str = "") -> str:
    return _image_hash_prefix(image_hash) or path.name


def _keep_failed_api_upload(upload_path: Path, image_hash: str = "") -> Path:
    """Move a failed upload aside for manual retry/debugging."""
    API_UPLOAD_FAILED_DIR.mkdir(parents=True, exist_ok=True)
    existing = _find_failed_upload_by_hash(image_hash)
    if existing:
        _discard_api_upload(upload_path)
        return existing

    failed_path = API_UPLOAD_FAILED_DIR / upload_path.name
    try:
        upload_path.replace(failed_path)
        return failed_path
    except OSError as e:
        log.error(f"Could not move failed upload {upload_path}: {e}")
        return upload_path


def _begin_api_upload_processing(image_hash: str) -> bool:
    """Reserve an API-upload image hash so repeated automations do not double-process it."""
    with _api_upload_processing_lock:
        if image_hash in _api_upload_processing_hashes:
            return False
        _api_upload_processing_hashes.add(image_hash)
        return True


def _finish_api_upload_processing(image_hash: str):
    with _api_upload_processing_lock:
        _api_upload_processing_hashes.discard(image_hash)


def _parse_boolish(value) -> Optional[bool]:
    if value is None:
        return None

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _vpn_status_from_request() -> tuple[Optional[bool], str]:
    """Read client VPN status from headers, JSON body, or form fields."""
    vpn_active = _parse_boolish(request.headers.get("X-VPN-Active"))
    vpn_check = request.headers.get("X-VPN-Check", "")

    if vpn_active is None:
        if request.is_json:
            data = request.get_json(silent=True) or {}
            vpn_active = _parse_boolish(data.get("vpn_active"))
            vpn_check = data.get("vpn_check", vpn_check)
        elif request.form:
            vpn_active = _parse_boolish(request.form.get("vpn_active"))
            vpn_check = request.form.get("vpn_check", vpn_check)

    return vpn_active, str(vpn_check or "")[:120]


def _authorized_api_request() -> bool:
    """Check phone upload API auth without leaking timing detail."""
    if not ANDROID_API_KEY:
        return False
    provided = request.headers.get("X-API-Key", "")
    return hmac.compare_digest(str(provided), str(ANDROID_API_KEY))


def _vpn_check_reliable_from_request(vpn_active: Optional[bool], vpn_check: str) -> bool:
    explicit = _parse_boolish(request.headers.get("X-VPN-Check-Reliable"))
    if explicit is None:
        if request.is_json:
            data = request.get_json(silent=True) or {}
            explicit = _parse_boolish(data.get("vpn_check_reliable"))
        elif request.form:
            explicit = _parse_boolish(request.form.get("vpn_check_reliable"))

    if explicit is not None:
        return explicit

    if vpn_active is True:
        return True

    normalized_check = (vpn_check or "").strip().lower()
    if normalized_check in {"env_override", "manual_override", "user_override"}:
        return True

    # Android VPNService tunnels are not always visible to Termux, and iOS Shortcuts
    # cannot inspect VPN state directly. Treat missing tunnel-interface evidence as
    # uncertain unless the remote IP gives us stronger evidence.
    if normalized_check in {"", "no_vpn_interface", "vpn_check_unavailable", "shortcut did not report vpn status"}:
        return False

    return False


def _android_vpn_status_from_request() -> tuple[Optional[bool], str]:
    """Read Android VPN status from headers, JSON body, or form fields."""
    return _vpn_status_from_request()


def _vpn_required_from_request() -> bool:
    vpn_required = _parse_boolish(request.headers.get("X-VPN-Required"))
    if vpn_required is not None:
        return vpn_required

    if request.is_json:
        data = request.get_json(silent=True) or {}
        vpn_required = _parse_boolish(data.get("vpn_required"))
    elif request.form:
        vpn_required = _parse_boolish(request.form.get("vpn_required"))

    return vpn_required is True


def _api_upload_device_name_from_request() -> str:
    explicit_name = request.headers.get("X-Device-Name", "").strip()
    platform = request.headers.get("X-Client-Platform", "").strip().lower()
    user_agent = request.headers.get("User-Agent", "").lower()

    if platform in {"ios", "iphone", "ipad"}:
        return explicit_name or ("iPad" if platform == "ipad" else "iPhone")
    if platform == "android":
        return explicit_name or "Android"

    if "shortcuts" in user_agent or "iphone" in user_agent or "cfnetwork" in user_agent:
        return explicit_name or "iPhone"
    if "python-requests" in user_agent or "android" in user_agent:
        return explicit_name or "Android"
    return explicit_name or "Phone"


def _request_remote_ip() -> str:
    if TRUSTED_PROXY_ENABLED:
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _remote_ip_in_cidrs(remote_ip: str, cidrs: list[str], label: str) -> bool:
    try:
        ip = ipaddress.ip_address(remote_ip)
    except ValueError:
        return False

    for cidr in cidrs:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            log.warning(f"Ignoring invalid {label} entry: {cidr}")

    return False


def _remote_ip_matches_known_vpn(remote_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(remote_ip)
    except ValueError:
        return False

    return _remote_ip_in_cidrs(str(ip), ANDROID_VPN_REMOTE_CIDRS, "VPN_REMOTE_CIDRS")


def _remote_ip_matches_vpn_off_network(remote_ip: str) -> bool:
    return _remote_ip_in_cidrs(remote_ip, VPN_OFF_REMOTE_CIDRS, "VPN_OFF_REMOTE_CIDRS")


def _remote_ip_country_code(remote_ip: str) -> Optional[str]:
    if not VPN_GEO_LOOKUP_ENABLED:
        return None

    try:
        ip = ipaddress.ip_address(remote_ip)
    except ValueError:
        return None

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return None

    cached = _remote_ip_country_cache.get(str(ip))
    now_ts = time.time()
    if cached and now_ts - cached["time"] < VPN_GEO_CACHE_TTL_SECONDS:
        return cached["country"]

    try:
        response = requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,countryCode,query"},
            timeout=VPN_GEO_LOOKUP_TIMEOUT_SECONDS,
        )
        data = response.json()
        country = str(data.get("countryCode") or "").upper() if data.get("status") == "success" else None
    except Exception as e:
        log.info(f"Could not geolocate remote IP {ip}: {e}")
        country = None

    _remote_ip_country_cache[str(ip)] = {"country": country, "time": now_ts}
    return country


def _remote_vpn_evidence(remote_ip: str) -> tuple[str, str]:
    """Return vpn/direct/unknown evidence for the request remote IP."""
    if _remote_ip_matches_known_vpn(remote_ip):
        return "vpn", "known_vpn_cidr"

    if _remote_ip_matches_vpn_off_network(remote_ip):
        return "direct", "known_direct_cidr"

    country = _remote_ip_country_code(remote_ip)
    if country:
        if country in VPN_OFF_COUNTRY_CODES:
            return "direct", f"country:{country}"
        return "vpn", f"country:{country}"

    return "unknown", "no_remote_evidence"


def maybe_warn_android_vpn_inactive(bot: TelegramBot, endpoint: str):
    """Warn the user when the Android client reaches GCP while reporting no VPN."""
    global _last_android_vpn_warning_at

    vpn_active, vpn_check = _android_vpn_status_from_request()
    vpn_check_reliable = _vpn_check_reliable_from_request(vpn_active, vpn_check)
    remote_ip = _request_remote_ip()
    evidence, evidence_detail = _remote_vpn_evidence(remote_ip)
    _record_vpn_observation(
        "android",
        endpoint,
        remote_ip,
        vpn_active,
        vpn_check,
        vpn_check_reliable,
        evidence,
        evidence_detail,
    )

    if vpn_active is not False:
        return

    if evidence == "vpn":
        log.info(
            f"Android reported VPN inactive at {endpoint}, but remote IP {remote_ip} "
            f"looks like VPN traffic ({evidence_detail}); suppressing warning."
        )
        return

    if evidence == "unknown" and not vpn_check_reliable:
        log.info(
            f"Android VPN warning suppressed: local check is unreliable "
            f"({vpn_check or 'no detail'}) and remote IP {remote_ip} has no direct-network evidence."
        )
        return

    now = datetime.now()
    with _vpn_warning_lock:
        if _last_android_vpn_warning_at:
            elapsed = now - _last_android_vpn_warning_at
            if elapsed.total_seconds() < ANDROID_VPN_WARNING_COOLDOWN_MINUTES * 60:
                log.info("Android VPN warning suppressed by cooldown.")
                return
        _last_android_vpn_warning_at = now

    log.warning(
        f"Android client reached {endpoint} from {remote_ip} while reporting VPN inactive "
        f"({vpn_check or 'no detail'}; evidence={evidence_detail})."
    )
    bot.send_message(
        ALLOWED_CHAT_ID,
        "⚠️ <b>Android VPN appears OFF</b>\n\n"
        f"The Android uploader reached <code>{endpoint}</code> from "
        f"<code>{remote_ip}</code>, but it reported no VPN interface "
        f"(<code>{vpn_check or 'no detail'}</code>).\n\n"
        f"Network evidence: <code>{evidence_detail}</code>.\n\n"
        "Turn on the VPN before relying on WiFi/cellular auto-upload from mainland China.",
    )


def maybe_warn_ios_vpn_unverified(bot: TelegramBot, endpoint: str):
    """Warn when the iOS Shortcut requires VPN but the request does not match known VPN egress."""
    global _last_ios_vpn_warning_at

    if not _vpn_required_from_request():
        return

    vpn_active, vpn_check = _vpn_status_from_request()
    vpn_check_reliable = _vpn_check_reliable_from_request(vpn_active, vpn_check)
    remote_ip = _request_remote_ip()
    evidence, evidence_detail = _remote_vpn_evidence(remote_ip)
    _record_vpn_observation(
        "ios",
        endpoint,
        remote_ip,
        vpn_active,
        vpn_check,
        vpn_check_reliable,
        evidence,
        evidence_detail,
    )

    if vpn_active is True:
        return

    if evidence == "vpn":
        log.info(
            f"iOS upload at {endpoint} marked VPN-required; remote IP {remote_ip} "
            f"looks like VPN traffic ({evidence_detail})."
        )
        return

    if evidence == "unknown" and not vpn_check_reliable:
        log.info(
            f"iOS VPN warning suppressed: Shortcut cannot verify VPN locally and "
            f"remote IP {remote_ip} has no direct-network evidence."
        )
        return

    now = datetime.now()
    with _vpn_warning_lock:
        if _last_ios_vpn_warning_at:
            elapsed = now - _last_ios_vpn_warning_at
            if elapsed.total_seconds() < IOS_VPN_WARNING_COOLDOWN_MINUTES * 60:
                log.info("iOS VPN warning suppressed by cooldown.")
                return
        _last_ios_vpn_warning_at = now

    detail = vpn_check or "shortcut did not report VPN status"
    log.warning(
        f"iOS Shortcut reached {endpoint} from {remote_ip}; VPN is required but unverified "
        f"({detail}; evidence={evidence_detail})."
    )
    bot.send_message(
        ALLOWED_CHAT_ID,
        "⚠️ <b>iPhone VPN may be OFF</b>\n\n"
        f"The iOS Shortcut reached <code>{endpoint}</code> from <code>{remote_ip}</code>, "
        "and the network evidence looks like a direct/non-VPN connection.\n\n"
        f"Detail: <code>{detail}</code>\n\n"
        f"Network evidence: <code>{evidence_detail}</code>.\n\n"
        "Turn on the VPN before relying on WiFi/cellular auto-upload from mainland China.",
    )


def save_meal(chat_id: int, analysis: Dict, source: str, file_id: str = "", image_hash: str = ""):
    """Save a meal analysis to the log (from direct Telegram photo)."""
    # Meal date/time use the user's clock so late-night meals land on the right
    # day; timestamp stays on the server clock because duplicate detection and
    # reservation staleness compare against datetime.now().
    user_now = database.user_local_now()
    date_str = user_now.date().isoformat()
    time_str = user_now.strftime("%I:%M %p")
    timestamp_str = datetime.now().isoformat()
    return database.save_meal(chat_id, date_str, time_str, timestamp_str, source, image_hash, file_id, analysis)


def _format_age(iso_value: Optional[str]) -> str:
    if not iso_value:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError:
        return iso_value

    seconds = max(0, int((datetime.now() - dt).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _upload_files(directory: Path) -> List[Path]:
    if not directory.exists():
        return []
    files = []
    for path in directory.iterdir():
        try:
            if path.is_file():
                files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return [path for _, path in sorted(files, key=lambda item: item[0], reverse=True)]


def _failed_upload_items() -> List[Path]:
    return _upload_files(API_UPLOAD_FAILED_DIR)


def _pending_upload_items() -> List[Path]:
    return _upload_files(API_UPLOAD_PENDING_DIR)


def _reconcile_missing_hashes(android_hashes, server_hashes, reserved_hashes=None) -> List[str]:
    """Return Android hashes that are not logged, pending, or already saved for retry."""
    logged_hashes = {str(item).lower() for item in server_hashes}
    reserved_hashes = {str(item).lower() for item in (reserved_hashes or [])}
    failed_prefixes = _failed_upload_hash_prefixes()
    with _api_upload_processing_lock:
        processing_hashes = {str(item).lower() for item in _api_upload_processing_hashes}

    missing = []
    for image_hash in android_hashes:
        normalized = str(image_hash or "").lower()
        if not normalized:
            continue
        if normalized in logged_hashes:
            continue
        if normalized in reserved_hashes:
            continue
        if normalized in processing_hashes:
            continue
        if _image_hash_prefix(normalized) in failed_prefixes:
            continue
        missing.append(image_hash)
    return missing


def format_failed_uploads(limit: int = 8) -> str:
    failed = _failed_upload_items()
    if not failed:
        return "✅ <b>No failed saved uploads.</b>"

    lines = [
        f"⚠️ <b>Failed Saved Uploads</b> ({len(failed)})",
        "Wait/retry later: <code>/retry_failed latest</code> or <code>/retry_failed &lt;hash/name&gt;</code>.",
        "Give up/delete: <code>/clear_failed latest confirm</code> or <code>/clear_failed &lt;hash/name&gt; confirm</code>.",
        "",
    ]
    for path in failed[:limit]:
        lines.append(f"• {_format_upload_file(path)}")

    if len(failed) > limit:
        lines.append(f"\n...and {len(failed) - limit} more.")

    return "\n".join(lines)


def _saved_upload_decision_markup(selector: str) -> Dict:
    safe_selector = str(selector or "latest")[:32]
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Wait / retry later",
                    "callback_data": f"quota_keep:{safe_selector}",
                }
            ],
            [
                {
                    "text": "Give up / delete photo",
                    "callback_data": f"quota_discard:{safe_selector}",
                }
            ],
        ]
    }


def _format_saved_upload_decision_message(
    failed_path: Path,
    image_hash: str,
    *,
    source_label: str = "Phone",
) -> str:
    selector = _failed_upload_selector(failed_path, image_hash)
    pause_summary = _gemini_quota_pause_summary()
    if not pause_summary:
        pause_summary = "Gemini is currently unavailable or quota-limited."

    return (
        f"⏸️ <b>Gemini quota is exhausted.</b>\n\n"
        f"{pause_summary}\n\n"
        f"I saved the {escape(source_label)} photo, but it is <b>not logged yet</b>.\n"
        f"Saved file: <code>{_html(failed_path)}</code>\n"
        f"Selector: <code>{_html(selector)}</code>\n\n"
        "<b>Your options</b>\n"
        f"• Wait and analyze later: <code>/retry_failed {_html(selector)}</code>\n"
        f"• Give up/delete it: <code>/clear_failed {_html(selector)} confirm</code>"
    )


def _send_saved_upload_decision(
    bot: TelegramBot,
    chat_id: int,
    failed_path: Path,
    image_hash: str,
    *,
    source_label: str = "Phone",
):
    selector = _failed_upload_selector(failed_path, image_hash)
    bot.send_message(
        chat_id,
        _format_saved_upload_decision_message(failed_path, image_hash, source_label=source_label),
        reply_markup=_saved_upload_decision_markup(selector),
    )


def _select_failed_upload(selector: str) -> Optional[Path]:
    failed = _failed_upload_items()
    if not failed:
        return None

    selector = (selector or "latest").strip().lower()
    if selector in {"", "latest", "newest"}:
        return failed[0]

    for path in failed:
        if selector in path.name.lower():
            return path

    return None


def _retry_failed_upload_path(gemini_client, path: Path) -> Dict:
    pause = _gemini_quota_pause()
    if pause:
        return {
            "status": "quota_paused",
            "name": path.name,
            "message": "Gemini daily quota is paused; file kept for later retry",
        }

    try:
        image_bytes = path.read_bytes()
    except OSError as e:
        return {
            "status": "read_error",
            "name": path.name,
            "message": f"could not read file: {e}",
        }

    img_hash = hashlib.md5(image_bytes).hexdigest()

    if database.meal_image_hash_exists(ALLOWED_CHAT_ID, img_hash):
        database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "saved", source="api_retry")
        _discard_api_upload(path)
        return {
            "status": "duplicate",
            "name": path.name,
            "message": "photo was already logged; saved file removed",
        }

    if not database.reserve_photo_hash(
        ALLOWED_CHAT_ID,
        img_hash,
        "api_retry",
        reclaim_statuses={"failed"},
    ):
        return {
            "status": "already_reserved",
            "name": path.name,
            "message": "photo is already being processed or was already handled; file kept",
        }

    analysis = analyze_food_photo_with_retries(gemini_client, image_bytes)
    if analysis is None:
        database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "failed", source="api_retry")
        return {
            "status": "analysis_failed",
            "name": path.name,
            "message": "Gemini analysis failed; file kept for retry",
        }

    if not analysis.get("is_food"):
        database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "skipped", source="api_retry")
        _discard_api_upload(path)
        return {
            "status": "not_food",
            "name": path.name,
            "message": "Gemini says it is not food; saved file removed",
        }

    try:
        meal_id = save_meal(ALLOWED_CHAT_ID, analysis, "api_retry", "failed_upload", img_hash)
        database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "saved", meal_id, source="api_retry")
    except Exception as e:
        database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "failed", source="api_retry")
        log.exception(f"Could not save retried upload {path}")
        return {
            "status": "save_error",
            "name": path.name,
            "message": f"meal save failed; file kept for retry: {e}",
        }

    _discard_api_upload(path)
    return {
        "status": "logged",
        "name": path.name,
        "analysis": analysis,
        "message": (
            f"{analysis.get('meal_description', 'Unknown meal')} "
            f"(~{analysis.get('total_calories') or '?'} kcal)"
        ),
    }


def retry_failed_upload(gemini_client, selector: str = "latest") -> str:
    path = _select_failed_upload(selector)
    if not path:
        return "❌ No matching failed upload found. Use <code>/failed</code> to list saved failures."

    result = _retry_failed_upload_path(gemini_client, path)
    if result["status"] == "quota_paused":
        return (
            f"⏸️ Retry held for <code>{escape(path.name)}</code>.\n\n"
            f"{_gemini_quota_pause_summary()}\n\n"
            "Photo status: still saved for later retry.\n"
            f"Give up/delete it with <code>/clear_failed {escape(_failed_upload_selector(path))} confirm</code>."
        )
    if result["status"] == "read_error":
        return (
            f"❌ Could not read <code>{escape(result['name'])}</code>: "
            f"<code>{escape(result['message'])}</code>"
        )
    if result["status"] == "analysis_failed":
        return (
            f"❌ Retry failed for <code>{escape(path.name)}</code>.\n\n"
            f"{_gemini_failure_context()}\n\n"
            "Photo status: still saved for later retry."
        )
    if result["status"] == "save_error":
        return (
            f"❌ Retry analyzed <code>{escape(path.name)}</code>, but saving failed.\n\n"
            f"<code>{escape(result['message'])}</code>"
        )
    if result["status"] == "not_food":
        return f"🚫 Retried <code>{escape(path.name)}</code>; Gemini says it is not food. Saved file removed."
    if result["status"] == "duplicate":
        return (
            f"🔄 Retried <code>{escape(path.name)}</code>; this photo was already logged. "
            "Saved file removed."
        )
    if result["status"] == "already_reserved":
        return (
            f"⏳ Retry skipped for <code>{escape(path.name)}</code>.\n\n"
            "That photo is already being processed or was already handled. "
            "The saved file is still on disk."
        )

    result_text = format_food_result(ALLOWED_CHAT_ID, result["analysis"])
    return f"✅ Retried and logged <code>{escape(path.name)}</code>:\n\n{result_text}"


def retry_all_failed_uploads(gemini_client, limit: int = 3) -> str:
    failed = _failed_upload_items()
    if not failed:
        return "✅ <b>No failed saved uploads to retry.</b>"

    pause_summary = _gemini_quota_pause_summary()
    if pause_summary:
        return (
            "⏸️ <b>Batch retry held.</b>\n\n"
            f"{pause_summary}\n\n"
            f"Saved failed uploads remain on disk: {len(failed)}.\n"
            "Give up on them with <code>/clear_failed latest confirm</code> or "
            "<code>/clear_failed all confirm</code>."
        )

    limit = _parse_positive_int(str(limit), 3, 1, RETRY_ALL_FAILED_MAX)
    selected = failed[:limit]
    results = [_retry_failed_upload_path(gemini_client, path) for path in selected]

    counts = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1

    lines = [
        f"🔁 <b>Batch Retry Finished</b> ({len(selected)} file(s))",
        (
            f"Logged: {counts.get('logged', 0)} | "
            f"Not food: {counts.get('not_food', 0)} | "
            f"Failed: {counts.get('analysis_failed', 0)} | "
            f"Quota held: {counts.get('quota_paused', 0)} | "
            f"Read errors: {counts.get('read_error', 0)} | "
            f"Save errors: {counts.get('save_error', 0)} | "
            f"Duplicates: {counts.get('duplicate', 0)} | "
            f"Reserved: {counts.get('already_reserved', 0)}"
        ),
        "",
    ]

    for result in results:
        status_icon = {
            "logged": "✅",
            "not_food": "🚫",
            "analysis_failed": "❌",
            "quota_paused": "⏸️",
            "read_error": "⚠️",
            "save_error": "⚠️",
            "duplicate": "🔄",
            "already_reserved": "⏳",
        }.get(result["status"], "•")
        lines.append(
            f"{status_icon} <code>{escape(result['name'])}</code>: "
            f"{escape(result.get('message', result['status']))}"
        )

    remaining = len(_failed_upload_items())
    lines.append(f"\nRemaining failed saved uploads: {remaining}")
    if counts.get("analysis_failed"):
        lines.append("")
        lines.append(_gemini_failure_context())

    return "\n".join(lines)


def run_gemini_probe(gemini_client) -> str:
    pause_summary = _gemini_quota_pause_summary()
    if pause_summary:
        return (
            "🟠 <b>Gemini probe skipped</b>\n"
            f"{pause_summary}\n\n"
            "No probe was sent, so this check did not spend another Gemini request. "
            "Retry after the pause expires."
        )

    start = time.time()
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents="Reply with exactly OK.",
        )
        elapsed = time.time() - start
        reply = (getattr(response, "text", "") or "").strip()
        _record_gemini_health(True, latency_seconds=elapsed, probe=True)
        return (
            "🟢 <b>Gemini probe OK</b>\n"
            f"Model: <code>{escape(GEMINI_MODEL)}</code>\n"
            f"Latency: {elapsed:.2f}s\n"
            f"Reply: <code>{escape(reply[:80])}</code>"
        )
    except Exception as e:
        elapsed = time.time() - start
        error_type = _classify_gemini_error(e)
        _record_gemini_health(
            False,
            error_type=error_type,
            error_summary=str(e),
            latency_seconds=elapsed,
            probe=True,
        )
        return (
            "🔴 <b>Gemini probe failed</b>\n"
            f"Model: <code>{escape(GEMINI_MODEL)}</code>\n"
            f"Type: <code>{escape(error_type)}</code>\n"
            f"Latency: {elapsed:.2f}s\n"
            f"Error: <code>{escape(str(e)[:500])}</code>"
        )


def format_vpn_status() -> str:
    vpn = _load_service_health().get("vpn", {})
    lines = ["🛡️ <b>VPN Evidence</b>"]
    for label in ("android", "ios"):
        item = vpn.get(label)
        title = "Android" if label == "android" else "iPhone"
        if not item:
            lines.append(f"\n<b>{title}</b>: no recent API observation")
            continue

        lines.extend([
            f"\n<b>{title}</b>",
            f"Last seen: {_format_age(item.get('at'))}",
            f"Endpoint: <code>{escape(str(item.get('endpoint', 'unknown')))}</code>",
            f"Remote IP: <code>{escape(str(item.get('remote_ip', 'unknown')))}</code>",
            f"Client reported VPN: <code>{escape(str(item.get('vpn_active')))}</code>",
            f"Local check: <code>{escape(str(item.get('vpn_check') or 'no detail'))}</code>",
            f"Reliable local check: <code>{escape(str(item.get('vpn_check_reliable')))}</code>",
            f"Network evidence: <code>{escape(str(item.get('evidence')))} / {escape(str(item.get('evidence_detail')))}</code>",
        ])

    lines.append(
        "\nNon-VPN countries: <code>"
        + escape(",".join(sorted(VPN_OFF_COUNTRY_CODES)) or "none")
        + "</code>"
    )
    return "\n".join(lines)


def maybe_warn_stale_android_heartbeat(bot: TelegramBot) -> bool:
    """Warn once (with cooldown) when the phone stops reaching the server.

    Surfaces silent failure modes — watcher dead, wrong server address,
    blocked network — where photos pile up in the phone's offline queue
    with no visible signal. Only fires for a heartbeat that existed and
    went stale; a never-connected setup is a setup task, not an outage.
    """
    global _last_stale_heartbeat_warning_at
    if HEARTBEAT_STALE_WARNING_HOURS <= 0:
        return False

    last_ping = database.get_last_android_heartbeat()
    if not last_ping:
        return False

    try:
        last_ping_dt = datetime.fromisoformat(last_ping)
    except ValueError:
        return False

    age = datetime.now() - last_ping_dt
    if age.total_seconds() < HEARTBEAT_STALE_WARNING_HOURS * 3600:
        return False

    now = datetime.now()
    if (_last_stale_heartbeat_warning_at is not None
            and (now - _last_stale_heartbeat_warning_at).total_seconds()
            < HEARTBEAT_STALE_WARNING_COOLDOWN_HOURS * 3600):
        return False
    _last_stale_heartbeat_warning_at = now

    hours = int(age.total_seconds() // 3600)
    log.warning(f"Android heartbeat stale for {hours}h; notifying user.")
    bot.send_message(
        ALLOWED_CHAT_ID,
        f"📵 <b>Android watcher hasn't reached the server in about {hours}h</b> "
        f"(last ping <code>{_html(last_ping)}</code>).\n\n"
        "The phone may be offline, the watcher may have stopped, or it can't "
        "reach the server — new photos are piling up in the phone's offline "
        "queue meanwhile.\n\n"
        "Check <code>~/watcher.log</code> on the phone, or <code>/android</code> here.",
    )
    return True


def format_operational_status() -> str:
    health = _load_service_health()
    last_ping = database.get_last_android_heartbeat()
    if not last_ping:
        android_line = "🔴 Android: no heartbeat yet"
    else:
        try:
            last_ping_dt = datetime.fromisoformat(last_ping)
            diff = datetime.now() - last_ping_dt
            mins = int(diff.total_seconds() / 60)
            if mins < 120:
                android_line = f"🟢 Android: online, last ping {mins}m ago"
            else:
                android_line = f"🔴 Android: stale, last ping {mins // 60}h ago"
        except ValueError:
            android_line = f"🔴 Android: invalid heartbeat <code>{_html(last_ping)}</code>"

    failed_count = len(_failed_upload_items())
    pending_count = len(_pending_upload_items())
    ok_24h, fail_24h = _gemini_recent_counts(24)
    gemini = health.get("gemini", {})

    lines = [
        "🧭 <b>CalorieTracker Status</b>",
        android_line,
        f"Gemini: {_gemini_health_label_from(gemini)}",
        f"Gemini 24h: {ok_24h} success(es), {fail_24h} failure(s)",
        f"Last Gemini probe: {_format_age(gemini.get('last_probe_at'))}",
        f"Uploads: {pending_count} pending, {failed_count} failed saved",
    ]

    report = health.get("daily_report", {})
    if report:
        report_icon = "🟢" if report.get("last_ok") else "🔴"
        lines.append(
            f"Daily report: {report_icon} target {report.get('last_target_date', 'unknown')}, "
            f"last attempt {_format_age(report.get('last_attempt_at'))}"
        )

    lines.extend([
        "",
        "Useful commands: <code>/gemini</code>, <code>/queue</code>, <code>/retry_failed latest</code>, <code>/report_status</code>, <code>/vpn</code>",
    ])
    return "\n".join(lines)


def _parse_positive_int(value: str, default: int, min_value: int = 1, max_value: int = 100) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(min_value, min(parsed, max_value))


def _format_upload_file(path: Path) -> str:
    try:
        stat = path.stat()
        age = _format_age(datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"))
        size_kb = max(1, int(stat.st_size / 1024))
        return f"<code>{escape(path.name)}</code> ({size_kb} KB, {age})"
    except OSError as e:
        return f"<code>{escape(path.name)}</code> (stat error: {escape(str(e))})"


def format_queue_status(limit: int = 8) -> str:
    limit = _parse_positive_int(str(limit), 8, 1, 25)
    pending = _pending_upload_items()
    failed = _failed_upload_items()
    lines = [
        "📦 <b>Upload Queue</b>",
        f"Pending analysis: {len(pending)}",
        f"Failed saved: {len(failed)}",
    ]

    if pending:
        lines.append("\n<b>Pending</b>")
        for path in pending[:limit]:
            lines.append(f"• {_format_upload_file(path)}")
        if len(pending) > limit:
            lines.append(f"...and {len(pending) - limit} more pending.")

    if failed:
        lines.append("\n<b>Failed</b>")
        for path in failed[:limit]:
            lines.append(f"• {_format_upload_file(path)}")
        if len(failed) > limit:
            lines.append(f"...and {len(failed) - limit} more failed.")

    if failed:
        lines.append("\nRetry with <code>/retry_failed latest</code> or <code>/retry_all_failed 3</code>.")
    return "\n".join(lines)


def clear_failed_uploads(selector: str = "latest", confirmed: bool = False) -> str:
    if not confirmed:
        return (
            "⚠️ <b>Confirmation required.</b>\n\n"
            "This deletes saved failed upload files from the server.\n"
            "Use <code>/clear_failed latest confirm</code>, "
            "<code>/clear_failed &lt;hash/name&gt; confirm</code>, or "
            "<code>/clear_failed all confirm</code>."
        )

    selector = (selector or "latest").strip().lower()
    if selector in {"all", "*", "everything"}:
        targets = _failed_upload_items()
    else:
        target = _select_failed_upload(selector)
        targets = [target] if target else []

    if not targets:
        return "✅ No matching failed upload files to delete."

    removed = []
    errors = []
    for path in targets:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as e:
            errors.append(f"{path.name}: {e}")
            continue
        # Tombstone the 'failed' ingestion row (status 'deleted') so /reconcile
        # keeps suppressing the photo while a deliberate re-send can reclaim it.
        match = re.search(r"_([0-9a-fA-F]{12})(?:\.[^.]+)?$", path.name)
        if match:
            database.discard_failed_photo_hashes_by_prefix(ALLOWED_CHAT_ID, match.group(1).lower())

    lines = [f"🧹 Removed {len(removed)} failed upload file(s)."]
    for name in removed[:10]:
        lines.append(f"• <code>{escape(name)}</code>")
    if len(removed) > 10:
        lines.append(f"...and {len(removed) - 10} more.")
    if errors:
        lines.append("\nErrors:")
        for error in errors[:5]:
            lines.append(f"• <code>{escape(error)}</code>")
    return "\n".join(lines)


def _local_time_from_timezone_offset(tz_str: str) -> Optional[datetime]:
    offset = database.parse_timezone_offset(tz_str)
    if offset is None:
        return None
    return datetime.now(timezone.utc).replace(tzinfo=None) + offset


def format_android_status() -> str:
    last_ping = database.get_last_android_heartbeat()
    timezone_value = database.get_android_timezone()
    lines = ["🤖 <b>Android Watcher</b>"]

    if not last_ping:
        lines.append("Status: 🔴 no heartbeat yet")
    else:
        try:
            last_ping_dt = datetime.fromisoformat(last_ping)
            minutes = int((datetime.now() - last_ping_dt).total_seconds() / 60)
            status = "🟢 online" if minutes < 120 else "🔴 stale"
            lines.append(f"Status: {status}")
            lines.append(f"Last ping: {_format_age(last_ping)}")
        except ValueError:
            lines.append(f"Last ping: <code>{escape(last_ping)}</code>")

    lines.append(f"Timezone: <code>{escape(timezone_value)}</code>")
    local_time = _local_time_from_timezone_offset(timezone_value)
    if local_time:
        lines.append(f"Phone local time estimate: <code>{local_time.strftime('%Y-%m-%d %H:%M')}</code>")

    vpn = _load_service_health().get("vpn", {}).get("android")
    if vpn:
        lines.append(f"Last API endpoint: <code>{escape(str(vpn.get('endpoint', 'unknown')))}</code>")
        lines.append(f"VPN evidence: <code>{escape(str(vpn.get('evidence_detail', 'unknown')))}</code>")

    lines.append("Manual phone test: <code>python3 ~/upload_photo.py --ping</code>")
    return "\n".join(lines)


def _parse_report_date_selector(selector: str) -> str:
    normalized = (selector or "today").strip().lower()
    if normalized in {"", "today"}:
        return database.user_local_now().date().isoformat()
    if normalized in {"yesterday", "last"}:
        return (database.user_local_now().date() - timedelta(days=1)).isoformat()
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError as e:
        raise ValueError("Use today, yesterday, or YYYY-MM-DD.") from e
    return parsed.isoformat()


def generate_report_for_command(selector: str = "today") -> str:
    try:
        target_date = _parse_report_date_selector(selector)
    except ValueError as e:
        return f"❌ {escape(str(e))}"

    try:
        import daily_report

        report = daily_report.generate_report(target_date)
        if not report:
            _record_report_health(False, target_date, error_summary="Report generation returned empty content.")
            return "❌ Report generation returned empty content."

        filepath = daily_report.save_report_file(target_date, report)
        _record_report_health(True, target_date, report_path=str(filepath))
        return (
            f"📊 <b>Manual Daily Report</b>\n"
            f"Target date: <code>{target_date}</code>\n"
            f"Saved: <code>{escape(str(filepath))}</code>\n\n"
            f"{report}"
        )
    except Exception as e:
        _record_report_health(False, target_date, error_summary=str(e))
        log.exception("Manual report generation failed")
        return f"❌ Manual report failed: <code>{escape(str(e)[:500])}</code>"


def format_report_status() -> str:
    report = _load_service_health().get("daily_report", {})
    if not report:
        return "📊 <b>Daily Report Status</b>\n\nNo daily report health data yet."

    icon = "🟢" if report.get("last_ok") else "🔴"
    lines = [
        "📊 <b>Daily Report Status</b>",
        f"Status: {icon} {'OK' if report.get('last_ok') else 'failed'}",
        f"Target date: <code>{escape(str(report.get('last_target_date', 'unknown')))}</code>",
        f"Last source: <code>{escape(str(report.get('last_source', 'unknown')))}</code>",
        f"Last attempt: {_format_age(report.get('last_attempt_at'))}",
        f"Last success: {_format_age(report.get('last_success_at'))}",
        f"Consecutive failures: {int(report.get('consecutive_failures', 0))}",
    ]
    if report.get("last_report_path"):
        lines.append(f"Saved report: <code>{escape(str(report.get('last_report_path')))}</code>")
    if report.get("last_error_summary"):
        lines.append(f"Last error: <code>{escape(str(report.get('last_error_summary'))[:300])}</code>")
    return "\n".join(lines)


def format_saved_reports(limit: int = 8) -> str:
    limit = _parse_positive_int(str(limit), 8, 1, 25)
    if not REPORTS_DIR.exists():
        return "📁 <b>Saved Reports</b>\n\nNo report directory yet."

    reports = [path for path in _upload_files(REPORTS_DIR) if path.name.startswith("report_") and path.suffix == ".md"]
    if not reports:
        return "📁 <b>Saved Reports</b>\n\nNo saved report files yet."

    lines = [f"📁 <b>Saved Reports</b> ({len(reports)})"]
    for path in reports[:limit]:
        lines.append(f"• {_format_upload_file(path)}")
    if len(reports) > limit:
        lines.append(f"...and {len(reports) - limit} more.")
    return "\n".join(lines)


def format_recent_logs(line_count: int = 30) -> str:
    line_count = _parse_positive_int(str(line_count), 30, 1, 80)
    cmd = [
        "journalctl",
        "-u",
        BOT_SERVICE_NAME,
        "-n",
        str(line_count),
        "--no-pager",
        "--output",
        "short-iso",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=6, check=False)
    except Exception as e:
        return f"🧾 <b>Recent Logs</b>\n\nCould not read journal logs: <code>{escape(str(e))}</code>"

    output = (result.stdout or "").strip()
    if result.returncode != 0:
        error_text = (result.stderr or output or "journalctl failed").strip()
        return (
            "🧾 <b>Recent Logs</b>\n\n"
            f"journalctl exited with {result.returncode}:\n"
            f"<pre>{escape(error_text[-3500:])}</pre>"
        )

    if not output:
        output = "(no recent logs)"
    return (
        f"🧾 <b>Recent Logs</b> ({line_count}, {escape(BOT_SERVICE_NAME)})\n"
        f"<pre>{escape(output[-3500:])}</pre>"
    )


def _dir_state(path: Path) -> str:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_path = path / ".write_test"
        test_path.write_text("ok")
        test_path.unlink(missing_ok=True)
        file_count = len([item for item in path.iterdir() if item.is_file()])
        return f"ok, {file_count} file(s)"
    except Exception as e:
        return f"not writable: {e}"


def format_safe_config() -> str:
    return "\n".join([
        "⚙️ <b>Runtime Config</b>",
        f"Gemini model: <code>{escape(GEMINI_MODEL)}</code>",
        f"Gemini API key set: <code>{bool(GEMINI_API_KEY)}</code>",
        f"Telegram bot token set: <code>{bool(BOT_TOKEN)}</code>",
        f"Telegram chat ID set: <code>{bool(TELEGRAM_CHAT_ID)}</code>",
        f"Android API key set: <code>{bool(ANDROID_API_KEY)}</code>",
        f"Gemini attempts: <code>{GEMINI_ANALYSIS_MAX_ATTEMPTS}</code>",
        f"Retry delay: <code>{GEMINI_RETRY_BASE_DELAY_SECONDS}-{GEMINI_RETRY_MAX_DELAY_SECONDS}s</code>",
        f"Daily quota pause: <code>{GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS}s</code>",
        f"Duplicate window: <code>{DUPLICATE_WINDOW_MINUTES}m</code>",
        f"VPN geo lookup: <code>{VPN_GEO_LOOKUP_ENABLED}</code>",
        f"Non-VPN countries: <code>{escape(','.join(sorted(VPN_OFF_COUNTRY_CODES)) or 'none')}</code>",
        f"Known VPN CIDRs: <code>{len(ANDROID_VPN_REMOTE_CIDRS)}</code>",
        f"Known direct CIDRs: <code>{len(VPN_OFF_REMOTE_CIDRS)}</code>",
        f"Pending dir: <code>{escape(_dir_state(API_UPLOAD_PENDING_DIR))}</code>",
        f"Failed dir: <code>{escape(_dir_state(API_UPLOAD_FAILED_DIR))}</code>",
        f"Reports dir: <code>{escape(_dir_state(REPORTS_DIR))}</code>",
        f"Service name: <code>{escape(BOT_SERVICE_NAME)}</code>",
    ])


def format_database_stats(chat_id: int) -> str:
    local_today = database.user_local_now().date()
    today_str = local_today.isoformat()
    seven_days_ago = (local_today - timedelta(days=6)).isoformat()
    all_meals = database.get_meals(chat_id, "1970-01-01", today_str)
    recent_meals = database.get_meals(chat_id, seven_days_ago, today_str)
    today_meals = get_todays_meals(chat_id)

    food_all = [m for m in all_meals if m.get("analysis", {}).get("is_food")]
    food_recent = [m for m in recent_meals if m.get("analysis", {}).get("is_food")]
    total_calories = sum((m["analysis"].get("total_calories") or 0) for m in food_all)
    recent_calories = sum((m["analysis"].get("total_calories") or 0) for m in food_recent)
    active_days = len({m.get("date") for m in food_all if m.get("date")})
    source_counts = {}
    for meal in food_all:
        source = meal.get("source") or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1

    lines = [
        "📈 <b>Database Stats</b>",
        f"Food meals today: {len(today_meals)}",
        f"Food meals last 7 days: {len(food_recent)}",
        f"Food meals all time: {len(food_all)}",
        f"Raw DB rows all time: {len(all_meals)}",
        f"Calories last 7 days: ~{recent_calories:,}",
        f"Calories all time: ~{total_calories:,}",
    ]
    if active_days:
        lines.append(f"Average per active day: ~{int(total_calories / active_days):,} kcal")
    if source_counts:
        lines.append("\n<b>Sources</b>")
        for source, count in sorted(source_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"• <code>{escape(source)}</code>: {count}")
    return "\n".join(lines)


def format_recent_meals(chat_id: int, limit: int = 10, days: int = 3) -> str:
    limit = _parse_positive_int(str(limit), 10, 1, 25)
    meals = get_recent_meals(chat_id, days=days)
    if not meals:
        return f"🕘 <b>Recent Meals</b>\n\nNo food meals logged in the last {days} days."

    lines = [f"🕘 <b>Recent Meals</b> (last {days} days)"]
    start = max(0, len(meals) - limit)
    for index, meal in list(enumerate(meals))[start:]:
        analysis = meal.get("analysis", {})
        desc = analysis.get("meal_description", "Unknown")
        calories = analysis.get("total_calories") or 0
        corrected = " ✏️" if meal.get("corrected") else ""
        lines.append(
            f"[{index}] <b>{escape(str(desc))}</b> "
            f"({escape(str(meal.get('date', '?')))} {escape(str(meal.get('time', '?')))}){corrected}"
        )
        lines.append(f"    ~{calories} kcal")

    lines.append("\nThese indexes match natural-language correction/delete context.")
    return "\n".join(lines)


def run_doctor(gemini_client) -> str:
    lines = ["🩺 <b>CalorieTracker Doctor</b>"]

    try:
        local_today_str = database.user_local_now().date().isoformat()
        database.get_meals(ALLOWED_CHAT_ID, local_today_str, local_today_str)
        lines.append("✅ Database readable")
    except Exception as e:
        lines.append(f"❌ Database error: <code>{escape(str(e)[:220])}</code>")

    for label, path in (
        ("pending uploads", API_UPLOAD_PENDING_DIR),
        ("failed uploads", API_UPLOAD_FAILED_DIR),
        ("reports", REPORTS_DIR),
    ):
        state = _dir_state(path)
        icon = "✅" if state.startswith("ok") else "❌"
        lines.append(f"{icon} {label}: <code>{escape(state)}</code>")

    lines.append("✅ Telegram token configured" if BOT_TOKEN else "❌ Telegram token missing")
    lines.append("✅ Telegram chat configured" if TELEGRAM_CHAT_ID else "❌ Telegram chat ID missing")
    lines.append("✅ Gemini key configured" if GEMINI_API_KEY else "❌ Gemini key missing")

    gemini_probe = run_gemini_probe(gemini_client)
    probe_ok = "Gemini probe OK" in gemini_probe
    lines.append("✅ Gemini live probe OK" if probe_ok else "❌ Gemini live probe failed")

    report = _load_service_health().get("daily_report", {})
    if report:
        icon = "✅" if report.get("last_ok") else "❌"
        lines.append(f"{icon} Last report target: <code>{escape(str(report.get('last_target_date', 'unknown')))}</code>")
    else:
        lines.append("⚪ Daily report health not recorded yet")

    return "\n".join(lines)


def send_long_message(bot: TelegramBot, chat_id: int, text: str):
    for chunk in telegram_message_chunks(text):
        bot.send_message(chat_id, chunk)


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
    lines.append(f"🍽️ <b>{_html(desc)}</b>\n")

    for item in analysis.get("food_items", []):
        name = item.get("name", "?")
        cals = item.get("estimated_calories", "?")
        p = item.get("protein_g") or 0
        c = item.get("carbs_g") or 0
        f = item.get("fat_g") or 0
        lines.append(f"  • {_html(name)}: ~{_html(cals)} kcal")
        lines.append(f"    P:{p}g | C:{c}g | F:{f}g")

    total = analysis.get("total_calories") or "?"
    tp = analysis.get("total_protein_g") or 0
    tc = analysis.get("total_carbs_g") or 0
    tf = analysis.get("total_fat_g") or 0
    lines.append(f"\n📊 <b>This meal: ~{_html(total)} kcal</b>")
    lines.append(f"🥩 P: {_html(tp)}g | 🍞 C: {_html(tc)}g | 🧈 F: {_html(tf)}g")

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
        lines.append(f"{i + 1}. <b>{_html(desc)}</b> ({_html(meal.get('time', '?'))}){corrected}")
        lines.append(f"   ~{cal} kcal | P:{p}g C:{c}g F:{f}g")

    lines.append(f"\n━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔥 <b>Total: ~{total_cal:,} kcal</b> ({len(meals)} meals)")
    return "\n".join(lines)


def format_history(chat_id: int, days: int = 7) -> str:
    """Format a summary of daily calorie totals over the past week."""
    local_today = database.user_local_now().date()
    # days - 1 back plus today = an inclusive N-day window
    cutoff_date = (local_today - timedelta(days=days - 1)).isoformat()
    today_str = local_today.isoformat()
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
            if d == today_str:
                friendly_date = "Today"
        except ValueError:
            friendly_date = _html(d)
        lines.append(f"• {friendly_date}: <b>~{daily_cals[d]} kcal</b>")
        
    avg = sum(daily_cals.values()) / len(daily_cals)
    lines.append(f"\n📊 <b>Average:</b> ~{int(avg)} kcal / day")
    return "\n".join(lines)


def handle_text_message(
    gemini_client: genai.Client,
    bot: TelegramBot,
    chat_id: int,
    text: str,
):
    """Process a text message as either a new meal, correction, or chat."""
    pause_summary = _gemini_quota_pause_summary()
    if pause_summary:
        bot.send_message(
            chat_id,
            "⏸️ <b>Gemini is paused right now.</b>\n\n"
            f"{pause_summary}\n\n"
            "Text corrections and manual meal parsing need Gemini too, so I did not send this request.",
        )
        return

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

        # Update by the DB id from the snapshot Gemini indexed, so a meal
        # logged mid-conversation cannot shift the target.
        meal_id = meals[meal_index]["id"]
        database.update_meal_analysis(meal_id, chat_id, new_analysis)

        # Format reply with diff
        diff = new_cal - old_cal
        diff_str = f"+{diff}" if diff > 0 else str(diff)
        reply_lines = [
            f"✏️ <b>Corrected meal {meal_index + 1}!</b>",
            f"",
            f"<b>{_html(old_desc)}</b> → <b>{_html(new_desc)}</b>",
            f"🔥 {_html(old_cal)} kcal → {_html(new_cal)} kcal ({_html(diff_str)})",
        ]
        if reason:
            reply_lines.append(f"\n💬 {_html(reason)}")

        bot.send_message(chat_id, "\n".join(reply_lines))
        log.info(f"  ✏️ Corrected meal {meal_index + 1} (DB ID {meal_id}): {old_cal} → {new_cal} kcal")

    elif intent == "delete":
        meal_indices = result.get("meal_indices", [])
        reason = result.get("reason", "")

        if not meals:
            bot.send_message(chat_id, "❌ Cannot delete because no meals are logged recently.")
            return

        if not meal_indices:
            bot.send_message(chat_id, "❌ Didn't catch which meals to delete. Try being more specific.")
            return

        # Resolve DB ids from the snapshot Gemini indexed, then ask for
        # confirmation before destroying any rows.
        ids = []
        labels = []
        for index in sorted(set(meal_indices)):
            if 0 <= index < len(meals):
                meal = meals[index]
                analysis = meal.get("analysis", {})
                ids.append(meal["id"])
                labels.append(
                    f"{analysis.get('meal_description', 'Unknown meal')} "
                    f"({meal.get('date', '?')} {meal.get('time', '?')}, "
                    f"~{analysis.get('total_calories') or 0} kcal)"
                )

        if not ids:
            bot.send_message(chat_id, "❌ Couldn't match those meals to the recent list.")
            return

        # Nonce binds the confirmation buttons to THIS message, so a stale
        # Delete button from an older confirmation cannot fire the newest
        # pending set (e.g. "delete the toast" deleting all of today's meals).
        token = uuid.uuid4().hex[:8]
        _pending_nl_deletes[chat_id] = {"ids": ids, "labels": labels, "at": datetime.now(), "token": token}
        msg_lines = [f"🗑️ <b>Delete {len(ids)} meal(s)?</b>", ""]
        for label in labels:
            msg_lines.append(f"• {_html(label)}")
        if reason:
            msg_lines.append(f"\n💬 {_html(reason)}")
        msg_lines.append("\nThis cannot be undone.")
        bot.send_message(
            chat_id,
            "\n".join(msg_lines),
            reply_markup={
                "inline_keyboard": [[
                    {"text": "✅ Delete", "callback_data": f"nl_delete_confirm:{token}"},
                    {"text": "❌ Cancel", "callback_data": f"nl_delete_cancel:{token}"},
                ]]
            },
        )

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
        bot.send_message(chat_id, _html(reply))
        log.info(f"  💬 Chat response sent")


def handle_callback_query(gemini_client, bot: TelegramBot, callback_query: Dict) -> bool:
    """Handle Telegram inline-button actions."""
    callback_id = callback_query.get("id", "")
    data = callback_query.get("data", "")
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id") or callback_query.get("from", {}).get("id")

    if chat_id != ALLOWED_CHAT_ID:
        bot.answer_callback_query(callback_id, "Not authorized.")
        return True

    if data.startswith("quota_keep:"):
        selector = data.split(":", 1)[1] or "latest"
        bot.answer_callback_query(callback_id, "Kept for retry later.")
        bot.send_message(
            chat_id,
            "⏸️ Kept the saved photo for later retry.\n\n"
            f"Wait and analyze later with <code>/retry_failed {escape(selector)}</code>.\n"
            f"Give up/delete it with <code>/clear_failed {escape(selector)} confirm</code>.",
        )
        return True

    if data.startswith("quota_discard:"):
        selector = data.split(":", 1)[1] or "latest"
        bot.answer_callback_query(callback_id, "Deleting saved photo.")
        bot.send_message(chat_id, clear_failed_uploads(selector, confirmed=True))
        return True

    if data.split(":", 1)[0] == "nl_delete_confirm":
        token = data.split(":", 1)[1] if ":" in data else ""
        pending = _pending_nl_deletes.get(chat_id)
        if not pending:
            bot.answer_callback_query(callback_id, "Nothing to delete.")
            bot.send_message(chat_id, "⌛ That delete request expired or was already handled. Nothing was deleted.")
            return True
        if pending.get("token") != token:
            # Stale button from an older confirmation message: never execute
            # the newer pending set, and leave it intact for its own buttons.
            bot.answer_callback_query(callback_id, "That delete request expired or was superseded.")
            bot.send_message(chat_id, "⌛ That delete request expired or was superseded. Nothing was deleted.")
            return True
        if (datetime.now() - pending["at"]).total_seconds() > NL_DELETE_CONFIRM_TTL_SECONDS:
            _pending_nl_deletes.pop(chat_id, None)
            bot.answer_callback_query(callback_id, "Nothing to delete.")
            bot.send_message(chat_id, "⌛ That delete request expired or was already handled. Nothing was deleted.")
            return True
        _pending_nl_deletes.pop(chat_id, None)
        for meal_id in pending["ids"]:
            database.delete_meal(meal_id, chat_id)
        bot.answer_callback_query(callback_id, "Deleted.")
        msg_lines = [f"🗑️ <b>Deleted {len(pending['ids'])} meal(s):</b>"]
        for label in pending["labels"]:
            msg_lines.append(f"• {_html(label)}")
        bot.send_message(chat_id, "\n".join(msg_lines))
        log.info(f"  🗑️ NL delete confirmed for meal ids {pending['ids']}")
        return True

    if data.split(":", 1)[0] == "nl_delete_cancel":
        token = data.split(":", 1)[1] if ":" in data else ""
        pending = _pending_nl_deletes.get(chat_id)
        if pending is not None and pending.get("token") != token:
            # A stale Cancel button must not discard the newer pending request.
            bot.answer_callback_query(callback_id, "That delete request expired or was superseded.")
            return True
        _pending_nl_deletes.pop(chat_id, None)
        bot.answer_callback_query(callback_id, "Cancelled.")
        bot.send_message(chat_id, "👍 Cancelled — nothing was deleted.")
        return True

    bot.answer_callback_query(callback_id, "Unknown action.")
    return False


# ─── Flask REST API ───────────────────────────────────────────────
def _build_api_app(bot: TelegramBot, gemini_client) -> Flask:
    """Build the phone upload/heartbeat API app (module-level for testability)."""
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = MAX_API_UPLOAD_BYTES

    @app.errorhandler(413)
    def payload_too_large(error):
        return jsonify({"error": "Photo too large"}), 413

    @app.route('/ping', methods=['POST'])
    def ping():
        if not _authorized_api_request():
            return jsonify({"error": "Unauthorized"}), 401

        maybe_warn_android_vpn_inactive(bot, "/ping")

        # None preserves the stored offset; meal dating depends on it.
        tz = None
        if request.is_json:
            data = request.get_json(silent=True)
            if data and "timezone" in data:
                tz = data["timezone"]

        database.update_android_heartbeat(timezone=tz)
        log.info(f"📡 Heartbeat ping received from Android Watcher (TZ: {tz or 'unchanged'})")
        return jsonify({"status": "ok"})

    @app.route('/reconcile', methods=['POST'])
    def reconcile():
        if not _authorized_api_request():
            return jsonify({"error": "Unauthorized"}), 401

        maybe_warn_android_vpn_inactive(bot, "/reconcile")

        data = request.get_json(silent=True)
        if not data or 'hashes' not in data:
            return jsonify({"error": "Missing hashes array"}), 400

        android_hashes = set(data['hashes'])
        server_hashes = set(database.get_today_hashes(ALLOWED_CHAT_ID))
        reserved_hashes = set(database.get_reserved_photo_hashes(ALLOWED_CHAT_ID))

        # Missing hashes are those on Android but not logged, pending, or saved for retry.
        missing_hashes = _reconcile_missing_hashes(android_hashes, server_hashes, reserved_hashes)

        log.info(f"🔄 Reconcile Sync: Android sent {len(android_hashes)} hashes. Server is missing {len(missing_hashes)}.")
        return jsonify({"missing_hashes": missing_hashes})

    @app.route('/upload', methods=['POST'])
    def upload():
        if not _authorized_api_request():
            return jsonify({"error": "Unauthorized"}), 401

        if 'photo' not in request.files:
            return jsonify({"error": "No photo provided"}), 400

        file = request.files['photo']
        if request.content_length and request.content_length > MAX_API_UPLOAD_BYTES:
            return jsonify({"error": "Photo too large"}), 413

        image_bytes = file.stream.read(MAX_API_UPLOAD_BYTES + 1)
        if len(image_bytes) > MAX_API_UPLOAD_BYTES:
            return jsonify({"error": "Photo too large"}), 413
        if not image_bytes:
            return jsonify({"error": "Empty photo"}), 400

        img_hash = hashlib.md5(image_bytes).hexdigest()

        existing_failed = _find_failed_upload_by_hash(img_hash)
        if existing_failed:
            log.info(f"  🔄 API upload already saved for later retry: {existing_failed}")
            return jsonify({"status": "already_saved_for_retry"})

        if not database.reserve_photo_hash(ALLOWED_CHAT_ID, img_hash, "api_upload"):
            log.info(f"  🔄 API upload already reserved or logged for hash {img_hash}, skipping")
            return jsonify({"status": "duplicate"})

        device_name = _api_upload_device_name_from_request()
        client_platform = request.headers.get("X-Client-Platform", "").strip().lower()
        device_label = device_name.lower()

        if client_platform == "android" or device_name == "Android":
            maybe_warn_android_vpn_inactive(bot, "/upload")
        elif client_platform in {"ios", "iphone", "ipad"} or "iphone" in device_label or "ipad" in device_label:
            maybe_warn_ios_vpn_unverified(bot, "/upload")

        if not _begin_api_upload_processing(img_hash):
            log.info(f"  🔄 API upload already processing for hash {img_hash}, skipping duplicate trigger")
            database.release_photo_hash(ALLOWED_CHAT_ID, img_hash)
            return jsonify({"status": "already_processing"})

        try:
            upload_path = _stage_api_upload(image_bytes, img_hash, file.filename or "upload")
        except OSError as e:
            database.release_photo_hash(ALLOWED_CHAT_ID, img_hash)
            _finish_api_upload_processing(img_hash)
            log.error(f"  ❌ API Upload: Could not stage upload: {e}")
            return jsonify({"error": "Could not stage upload"}), 500

        # Process the photo in a background thread to return 200 OK instantly to iOS
        def background_process(upload_file_path, hsh, device):
            device_display = _html(device)
            staged_path = Path(upload_file_path)
            processing_msg = None
            try:
                log.info(f"🔍 Analyzing food from {device} API upload in background...")

                # Send instant feedback to the user so they know the watcher worked
                processing_msg = bot.send_message(ALLOWED_CHAT_ID, f"📲 Received photo from {device_display}, analyzing... 🔍")

                try:
                    bytes_data = staged_path.read_bytes()
                except OSError as e:
                    log.error(f"  ❌ API Upload: Could not read staged upload: {e}")
                    if processing_msg and processing_msg.get("message_id"):
                        bot.delete_message(ALLOWED_CHAT_ID, processing_msg["message_id"])
                    database.release_photo_hash(ALLOWED_CHAT_ID, hsh)
                    bot.send_message(
                        ALLOWED_CHAT_ID,
                        f"⚠️ <b>Auto-upload from {device_display} failed before analysis.</b>\n\n"
                        "The server could not read the staged image file, so it was not logged.",
                    )
                    return

                analysis = analyze_food_photo_with_retries(gemini_client, bytes_data)

                # Delete the temporary processing message
                if processing_msg and processing_msg.get("message_id"):
                    bot.delete_message(ALLOWED_CHAT_ID, processing_msg["message_id"])

                if analysis is None:
                    failed_path = _keep_failed_api_upload(staged_path, hsh)
                    database.mark_photo_hash_status(ALLOWED_CHAT_ID, hsh, "failed", source="api_upload")
                    log.error(f"  ❌ API Upload: Analysis failed; kept upload at {failed_path}")
                    if _gemini_quota_pause():
                        _send_saved_upload_decision(
                            bot,
                            ALLOWED_CHAT_ID,
                            failed_path,
                            hsh,
                            source_label=device,
                        )
                    else:
                        bot.send_message(
                            ALLOWED_CHAT_ID,
                            f"⚠️ <b>Auto-upload from {device_display} could not be analyzed.</b>\n\n"
                            f"I retried Gemini {GEMINI_ANALYSIS_MAX_ATTEMPTS} time(s), but it still failed.\n\n"
                            f"{_gemini_failure_context()}\n\n"
                            "The photo was not logged, but I kept it on the server for manual retry/debugging:\n"
                            f"<code>{_html(failed_path)}</code>\n"
                            f"Hash: <code>{_html(hsh)}</code>\n\n"
                            f"Wait/retry later: <code>/retry_failed {_html(_failed_upload_selector(failed_path, hsh))}</code>\n"
                            f"Give up/delete: <code>/clear_failed {_html(_failed_upload_selector(failed_path, hsh))} confirm</code>.",
                        )
                    return

                if analysis.get("is_food"):
                    meal_id = save_meal(ALLOWED_CHAT_ID, analysis, "api_auto", "api", hsh)
                    database.mark_photo_hash_status(ALLOWED_CHAT_ID, hsh, "saved", meal_id, source="api_auto")
                    log.info(f"  ✅ API Food: {analysis.get('meal_description')} (~{analysis.get('total_calories')} kcal)")
                    result_text = format_food_result(ALLOWED_CHAT_ID, analysis)
                    bot.send_message(ALLOWED_CHAT_ID, f"📲 <b>Auto-Logged from {device_display}:</b>\n\n" + result_text)
                    _discard_api_upload(staged_path)
                else:
                    log.info("  ⏭️ API Upload: Not food")
                    database.mark_photo_hash_status(ALLOWED_CHAT_ID, hsh, "skipped", source="api_auto")
                    _discard_api_upload(staged_path)
            except Exception as e:
                log.exception(f"Unhandled API upload background error for {hsh}")
                if processing_msg and processing_msg.get("message_id"):
                    bot.delete_message(ALLOWED_CHAT_ID, processing_msg["message_id"])
                failed_path = staged_path
                if staged_path.exists():
                    failed_path = _keep_failed_api_upload(staged_path, hsh)
                    database.mark_photo_hash_status(ALLOWED_CHAT_ID, hsh, "failed", source="api_upload")
                else:
                    database.release_photo_hash(ALLOWED_CHAT_ID, hsh)
                bot.send_message(
                    ALLOWED_CHAT_ID,
                    f"⚠️ <b>Auto-upload from {device_display} hit an internal server error.</b>\n\n"
                    f"Error: <code>{_html(str(e)[:300])}</code>\n"
                    f"Saved file: <code>{_html(failed_path)}</code>\n"
                    f"Hash: <code>{_html(hsh)}</code>",
                )
            finally:
                _finish_api_upload_processing(hsh)

        threading.Thread(target=background_process, args=(str(upload_path), img_hash, device_name)).start()

        return jsonify({"status": "processing_in_background"})

    return app


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def _sweep_stranded_pending_uploads(bot: TelegramBot):
    """Recover uploads stranded mid-processing by a previous shutdown.

    Anything still in the pending dir at boot was never analyzed; move it to
    the failed dir so /retry_failed can see it instead of it sitting invisible.
    """
    moved = []
    for path in _pending_upload_items():
        try:
            img_hash = hashlib.md5(path.read_bytes()).hexdigest()
        except OSError as e:
            log.warning(f"Could not read stranded pending upload {path}: {e}")
            continue
        failed_path = _keep_failed_api_upload(path, img_hash)
        database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "failed", source="startup_sweep")
        moved.append(failed_path.name)

    # A crash between reserving a hash and finishing analysis leaves a
    # 'processing' row with no file behind it; phone retries then get
    # "duplicate" (and delete their queued copy) until the reservation goes
    # stale. Nothing can be legitimately in flight at boot, so release any
    # reservation that has no staged or failed file backing it.
    failed_prefixes = _failed_upload_hash_prefixes()
    released = 0
    for img_hash in database.get_processing_photo_hashes(ALLOWED_CHAT_ID):
        if _image_hash_prefix(img_hash) in failed_prefixes:
            continue
        database.release_photo_hash(ALLOWED_CHAT_ID, img_hash)
        released += 1
    if released:
        log.info(f"♻️ Released {released} orphaned in-flight photo reservation(s) at boot.")

    if not moved:
        return

    lines = [
        f"♻️ <b>Recovered {len(moved)} upload(s) stranded by the last shutdown.</b>",
        "They were pending analysis and are now saved as failed uploads:",
    ]
    for name in moved[:10]:
        lines.append(f"• <code>{escape(name)}</code>")
    if len(moved) > 10:
        lines.append(f"...and {len(moved) - 10} more.")
    lines.append("\nRecover them with <code>/retry_failed latest</code> or <code>/retry_all_failed 3</code>.")
    bot.send_message(ALLOWED_CHAT_ID, "\n".join(lines))


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

    if not ALLOWED_CHAT_ID:
        log.error(
            "TELEGRAM_CHAT_ID not set or invalid. "
            "Set it to the numeric Telegram chat ID allowed to use this private bot."
        )
        sys.exit(1)

    if not ANDROID_API_KEY:
        log.error(
            "ANDROID_API_KEY not set. "
            "Generate a random value and set it on both the server and phone clients."
        )
        sys.exit(1)

    # Initialize
    bot = TelegramBot(BOT_TOKEN)
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

    # systemd stop sends SIGTERM; raising KeyboardInterrupt on the main thread
    # reuses the existing shutdown path, and the interpreter then joins the
    # non-daemon upload threads so in-flight uploads finish before exit.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    _sweep_stranded_pending_uploads(bot)

    app = _build_api_app(bot, gemini_client)

    # Start Flask in a background thread
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False), daemon=True).start()
    log.info("🚀 Flask REST API started on port 5000")
    
    log.info("Bot is running! Listening for corrections and commands.")
    log.info("Press Ctrl+C to stop.\n")

    try:
        while True:
            updates = bot.get_updates(timeout=30)
            maybe_warn_stale_android_heartbeat(bot)

            for update in updates:
                callback_query = update.get("callback_query")
                if callback_query:
                    handle_callback_query(gemini_client, bot, callback_query)
                    continue

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
                command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
                args = text.split()[1:] if text.startswith("/") else []

                # ─── Commands ────────────────────────────────────
                if command in {"/start", "/help"}:
                    log.info(f"[{user}] /help")
                    bot.send_message(chat_id, HELP_TEXT)
                    continue

                if command == "/commands":
                    log.info(f"[{user}] /commands")
                    bot.send_message(chat_id, COMMAND_MENU_TEXT)
                    continue

                if command == "/today":
                    log.info(f"[{user}] /today")
                    bot.send_message(chat_id, format_today_summary(chat_id))
                    continue

                if command == "/meals":
                    log.info(f"[{user}] /meals")
                    bot.send_message(chat_id, format_meals_list(chat_id))
                    continue

                if command == "/recent":
                    limit = _parse_positive_int(args[0], 10, 1, 25) if args else 10
                    log.info(f"[{user}] /recent {limit}")
                    bot.send_message(chat_id, format_recent_meals(chat_id, limit=limit))
                    continue
                    
                if command == "/history":
                    days = _parse_positive_int(args[0], 7, 1, 60) if args else 7
                    log.info(f"[{user}] /history {days}")
                    bot.send_message(chat_id, format_history(chat_id, days=days))
                    continue

                if command == "/ping_android":
                    # Silent heartbeat ping
                    database.update_android_heartbeat()
                    continue

                if command == "/status":
                    log.info(f"[{user}] /status")
                    bot.send_message(chat_id, format_operational_status())
                    continue

                if command == "/doctor":
                    log.info(f"[{user}] /doctor")
                    bot.send_message(chat_id, "🩺 Running self-check...")
                    bot.send_message(chat_id, run_doctor(gemini_client))
                    continue

                if command == "/gemini":
                    log.info(f"[{user}] /gemini")
                    bot.send_message(chat_id, "🔎 Running Gemini probe...")
                    bot.send_message(chat_id, run_gemini_probe(gemini_client))
                    continue

                if command == "/android":
                    log.info(f"[{user}] /android")
                    bot.send_message(chat_id, format_android_status())
                    continue

                if command == "/queue":
                    limit = _parse_positive_int(args[0], 8, 1, 25) if args else 8
                    log.info(f"[{user}] /queue {limit}")
                    bot.send_message(chat_id, format_queue_status(limit))
                    continue

                if command == "/failed":
                    log.info(f"[{user}] /failed")
                    bot.send_message(chat_id, format_failed_uploads())
                    continue

                if command == "/retry_failed":
                    selector = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else "latest"
                    log.info(f"[{user}] /retry_failed {selector}")
                    bot.send_message(chat_id, f"🔁 Retrying failed upload: <code>{escape(selector)}</code>")
                    bot.send_message(chat_id, retry_failed_upload(gemini_client, selector))
                    continue

                if command == "/retry_all_failed":
                    limit = _parse_positive_int(args[0], 3, 1, RETRY_ALL_FAILED_MAX) if args else 3
                    log.info(f"[{user}] /retry_all_failed {limit}")
                    bot.send_message(chat_id, f"🔁 Retrying up to {limit} failed saved upload(s)...")
                    bot.send_message(chat_id, retry_all_failed_uploads(gemini_client, limit))
                    continue

                if command == "/clear_failed":
                    confirmed = bool(args) and args[-1].lower() == "confirm"
                    selector_parts = args[:-1] if confirmed else args
                    selector = " ".join(selector_parts) or "latest"
                    log.info(f"[{user}] /clear_failed {selector} confirmed={confirmed}")
                    bot.send_message(chat_id, clear_failed_uploads(selector, confirmed=confirmed))
                    continue

                if command == "/vpn":
                    log.info(f"[{user}] /vpn")
                    bot.send_message(chat_id, format_vpn_status())
                    continue

                if command == "/report":
                    selector = args[0] if args else "today"
                    log.info(f"[{user}] /report {selector}")
                    bot.send_message(chat_id, f"📊 Generating report for <code>{escape(selector)}</code>...")
                    send_long_message(bot, chat_id, generate_report_for_command(selector))
                    continue

                if command == "/report_status":
                    log.info(f"[{user}] /report_status")
                    bot.send_message(chat_id, format_report_status())
                    continue

                if command == "/reports":
                    limit = _parse_positive_int(args[0], 8, 1, 25) if args else 8
                    log.info(f"[{user}] /reports {limit}")
                    bot.send_message(chat_id, format_saved_reports(limit))
                    continue

                if command == "/logs":
                    line_count = _parse_positive_int(args[0], 30, 1, 80) if args else 30
                    log.info(f"[{user}] /logs {line_count}")
                    send_long_message(bot, chat_id, format_recent_logs(line_count))
                    continue

                if command == "/config":
                    log.info(f"[{user}] /config")
                    bot.send_message(chat_id, format_safe_config())
                    continue

                if command == "/stats":
                    log.info(f"[{user}] /stats")
                    bot.send_message(chat_id, format_database_stats(chat_id))
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
                    img_hash = ""
                    reserved_photo = False
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

                        # A deliberate human re-send may re-log a photo that was
                        # previously skipped, failed, or whose meal was deleted;
                        # the automated /upload path stays strict.
                        if not database.reserve_photo_hash(
                            chat_id,
                            img_hash,
                            "telegram",
                            reclaim_statuses={"failed", "skipped", "deleted"},
                        ):
                            log.info(f"  🔄 Photo already reserved or logged, skipping")
                            bot.send_message(
                                chat_id,
                                "🔄 This photo is already being processed or was already logged.\n"
                                "It won't be counted twice.",
                            )
                            continue
                        reserved_photo = True

                        processing_msg = bot.send_message(chat_id, "🔍 Processing image... one moment!")
                        analysis = analyze_food_photo(gemini_client, image_bytes)
                        if processing_msg and processing_msg.get("message_id"):
                            bot.delete_message(chat_id, processing_msg["message_id"])

                        if analysis is None:
                            if _gemini_quota_pause():
                                try:
                                    staged_path = _stage_api_upload(image_bytes, img_hash, "telegram_photo.jpg")
                                    failed_path = _keep_failed_api_upload(staged_path, img_hash)
                                    database.mark_photo_hash_status(chat_id, img_hash, "failed", source="telegram")
                                    reserved_photo = False
                                    _send_saved_upload_decision(
                                        bot,
                                        chat_id,
                                        failed_path,
                                        img_hash,
                                        source_label="Telegram",
                                    )
                                except OSError as e:
                                    database.release_photo_hash(chat_id, img_hash)
                                    reserved_photo = False
                                    log.error(f"Could not save Telegram photo for later retry: {e}")
                                    bot.send_message(
                                        chat_id,
                                        "❌ <b>I couldn't analyze that photo and could not save it for retry.</b>\n\n"
                                        f"{_gemini_failure_context()}",
                                    )
                                continue

                            database.release_photo_hash(chat_id, img_hash)
                            reserved_photo = False
                            bot.send_message(
                                chat_id,
                                "❌ <b>I couldn't analyze that photo.</b>\n\n"
                                f"{_gemini_failure_context()}\n\n"
                                "Try <code>/gemini</code> to run a live check.",
                            )
                            continue

                        if analysis.get("is_food"):
                            meal_id = save_meal(chat_id, analysis, "telegram", file_id, img_hash)
                            database.mark_photo_hash_status(chat_id, img_hash, "saved", meal_id, source="telegram")
                            reserved_photo = False
                            log.info(
                                f"  ✅ Food: {analysis.get('meal_description')} "
                                f"(~{analysis.get('total_calories')} kcal)"
                            )
                            result_text = format_food_result(chat_id, analysis)
                            bot.send_message(chat_id, result_text)
                        else:
                            database.mark_photo_hash_status(chat_id, img_hash, "skipped", source="telegram")
                            reserved_photo = False
                            log.info("  ⏭️ Not food. Silently ignoring.")

                    except Exception as e:
                        if reserved_photo:
                            database.release_photo_hash(chat_id, img_hash)
                        # Defense in depth: network errors can embed the token URL.
                        log.error(f"Error processing photo: {bot._redact(e)}")
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
