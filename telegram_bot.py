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
import random
import re
import signal
import socket
import statistics
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
from google.genai import types
from PIL import Image, ImageOps
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

import claude_analyzer
import database
import fitness_plan
import nutrition
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
from utils import (
    meal_calorie_mismatch,
    parse_ai_json,
    parse_boolish,
    push_wechat,
    safe_food_items,
    safe_number,
    telegram_message_chunks,
    utf16_len,
)

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
# Native JSON mode for JSON-producing Gemini calls: a malformed free-form
# response terminally burns a free-tier request, so ask the API to enforce
# JSON output. parse_ai_json stays as the fallback for wrapped responses.
GEMINI_JSON_CONFIG = types.GenerateContentConfig(response_mime_type="application/json")
API_UPLOAD_PENDING_DIR = Path.home() / "CalorieTracker" / "logs" / "pending_uploads"
API_UPLOAD_FAILED_DIR = Path.home() / "CalorieTracker" / "logs" / "failed_uploads"
SERVICE_HEALTH_PATH = service_health.DEFAULT_PATH

# Echo phone-uploaded photos back into the chat as the meal card's image
# (chat-sent photos are already visible). Default on; set 0/false to disable.
ECHO_UPLOAD_PHOTOS = parse_boolish(os.environ.get("ECHO_UPLOAD_PHOTOS")) is not False
BOT_SERVICE_NAME = os.environ.get("CALORIE_BOT_SERVICE_NAME", "caloriebot.service")
RETRY_ALL_FAILED_MAX = _env_int("RETRY_ALL_FAILED_MAX", 10, 1, 50)
MAX_API_UPLOAD_BYTES = _env_int("MAX_API_UPLOAD_BYTES", 25 * 1024 * 1024, 1024, 100 * 1024 * 1024)
# Admission control for photo-analysis fan-out: every analysis thread pins its
# full image bytes for its whole life, and the threads convoy on the Claude
# CLI lock — a 10-photo album on the 1GB host is an OOM. Slots are taken
# non-blocking; a denied Telegram photo is turned away with a re-send-later
# reply, a denied /upload is kept as a failed upload for /retry_all_failed.
MAX_CONCURRENT_ANALYSES = _env_int("MAX_CONCURRENT_ANALYSES", 3, 1, 16)
_analysis_slots = threading.BoundedSemaphore(MAX_CONCURRENT_ANALYSES)
# Throttle for the "uploads parked, slots busy" chat notice (once a minute —
# an album burst must not turn into an album of notices).
_busy_notice_lock = threading.Lock()
_last_busy_notice_monotonic = float("-inf")
# Gemini calls carry no client-side socket timeout in google-genai 0.3.0, so a
# black-holed connection would hang its caller (the polling thread included)
# forever. All generate_content calls run under this wall-clock deadline.
GEMINI_HTTP_DEADLINE_SECONDS = _env_int("GEMINI_HTTP_DEADLINE_SECONDS", 90, 15, 600)
# Captured at import: tests stub the module-level `threading` name with
# synchronous fakes, and the deadline worker must stay a real thread.
_DEADLINE_THREAD = threading.Thread
# How many days of meals a natural-language correction/delete can reach back
# over (widened from 3 so "earlier this week" resolves, not just "yesterday").
# NOTE: HELP_TEXT's Corrections section states this number — keep them in sync.
TEXT_EDIT_WINDOW_DAYS = _env_int("TEXT_EDIT_WINDOW_DAYS", 7, 1, 31)
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
_remote_ip_country_cache = {}
GEO_CACHE_MAX_ENTRIES = 256
_geo_cache_lock = threading.Lock()
# Pending natural-language deletes awaiting inline-button confirmation.
_pending_nl_deletes: Dict[int, Dict] = {}

HELP_TEXT = """🍽️ <b>CalorieTracker Bot</b>

I log meals from food photos your phone uploads automatically
(iPhone camera Shortcut / Android watcher). You can also send
food photos directly here in the chat!

<b>Commands:</b>
/meals — View today's meals (numbered list)
/recent — View recent meals from the last 3 days
/today — View today's calorie & macro summary
/history — View your calorie totals for the last 7 days
/status — Bot, Android, Gemini, queue, and report health
/commands — Show the full operations command menu
/help — Show this help message

<b>Fitness &amp; diet:</b>
/weight — Log a weigh-in or see your trend
/diet — Set/see your diet mode &amp; macro targets
/macros — Macro check vs your targets
/workout — Log a strength session
/train — Which muscle group is due
/activity — Log calories burned / steps / km
/train_run — Today's run, or set VDOT from a race
/plan — This week's run schedule
/profile — Your merged fitness profile

<b>Photo:</b>
📸 Send a food photo for instant calorie analysis
Add a caption like “half portion” and I'll apply it to the logged meal.

<b>Corrections:</b>
Just type a correction in natural language! I remember your meals from the last 7 days.
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

<b>Fitness &amp; diet</b>
/weight 72.5 — Log a weigh-in (no arg: trend)
/diet balanced — Set diet mode (no arg: plan + targets)
/diet target 2000 150 200 60 — Cal/P/C/F targets
/diet protein 2.0 — Protein grams per kg
/macros today — Macro check (today|week|N)
/workout legs — Log a strength session
/train — Muscle group due next
/activity 450 8000 5 — kcal, steps, km (no arg: net)
/train_run — Today's run (5k 19:57 sets VDOT)
/plan — This week's run schedule
/profile — Merged fitness profile

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


def _acquire_analysis_slot():
    """Non-blocking claim of one photo-analysis slot (MAX_CONCURRENT_ANALYSES).

    Returns a single-release token dict, or None when all slots are busy.
    The token makes the release idempotent: both the spawner (when its worker
    thread never took ownership) and the worker's finally can safely call
    _release_analysis_slot with it.
    """
    return {"held": True} if _analysis_slots.acquire(blocking=False) else None


def _release_analysis_slot(token):
    if not token or not token.pop("held", False):
        return
    try:
        _analysis_slots.release()
    except ValueError:
        # BoundedSemaphore refuses an over-release. Slot bookkeeping is
        # containment, not accounting truth — never crash a worker over it.
        log.warning("Photo-analysis slot over-released; ignoring.")


# Telegram photos parked by the slot-denied branch of handle_photo_message,
# in arrival order. In-memory ONLY as a drain hint: the photos themselves
# are durable failed uploads, so a restart merely downgrades them from
# "auto-drained" to "manual /retry_failed" — nothing is lost.
_parked_photo_lock = threading.Lock()
_parked_photo_paths: List[Path] = []


def _park_photo_for_drain(path: Path):
    with _parked_photo_lock:
        _parked_photo_paths.append(path)


def _drain_one_parked_photo(bot, gemini_client):
    """Best-effort: after a slot release, analyze ONE parked slot-denied photo.

    Called from the analysis workers' finally blocks. Runs only when a slot
    is immediately re-acquirable (never blocks the releasing worker); the
    drain itself happens on a background thread through the existing
    failed-upload retry machinery. A drained photo is popped BEFORE its
    retry and never re-parked on failure — the file simply stays a normal
    failed upload for /retry_failed — so a poison photo cannot drain-loop.
    Each successful drain's own release cascades to the next parked item.
    Totally non-raising: this runs inside finally blocks.
    """
    with _parked_photo_lock:
        path = _parked_photo_paths.pop(0) if _parked_photo_paths else None
    if path is None:
        return
    slot = _acquire_analysis_slot()
    if slot is None:
        # Capacity vanished between the release and here; re-park at the
        # front so the next release picks it up in order.
        with _parked_photo_lock:
            _parked_photo_paths.insert(0, path)
        return

    def _drain_worker():
        try:
            result = _retry_failed_upload_path(gemini_client, path)
            status = result.get("status")
            if status == "logged":
                bot.send_message(
                    ALLOWED_CHAT_ID,
                    "✅ <b>Queued photo analyzed:</b>\n\n"
                    + format_food_result(ALLOWED_CHAT_ID, result["analysis"]),
                )
            elif status in {"not_food", "duplicate", "already_reserved"}:
                log.info(f"  ⏭️ Parked-photo drain: {status} for {path.name}")
            else:
                # The user was told "queued" — a silently-kept failure would
                # read as an eaten photo. One short pointer, no spam.
                bot.send_message(
                    ALLOWED_CHAT_ID,
                    "⚠️ A queued photo could not be analyzed; it is kept as a "
                    "failed upload — retry with <code>/retry_failed</code>.",
                )
        except Exception as e:
            log.error(f"Parked-photo drain failed: {bot._redact(e)}")
        finally:
            _release_analysis_slot(slot)
            _drain_one_parked_photo(bot, gemini_client)

    try:
        worker = threading.Thread(target=_drain_worker)
        worker.start()
    except Exception as e:
        # Thread spawn failing is exactly the memory pressure the slot cap
        # exists for: return the slot, keep the photo parked, stay quiet.
        _release_analysis_slot(slot)
        with _parked_photo_lock:
            _parked_photo_paths.insert(0, path)
        log.error(f"Could not spawn parked-photo drain worker: {e}")
        return
    # Thread doubles that never run the target (test fakes) must not strand
    # the slot; the token release is idempotent — see handle_photo_message.
    if not (hasattr(worker, "is_alive") and worker.is_alive()):
        _release_analysis_slot(slot)


def _generate_content_with_deadline(client, **kwargs):
    """client.models.generate_content bounded by GEMINI_HTTP_DEADLINE_SECONDS.

    The call runs on a daemon worker thread; on deadline the worker is
    abandoned (it may linger until the dead socket errors out, but shutdown
    is never blocked on it) and a RuntimeError is raised. Its message says
    'timeout' so _classify_gemini_error files it as network_service and the
    existing retry machinery treats it as retryable network trouble.
    """
    outcome = {}

    def _run():
        # Total capture: the worker's outcome must reach the caller intact,
        # whatever generate_content raises.
        try:
            outcome["response"] = client.models.generate_content(**kwargs)
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller thread
            outcome["error"] = e

    worker = _DEADLINE_THREAD(target=_run, daemon=True)
    worker.start()
    worker.join(GEMINI_HTTP_DEADLINE_SECONDS)
    if worker.is_alive():
        log.error(
            f"Gemini call exceeded the {GEMINI_HTTP_DEADLINE_SECONDS}s deadline; "
            "abandoning the worker thread."
        )
        raise RuntimeError(
            f"Gemini call exceeded deadline (timeout after {GEMINI_HTTP_DEADLINE_SECONDS}s)"
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["response"]


# Paired Telegram-HTML tags this bot emits; the truncation tag repair only
# needs to understand these.
_TELEGRAM_PAIRED_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|pre|a)(\s[^<>]*)?>", re.IGNORECASE)


def _truncate_telegram_html(text: str) -> str:
    """Cut text to ~4000 UTF-16 units without breaking Telegram HTML.

    Rebuilding from codepoints means a surrogate pair is never split. The tag
    repair then keeps the result parseable — a cut mid-tag ('<b' with no '>')
    or mid-element (unclosed '<b>') would 400 the send and degrade the whole
    message to raw plain text: the partial tag is stripped, and a cut inside
    an element drops back to just before its opener. Dropping (rather than
    appending closers) keeps the result a strict prefix of the original,
    which downstream consumers rely on.
    """
    kept = []
    units = 0
    for ch in text:
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > 4000:
            break
        kept.append(ch)
    truncated = "".join(kept)

    # A cut inside a tag leaves a bare '<...' tail.
    lt = truncated.rfind("<")
    if lt != -1 and ">" not in truncated[lt:]:
        truncated = truncated[:lt]

    # Stack of tags the cut left unclosed, with opener positions. The first
    # unclosed opener is the outermost: everything from it onward belongs to
    # elements the cut broke, so drop back to it for balanced output.
    open_tags = []
    for match in _TELEGRAM_PAIRED_TAG_RE.finditer(truncated):
        name = match.group(2).lower()
        if match.group(1):
            if open_tags and open_tags[-1][0] == name:
                open_tags.pop()
        else:
            open_tags.append((name, match.start()))
    if open_tags:
        truncated = truncated[:open_tags[0][1]]

    return truncated + "\n\n<i>(truncated)</i>"


# ─── Telegram Bot API ─────────────────────────────────────────────
class TelegramConnectionError(RuntimeError):
    """Connection-class Telegram failure (no HTTP response ever arrived).

    Distinct from HTTP-status failures so send paths can retry the IDENTICAL
    request once: a cold keep-alive socket (server closed it while idle —
    RemoteDisconnected) fails exactly one attempt and the very next attempt
    on a fresh connection succeeds, whereas an HTTP 400 would fail the same
    way forever. Message is deliberately token-free.
    """


class TelegramBot:
    """Simple Telegram Bot using HTTP polling (no library needed)."""

    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.offset = 0  # Track last processed update
        self.session = requests.Session()
        # Consecutive getUpdates failures drive the poll backoff; the alert
        # latch sends the outage notice once per streak, re-armed on success.
        # The latch also persists in service_health across restarts; the
        # swept flag makes the first successful poll of each boot clear any
        # stale persisted latch exactly once.
        self._poll_failure_streak = 0
        self._poll_alert_sent = False
        self._poll_latch_swept = False

    def _redact(self, value) -> str:
        """Strip the bot token from text destined for logs or user messages.

        Total by design: several callers invoke this inside last-line-of-
        defense except blocks, so a pathological value whose __str__ raises
        must not turn containment into a new crash.
        """
        try:
            text = str(value)
        except Exception:
            return f"<unprintable {type(value).__name__}>"
        return text.replace(self.token, "<token>") if self.token else text

    def _call(self, method: str, **kwargs) -> dict:
        """Make a Telegram Bot API call."""
        try:
            resp = self.session.post(f"{self.base_url}/{method}", json=kwargs, timeout=60)
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            # Stdlib TimeoutError carries no token URL, and get_updates keys
            # its quiet-retry branch on it.
            raise TimeoutError(f"Telegram timeout calling {method}") from None
        except requests.RequestException as e:
            # requests embeds the full URL (token included) in HTTPError messages,
            # and ConnectionError/ConnectTimeout messages carry it too.
            resp = getattr(e, "response", None)
            if resp is None and isinstance(e, requests.exceptions.ConnectionError):
                # Connection-class only (RemoteDisconnected arrives wrapped in
                # requests' ConnectionError): the typed error lets send paths
                # retry identically. Timeouts were already caught above and
                # HTTPError always carries a response.
                raise TelegramConnectionError(
                    f"Telegram connection error calling {method}"
                ) from None
            status = resp.status_code if resp is not None else "network-error"
            raise RuntimeError(f"Telegram {status} calling {method}") from None
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data.get("result", {})

    # A getUpdates failure streak this long triggers the one-time outage
    # alert: sendMessage still works during a 409/network flap, so the alert
    # is deliverable even while the poll loop itself is dead.
    POLL_ALERT_STREAK = 6

    def _note_poll_failure(self, summary: str, quiet: bool = False):
        """Record one failed getUpdates: streak, health ledger, alert, backoff.

        quiet=True (client timeout) still counts toward the streak — a long
        poll normally returns 200-empty, so timing out is real trouble — but
        skips the ERROR log and the penalty sleep.
        """
        # A noted failure IS the loop making progress (alive, containing
        # trouble). Stamping here also hands the streak-6 alert sends below
        # a fresh watchdog budget: black-holed sendMessage retries can
        # stack past 120s and must not get the bot killed mid-alert.
        _stamp_progress()
        self._poll_failure_streak += 1
        streak = self._poll_failure_streak
        if "Telegram 401" in summary:
            log.error(f"getUpdates failed (streak {streak}): token revoked/invalid — {summary}")
        elif "Telegram 409" in summary:
            log.error(f"getUpdates failed (streak {streak}): another getUpdates poller is running — {summary}")
        elif not quiet:
            log.error(f"Error getting updates (streak {streak}): {summary}")

        alerting = streak >= self.POLL_ALERT_STREAK and not self._poll_alert_sent
        latch_state = {}

        def mutate(data):
            telegram = data.get("telegram")
            if not isinstance(telegram, dict):
                telegram = {}
            telegram["poll_failure_streak"] = streak
            telegram["last_poll_error"] = summary[:300]
            telegram["last_poll_error_at"] = service_health.timestamp()
            if alerting:
                # The alert latch persists here so a restart mid-outage
                # (crash, watchdog kill — both plausible WHILE Telegram is
                # unreachable) does not re-alert on every boot. Cleared on
                # the next successful poll.
                latch_state["already_on_disk"] = bool(telegram.get("poll_alert_latched_at"))
                telegram["poll_alert_latched_at"] = service_health.timestamp()
            data["telegram"] = telegram

        service_health.update(mutate, SERVICE_HEALTH_PATH, warn=log.warning)

        if alerting:
            self._poll_alert_sent = True  # latched until the next successful poll
            if latch_state.get("already_on_disk"):
                log.warning(
                    "getUpdates outage alert suppressed: already sent before a "
                    "restart during this outage (persisted latch)."
                )
            else:
                # WeChat FIRST: it is the channel that still reaches the user
                # when Telegram itself is down — the exact blind spot of the
                # in-chat alert — and the in-chat send below can black-hole
                # for 120s+ (or die with the process) before it gives up.
                # Never raises.
                push_wechat(
                    "CalorieBot cannot reach Telegram",
                    f"{streak} consecutive getUpdates poll failures.\n"
                    f"Last error: {summary[:200]}\n"
                    "Messages sent to the bot may not be processed until this recovers.",
                )
                try:
                    self.send_message(
                        ALLOWED_CHAT_ID,
                        "⚠️ <b>I can't fetch Telegram updates right now.</b>\n\n"
                        f"{streak} getUpdates polls in a row have failed "
                        f"(last error: <code>{_html(summary[:200])}</code>).\n\n"
                        "Messages you send me may not be processed until this recovers.",
                    )
                except Exception:
                    log.error("Could not send the getUpdates outage alert.")

        if quiet:
            return
        # Exponential backoff 5s → 300s cap. The first failure sleeps exactly
        # 5s (pinned) and the capped stage exactly 300s, so the ±20% jitter in
        # between can never reorder consecutive sleeps (ranges are disjoint).
        delay = 5 * 2 ** min(streak - 1, 6)
        if delay >= 300:
            delay = 300
        elif streak > 1:
            delay *= random.uniform(0.8, 1.2)
        # Sliced so the 300s cap keeps the progress clock fresh: a
        # deliberate backoff must not look like a hang (total sleep identical).
        _sleep_with_progress_stamps(delay)

    def get_updates(self, timeout: int = 30) -> list:
        """Long-poll for new messages."""
        try:
            result = self._call(
                "getUpdates", offset=self.offset, timeout=timeout
            )
        except TimeoutError as e:
            # Quiet branch: no ERROR log, no penalty sleep — but the streak
            # still grows, so persistent timeouts surface and alert too.
            self._note_poll_failure(str(e), quiet=True)
            return []
        except Exception as e:
            # Contain and back off; the offset is never advanced, so the
            # failed batch is re-polled rather than silently skipped.
            self._note_poll_failure(self._redact(e))
            return []
        # The first successful poll of a boot sweeps too (not just streak
        # recoveries): a latch persisted by a previous process during an
        # outage that ended while the bot was down would otherwise linger
        # and silently swallow the NEXT outage's alert.
        if self._poll_failure_streak or not self._poll_latch_swept:
            self._poll_failure_streak = 0
            self._poll_alert_sent = False
            self._poll_latch_swept = True

            def mutate(data):
                telegram = data.get("telegram")
                if isinstance(telegram, dict):
                    telegram["poll_failure_streak"] = 0
                    # A successful poll ends the outage: re-arm the one-shot
                    # alert for the next streak.
                    telegram.pop("poll_alert_latched_at", None)
                    data["telegram"] = telegram

            service_health.update(mutate, SERVICE_HEALTH_PATH, warn=log.warning)
        if result:
            self.offset = result[-1]["update_id"] + 1
        return result

    def send_message(self, chat_id: int, text: str, parse_mode: str = "HTML", reply_markup: Optional[dict] = None):
        """Send a text message. Returns the Message dict on success, or None."""
        # Truncate if too long for Telegram (4096-char limit, counted in
        # UTF-16 code units — astral emoji are 2 units each), repairing any
        # HTML tag the cut point lands inside.
        if utf16_len(text) > 4000:
            text = _truncate_telegram_html(text)
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
            if isinstance(e, TelegramConnectionError):
                # Cold-connection retry: identical arguments (same
                # parse_mode). The formatting fallback below would hand the
                # user raw tags for what was purely transport trouble.
                try:
                    return self._call("sendMessage", **payload)
                except Exception as retry_error:
                    # Two connection-class failures rule out formatting as
                    # the cause; the parse_mode-stripped fallback would only
                    # add a third wire attempt (and a possible third
                    # duplicate when Telegram processed a lost-response
                    # send). Stop here.
                    log.error(
                        f"sendMessage connection retry failed: {self._redact(retry_error)}"
                    )
                    return None
            # Retry without parse_mode in case of formatting issues
            try:
                fallback = {"chat_id": chat_id, "text": text}
                if reply_markup:
                    fallback["reply_markup"] = reply_markup
                return self._call("sendMessage", **fallback)
            except Exception as final_error:
                log.error(
                    f"sendMessage failed after all fallbacks: {self._redact(final_error)}"
                )
                return None

    def send_photo(self, chat_id: int, photo_bytes: bytes, caption: str = "", parse_mode: str = "HTML"):
        """Send a photo with an optional caption. Returns the Message dict or None.

        Telegram caps captions at 1024 UTF-16 units — callers must keep
        captions within that or send the text as a separate message.
        """

        def _post(data):
            resp = self.session.post(
                f"{self.base_url}/sendPhoto",
                data=data,
                files={"photo": ("meal.jpg", photo_bytes, "image/jpeg")},
                timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("ok"):
                return payload.get("result")
            log.error(f"sendPhoto rejected: {self._redact(payload.get('description', 'unknown'))}")
            return None

        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = parse_mode
        try:
            return _post(data)
        except Exception as e:
            log.error(f"Failed to send photo: {self._redact(e)}")
            if isinstance(e, requests.exceptions.ConnectionError):
                # Cold-connection retry, identical arguments (same
                # parse_mode) — see send_message. The duplicate-photo
                # trade-off documented below applies here too.
                try:
                    return _post(data)
                except Exception as retry_error:
                    # Two connection failures rule out caption formatting;
                    # a third attempt only risks a third duplicate photo.
                    log.error(
                        f"sendPhoto connection retry failed: {self._redact(retry_error)}"
                    )
                    return None
            if caption:
                # Mirror send_message exactly: the retry KEEPS the caption and
                # drops only parse_mode — the meal card text is the payload
                # this method exists to deliver, so it is never sacrificed
                # (worst case the user sees raw <b> tags). Known trade-off: a
                # response lost AFTER Telegram processed the first post makes
                # this retry produce a duplicate photo — cosmetic, and rarer
                # than the transient failures the retry recovers from.
                try:
                    return _post({"chat_id": chat_id, "caption": caption})
                except Exception as final_error:
                    log.error(
                        f"sendPhoto failed after all fallbacks: {self._redact(final_error)}"
                    )
                    return None
            else:
                log.error("sendPhoto failed with no caption fallback to try.")
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

    def send_chat_action(self, chat_id: int, action: str):
        """Fire-and-forget sendChatAction ('typing' / 'upload_photo' bubble).

        Pure UX (the action auto-expires after ~5s); failures are swallowed
        and never retried — spending a second RTT on an indicator would
        defeat its purpose.
        """
        try:
            self._call("sendChatAction", chat_id=chat_id, action=action)
        except Exception as e:
            log.debug(f"sendChatAction failed (ignored): {self._redact(e)}")

    def get_file(self, file_id: str) -> bytes:
        """Download a file from Telegram."""
        file_info = self._call("getFile", file_id=file_id)
        file_path = file_info.get("file_path")
        if not file_path:
            raise RuntimeError("Could not get file_path from telegram.")
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        try:
            # Streamed so a slow multi-MB download (this runs on the poll
            # thread for chat photos) reads as progress while bytes are
            # actually flowing: each chunk stamps the watchdog progress
            # clock. A stalled socket still trips the 30s read timeout.
            with self.session.get(url, timeout=30, stream=True) as resp:
                resp.raise_for_status()
                chunks = []
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    _stamp_progress()
                    chunks.append(chunk)
        except requests.RequestException as e:
            # Network errors (ConnectionError/ConnectTimeout) embed the token URL too.
            resp = getattr(e, "response", None)
            status = resp.status_code if resp is not None else "network-error"
            raise RuntimeError(f"Telegram {status} calling getFile") from None
        return b"".join(chunks)


# ─── Meals Data Access ─────────────────────────────────────────────
def get_todays_meals(chat_id: int) -> List[Dict]:
    """Get all food meals logged today (user-local date)."""
    today_str = database.user_local_today().isoformat()
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


def _update_service_health(mutate) -> Dict:
    """Apply a mutation under the inter-process file lock.

    Serializes against the cron daily_report process as well as this
    process's own Flask/background threads.
    """
    return service_health.update(mutate, SERVICE_HEALTH_PATH, warn=log.warning)


def _set_gemini_daily_quota_pause(error: Exception) -> str:
    pause_until = datetime.now() + timedelta(seconds=GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS)

    def mutate(data):
        gemini = data.setdefault("gemini", {})
        gemini["quota_pause_set_at"] = _health_timestamp()
        gemini["quota_pause_until"] = pause_until.isoformat(timespec="seconds")
        gemini["quota_pause_reason"] = str(error)[:500]

    _update_service_health(mutate)
    return pause_until.isoformat(timespec="seconds")


def _record_gemini_health(
    ok: bool,
    *,
    error_type: str = "",
    error_summary: str = "",
    latency_seconds: Optional[float] = None,
    probe: bool = False,
):
    def mutate(data):
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

    _update_service_health(mutate)


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
    def mutate(data):
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

    _update_service_health(mutate)


def _record_report_health(
    ok: bool,
    target_date: str,
    *,
    source: str = "telegram_command",
    report_path: str = "",
    error_summary: str = "",
):
    _update_service_health(
        lambda data: service_health.apply_report_health(
            data, ok, target_date,
            source=source, report_path=report_path, error_summary=error_summary,
        )
    )


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


def _normalize_photo_for_analysis(image_bytes: bytes) -> Optional[bytes]:
    """Single-decode normalize: EXIF-upright, <=1568px, JPEG quality 85.

    Computed once per photo and reused by every consumer (Claude temp file,
    Gemini inline blob, chat echo), replacing three separate full-size PIL
    decodes — the biggest RAM spike on the 1GB host. 1568px matches the
    Anthropic API's own server-side cap (accuracy-neutral for Claude) and is
    comfortably above Gemini's needs. draft() needs an aspect-corrected box:
    PIL only engages the cheap DCT-domain downscale when BOTH dims stay >=
    the request, so a square (1568,1568) box on a 4:3 image never bites.
    Returns None on any failure — callers fall back to the original bytes
    and the pre-existing per-consumer preparation paths.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        width, height = img.size
        scale = 1568 / max(width, height)
        if 0 < scale < 1:
            img.draft("RGB", (max(1, round(width * scale)), max(1, round(height * scale))))
        img = ImageOps.exif_transpose(img)
        img.thumbnail((1568, 1568))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"Could not normalize photo for analysis; using originals: {e}")
        return None


def _prepare_image_for_gemini(image_bytes: bytes) -> Image.Image:
    """Downscale a photo before sending it to Gemini.

    Phone photos can be 25MB; Gemini needs nowhere near that, and >20MB
    inline requests hard-fail. EXIF orientation is baked in first so the
    thumbnail is not sideways. If anything about resizing goes wrong, fall
    back to the original un-resized image — but let a genuinely unreadable
    image raise the same error the un-resized path would have raised.
    """
    img = Image.open(io.BytesIO(image_bytes))
    try:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((1024, 1024))
        return img.convert("RGB")
    except Exception as e:
        log.warning(f"Could not downscale image for Gemini; sending original: {e}")
        return Image.open(io.BytesIO(image_bytes))


def _compress_photo_for_echo(image_bytes: bytes) -> Optional[bytes]:
    """JPEG-compress a photo for echoing back to the chat.

    Phone originals can be 25MB; the echo only needs to be recognizable.
    Returns None when the bytes can't be made sendable (caller falls back
    to a text-only meal card).
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=82)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"Could not compress photo for echo: {e}")
        # Pass the original through only when Telegram will actually accept
        # it (sendPhoto takes JPEG/PNG/WebP, capped at 10MB). Undecodable
        # bytes in any other format would be a guaranteed-futile multipart
        # upload (possibly two, with the retry) on every affected meal.
        if len(image_bytes) > 9_000_000:
            return None
        is_jpeg = image_bytes[:3] == b"\xff\xd8\xff"
        is_png = image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        return image_bytes if (is_jpeg or is_png) else None


def _echo_meal_photo(bot: TelegramBot, chat_id: int, image_bytes: bytes, result_text: str,
                     normalized_bytes: Optional[bytes] = None):
    """Send the analyzed photo with the meal card, falling back to text.

    Chat-sent photos are already visible in the conversation, but API
    uploads (Android watcher / iOS Shortcut) used to produce text-only meal
    cards — the user never saw WHICH photo a meal came from. Best-effort:
    any failure still delivers the plain text card.

    ``normalized_bytes`` (the worker's single-decode 1568px q85 JPEG) is
    sent as-is — well under sendPhoto's caps — skipping this path's own
    full decode; only a failed normalization pays for _compress_photo_for_echo.
    """
    if not ECHO_UPLOAD_PHOTOS:
        bot.send_message(chat_id, result_text)
        return
    compressed = normalized_bytes if normalized_bytes else _compress_photo_for_echo(image_bytes)
    if compressed is None:
        bot.send_message(chat_id, result_text)
        return
    if utf16_len(result_text) <= 1024:
        if bot.send_photo(chat_id, compressed, caption=result_text):
            return
    elif bot.send_photo(chat_id, compressed):
        bot.send_message(chat_id, result_text)
        return
    bot.send_message(chat_id, result_text)


def _analyze_food_photo_once(
    client: genai.Client,
    image_bytes: bytes,
    normalized_bytes: Optional[bytes] = None,
    prep_cache: Optional[dict] = None,
) -> Dict:
    """Analyze a food photo once with Gemini, letting callers handle failures.

    With ``normalized_bytes`` (the worker's single-decode JPEG) the image
    rides as an inline blob Part — no second full decode, no SDK re-encode.
    Without it, the original _prepare_image_for_gemini path runs unchanged.
    ``prep_cache`` (a dict owned by the retry loop) keeps whichever image
    part was built so retries reuse it instead of re-preparing per attempt.
    """
    if prep_cache is None:
        prep_cache = {}
    if "image" not in prep_cache:
        if normalized_bytes:
            prep_cache["image"] = types.Part.from_bytes(
                data=normalized_bytes, mime_type="image/jpeg"
            )
        else:
            prep_cache["image"] = _prepare_image_for_gemini(image_bytes)
    response = _generate_content_with_deadline(
        client,
        model=GEMINI_MODEL,
        contents=[FOOD_DETECTION_PROMPT, prep_cache["image"]],
        config=GEMINI_JSON_CONFIG,
    )
    return parse_ai_json(response.text)


def analyze_food_photo(client: genai.Client, image_bytes: bytes,
                       normalized_bytes: Optional[bytes] = None) -> Optional[Dict]:
    """Analyze a food photo (single attempt for the interactive path).

    Normalizes here when the caller didn't pass ``normalized_bytes``: the
    Telegram photo worker has no other consumer of the normalized JPEG, so
    this seam keeps the single-decode win without widening its call sites.
    """
    if normalized_bytes is None:
        normalized_bytes = _normalize_photo_for_analysis(image_bytes)
    return analyze_food_photo_with_retries(
        client, image_bytes, max_attempts=1, normalized_bytes=normalized_bytes
    )


def analyze_food_photo_with_retries(
    client: genai.Client,
    image_bytes: bytes,
    max_attempts: int = GEMINI_ANALYSIS_MAX_ATTEMPTS,
    normalized_bytes: Optional[bytes] = None,
) -> Optional[Dict]:
    """Analyze a photo: Claude (subscription) first, Gemini as fallback/default.

    The Claude hop is best-effort — unconfigured, rate-windowed, or failing,
    it returns None and the original Gemini retry path below runs unchanged.
    ``normalized_bytes`` is the caller's single-decode 1568px JPEG: Claude
    writes it to the temp file (accuracy-neutral — the API caps at 1568px
    server-side, and it stays under the API's 5MB reject limit) and Gemini
    sends it inline. None means "normalize failed/unavailable": both legs
    fall back to the original bytes and their old preparation paths.
    """
    # Entry stamp bounds the watchdog-visible gap across the Claude leg:
    # the CLI is single-flight with a contended-caller bypass, so the longest
    # legitimate silence here is one CLI run (~120s), well inside the hang
    # threshold when measured from here.
    _stamp_progress()
    if claude_analyzer.is_configured():
        result = claude_analyzer.analyze_food_photo(normalized_bytes or image_bytes)
        if result is not None:
            # Inert provenance marker: nothing reads it yet, but it lands in
            # the saved meal's analysis JSON so engine mix is auditable later.
            result["analyzed_by"] = "claude"
            return result
        log.warning("Claude analyzer unavailable/failed; falling back to Gemini.")

    pause = _gemini_quota_pause()
    if pause:
        log.warning("Skipping Gemini photo analysis because daily quota pause is active.")
        return None

    max_attempts = max(1, max_attempts)
    start = time.time()
    # One prepared image part shared across attempts: retries must not pay
    # (or re-spike RAM on) the image preparation again.
    prep_cache: dict = {}

    for attempt in range(1, max_attempts + 1):
        # Per-attempt stamp: each Gemini try is bounded (90s deadline
        # wrapper + <=60s clamped retry sleep), so a healthy multi-attempt
        # run never looks like a hang.
        _stamp_progress()
        try:
            result = _analyze_food_photo_once(client, image_bytes, normalized_bytes, prep_cache)
            _record_gemini_health(True, latency_seconds=time.time() - start)
            # Log parity with the Claude success branch, including the model.
            log.info(f"✅ Photo analyzed by Gemini ({GEMINI_MODEL}).")
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


_parse_boolish = parse_boolish


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

    now_ts = time.time()
    with _geo_cache_lock:
        cached = _remote_ip_country_cache.get(str(ip))
        if cached and now_ts - cached["time"] < VPN_GEO_CACHE_TTL_SECONDS:
            return cached["country"]

    # Never hold the lock across the blocking network call.
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

    with _geo_cache_lock:
        _prune_geo_cache_locked(now_ts)
        _remote_ip_country_cache[str(ip)] = {"country": country, "time": now_ts}
    return country


def _prune_geo_cache_locked(now_ts: float):
    """Bound the geo cache before an insert; caller must hold _geo_cache_lock.

    Drops expired entries first and, if the cache is still full, the oldest
    entries by stored time, leaving room for one new entry.
    """
    if len(_remote_ip_country_cache) < GEO_CACHE_MAX_ENTRIES:
        return

    expired = [
        key for key, entry in _remote_ip_country_cache.items()
        if now_ts - entry["time"] >= VPN_GEO_CACHE_TTL_SECONDS
    ]
    for key in expired:
        del _remote_ip_country_cache[key]

    overflow = len(_remote_ip_country_cache) - (GEO_CACHE_MAX_ENTRIES - 1)
    if overflow > 0:
        oldest = sorted(
            _remote_ip_country_cache,
            key=lambda key: _remote_ip_country_cache[key]["time"],
        )[:overflow]
        for key in oldest:
            del _remote_ip_country_cache[key]


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
        f"The Android uploader reached <code>{_html(endpoint)}</code> from "
        f"<code>{_html(remote_ip)}</code>, but it reported no VPN interface "
        f"(<code>{_html(vpn_check or 'no detail')}</code>).\n\n"
        f"Network evidence: <code>{_html(evidence_detail)}</code>.\n\n"
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
        f"The iOS Shortcut reached <code>{_html(endpoint)}</code> from <code>{_html(remote_ip)}</code>, "
        "and the network evidence looks like a direct/non-VPN connection.\n\n"
        f"Detail: <code>{_html(detail)}</code>\n\n"
        f"Network evidence: <code>{_html(evidence_detail)}</code>.\n\n"
        "Turn on the VPN before relying on WiFi/cellular auto-upload from mainland China.",
    )


def save_meal(chat_id: int, analysis: Dict, source: str, file_id: str = "", image_hash: str = "",
              captured_at: Optional[datetime] = None, mark_status: Optional[str] = None):
    """Save a meal analysis to the log (from direct Telegram photo).

    captured_at, when given, is the device-local moment the photo was TAKEN
    (from the camera filename) — a backfilled photo from yesterday must land
    on yesterday's ledger, not on the day the sync finally uploaded it.
    mark_status passes through to database.save_meal: the meal INSERT and
    the ledger status write happen in one transaction, so a crash between
    them can't strand a saved meal behind a 'processing' reservation.
    """
    # Meal date/time use the user's clock so late-night meals land on the right
    # day; timestamp stays on the server clock because duplicate detection and
    # reservation staleness compare against datetime.now().
    user_now = captured_at or database.user_local_now()
    date_str = user_now.date().isoformat()
    time_str = user_now.strftime("%I:%M %p")
    timestamp_str = datetime.now().isoformat()
    return database.save_meal(chat_id, date_str, time_str, timestamp_str, source, image_hash,
                              file_id, analysis, mark_status=mark_status)


# Oldest capture time /upload will honor; anything older (or unparseable, or
# in the future) falls back to upload-time dating rather than trusting junk.
CAPTURED_AT_MAX_AGE_DAYS = _env_int("CAPTURED_AT_MAX_AGE_DAYS", 45, 1, 365)


def _looks_like_image(data: bytes) -> bool:
    """Magic-byte check for the raw-body upload path (JPEG/PNG/HEIC/TIFF/WebP)."""
    if not data or len(data) < 12:
        return False
    return (
        data[:3] == b"\xff\xd8\xff"                     # JPEG
        or data[:8] == b"\x89PNG\r\n\x1a\n"             # PNG
        or data[4:8] == b"ftyp"                          # HEIC/HEIF/AVIF (ISO BMFF)
        or data[:4] in (b"II*\x00", b"MM\x00*")         # TIFF
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")  # WebP
    )


def _parse_captured_at(raw: str) -> Optional[datetime]:
    """Validate the additive captured_at form field ('YYYY-MM-DD HH:MM:SS').

    The value is device-local wall time taken from the camera filename, so it
    is compared against the user-local clock, allowing an hour of skew.
    """
    try:
        captured = datetime.strptime((raw or "").strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    now_local = database.user_local_now().replace(tzinfo=None)
    if captured > now_local + timedelta(hours=1):
        return None
    if captured < now_local - timedelta(days=CAPTURED_AT_MAX_AGE_DAYS):
        return None
    return captured


def _format_age(iso_value: Optional[str]) -> str:
    if not iso_value:
        return "never"
    if not isinstance(iso_value, str):
        # fromisoformat raises TypeError (not ValueError) for non-strings; a
        # junk-typed stored timestamp must not take down /status.
        return _html(str(iso_value)[:64])
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError:
        # Callers interpolate this into parse_mode=HTML text; a corrupted
        # stored timestamp must arrive escaped (and short), not as markup.
        return _html(iso_value[:64])

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


_ORIGINAL_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
_UPLOAD_NAME_HASH_RE = re.compile(r"_([0-9a-fA-F]{12})(?:\.[^.]+)?$")


def _upload_file_ledger_hash(path: Path, image_bytes: Optional[bytes] = None) -> str:
    """Ledger hash for a staged/failed upload file.

    When the phone recompresses before upload it declares the ORIGINAL
    file's hash, which becomes the ledger key and the filename prefix —
    the staged bytes hash differently. Resolve through the ledger first;
    fall back to hashing the bytes for legacy files, reading them from
    ``path`` only when the caller didn't supply them.
    """
    match = _UPLOAD_NAME_HASH_RE.search(path.name)
    if match:
        ledger_hash = database.find_photo_hash_by_prefix(ALLOWED_CHAT_ID, match.group(1))
        if ledger_hash:
            return ledger_hash
    if image_bytes is None:
        image_bytes = path.read_bytes()
    return hashlib.md5(image_bytes).hexdigest()


def _retry_failed_upload_path(gemini_client, path: Path) -> Dict:
    # A Gemini-only quota pause must not block the Claude-first analyzer;
    # the Gemini leg inside analyze_food_photo_with_retries still respects
    # the pause. Only short-circuit when Claude cannot run at all.
    if _gemini_quota_pause() and not claude_analyzer.is_configured():
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

    img_hash = _upload_file_ledger_hash(path, image_bytes)

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

    # Same single-decode normalization as the live workers; None (e.g. a
    # corrupt failed file) falls back to the original-bytes paths.
    normalized_bytes = _normalize_photo_for_analysis(image_bytes)
    analysis = analyze_food_photo_with_retries(
        gemini_client, image_bytes, normalized_bytes=normalized_bytes
    )
    if analysis is None:
        database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "failed", source="api_retry")
        if _gemini_quota_pause():
            return {
                "status": "quota_paused",
                "name": path.name,
                "message": "Gemini daily quota is paused; file kept for later retry",
            }
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
        save_meal(ALLOWED_CHAT_ID, analysis, "api_retry", "failed_upload", img_hash,
                  mark_status="saved")
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
    # Same gate as the single-file path: a Gemini pause only holds the batch
    # when the Claude-first analyzer can't step in.
    if pause_summary and not claude_analyzer.is_configured():
        return (
            "⏸️ <b>Batch retry held.</b>\n\n"
            f"{pause_summary}\n\n"
            f"Saved failed uploads remain on disk: {len(failed)}.\n"
            "Give up on them with <code>/clear_failed latest confirm</code> or "
            "<code>/clear_failed all confirm</code>."
        )

    limit = _parse_positive_int(str(limit), 3, 1, RETRY_ALL_FAILED_MAX)
    selected = failed[:limit]
    results = []
    for path in selected:
        # Stamp between photos: this batch runs synchronously on the poll
        # thread and can legitimately take minutes (~390s observed for a
        # full batch) — each file is a fresh bounded unit of work, not a
        # hang, so the watchdog clock must be reset per file.
        _stamp_progress()
        results.append(_retry_failed_upload_path(gemini_client, path))

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
        response = _generate_content_with_deadline(
            gemini_client,
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
        # The phone recovered; clear the cooldown so the NEXT outage warns
        # promptly instead of staying silent for the rest of the window.
        _last_stale_heartbeat_warning_at = None
        return False

    now = datetime.now()
    if (_last_stale_heartbeat_warning_at is not None
            and (now - _last_stale_heartbeat_warning_at).total_seconds()
            < HEARTBEAT_STALE_WARNING_COOLDOWN_HOURS * 3600):
        return False
    # Stamp before sending so a slow/looping caller can't spam per tick.
    _last_stale_heartbeat_warning_at = now

    hours = int(age.total_seconds() // 3600)
    log.warning(f"Android heartbeat stale for {hours}h; notifying user.")
    sent = bot.send_message(
        ALLOWED_CHAT_ID,
        f"📵 <b>Android watcher hasn't reached the server in about {hours}h</b> "
        f"(last ping <code>{_html(last_ping)}</code>).\n\n"
        "The phone may be offline, the watcher may have stopped, or it can't "
        "reach the server — new photos are piling up in the phone's offline "
        "queue meanwhile.\n\n"
        "Check <code>~/watcher.log</code> on the phone, or <code>/android</code> here.",
    )
    if sent is None:
        # send_message swallows errors and returns None; don't let a failed
        # delivery consume the whole cooldown. Roll the stamp back so the
        # warning retries in ~15 minutes instead of going silent for hours.
        _last_stale_heartbeat_warning_at = (
            now
            - timedelta(hours=HEARTBEAT_STALE_WARNING_COOLDOWN_HOURS)
            + timedelta(minutes=15)
        )
        return False
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

    # getUpdates outage visibility: the streak is written by _note_poll_failure
    # and zeroed by the next successful poll, so a nonzero value here means
    # the poll loop is failing RIGHT NOW (or died mid-streak).
    telegram_health = health.get("telegram")
    if not isinstance(telegram_health, dict):
        telegram_health = {}
    poll_streak = telegram_health.get("poll_failure_streak")
    if isinstance(poll_streak, int) and poll_streak > 0:
        polling_line = (
            f"Telegram polling: ⚠️ {poll_streak} consecutive poll failures, "
            f"last error {_format_age(telegram_health.get('last_poll_error_at'))}"
        )
    else:
        polling_line = "Telegram polling: OK"

    lines = [
        "🧭 <b>CalorieTracker Status</b>",
        android_line,
        polling_line,
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
        return database.user_local_today().isoformat()
    if normalized in {"yesterday", "last"}:
        return (database.user_local_today() - timedelta(days=1)).isoformat()
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

        # Best-effort same-day Garmin pull so a manual /report reflects the
        # latest activity, mirroring the nightly cron. Self-guarded — a Garmin
        # failure never blocks the report.
        try:
            daily_report._sync_garmin_activity(target_date)
        except Exception:
            log.warning("Garmin sync for /report failed; continuing.", exc_info=True)

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
        f"Claude analyzer: <code>{escape(claude_analyzer.status_label())}</code>",
        f"Claude OAuth token set: <code>{bool(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'))}</code>",
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
    local_today = database.user_local_today()
    today_str = local_today.isoformat()
    seven_days_ago = (local_today - timedelta(days=6)).isoformat()
    # The all-time reduction runs as a SQL aggregate (get_meal_stats): the
    # old full-table get_meals + per-row json.loads scan was the only query
    # whose cost grows without bound (measured 65ms @5k meals, 62% of it
    # JSON parsing — 2.4x slower than the aggregate, and widening).
    stats = database.get_meal_stats(chat_id)
    recent_meals = database.get_meals(chat_id, seven_days_ago, today_str)
    today_meals = get_todays_meals(chat_id)

    food_recent = [m for m in recent_meals if m.get("analysis", {}).get("is_food")]
    recent_calories = sum(safe_number(m["analysis"].get("total_calories")) for m in food_recent)

    lines = [
        "📈 <b>Database Stats</b>",
        f"Food meals today: {len(today_meals)}",
        f"Food meals last 7 days: {len(food_recent)}",
        f"Food meals all time: {stats['food_meals']}",
        f"Raw DB rows all time: {stats['total_meals']}",
        f"Calories last 7 days: ~{int(recent_calories):,}",
        f"Calories all time: ~{int(stats['total_calories']):,}",
    ]
    if stats["active_days"]:
        lines.append(f"Average per active day: ~{stats['avg_calories_per_active_day']:,} kcal")
    if stats["source_counts"]:
        lines.append("\n<b>Sources</b>")
        for source, count in sorted(stats["source_counts"].items(), key=lambda item: (-item[1], item[0])):
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
        local_today_str = database.user_local_today().isoformat()
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

    claude_state = claude_analyzer.status_label()
    if claude_state == "enabled":
        lines.append("✅ Claude analyzer enabled (CLI found)")
    elif claude_state == "off":
        lines.append("⚪ Claude analyzer off — photos go straight to Gemini")
    else:
        lines.append("❌ Claude analyzer enabled but the CLI was not found")

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

    for item in safe_food_items(analysis):
        name = item.get("name", "?")
        cals = item.get("estimated_calories", "?")
        if isinstance(cals, (int, float)):
            # safe_number: JSON Infinity/NaN must never render as 'inf kcal';
            # non-numeric strings still display escaped as-is.
            cals = safe_number(cals)
        p = safe_number(item.get("protein_g"))
        c = safe_number(item.get("carbs_g"))
        f = safe_number(item.get("fat_g"))
        lines.append(f"  • {_html(name)}: ~{_html(cals)} kcal")
        lines.append(f"    P:{p}g | C:{c}g | F:{f}g")

    total = analysis.get("total_calories") or "?"
    tp = analysis.get("total_protein_g") or 0
    tc = analysis.get("total_carbs_g") or 0
    tf = analysis.get("total_fat_g") or 0
    lines.append(f"\n📊 <b>This meal: ~{_html(total)} kcal</b>")
    lines.append(f"🥩 P: {_html(tp)}g | 🍞 C: {_html(tc)}g | 🧈 F: {_html(tf)}g")

    # Gemini sometimes hallucinates a meal total that contradicts its own
    # per-item breakdown; flag it so the user can correct it right away.
    item_sum = meal_calorie_mismatch(analysis)
    if item_sum is not None:
        lines.append(
            f"⚠️ Item calories sum to ~{item_sum} kcal but the meal total is "
            f"~{_html(total)} kcal — reply with a correction if this looks wrong."
        )

    # Daily totals
    daily = format_daily_totals(chat_id)
    if daily:
        lines.append("\n━━━━━━━━━━━━━━━━━━")
        lines.append(daily)

    return "\n".join(lines)


def format_daily_totals(chat_id: int) -> str:
    """Calculate and format today's running totals."""
    meals = get_todays_meals(chat_id)
    if not meals:
        return ""
    # max(0, ...): a negative hallucinated total must not subtract from the
    # day (mirrors the per-meal clamp in _daily_average_meals).
    total_cal = sum(max(0, safe_number(m["analysis"].get("total_calories"))) for m in meals)
    total_p = sum(max(0, safe_number(m["analysis"].get("total_protein_g"))) for m in meals)
    total_c = sum(max(0, safe_number(m["analysis"].get("total_carbs_g"))) for m in meals)
    total_f = sum(max(0, safe_number(m["analysis"].get("total_fat_g"))) for m in meals)
    lines = [
        f"📋 <b>Today's Total ({len(meals)} meals):</b>",
        f"🔥 {total_cal:,} kcal",
        f"🥩 P: {total_p}g | 🍞 C: {total_c}g | 🧈 F: {total_f}g",
    ]
    return "\n".join(lines)


# ─── Formatting ────────────────────────────────────────────────────
def _daily_calorie_totals(chat_id: int, start_date: str, end_date: str) -> Dict[str, int]:
    """Sum food-meal calories per user-local day over an inclusive date window."""
    meals = database.get_meals(chat_id, start_date, end_date)
    totals: Dict[str, int] = {}
    for m in meals:
        analysis = m.get("analysis", {})
        if not analysis.get("is_food"):
            continue
        d = m.get("date", "")
        # max(0, ...): negative hallucinated totals must not subtract from a day.
        totals[d] = totals.get(d, 0) + max(0, safe_number(analysis.get("total_calories")))
    return totals


# ─── Fitness & diet command surface ────────────────────────────────
# All handlers below are PURE DB reads/writes plus arithmetic from the
# nutrition/fitness_plan pure modules — no Gemini — so they keep working
# during a Gemini quota pause. Every number that reaches them from meals or
# model output is coerced (safe_number / the parsers) so a hostile value can
# never raise.

# The strength groups /train tracks and /workout recognises as leading tokens.
FITNESS_MUSCLE_GROUPS = ("legs", "chest", "shoulders", "back", "arms", "core")
_WORKOUT_GROUP_WORDS = {
    "legs", "leg", "chest", "shoulders", "shoulder", "back", "arms", "arm",
    "core", "abs", "glutes", "biceps", "triceps", "cardio", "push", "pull",
    "full", "fullbody", "upper", "lower", "quads", "hamstrings", "calves",
}

ACTIVITY_KCAL_MAX = 20000
ACTIVITY_STEPS_MAX = 200000
ACTIVITY_KM_MAX = 500

# Weigh-in staleness warning threshold (unrelated to TRAIN_LOOKBACK_DAYS's 14).
WEIGHT_STALE_AFTER_DAYS = 14
# /train recommendation lookback (unrelated to WEIGHT_STALE_AFTER_DAYS's 14).
TRAIN_LOOKBACK_DAYS = 14
# Body-weight trend window for /weight (mirrored in daily_report — no import).
WEIGHT_TREND_WINDOW_DAYS = 7

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Deterministic read-only fitness questions answered with ZERO Gemini spend.
_RUN_QUERY_RE = re.compile(
    r"(what\s+(?:should|do)\s+i\s+run|today'?s\s+run|todays\s+run|"
    r"what'?s\s+my\s+run|my\s+run\s+today)",
    re.IGNORECASE,
)
_MACRO_QUERY_RE = re.compile(
    r"(how(?:'s|s|\s+is|\s+are)\s+my\s+(?:protein|macros?|carbs?|fat)|"
    r"macros?\s+(?:today|so\s+far)|my\s+macros)",
    re.IGNORECASE,
)
_PLAN_QUERY_RE = re.compile(
    r"(this\s+week'?s\s+(?:run\s+)?plan|week'?s\s+plan|"
    r"my\s+(?:training\s+)?plan\s+this\s+week)",
    re.IGNORECASE,
)
# Logging-shaped messages (any number, or an ate/had/burned/log/weigh/hit
# verb) must NOT be intercepted by the read-only fast path above: "had 2 eggs
# before today's run" is a meal log, not a run question, even though it
# contains "today's run". These go to the Gemini NL pipeline instead.
_FITNESS_LOG_GUARD_RE = re.compile(
    r"\d|\b(?:ate|eat|had|burn(?:ed|t)?|log(?:ged|ging)?|weigh(?:ed|ing|t)?|hit)\b",
    re.IGNORECASE,
)


def _iso_date(value) -> Optional[date]:
    """A ``date`` from a 'YYYY-MM-DD'-ish value, or None. Never raises."""
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _arg_float(value, default: float = 0.0) -> float:
    """Parse a command-line arg string to a finite float, else ``default``.

    ``utils.safe_number`` only accepts real int/float, so string args from
    ``text.split()`` need their own parse; NaN/inf/absurd magnitudes are
    rejected the same way.
    """
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if num != num or abs(num) > 1e12:  # NaN or absurd magnitude
        return default
    return num


def _clamp(num: float, low: float, high: float) -> float:
    return max(low, min(high, num))


def _fmt_seconds(seconds) -> str:
    """Format a duration in seconds as ``h:mm:ss`` / ``m:ss`` (— for junk)."""
    total = int(round(safe_number(seconds)))
    if total <= 0:
        return "—"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _profile_or_empty(chat_id: int) -> Dict:
    return database.get_fitness_profile(chat_id) or {}


def _profile_for_plan(chat_id: int, today: date) -> Dict:
    """Stored fitness profile, or the sensible default plan for a new user."""
    return database.get_fitness_profile(chat_id) or fitness_plan.default_profile(
        chat_id, today
    )


def _display_vdot(profile: Dict) -> float:
    """The profile's VDOT if in range, else the default. Never raises."""
    vdot = safe_number((profile or {}).get("vdot"), 0)
    if fitness_plan.VDOT_MIN <= vdot <= fitness_plan.VDOT_MAX:
        return vdot
    return float(fitness_plan.DEFAULT_VDOT)


def _weeks_to_race(profile: Dict, today: date) -> Optional[float]:
    return fitness_plan.weeks_to_race(profile, today)


def _resolved_diet_targets(profile: Dict, latest_weight: Optional[Dict]):
    """(mode, targets) resolving diet_mode + stored targets + latest weight."""
    mode = (profile or {}).get("diet_mode") or "balanced"
    weight = safe_number(latest_weight.get("weight_kg")) if latest_weight else None
    return mode, nutrition.profile_diet_targets(mode, weight, profile)


def _diet_context(chat_id: int):
    """(profile, latest, mode, targets) shared by the diet-facing commands."""
    profile = _profile_or_empty(chat_id)
    latest = database.get_latest_body_weight(chat_id)
    mode, targets = _resolved_diet_targets(profile, latest)
    return profile, latest, mode, targets


def _today_activities(chat_id: int) -> List[Dict]:
    today = database.user_local_today().isoformat()
    return database.get_activities(chat_id, today, today)


def _sum_active_calories(activities) -> float:
    return sum(safe_number(a.get("active_calories")) for a in activities or [])


def _activity_totals(activities):
    """(burned_kcal, steps, km) summed over ``activities``; junk-tolerant."""
    burned = _sum_active_calories(activities)
    steps = 0.0
    km = 0.0
    for activity in activities or []:
        raw = activity.get("raw")
        if isinstance(raw, dict):
            steps += safe_number(raw.get("steps"))
        km += safe_number(activity.get("distance_km"))
    return burned, steps, km


def _today_consumed_calories(chat_id: int) -> float:
    return sum(
        safe_number(m["analysis"].get("total_calories"))
        for m in get_todays_meals(chat_id)
    )


# ── /weight ────────────────────────────────────────────────────────
def format_weight_status(chat_id: int) -> str:
    """Latest weigh-in (with a staleness warning) plus the 7-day trend."""
    latest = database.get_latest_body_weight(chat_id)
    if not latest:
        return (
            "⚖️ <b>Weight</b>\n\nNo weigh-ins logged yet. "
            "Send <code>/weight 72.5</code> to start."
        )
    kg = safe_number(latest.get("weight_kg"))
    date_str = latest.get("date", "?")
    lines = ["⚖️ <b>Weight</b>", "", f"Latest: <b>{kg:g} kg</b> ({_html(date_str)})"]

    today = database.user_local_today()
    logged = _iso_date(date_str)
    if logged is not None:
        age = (today - logged).days
        if age > WEIGHT_STALE_AFTER_DAYS:
            lines.append(f"⚠️ That's {age} days old — send a fresh <code>/weight</code>.")

    weights = database.get_body_weights(
        chat_id, (today - timedelta(days=WEIGHT_TREND_WINDOW_DAYS)).isoformat(), today.isoformat()
    )
    if len(weights) >= 2:
        first = safe_number(weights[0].get("weight_kg"))
        last = safe_number(weights[-1].get("weight_kg"))
        delta = round(last - first, 1)
        arrow = "▼" if delta < 0 else ("▲" if delta > 0 else "▬")
        lines.append(f"{WEIGHT_TREND_WINDOW_DAYS}-day trend: {arrow} {delta:+g} kg ({len(weights)} weigh-ins)")
    return "\n".join(lines)


def _cmd_weight(bot: TelegramBot, chat_id: int, args: List[str]):
    text_arg = " ".join(args).strip()
    if not text_arg:
        bot.send_message(chat_id, format_weight_status(chat_id))
        return
    kg = nutrition.parse_weight_kg(text_arg)
    if kg is None:  # a bare number like "72.5" needs a unit hint to parse
        kg = nutrition.parse_weight_kg(text_arg + " kg")
    if kg is None:
        bot.send_message(
            chat_id,
            "⚖️ I couldn't read a weight there. Try <code>/weight 72.5</code> "
            "or <code>/weight 159 lb</code> (30–300 kg).",
        )
        return
    today = database.user_local_today()
    database.save_body_weight(chat_id, today.isoformat(), kg, source="telegram")
    bot.send_message(chat_id, f"⚖️ Logged <b>{kg:g} kg</b> for {today.isoformat()}.")


# ── /diet ──────────────────────────────────────────────────────────
def format_diet_status(chat_id: int) -> str:
    """Current diet mode + latest weight + resolved macro targets + disclaimer."""
    profile, latest, _mode, targets = _diet_context(chat_id)
    label = nutrition.MODE_SPECS[targets["mode"]]["label"]

    lines = ["🥗 <b>Diet plan</b>", "", f"Mode: <b>{_html(label)}</b>"]
    if latest:
        weight = safe_number(latest.get("weight_kg"))
        lines.append(f"Weight: {weight:g} kg ({_html(latest.get('date', '?'))})")
    else:
        lines.append("Weight: <i>not logged — /weight to set</i>")
    if not profile.get("diet_mode"):
        lines.append(
            "<i>(no mode set — showing balanced defaults; "
            "/diet keto|high_protein|balanced)</i>"
        )

    calories = targets.get("calories")
    if calories:
        lines.append(f"Calories: <b>{int(calories):,}</b> kcal target")
    macro_bits = []
    for key, letter in (
        ("target_protein_g", "P"),
        ("target_carbs_g", "C"),
        ("target_fat_g", "F"),
    ):
        val = targets.get(key)
        if val is not None:
            macro_bits.append(f"{letter} {int(round(val))}g")
    if macro_bits:
        lines.append("Targets: " + " · ".join(macro_bits))
    split = targets.get("split") or {}
    if split:
        lines.append(nutrition.format_split_line(split))
    cap = targets.get("carb_cap_g")
    if cap:
        lines.append(f"Carb cap: {int(cap)}g")

    lines.append("")
    lines.append(f"<i>{_html(nutrition.DISCLAIMER)}</i>")
    return "\n".join(lines)


def _diet_set_mode(bot: TelegramBot, chat_id: int, args: List[str]):
    sub = args[0].lower()
    database.save_fitness_profile(chat_id, diet_mode=sub)
    label = nutrition.MODE_SPECS[sub]["label"]
    bot.send_message(
        chat_id,
        f"🥗 Diet mode set to <b>{_html(label)}</b>.\n\n"
        f"<i>{_html(nutrition.DISCLAIMER)}</i>",
    )


def _diet_set_target(bot: TelegramBot, chat_id: int, args: List[str]):
    nums = args[1:]
    if len(nums) < 4:
        bot.send_message(
            chat_id,
            "Usage: <code>/diet target &lt;cal&gt; &lt;protein_g&gt; "
            "&lt;carbs_g&gt; &lt;fat_g&gt;</code>",
        )
        return
    cal = int(round(_arg_float(nums[0])))
    if cal <= 0:
        bot.send_message(chat_id, "Calorie target must be a positive number.")
        return
    protein = int(round(_arg_float(nums[1])))
    carbs = int(round(_arg_float(nums[2])))
    fat = int(round(_arg_float(nums[3])))
    database.save_fitness_profile(
        chat_id,
        target_calories=cal,
        target_protein_g=protein,
        target_carbs_g=carbs,
        target_fat_g=fat,
    )
    bot.send_message(
        chat_id,
        f"🎯 Targets set: <b>{cal:,} kcal</b> · P {protein}g · C {carbs}g · F {fat}g.",
    )


def _diet_set_protein(bot: TelegramBot, chat_id: int, args: List[str]):
    if len(args) < 2:
        bot.send_message(chat_id, "Usage: <code>/diet protein &lt;g_per_kg&gt;</code>")
        return
    ppk = round(_arg_float(args[1]), 2)
    if ppk <= 0:
        bot.send_message(chat_id, "Protein target must be a positive g/kg number.")
        return
    database.save_fitness_profile(chat_id, protein_g_per_kg=ppk)
    bot.send_message(
        chat_id, f"🥩 Protein target set to <b>{ppk:g} g/kg</b> of body weight."
    )


_DIET_SUBCOMMANDS = {"target": _diet_set_target, "protein": _diet_set_protein}


def _cmd_diet(bot: TelegramBot, chat_id: int, args: List[str]):
    if not args:
        bot.send_message(chat_id, format_diet_status(chat_id))
        return
    sub = args[0].lower()

    if sub in nutrition.DIET_MODES:
        _diet_set_mode(bot, chat_id, args)
        return

    handler = _DIET_SUBCOMMANDS.get(sub)
    if handler:
        handler(bot, chat_id, args)
        return

    bot.send_message(
        chat_id,
        "Options: <code>/diet keto|high_protein|balanced</code>, "
        "<code>/diet target …</code>, <code>/diet protein …</code>, "
        "or <code>/diet</code> for your current plan.",
    )


# ── /macros ────────────────────────────────────────────────────────
def _macros_window(arg: str):
    """(start_iso, end_iso, label) for a today|week|N-day selector."""
    today = database.user_local_today()
    end = today.isoformat()
    key = (arg or "today").lower()
    if key == "week":
        return (today - timedelta(days=6)).isoformat(), end, "last 7 days"
    if key != "today":
        try:
            days = int(key)
        except ValueError:
            days = 1
        days = int(_clamp(days, 1, 31))
        if days > 1:
            return (today - timedelta(days=days - 1)).isoformat(), end, f"last {days} days"
    return today.isoformat(), end, "today"


def _daily_average_meals(meals):
    """(pseudo_meals, logged_days): per-day-average totals over ``meals``.

    Multi-day /macros windows must judge the AVERAGE day against the daily
    targets — summing a week and comparing to one day's numbers misreads a
    perfectly adherent keto week as ~6x over the carb cap. Averaging happens
    here (over the distinct days that actually have food logged, not the
    window length) so nutrition.analyze_macros stays pure and its
    suggestions remain daily-sized.
    """
    totals = {"total_protein_g": 0.0, "total_carbs_g": 0.0,
              "total_fat_g": 0.0, "total_calories": 0.0}
    days = set()
    for meal in meals or []:
        if not isinstance(meal, dict):
            continue
        analysis = meal.get("analysis")
        if not isinstance(analysis, dict) or analysis.get("is_food") is False:
            continue
        if meal.get("date"):
            days.add(str(meal["date"]))
        for key in totals:
            # Mirror analyze_macros: hostile stored shapes must not skew
            # or crash the averaging.
            totals[key] += max(0.0, safe_number(analysis.get(key)))
    logged_days = len(days)
    divisor = max(1, logged_days)
    averaged = {key: value / divisor for key, value in totals.items()}
    averaged["is_food"] = True
    return [{"analysis": averaged}], logged_days


def _cmd_macros(bot: TelegramBot, chat_id: int, args: List[str]):
    start, end, label = _macros_window(args[0] if args else "today")
    meals = database.get_meals(chat_id, start, end)
    profile, latest, mode, targets = _diet_context(chat_id)

    nudges = []
    if not profile.get("diet_mode"):
        nudges.append("No diet mode set — <code>/diet balanced</code> to pick one.")
    if not latest:
        nudges.append("No weight logged — <code>/weight 72</code> sharpens protein targets.")

    if not meals:
        msg = (
            f"🥗 No meals logged for {label}. Send a photo or describe a meal "
            "to start tracking macros."
        )
        if nudges:
            msg += "\n\n" + "\n".join("• " + n for n in nudges)
        bot.send_message(chat_id, msg)
        return

    if start == end:  # single day: judge the day's intake directly
        analysis = nutrition.analyze_macros(meals, targets)
        head = f"<i>Window: {label}</i>"
    else:  # multi-day: judge the per-day average against the daily targets
        averaged, logged_days = _daily_average_meals(meals)
        analysis = nutrition.analyze_macros(averaged, targets)
        head = f"<i>Daily average over {label} ({logged_days} logged)</i>"
    report = nutrition.format_macro_report(analysis, targets, mode)
    if nudges:
        head += "\n" + "\n".join("• " + n for n in nudges)
    bot.send_message(chat_id, head + "\n\n" + report)


# ── /workout ───────────────────────────────────────────────────────
def _split_workout_args(args: List[str]):
    """Leading recognised muscle-group tokens -> groups; the rest -> notes."""
    groups = []
    idx = 0
    while idx < len(args) and args[idx].strip(",").lower() in _WORKOUT_GROUP_WORDS:
        groups.append(args[idx].strip(","))
        idx += 1
    if not groups:  # nothing recognised: treat the whole thing as the group name
        return " ".join(args), ""
    return " ".join(groups), " ".join(args[idx:])


def _cmd_workout(bot: TelegramBot, chat_id: int, args: List[str]):
    if not args:
        bot.send_message(
            chat_id,
            "Usage: <code>/workout legs</code> or "
            "<code>/workout chest shoulders felt strong</code>.",
        )
        return
    groups, notes = _split_workout_args(args)
    today = database.user_local_today().isoformat()
    database.save_workout(
        chat_id, today, muscle_groups=groups, notes=notes, source="telegram"
    )
    msg = f"💪 Logged workout: <b>{_html(groups)}</b> ({today})."
    if notes:
        msg += f"\n💬 {_html(notes)}"
    bot.send_message(chat_id, msg)


# ── /train (Feature 5) ─────────────────────────────────────────────
def format_train_recommendation(chat_id: int) -> str:
    """Days since each strength group was trained; recommend the most overdue."""
    today = database.user_local_today()
    workouts = database.get_workouts(
        chat_id, (today - timedelta(days=TRAIN_LOOKBACK_DAYS)).isoformat(), today.isoformat()
    )
    last_seen: Dict[str, date] = {}
    for workout in workouts:
        groups = str(workout.get("muscle_groups") or "").lower()
        when = _iso_date(workout.get("date"))
        if when is None:
            continue
        for group in FITNESS_MUSCLE_GROUPS:
            if group in groups and (group not in last_seen or when > last_seen[group]):
                last_seen[group] = when

    lines = ["🏋️ <b>Training readiness</b>", ""]
    scored = []  # (group, days_since_or_None)
    for group in FITNESS_MUSCLE_GROUPS:
        days = (today - last_seen[group]).days if group in last_seen else None
        scored.append((group, days))
        if days is None:
            lines.append(f"• {group.capitalize()}: <b>not in last {TRAIN_LOOKBACK_DAYS}d</b>")
        elif days == 0:
            lines.append(f"• {group.capitalize()}: today")
        else:
            lines.append(f"• {group.capitalize()}: {days}d ago")

    # Longest untrained: never-trained (staleness 999) outrank trained groups.
    recommend = max(scored, key=lambda item: 999 if item[1] is None else item[1])
    lines.append("")
    lines.append(f"👉 Due next: <b>{recommend[0].capitalize()}</b>")
    return "\n".join(lines)


# ── /activity ──────────────────────────────────────────────────────
def format_activity_status(chat_id: int) -> str:
    """Today's activity totals plus the net (consumed − burned) line."""
    activities = _today_activities(chat_id)
    lines = ["🔥 <b>Today's activity</b>", ""]
    if not activities:
        lines.append("No activity logged yet today.")
        lines.append(
            "Log some with <code>/activity 450 8000 5</code> (kcal steps km)."
        )
        return "\n".join(lines)

    burned, steps_total, km_total = _activity_totals(activities)

    # A distance-only session (e.g. 'ran 9.2 km' with no kcal) must still
    # show — but the net line is only honest when something was burned.
    if burned > 0:
        lines.append(f"Burned: <b>{int(round(burned)):,} kcal</b>")
    if steps_total:
        lines.append(f"Steps: {int(round(steps_total)):,}")
    if km_total:
        lines.append(f"Distance: {km_total:g} km")
    if len(lines) == 2:  # rows exist but carry no usable numbers
        lines.append("No burn, steps, or distance recorded yet today.")

    if burned > 0:
        consumed = _today_consumed_calories(chat_id)
        net = consumed - burned
        lines.append("")
        lines.append(
            f"⚖️ Net: <b>{int(round(net)):,} kcal</b> "
            f"({int(round(consumed)):,} eaten − {int(round(burned)):,} burned)"
        )
    return "\n".join(lines)


def _cmd_activity(bot: TelegramBot, chat_id: int, args: List[str]):
    if not args:
        bot.send_message(chat_id, format_activity_status(chat_id))
        return

    today = database.user_local_today().isoformat()

    # Date form: /activity <YYYY-MM-DD> <kcal>
    if _ISO_DATE_RE.match(args[0]):
        when = _iso_date(args[0])
        if when is None:
            bot.send_message(
                chat_id, "That date isn't valid. Use <code>/activity 2026-07-13 450</code>."
            )
            return
        if len(args) < 2:
            bot.send_message(
                chat_id,
                "Add the burned calories: <code>/activity 2026-07-13 450</code>.",
            )
            return
        kcal = _clamp(_arg_float(args[1]), 0, ACTIVITY_KCAL_MAX)
        database.save_activity(
            chat_id, when.isoformat(), source="manual",
            activity_type="manual", active_calories=kcal,
        )
        bot.send_message(
            chat_id,
            f"🔥 Logged <b>{int(round(kcal)):,} kcal</b> burned on {when.isoformat()}.",
        )
        return

    # Positional: /activity [kcal] [steps] [km]
    kcal = _clamp(_arg_float(args[0]), 0, ACTIVITY_KCAL_MAX) if len(args) >= 1 else 0
    steps = int(round(_clamp(_arg_float(args[1]), 0, ACTIVITY_STEPS_MAX))) if len(args) >= 2 else None
    km = _clamp(_arg_float(args[2]), 0, ACTIVITY_KM_MAX) if len(args) >= 3 else None
    raw = {"steps": steps} if steps else None
    database.save_activity(
        chat_id, today, source="manual", activity_type="manual",
        active_calories=kcal or None,
        distance_km=km if km else None,
        raw=raw,
    )
    bits = [f"{int(round(kcal)):,} kcal"]
    if steps:
        bits.append(f"{steps:,} steps")
    if km:
        bits.append(f"{km:g} km")
    bot.send_message(chat_id, "🔥 Logged activity: " + " · ".join(bits) + f" ({today}).")


# ── /train_run ─────────────────────────────────────────────────────
def _parse_race_distance(token):
    """(meters, label) for a race-distance token like '5k'/'marathon', or (None, None)."""
    text = str(token or "").strip().lower()
    named = {
        "marathon": (42195.0, "Marathon"),
        "full": (42195.0, "Marathon"),
        "half": (21097.5, "Half marathon"),
        "half-marathon": (21097.5, "Half marathon"),
        "halfmarathon": (21097.5, "Half marathon"),
        "mile": (1609.344, "Mile"),
    }
    if text in named:
        return named[text]
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(k|km|m|mi|mile|miles)?$", text)
    if not match:
        return None, None
    value = float(match.group(1))
    unit = match.group(2) or "k"
    if unit in ("k", "km"):
        meters, label = value * 1000.0, f"{value:g}K"
    elif unit == "m":
        meters, label = value, f"{value:g}m"
    else:  # mi / mile / miles
        meters, label = value * fitness_plan.KM_PER_MILE * 1000.0, f"{value:g}mi"
    if meters <= 0:
        return None, None
    return meters, label


def _parse_race_time(token) -> Optional[float]:
    """'mm:ss' / 'hh:mm:ss' / bare seconds -> total seconds, or None."""
    text = str(token or "").strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3:
            return None
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        if any(num >= 60 for num in nums[1:]):  # 19:99 is never a real time
            return None
        seconds = 0.0
        for num in nums:
            seconds = seconds * 60 + num
        return seconds if seconds > 0 else None
    value = _arg_float(text, 0)
    return value if value > 0 else None


def _stale_race_notice(profile: Dict, today: date) -> Optional[str]:
    """One-line notice when a stored goal race date has already passed.

    Legacy rows (stored before past dates were rejected) silently pin the
    plan to base phase — surface why instead of letting the plan look broken.
    """
    race = _iso_date((profile or {}).get("goal_race_date"))
    if race is None or race >= today:
        return None
    return (
        f"🎯 Race date {race.isoformat()} has passed — set a new goal with "
        "<code>/train_run race &lt;date&gt;</code>"
    )


def format_todays_run(chat_id: int) -> str:
    """Today's prescribed run for the user's plan (default plan if none set)."""
    today = database.user_local_today()
    profile = _profile_for_plan(chat_id, today)
    workout = fitness_plan.todays_workout(profile, today)
    phase = fitness_plan.phase_for_date(profile, today)
    vdot = _display_vdot(profile)
    lines = [
        f"🏃 <b>Today's run — {today.strftime('%A')}</b>",
        "",
        _html(workout),
        "",
        f"Phase: {phase} · VDOT {vdot:g}",
    ]
    weeks = _weeks_to_race(profile, today)
    if weeks is not None:
        lines.append(f"Race in ~{weeks:g} weeks")
    notice = _stale_race_notice(profile, today)
    if notice:
        lines.append(notice)
    return "\n".join(lines)


def _train_run_set_vdot(bot: TelegramBot, chat_id: int, args: List[str]):
    if len(args) < 2:
        bot.send_message(chat_id, "Usage: <code>/train_run vdot 50</code>.")
        return
    vdot = _arg_float(args[1], 0)
    if not (fitness_plan.VDOT_MIN <= vdot <= fitness_plan.VDOT_MAX):
        bot.send_message(
            chat_id,
            f"VDOT must be between {int(fitness_plan.VDOT_MIN)} "
            f"and {int(fitness_plan.VDOT_MAX)}.",
        )
        return
    database.save_fitness_profile(chat_id, vdot=float(vdot))
    bot.send_message(chat_id, f"🏃 VDOT set to <b>{vdot:g}</b>. Training paces updated.")


def _train_run_set_race_date(bot: TelegramBot, chat_id: int, args: List[str]):
    when = _iso_date(args[1]) if len(args) >= 2 and _ISO_DATE_RE.match(args[1]) else None
    if when is None:
        bot.send_message(chat_id, "Usage: <code>/train_run race 2026-10-01</code>.")
        return
    if when < database.user_local_today():
        # A past goal silently degrades the plan to base forever — reject it
        # with feedback instead of storing it.
        bot.send_message(
            chat_id,
            f"Race date {when.isoformat()} is in the past — use a future date, "
            "e.g. <code>/train_run race 2026-10-01</code>.",
        )
        return
    database.save_fitness_profile(chat_id, goal_race_date=when.isoformat())
    bot.send_message(chat_id, f"📅 Goal race date set to <b>{when.isoformat()}</b>.")


def _train_run_log_race_result(bot: TelegramBot, chat_id: int, args: List[str]) -> bool:
    """True = replied and handled; False = fall through to usage."""
    meters, label = _parse_race_distance(args[0])
    seconds = _parse_race_time(args[1])
    if meters is not None and seconds is not None:
        vdot = fitness_plan.vdot_from_race(meters, seconds)
        if vdot is None:
            bot.send_message(
                chat_id,
                "Couldn't compute VDOT from that race. Check the distance and time.",
            )
            return True
        vdot = round(vdot, 1)
        database.save_fitness_profile(
            chat_id,
            vdot=float(vdot),
            race_distance_km=meters / 1000.0,
            race_time_seconds=int(round(seconds)),
            race_label=label,
        )
        bot.send_message(
            chat_id,
            f"🏃 Computed <b>VDOT {vdot:g}</b> from {_html(label)} "
            f"in {_html(args[1])}.\nTraining paces updated.",
        )
        return True
    return False


_TRAIN_RUN_SUBCOMMANDS = {"vdot": _train_run_set_vdot, "race": _train_run_set_race_date}


def _cmd_train_run(bot: TelegramBot, chat_id: int, args: List[str]):
    if not args:
        bot.send_message(chat_id, format_todays_run(chat_id))
        return
    sub = args[0].lower()

    handler = _TRAIN_RUN_SUBCOMMANDS.get(sub)
    if handler:
        handler(bot, chat_id, args)
        return

    # /train_run <distance> <time>  -> compute & store VDOT
    if len(args) >= 2 and _train_run_log_race_result(bot, chat_id, args):
        return

    bot.send_message(
        chat_id,
        "Try <code>/train_run</code>, <code>/train_run 5k 19:57</code>, "
        "<code>/train_run vdot 50</code>, or <code>/train_run race 2026-10-01</code>.",
    )


# ── /plan ──────────────────────────────────────────────────────────
def format_week_plan(chat_id: int) -> str:
    """This week's Mon–Sun schedule (today marked) + phase + paces + VDOT."""
    today = database.user_local_today()
    profile = _profile_for_plan(chat_id, today)
    plan = fitness_plan.week_plan(profile, today)
    phase = fitness_plan.phase_for_date(profile, today)
    vdot = _display_vdot(profile)
    paces = fitness_plan.paces_from_vdot(vdot)
    today_name = today.strftime("%A")

    lines = [f"📅 <b>This week — {phase} phase</b>", ""]
    for day_name, workout in plan:
        if day_name == today_name:
            lines.append(f"👉 <b>{day_name[:3]}</b>: {_html(workout)}")
        else:
            lines.append(f"   {day_name[:3]}: {_html(workout)}")

    lines.append("")
    lines.append(f"VDOT {vdot:g}")
    lines.append(
        "Paces/km — E {} · M {} · T {} · I {} · R {}".format(
            fitness_plan.format_pace(paces["E"]),
            fitness_plan.format_pace(paces["M"]),
            fitness_plan.format_pace(paces["T"]),
            fitness_plan.format_pace(paces["I"]),
            fitness_plan.format_pace(paces["R"]),
        )
    )
    weeks = _weeks_to_race(profile, today)
    if weeks is not None:
        lines.append(f"Weeks to race: ~{weeks:g}")
    notice = _stale_race_notice(profile, today)
    if notice:
        lines.append(notice)
    return "\n".join(lines)


# ── /profile ───────────────────────────────────────────────────────
def format_fitness_profile(chat_id: int) -> str:
    """Pretty-print the merged fitness profile: diet + targets + VDOT + race."""
    profile = database.get_fitness_profile(chat_id)
    lines = ["🧾 <b>Fitness profile</b>", ""]
    if not profile:
        lines.append("No profile yet. Set one with /diet, /weight, or /train_run.")
        return "\n".join(lines)

    mode = profile.get("diet_mode")
    label = nutrition.MODE_SPECS.get(mode, {}).get("label") if mode else None
    lines.append("<b>Diet</b>")
    lines.append(f"• Mode: {_html(label) if label else '—'}")
    tcals = profile.get("target_calories")
    lines.append(f"• Calorie target: {int(tcals):,} kcal" if tcals else "• Calorie target: —")
    macro_bits = []
    for key, letter in (
        ("target_protein_g", "P"),
        ("target_carbs_g", "C"),
        ("target_fat_g", "F"),
    ):
        val = profile.get(key)
        if val:
            macro_bits.append(f"{letter} {int(val)}g")
    lines.append("• Macro targets: " + (" · ".join(macro_bits) if macro_bits else "—"))
    ppk = profile.get("protein_g_per_kg")
    if ppk:
        lines.append(f"• Protein: {safe_number(ppk):g} g/kg")

    latest = database.get_latest_body_weight(chat_id)
    goal_weight = profile.get("goal_weight_kg")
    if latest or goal_weight:
        lines.append("")
        lines.append("<b>Weight</b>")
        if latest:
            lines.append(
                f"• Latest: {safe_number(latest.get('weight_kg')):g} kg "
                f"({_html(latest.get('date', '?'))})"
            )
        if goal_weight:
            lines.append(f"• Goal: {safe_number(goal_weight):g} kg")

    lines.append("")
    lines.append("<b>Running</b>")
    vdot = profile.get("vdot")
    lines.append(
        f"• VDOT: {safe_number(vdot):g}" if vdot
        else f"• VDOT: {fitness_plan.DEFAULT_VDOT} (default)"
    )
    race_label = profile.get("race_label")
    race_time = profile.get("race_time_seconds")
    if race_label and race_time:
        lines.append(f"• Best: {_html(race_label)} in {_fmt_seconds(race_time)}")
    race_date = profile.get("goal_race_date")
    if race_date:
        lines.append(f"• Goal race: {_html(race_date)}")

    lines.append("")
    lines.append(f"<i>{_html(nutrition.DISCLAIMER)}</i>")
    return "\n".join(lines)


def _maybe_answer_fitness_query(bot: TelegramBot, chat_id: int, text: str) -> bool:
    """Answer read-only run/macro/plan questions with ZERO Gemini spend.

    Returns True when it handled the message (so the caller returns before any
    Gemini call), keeping these working during a quota pause too.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    # A message that looks like it LOGS something (numbers, eating/burning/
    # logging verbs) must reach the NL pipeline so the meal/activity is saved;
    # answering it here would silently drop the log. Worst case a wordy
    # question falls through to Gemini and is still answered — just not free.
    if _FITNESS_LOG_GUARD_RE.search(text):
        return False
    if _RUN_QUERY_RE.search(text):
        bot.send_message(chat_id, format_todays_run(chat_id))
        log.info("  🏃 Deterministic run query answered (no Gemini spend)")
        return True
    if _MACRO_QUERY_RE.search(text):
        _cmd_macros(bot, chat_id, ["today"])
        log.info("  🥗 Deterministic macro query answered (no Gemini spend)")
        return True
    if _PLAN_QUERY_RE.search(text):
        bot.send_message(chat_id, format_week_plan(chat_id))
        log.info("  📅 Deterministic plan query answered (no Gemini spend)")
        return True
    return False


def format_today_summary(chat_id: int) -> str:
    """Format today's calorie & macro summary."""
    meals = get_todays_meals(chat_id)

    if not meals:
        return "📋 <b>Today's Summary</b>\n\nNo meals logged yet today."

    total_cal = sum(safe_number(m["analysis"].get("total_calories")) for m in meals)
    total_p = sum(safe_number(m["analysis"].get("total_protein_g")) for m in meals)
    total_c = sum(safe_number(m["analysis"].get("total_carbs_g")) for m in meals)
    total_f = sum(safe_number(m["analysis"].get("total_fat_g")) for m in meals)

    lines = [
        "📋 <b>Today's Summary</b>\n",
        f"🔥 <b>{total_cal:,} kcal</b>",
        f"🥩 Protein: {total_p}g",
        f"🍞 Carbs: {total_c}g",
        f"🧈 Fat: {total_f}g",
        f"📸 Meals: {len(meals)}",
    ]

    # Net line only when the user actually burned something today. A
    # distance/steps-only session (e.g. 'ran 9.2 km' with no kcal) still
    # gets an activity line instead of vanishing from the summary.
    burned, steps_total, km_total = _activity_totals(_today_activities(chat_id))
    if burned > 0:
        lines.append(f"🔥 Burned: {int(round(burned)):,} kcal")
        lines.append(f"⚖️ Net: <b>{int(round(total_cal - burned)):,} kcal</b>")
    elif steps_total or km_total:
        bits = []
        if km_total:
            bits.append(f"{km_total:g} km")
        if steps_total:
            bits.append(f"{int(round(steps_total)):,} steps")
        lines.append("🏃 Activity: " + " · ".join(bits))

    # Typical-day comparison over the prior 7 user-local days (today excluded).
    # Median rather than mean: under-logged days would bias a mean low.
    local_today = database.user_local_today()
    prior_totals = _daily_calorie_totals(
        chat_id,
        (local_today - timedelta(days=7)).isoformat(),
        (local_today - timedelta(days=1)).isoformat(),
    )
    if len(prior_totals) >= 2:
        typical = int(statistics.median(prior_totals.values()))
        lines.append("")
        lines.append(f"📊 Typical day: ~{typical:,} kcal")
        if total_cal <= typical:
            lines.append(f"⏳ ~{typical - total_cal:,} kcal headroom vs typical")
        else:
            lines.append(f"📈 ~{total_cal - typical:,} kcal above typical")

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
        # safe_number: hostile stored shapes ("640", [], 1e400) must not
        # crash the accumulation.
        cal = safe_number(a.get("total_calories"))
        p = safe_number(a.get("total_protein_g"))
        c = safe_number(a.get("total_carbs_g"))
        f = safe_number(a.get("total_fat_g"))
        total_cal += cal
        corrected = " ✏️" if meal.get("corrected") else ""
        lines.append(f"{i + 1}. <b>{_html(desc)}</b> ({_html(meal.get('time', '?'))}){corrected}")
        lines.append(f"   ~{cal} kcal | P:{p}g C:{c}g F:{f}g")

    lines.append("\n━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔥 <b>Total: ~{total_cal:,} kcal</b> ({len(meals)} meals)")
    return "\n".join(lines)


def format_history(chat_id: int, days: int = 7) -> str:
    """Format a summary of daily calorie totals over the past week."""
    local_today = database.user_local_today()
    # days - 1 back plus today = an inclusive N-day window
    cutoff_date = (local_today - timedelta(days=days - 1)).isoformat()
    today_str = local_today.isoformat()
    daily_cals = _daily_calorie_totals(chat_id, cutoff_date, today_str)

    if not daily_cals:
        return f"📅 <b>{days}-Day History</b>\n\nNo meals logged in the past {days} days."

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


def _coerce_meal_index(value) -> Optional[int]:
    """Coerce a Gemini-supplied meal index to int, or None if it isn't one.

    JSON mode guarantees syntax, not that the model returns integers — a
    hallucinated string/float/null index must not crash correction/delete
    with a TypeError; it is simply treated as no match.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return None
    return None


def _nl_correction(bot: TelegramBot, chat_id: int, text: str, meals: List[Dict], result: Dict):
    meal_index = _coerce_meal_index(result.get("meal_index", 0))
    new_analysis = result.get("analysis", {})
    reason = result.get("reason", "")

    if not meals:
        bot.send_message(chat_id, "❌ Cannot correct because no meals are logged recently.")
        return

    if meal_index is None or meal_index < 0 or meal_index >= len(meals):
        # meal_index is raw model output: escape it (and keep it short) so
        # hallucinated markup can neither inject formatting nor 400 the send.
        shown_index = _html(str(result.get("meal_index"))[:40])
        bot.send_message(chat_id, f"❌ Invalid meal index ({shown_index}). You have {len(meals)} recent meals.")
        return

    # An empty or non-food analysis would overwrite the meal into a row every
    # is_food view filters out — a silent delete wearing a "Corrected!" reply,
    # bypassing the confirmation the real delete path requires. Refuse it.
    if not isinstance(new_analysis, dict) or not new_analysis.get("is_food"):
        bot.send_message(
            chat_id,
            "❌ That correction didn't include a usable updated analysis, so I left "
            "the meal unchanged. Try restating it, e.g. “meal 2 was roast duck rice, ~780 kcal”.",
        )
        return

    # A hostile food_items shape (scalar/dict/strings) persisted here would
    # crash every later prompt build over the edit window; store dicts only.
    if "food_items" in new_analysis:
        new_analysis["food_items"] = safe_food_items(new_analysis)

    # Compute EVERY reply input before the write: numeric coercion via
    # safe_number means a hallucinated string calorie value can no longer
    # raise after update_meal_analysis already ran (the user was told the
    # request failed although the row had been rewritten).
    old_analysis = meals[meal_index]["analysis"]
    old_cal = safe_number(old_analysis.get("total_calories"), 0)
    new_cal = safe_number(new_analysis.get("total_calories"), 0)
    old_desc = old_analysis.get("meal_description", "Unknown")
    new_desc = new_analysis.get("meal_description", old_desc)
    diff = new_cal - old_cal
    diff_str = f"+{diff}" if diff > 0 else str(diff)

    # Update by the DB id from the snapshot Gemini indexed, so a meal
    # logged mid-conversation cannot shift the target.
    meal_id = meals[meal_index]["id"]
    database.update_meal_analysis(meal_id, chat_id, new_analysis)
    reply_lines = [
        f"✏️ <b>Corrected meal {meal_index + 1}!</b>",
        "",
        f"<b>{_html(old_desc)}</b> → <b>{_html(new_desc)}</b>",
        f"🔥 {_html(old_cal)} kcal → {_html(new_cal)} kcal ({_html(diff_str)})",
    ]
    if reason:
        reply_lines.append(f"\n💬 {_html(reason)}")

    bot.send_message(chat_id, "\n".join(reply_lines))
    log.info(f"  ✏️ Corrected meal {meal_index + 1} (DB ID {meal_id}): {old_cal} → {new_cal} kcal")


def _nl_delete(bot: TelegramBot, chat_id: int, text: str, meals: List[Dict], result: Dict):
    meal_indices = result.get("meal_indices", [])
    if not isinstance(meal_indices, list):
        # Iterating a scalar string "12" would stage meals 1 and 2 (a dict,
        # its keys). Honor a bare integer; anything else means "no match".
        if isinstance(meal_indices, int) and not isinstance(meal_indices, bool):
            meal_indices = [meal_indices]
        else:
            meal_indices = []
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
    valid_indices = {i for i in (_coerce_meal_index(x) for x in meal_indices) if i is not None}
    for index in sorted(valid_indices):
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


def _nl_new_meal(bot: TelegramBot, chat_id: int, text: str, meals: List[Dict], result: Dict):
    analysis = result.get("analysis", {})
    if isinstance(analysis, dict) and analysis.get("is_food"):
        # A hostile food_items shape persisted here would crash every later
        # prompt build over the edit window; store dicts only.
        if "food_items" in analysis:
            analysis["food_items"] = safe_food_items(analysis)
        save_meal(chat_id, analysis, "manual_text", "text", "")
        log.info(f"  ✅ Manual Food: {analysis.get('meal_description')} (~{analysis.get('total_calories')} kcal)")
        result_text = format_food_result(chat_id, analysis)
        bot.send_message(chat_id, "✅ Added new manual meal:\n\n" + result_text)
    else:
        bot.send_message(chat_id, "🚫 I couldn't detect food in that description.")


def _nl_log_weight(bot: TelegramBot, chat_id: int, text: str, meals: List[Dict], result: Dict):
    kg = nutrition.parse_weight_kg(text)
    if kg is None:  # fall back to the model's structured field
        field = safe_number(result.get("weight_kg"), 0)
        if nutrition.MIN_WEIGHT_KG <= field <= nutrition.MAX_WEIGHT_KG:
            kg = round(field, 1)
    if kg is None:
        bot.send_message(
            chat_id,
            "⚖️ I couldn't read a valid body weight (30–300 kg). "
            "Try “I weigh 72.5 kg”.",
        )
        return
    today = database.user_local_today()
    database.save_body_weight(chat_id, today.isoformat(), kg, source="telegram")
    bot.send_message(chat_id, f"⚖️ Logged <b>{kg:g} kg</b> for {today.isoformat()}.")
    log.info(f"  ⚖️ NL weight logged: {kg} kg")


def _nl_log_activity(bot: TelegramBot, chat_id: int, text: str, meals: List[Dict], result: Dict):
    kcal = _clamp(safe_number(result.get("active_calories")), 0, ACTIVITY_KCAL_MAX)
    steps = int(round(_clamp(safe_number(result.get("steps")), 0, ACTIVITY_STEPS_MAX)))
    km = _clamp(safe_number(result.get("distance_km")), 0, ACTIVITY_KM_MAX)
    if kcal <= 0 and steps <= 0 and km <= 0:
        bot.send_message(
            chat_id,
            "🏃 I couldn't find any activity numbers to log. "
            "Try “burned 450 kcal running 5 km”.",
        )
        return
    today = database.user_local_today()
    raw = {"steps": steps} if steps else None
    database.save_activity(
        chat_id, today.isoformat(), source="manual", activity_type="manual",
        active_calories=kcal or None, distance_km=km or None, raw=raw,
    )
    bits = []
    if kcal > 0:
        bits.append(f"{int(round(kcal)):,} kcal")
    if steps > 0:
        bits.append(f"{steps:,} steps")
    if km > 0:
        bits.append(f"{km:g} km")
    bot.send_message(
        chat_id, "🏃 Logged activity: " + " · ".join(bits) + f" ({today.isoformat()})."
    )
    log.info(f"  🏃 NL activity logged: {bits}")


def _nl_chat(bot: TelegramBot, chat_id: int, text: str, meals: List[Dict], result: Dict):
    reply = result.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        # A null/blank reply becomes an empty-text sendMessage, which the
        # real Telegram API rejects — the user would hear nothing at all.
        reply = "I'm not sure what you mean. Try describing a meal or correction!"
    bot.send_message(chat_id, _html(reply))
    log.info("  💬 Chat response sent")


_NL_INTENT_HANDLERS = {
    "correction": _nl_correction,
    "delete": _nl_delete,
    "new_meal": _nl_new_meal,
    "log_weight": _nl_log_weight,
    "log_activity": _nl_log_activity,
}

# A compound message ("correct meal 2 AND delete meal 3") expands into this
# many actions at most; anything longer is almost certainly model runaway.
NL_MAX_ACTIONS = 5


def _normalize_nl_actions(result) -> List[Dict]:
    """Coerce any parsed model response into an ordered list of action dicts.

    Gemini's JSON mode guarantees syntax, not schema. Despite the prompt
    demanding a single object, compound requests have produced a bare JSON
    ARRAY of intent objects (which crash-looped the bot on 2026-07-16), and
    the designed compound shape is {"intent": "multi", "actions": [...]}.
    Every shape funnels through here; unusable input yields [] rather than
    an exception.
    """
    if isinstance(result, dict):
        actions = result.get("actions")
        # Only honor "actions" when the wrapper is the designed multi shape
        # or has no recognized single intent of its own — a real single
        # intent carrying a hallucinated decomposition list must win.
        # Hashability guard: a list/dict intent must read as "no recognized
        # single intent", not raise TypeError out of the membership test.
        recognized = (
            isinstance(result.get("intent"), str)
            and result["intent"] in _NL_INTENT_HANDLERS
        )
        use_actions = (
            isinstance(actions, list)
            and actions
            and (result.get("intent") == "multi" or not recognized)
        )
        items = actions if use_actions else [result]
    elif isinstance(result, list):
        items = result
    else:
        return []

    usable = [a for a in items if isinstance(a, dict)]
    dropped = len(items) - len(usable)
    if dropped:
        log.warning(f"  Dropped {dropped} non-dict NL action(s) from model response.")
    if len(usable) > NL_MAX_ACTIONS:
        log.warning(f"  Capping NL actions at {NL_MAX_ACTIONS} (model returned {len(usable)}).")
        usable = usable[:NL_MAX_ACTIONS]

    # _pending_nl_deletes holds ONE confirmation per chat, so a second delete
    # action would dead-end the first prompt's buttons. The prompt says to
    # merge deletions; enforce it here for models that don't.
    deletes = [a for a in usable if a.get("intent") == "delete"]
    if len(deletes) > 1:
        merged: List = []
        for a in deletes:
            indices = a.get("meal_indices")
            if isinstance(indices, list):
                merged.extend(indices)
        deletes[0]["meal_indices"] = merged
        usable = [a for a in usable if a.get("intent") != "delete" or a is deletes[0]]
        log.info(f"  Merged {len(deletes)} delete actions into one ({len(merged)} indices).")
    return usable


def handle_text_message(
    gemini_client: genai.Client,
    bot: TelegramBot,
    chat_id: int,
    text: str,
):
    """Process a text message as either a new meal, correction, or chat."""
    # Deterministic fast-path: read-only run/macro/plan questions are answered
    # from the pure planners with ZERO Gemini spend (before the pause check, so
    # they still work during a quota pause).
    if _maybe_answer_fitness_query(bot, chat_id, text):
        return

    pause_summary = _gemini_quota_pause_summary()
    if pause_summary:
        bot.send_message(
            chat_id,
            "⏸️ <b>Gemini is paused right now.</b>\n\n"
            f"{pause_summary}\n\n"
            "Text corrections and manual meal parsing need Gemini too, so I did not send this request.",
        )
        return

    meals = get_recent_meals(chat_id, days=TEXT_EDIT_WINDOW_DAYS)

    meals_list_str = "No meals logged recently."
    if meals:
        meals_list_parts = []
        for i, meal in enumerate(meals):
            a = meal["analysis"]
            d = meal.get("date", "?")
            desc = a.get("meal_description", "Unknown")
            cal = a.get("total_calories", 0)
            # Stored analyses are untrusted (any past Gemini response shape):
            # one poison row must not kill every later prompt build.
            items_str = ", ".join(
                str(item.get("name", "?")) for item in safe_food_items(a)
            )
            meals_list_parts.append(
                f"[{i}] Date: {d} | Meal: {desc} (~{cal} kcal) — Items: {items_str}"
            )
        meals_list_str = "\n".join(meals_list_parts)

    # Relative-date context lets Gemini resolve "yesterday"/"this morning" to
    # the right Date in the list instead of guessing from raw dates.
    local_now = database.user_local_now()
    today_local = local_now.date()
    prompt = TEXT_HANDLER_PROMPT.format(
        meals_list=meals_list_str,
        user_message=text,
        today=today_local.isoformat(),
        weekday=today_local.strftime("%A"),
        yesterday=(today_local - timedelta(days=1)).isoformat(),
    )

    # 'Typing...' ack immediately before the (up to ~90s) Gemini leg: the
    # observed failure was 24s of dead silence convincing the user their
    # message was lost, prompting a duplicate send. Fire-and-forget.
    bot.send_chat_action(chat_id, "typing")

    try:
        response = _generate_content_with_deadline(
            gemini_client,
            model=GEMINI_MODEL,
            contents=[prompt],
            config=GEMINI_JSON_CONFIG,
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

    # Gemini runs in JSON mode without a response_schema, so the response can
    # be any JSON type and "intent" any value. Normalize everything (single
    # object, {"intent": "multi", "actions": [...]}, or a bare array) into a
    # list of actions; non-string intents fall through to the chat reply,
    # exactly like the pre-refactor if/elif == chain did. All indices in a
    # compound refer to the same `meals` snapshot the prompt showed, and the
    # handlers resolve them to DB ids immediately, so earlier actions cannot
    # shift a later action's target.
    actions = _normalize_nl_actions(result)
    if not actions:
        log.warning(f"  NL response had no usable actions (top-level {type(result).__name__}).")
        bot.send_message(
            chat_id,
            "❌ I couldn't work out what to do with that. "
            "Try one request at a time, e.g. “change meal 2 to roast duck rice”.",
        )
        return

    failures = 0
    for action in actions:
        intent = action.get("intent")
        handler = _NL_INTENT_HANDLERS.get(intent, _nl_chat) if isinstance(intent, str) else _nl_chat
        try:
            handler(bot, chat_id, text, meals, action)
        except Exception as e:
            # One malformed action must not abort its siblings (or the bot).
            failures += 1
            log.error(f"  NL action {intent!r} failed: {bot._redact(e)}")
    if failures == len(actions):
        note = (
            "❌ That request failed. Please try again."
            if failures == 1
            else f"❌ All {failures} requested actions failed. Please try again."
        )
        bot.send_message(chat_id, note)
    elif failures:
        bot.send_message(
            chat_id,
            f"⚠️ {failures} of {len(actions)} requested action(s) failed — the rest were applied.",
        )


def handle_text_message_safe(gemini_client, bot: TelegramBot, chat_id: int, text: str):
    """handle_text_message that can never take down the polling loop.

    An exception escaping into main() kills the process BEFORE the update's
    offset is confirmed to Telegram, so systemd restarts into a re-delivery
    of the same poisoned message — an unbounded crash loop that also burns a
    Gemini call per cycle (observed live on 2026-07-16 with a compound
    correction+delete message). Contain everything here.
    """
    try:
        handle_text_message(gemini_client, bot, chat_id, text)
    except Exception as e:
        # Defense in depth: network errors can embed the token URL.
        log.error(f"Unhandled error in text handler: {bot._redact(e)}")
        try:
            bot.send_message(chat_id, "❌ Something went wrong handling that message. Please try again.")
        except Exception:
            log.error("Could not send the text-handler failure notice.")


def handle_callback_query(gemini_client, bot: TelegramBot, callback_query: Dict) -> bool:
    """Handle Telegram inline-button actions."""
    callback_id = callback_query.get("id", "")
    # An explicit null (or non-string) data field must fall through to the
    # "Unknown action." answer, not raise before the callback is answered.
    data = callback_query.get("data") or ""
    if not isinstance(data, str):
        data = ""
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


def _analyze_telegram_photo_background(
    gemini_client,
    bot: TelegramBot,
    chat_id: int,
    image_bytes: bytes,
    file_id: str,
    img_hash: str,
    staged_path: Optional[Path] = None,
    analysis_slot: Optional[dict] = None,
    caption: str = "",
):
    """Analyze a Telegram photo off the long-poll thread and report the result.

    A user caption ("half portion, no rice") is applied AFTER the meal is
    saved, as an ordinary NL correction against the just-saved meal — it is
    never dispatched as a command (pinned security property), and a caption
    on a not-food photo does nothing.

    Owns the hash reservation made by handle_photo_message: every exit path
    must mark it saved/skipped/failed or release it. Also owns the staged
    crash-recovery copy: it is removed on every normal exit, so it only
    survives a hard kill — where the boot sweep recovers it as a failed
    upload and offers /retry_failed.
    """
    reserved_photo = True
    # A caption only takes effect when the meal actually saves; every other
    # exit must SAY the caption wasn't applied — silently dropping "only ate
    # half" means the eventually-logged meal overstates what the user
    # explicitly corrected for.
    caption_note = ""
    if isinstance(caption, str) and caption.strip() and not caption.strip().startswith("/"):
        caption_note = (
            "\n\nℹ️ Your caption was NOT applied (the photo wasn't logged as a meal"
            " yet). Send it as a normal message once the meal is logged."
        )
    try:
        # Feedback via chat action instead of a send+delete message pair:
        # two full RTTs off this path, and the "uploading photo..." bubble
        # auto-expires (~5s) so there is nothing to clean up. Fire-and-forget.
        bot.send_chat_action(chat_id, "upload_photo")
        normalized_bytes = _normalize_photo_for_analysis(image_bytes)
        analysis = analyze_food_photo(gemini_client, image_bytes, normalized_bytes)

        if analysis is None:
            if _gemini_quota_pause():
                try:
                    if staged_path is None:
                        staged_path = _stage_api_upload(image_bytes, img_hash, "telegram_photo.jpg")
                    failed_path = _keep_failed_api_upload(staged_path, img_hash)
                    staged_path = None  # ownership moved to the failed dir
                    database.mark_photo_hash_status(chat_id, img_hash, "failed", source="telegram")
                    reserved_photo = False
                    _send_saved_upload_decision(
                        bot,
                        chat_id,
                        failed_path,
                        img_hash,
                        source_label="Telegram",
                    )
                    if caption_note:
                        bot.send_message(chat_id, caption_note.strip())
                except OSError as e:
                    database.release_photo_hash(chat_id, img_hash)
                    reserved_photo = False
                    log.error(f"Could not save Telegram photo for later retry: {e}")
                    bot.send_message(
                        chat_id,
                        "❌ <b>I couldn't analyze that photo and could not save it for retry.</b>\n\n"
                        f"{_gemini_failure_context()}" + caption_note,
                    )
                return

            database.release_photo_hash(chat_id, img_hash)
            reserved_photo = False
            bot.send_message(
                chat_id,
                "❌ <b>I couldn't analyze that photo.</b>\n\n"
                f"{_gemini_failure_context()}\n\n"
                "Try <code>/gemini</code> to run a live check." + caption_note,
            )
            return

        if analysis.get("is_food"):
            save_meal(chat_id, analysis, "telegram", file_id, img_hash,
                      mark_status="saved")
            reserved_photo = False
            log.info(
                f"  ✅ Food: {analysis.get('meal_description')} "
                f"(~{analysis.get('total_calories')} kcal)"
            )
            result_text = format_food_result(chat_id, analysis)
            caption_text = caption.strip() if isinstance(caption, str) else ""
            if caption_text.startswith("/"):
                # Pinned property: captions NEVER enter command dispatch.
                # Say so on the meal card instead of silently dropping it.
                result_text += (
                    "\n\n<i>(caption ignored: commands in captions are not executed)</i>"
                )
            bot.send_message(chat_id, result_text)
            if caption_text and not caption_text.startswith("/"):
                # Frame the caption before the NL call: unframed, a caption
                # that NAMES the food ("beef noodle soup" — the most common
                # caption) hits the prompt's food-is-always-new_meal rule and
                # double-logs the meal that was just saved from this photo,
                # and "half portion" gives the model no cue which meal it
                # targets. handle_text_message_safe contains its own
                # failures — a bad caption cannot kill this cleanup path.
                desc = str(analysis.get("meal_description", "the photographed meal"))[:80]
                framed = (
                    "(This text was the caption on a food photo that was JUST "
                    f'logged as the most recent meal in the list: "{desc}". '
                    "If the caption describes, names, or adjusts that meal, "
                    'treat it as a "correction" to that meal — do NOT log a '
                    "new meal for it. If it is unrelated, handle it normally.) "
                    f"Caption: {caption_text}"
                )
                handle_text_message_safe(gemini_client, bot, chat_id, framed)
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
    finally:
        # The caller's admission slot is owned here from the moment this
        # thread actually ran; every exit returns it.
        _release_analysis_slot(analysis_slot)
        if staged_path is not None:
            _discard_api_upload(staged_path)
        # The freed slot may unblock a parked (slot-denied) photo.
        _drain_one_parked_photo(bot, gemini_client)


def handle_photo_message(
    gemini_client,
    bot: TelegramBot,
    chat_id: int,
    message: Dict,
    user: str = "User",
) -> bool:
    """Handle a Telegram photo (or image document) message.

    Returns True when the message carried an image, so the caller should stop
    processing it. Download, duplicate detection, and the hash reservation run
    synchronously — a re-send arriving in the same update batch still hits the
    reservation — then analysis moves to a background thread so a slow
    Claude/Gemini call cannot stall the long-poll loop (same shape as the
    /upload background thread; non-daemon so shutdown joins in-flight work).
    """
    photos = message.get("photo")
    document = message.get("document")
    file_id = None

    # Guarded extraction: this runs BEFORE the try/except below, so a
    # malformed photo/document field must degrade to "not an image"
    # (return False), never raise through _process_update.
    if isinstance(photos, list) and photos and isinstance(photos[-1], dict):
        file_id = photos[-1].get("file_id")
    elif isinstance(document, dict):
        mime = document.get("mime_type", "")
        if isinstance(mime, str) and mime.startswith("image/"):
            file_id = document.get("file_id")
            if file_id:
                log.info(f"  📎 Received image as document ({mime})")

    if not file_id:
        return False

    # Caption rides along as post-save NL context ("half portion, no rice");
    # non-string shapes degrade to no caption. Telegram-chat photos only —
    # the /upload path stays caption-free.
    caption = message.get("caption")
    if not isinstance(caption, str):
        caption = ""

    log.info(f"[{user}] Received photo, analyzing...")
    img_hash = ""
    reserved_photo = False
    analysis_slot = None
    try:
        image_bytes = bot.get_file(file_id)

        # Telegram getFile hands over up to 20MB with no cap of its own
        # (MAX_API_UPLOAD_BYTES only guarded /upload): refuse oversize
        # images before they pin memory for an analysis thread's lifetime.
        if len(image_bytes) > MAX_API_UPLOAD_BYTES:
            bot.send_message(
                chat_id,
                "❌ That image is too large for me to analyze "
                f"(over {MAX_API_UPLOAD_BYTES // (1024 * 1024)}MB). "
                "Please send a smaller photo.",
            )
            return True

        # Duplicate detection
        img_hash = hashlib.md5(image_bytes).hexdigest()
        if is_duplicate_photo(chat_id, img_hash):
            log.info("  🔄 Duplicate photo detected, skipping")
            bot.send_message(
                chat_id,
                "🔄 This looks like the same photo you already sent!\n"
                "It won't be counted twice.",
            )
            return True

        # A deliberate human re-send may re-log a photo that was
        # previously skipped, failed, or whose meal was deleted;
        # the automated /upload path stays strict.
        if not database.reserve_photo_hash(
            chat_id,
            img_hash,
            "telegram",
            reclaim_statuses={"failed", "skipped", "deleted"},
        ):
            log.info("  🔄 Photo already reserved or logged, skipping")
            bot.send_message(
                chat_id,
                "🔄 This photo is already being processed or was already logged.\n"
                "It won't be counted twice.",
            )
            return True
        reserved_photo = True

        # Admission control BEFORE staging/thread spawn: past the cap, park
        # the ORIGINAL bytes as a failed upload (durable) and queue it for
        # the auto-drain that runs when a slot frees — instead of demanding
        # a manual re-send. The full-size image goes to disk, not RAM.
        analysis_slot = _acquire_analysis_slot()
        if analysis_slot is None:
            log.warning("  ⏳ Photo analysis slots exhausted; parking the photo for auto-drain.")
            try:
                parked_staged = _stage_api_upload(image_bytes, img_hash, "telegram_photo.jpg")
            except OSError as e:
                parked_staged = None
                log.error(f"Could not park slot-denied Telegram photo: {e}")
            if parked_staged is None:
                # Unwritable disk (or a stubbed stage in tests): degrade to
                # the old ask-for-a-re-send.
                database.release_photo_hash(chat_id, img_hash)
                reserved_photo = False
                bot.send_message(
                    chat_id,
                    "⏳ I am analyzing several photos right now — please re-send this one in a minute.",
                )
                return True
            parked_path = _keep_failed_api_upload(parked_staged, img_hash)
            database.mark_photo_hash_status(chat_id, img_hash, "failed", source="telegram")
            reserved_photo = False
            _park_photo_for_drain(parked_path)
            # The drain-on-release hook can have already fired (and seen an
            # empty queue) between our failed acquire and the park — kick one
            # drain attempt now so a photo can't wait on all-free slots.
            _drain_one_parked_photo(bot, gemini_client)
            queued_note = ""
            if isinstance(caption, str) and caption.strip() and not caption.strip().startswith("/"):
                # The drain path has no caption plumbing: say so now rather
                # than silently logging the uncorrected meal later.
                queued_note = (
                    "\n\nℹ️ Your caption will NOT be applied automatically — "
                    "send it as a normal message once the meal is logged."
                )
            bot.send_message(
                chat_id,
                "⏳ Queued — I'm analyzing several photos right now and will "
                "analyze this one automatically when a slot frees." + queued_note,
            )
            return True

        # Stage a crash-recovery copy before spawning the thread: the
        # long-poll offset is confirmed on the next getUpdates, so Telegram
        # never re-delivers this photo. A hard kill mid-analysis leaves this
        # file for the boot sweep; the background thread removes it on every
        # normal exit. Best-effort — an unwritable disk must not block
        # analysis (that photo just stays kill-unrecoverable, as before).
        staged_path = None
        try:
            staged_path = _stage_api_upload(image_bytes, img_hash, "telegram_photo.jpg")
        except OSError as e:
            log.warning(f"Could not stage Telegram photo for crash recovery: {e}")

        worker = threading.Thread(
            target=_analyze_telegram_photo_background,
            args=(gemini_client, bot, chat_id, image_bytes, file_id, img_hash, staged_path,
                  analysis_slot, caption),
        )
        worker.start()
        # Slot ownership passed to the worker's finally. A thread double that
        # never runs the target (test fakes; a failed spawn) must not strand
        # the slot — the token release is idempotent, so reclaiming here is
        # safe even when a synchronous fake already released it.
        if not (hasattr(worker, "is_alive") and worker.is_alive()):
            _release_analysis_slot(analysis_slot)
    except Exception as e:
        if reserved_photo:
            database.release_photo_hash(chat_id, img_hash)
        _release_analysis_slot(analysis_slot)
        # The freed slot may unblock a parked (slot-denied) photo.
        _drain_one_parked_photo(bot, gemini_client)
        # Defense in depth: network errors can embed the token URL.
        log.error(f"Error processing photo: {bot._redact(e)}")
        bot.send_message(chat_id, "❌ Error processing your photo. Please try again.")
    return True


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

        if request.content_length and request.content_length > MAX_API_UPLOAD_BYTES:
            return jsonify({"error": "Photo too large"}), 413

        if 'photo' in request.files:
            file = request.files['photo']
            image_bytes = file.stream.read(MAX_API_UPLOAD_BYTES + 1)
            upload_name = file.filename or "upload"
        else:
            # iOS Shortcuts "Request Body: File" sends the image as the RAW
            # request body with no multipart wrapper — the practical
            # workaround for an iOS bug where Form file fields silently
            # coerce to ~50 bytes of text. Accept the body when it actually
            # looks like an image. The read itself is capped: a chunked body
            # has no Content-Length for the early 413 guard, and get_data()
            # would buffer a multi-GB stream into RAM before any slice.
            body = request.stream.read(MAX_API_UPLOAD_BYTES + 1)
            if _looks_like_image(body):
                image_bytes = body
                upload_name = "raw_body.jpg"
                log.info(
                    f"  📥 /upload raw-body image ({len(body)} bytes, "
                    f"platform={request.headers.get('X-Client-Platform', '?')})"
                )
            else:
                # Field-level detail makes a malformed client diagnosable
                # from the server log alone.
                log.info(
                    f"  ❌ /upload 400: no 'photo' file field and body is not an image. "
                    f"files={list(request.files.keys())} form={list(request.form.keys())} "
                    f"content_length={request.content_length} "
                    f"platform={request.headers.get('X-Client-Platform', '?')}"
                )
                return jsonify({"error": "No photo provided"}), 400

        if len(image_bytes) > MAX_API_UPLOAD_BYTES:
            return jsonify({"error": "Photo too large"}), 413
        if not image_bytes:
            log.info(
                f"  ❌ /upload 400: empty photo bytes "
                f"(platform={request.headers.get('X-Client-Platform', '?')})"
            )
            return jsonify({"error": "Empty photo"}), 400

        img_hash = hashlib.md5(image_bytes).hexdigest()
        # Raw-body clients have no form fields, so both declarations are also
        # accepted as headers.
        declared_hash = (request.form.get("original_hash")
                         or request.headers.get("X-Original-Hash") or "").strip().lower()
        if _ORIGINAL_HASH_RE.match(declared_hash):
            # The phone may recompress before uploading; dedup, /reconcile,
            # and the meals ledger must key on the ORIGINAL file's hash.
            img_hash = declared_hash
        # Additive: the client may declare when the photo was TAKEN so a
        # backfilled upload (nightly sync, offline-queue drain) lands on the
        # capture day's ledger instead of the upload day's.
        captured_at = _parse_captured_at(request.form.get("captured_at", "")
                                         or request.headers.get("X-Captured-At", ""))

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
            upload_path = _stage_api_upload(image_bytes, img_hash, upload_name)
        except OSError as e:
            database.release_photo_hash(ALLOWED_CHAT_ID, img_hash)
            _finish_api_upload_processing(img_hash)
            log.error(f"  ❌ API Upload: Could not stage upload: {e}")
            return jsonify({"error": "Could not stage upload"}), 500

        # Admission control before spawning the worker: the file is already
        # staged, so a denied upload is kept as a failed upload — the phone
        # gets a durable "saved" answer and /retry_all_failed drains it.
        analysis_slot = _acquire_analysis_slot()
        if analysis_slot is None:
            failed_path = _keep_failed_api_upload(upload_path, img_hash)
            database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "failed", source="api_upload")
            _finish_api_upload_processing(img_hash)
            log.warning(
                f"  ⏳ /upload denied an analysis slot (cap {MAX_CONCURRENT_ANALYSES}); "
                f"kept for retry at {failed_path}"
            )
            # Without a notice, a parked burst is SILENT meal loss: the phone
            # saw HTTP 200 so it never re-offers, and /reconcile treats
            # failed-dir hashes as present. Tell the user once per minute.
            global _last_busy_notice_monotonic
            with _busy_notice_lock:
                now_mono = time.monotonic()
                should_notify = now_mono - _last_busy_notice_monotonic > 60
                if should_notify:
                    _last_busy_notice_monotonic = now_mono
            if should_notify:
                bot.send_message(
                    ALLOWED_CHAT_ID,
                    "⏳ <b>Some phone uploads were parked — analysis slots are busy.</b>\n\n"
                    "They are saved as failed uploads and nothing is lost. "
                    "Drain them with <code>/retry_all_failed 3</code> once the "
                    "current photos finish.",
                )
            return jsonify({"status": "saved_for_retry_busy"})

        # Process the photo in a background thread to return 200 OK instantly to iOS
        def background_process(upload_file_path, hsh, device, captured=None):
            device_display = _html(device)
            staged_path = Path(upload_file_path)
            processing_msg = None

            def drop_processing_notice():
                # Deleted AFTER the outcome message is sent: deleting first
                # spends an extra RTT that delays the meal card, and leaves
                # a feedback gap if the outcome send is slow.
                nonlocal processing_msg
                if processing_msg and processing_msg.get("message_id"):
                    bot.delete_message(ALLOWED_CHAT_ID, processing_msg["message_id"])
                processing_msg = None

            try:
                log.info(f"🔍 Analyzing food from {device} API upload in background...")

                # Send instant feedback to the user so they know the watcher worked
                processing_msg = bot.send_message(ALLOWED_CHAT_ID, f"📲 Received photo from {device_display}, analyzing... 🔍")

                try:
                    bytes_data = staged_path.read_bytes()
                except OSError as e:
                    log.error(f"  ❌ API Upload: Could not read staged upload: {e}")
                    drop_processing_notice()
                    # Mirror the unhandled-exception path: if the staged file
                    # still exists, keep it (under the declared hash) and mark
                    # the reservation failed. Releasing the reservation while
                    # the file stays in pending would let the boot sweep's md5
                    # fallback re-key it under the recompressed bytes' hash,
                    # enabling duplicate meals and double Gemini spend.
                    if staged_path.exists():
                        failed_path = _keep_failed_api_upload(staged_path, hsh)
                        database.mark_photo_hash_status(ALLOWED_CHAT_ID, hsh, "failed", source="api_upload")
                        bot.send_message(
                            ALLOWED_CHAT_ID,
                            f"⚠️ <b>Auto-upload from {device_display} failed before analysis.</b>\n\n"
                            "The server could not read the staged image file, so it was not logged. "
                            "I kept the file on the server for manual retry/debugging:\n"
                            f"<code>{_html(failed_path)}</code>\n"
                            f"Hash: <code>{_html(hsh)}</code>\n\n"
                            f"Retry later: <code>/retry_failed {_html(_failed_upload_selector(failed_path, hsh))}</code>",
                        )
                    else:
                        database.release_photo_hash(ALLOWED_CHAT_ID, hsh)
                        bot.send_message(
                            ALLOWED_CHAT_ID,
                            f"⚠️ <b>Auto-upload from {device_display} failed before analysis.</b>\n\n"
                            "The server could not read the staged image file, so it was not logged.",
                        )
                    return

                # Single-decode normalization BEFORE the analyzer: the same
                # bytes feed Claude's temp file, Gemini's inline blob, AND
                # the chat echo below (the echo artifact is pre-computed —
                # no post-analysis PIL spike on this path anymore).
                normalized_bytes = _normalize_photo_for_analysis(bytes_data)
                analysis = analyze_food_photo_with_retries(
                    gemini_client, bytes_data, normalized_bytes=normalized_bytes
                )

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
                    drop_processing_notice()
                    return

                if analysis.get("is_food"):
                    save_meal(ALLOWED_CHAT_ID, analysis, "api_auto", "api", hsh,
                              captured_at=captured, mark_status="saved")
                    log.info(f"  ✅ API Food: {analysis.get('meal_description')} (~{analysis.get('total_calories')} kcal)")
                    result_text = format_food_result(ALLOWED_CHAT_ID, analysis)
                    # Discard the staged file BEFORE the echo: the meal is
                    # saved and hash-marked, and (when normalization failed
                    # and the echo has to re-decode) the echo's PIL decode is
                    # the biggest memory spike on this path — dying inside it
                    # must not leave a pending file for the boot sweep to
                    # "recover" into a false failed-upload notice.
                    _discard_api_upload(staged_path)
                    _echo_meal_photo(
                        bot, ALLOWED_CHAT_ID, bytes_data,
                        f"📲 <b>Auto-Logged from {device_display}:</b>\n\n" + result_text,
                        normalized_bytes=normalized_bytes,
                    )
                    drop_processing_notice()
                else:
                    log.info("  ⏭️ API Upload: Not food")
                    database.mark_photo_hash_status(ALLOWED_CHAT_ID, hsh, "skipped", source="api_auto")
                    _discard_api_upload(staged_path)
                    drop_processing_notice()
            except Exception as e:
                log.exception(f"Unhandled API upload background error for {hsh}")
                drop_processing_notice()
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
                _release_analysis_slot(analysis_slot)
                _finish_api_upload_processing(hsh)
                # The freed slot may unblock a parked (slot-denied) photo.
                _drain_one_parked_photo(bot, gemini_client)

        try:
            worker = threading.Thread(target=background_process, args=(str(upload_path), img_hash, device_name, captured_at))
            worker.start()
        except Exception as e:
            # Thread spawn fails exactly under the memory pressure admission
            # control exists for: without this, the slot leaks permanently
            # (3 leaks = all photo analysis dead until restart) and the hash
            # stays stuck in the processing set.
            log.error(f"  ❌ /upload could not spawn analysis worker: {e}")
            _release_analysis_slot(analysis_slot)
            _drain_one_parked_photo(bot, gemini_client)
            failed_path = _keep_failed_api_upload(upload_path, img_hash)
            database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "failed", source="api_upload")
            _finish_api_upload_processing(img_hash)
            return jsonify({"status": "saved_for_retry_busy"})
        # Slot ownership passed to the worker's finally; reclaim (idempotent)
        # if a thread double never ran the target — see handle_photo_message.
        if not (hasattr(worker, "is_alive") and worker.is_alive()):
            _release_analysis_slot(analysis_slot)

        return jsonify({"status": "processing_in_background"})

    return app


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def _sd_notify(state: str):
    """Best-effort sd_notify(3): send one datagram to systemd's NOTIFY_SOCKET.

    Silent no-op when NOTIFY_SOCKET is unset (dev runs, tests, non-systemd).
    A leading '@' means the Linux abstract socket namespace and translates
    to a leading NUL byte. All OSErrors are swallowed: watchdog petting is
    defense in depth against hangs and must never become a crash source.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(addr)
            sock.send(state.encode())
        finally:
            sock.close()
    except OSError as e:
        log.debug(f"sd_notify({state!r}) failed: {e}")


# ─── Progress-gated watchdog heartbeat ───────────────────────────────
# WatchdogSec must catch TRUE hangs (process alive, nothing moving) without
# killing healthy-but-slow work: /retry_all_failed runs minutes of
# synchronous analysis on the poll thread, a 20MB getFile can trickle, and
# black-holed sendMessage retries stack. In-loop pets cannot tell those
# apart from a hang, so a dedicated daemon thread pets every ~30s for as
# long as the shared last-progress stamp is fresher than the hang
# threshold. Slow work stamps progress at bounded checkpoints; a real hang
# stops stamping, the pets stop with it, and systemd restarts the bot.

_WATCHDOG_HEARTBEAT_INTERVAL_SECONDS = 30
# Generous multiple of the longest legitimate gap between progress stamps
# (Claude CLI lock wait + 120s run ≈ 360s is the worst bounded segment;
# every other checkpoint is far shorter), so only a genuine hang starves
# the heartbeat.
WATCHDOG_HANG_THRESHOLD_SECONDS = 600

_progress_lock = threading.Lock()
_last_progress_monotonic = time.monotonic()


def _stamp_progress():
    """Record that the process just completed a bounded unit of work.

    Called from the poll loop, long synchronous operations, and background
    workers alike: the watchdog's question is "is this PROCESS alive and
    moving", and any thread's bounded progress answers it.
    """
    global _last_progress_monotonic
    with _progress_lock:
        _last_progress_monotonic = time.monotonic()


def _progress_age_seconds() -> float:
    with _progress_lock:
        return time.monotonic() - _last_progress_monotonic


def _watchdog_heartbeat_once() -> bool:
    """Send one pet iff progress is fresh; returns whether it was sent."""
    if _progress_age_seconds() > WATCHDOG_HANG_THRESHOLD_SECONDS:
        return False
    _sd_notify("WATCHDOG=1")
    return True


def _watchdog_heartbeat_loop(stop_event):
    stall_logged = False
    while not stop_event.wait(_WATCHDOG_HEARTBEAT_INTERVAL_SECONDS):
        if _watchdog_heartbeat_once():
            stall_logged = False
        elif not stall_logged:
            # Once per stall: the upcoming watchdog kill must be
            # explainable from the journal.
            stall_logged = True
            log.error(
                f"Watchdog pets withheld: no progress for {int(_progress_age_seconds())}s "
                f"(threshold {WATCHDOG_HANG_THRESHOLD_SECONDS}s) — letting systemd's "
                "WatchdogSec restart the bot."
            )


def _start_watchdog_heartbeat():
    """Start the heartbeat thread; returns an Event that stops it.

    Daemon on purpose: Python joins only non-daemon threads at interpreter
    shutdown, so the heartbeat keeps petting while in-flight upload threads
    finish — a slow-but-legal stop is TimeoutStopSec's business, not the
    watchdog's.
    """
    _stamp_progress()
    stop_event = threading.Event()
    threading.Thread(
        target=_watchdog_heartbeat_loop,
        args=(stop_event,),
        name="watchdog-heartbeat",
        daemon=True,
    ).start()
    return stop_event


# Progress-stamp sleep slice: keeps a deliberate long sleep visibly alive
# to the heartbeat thread, far inside the hang threshold.
_PROGRESS_SLEEP_SLICE_SECONDS = 30


def _sleep_with_progress_stamps(seconds):
    """time.sleep(seconds) in <=30s slices, stamping progress in between.

    The getUpdates backoff sleeps up to 300s. A deliberate backoff is
    healthy waiting, not a hang, so each slice refreshes the progress clock
    and the heartbeat thread keeps petting through it. The total time slept
    is exactly `seconds`, preserving the pinned backoff schedule.
    """
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, _PROGRESS_SLEEP_SLICE_SECONDS)
        time.sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            _stamp_progress()


def _sweep_stranded_pending_uploads(bot: TelegramBot):
    """Recover uploads stranded mid-processing by a previous shutdown.

    Anything still in the pending dir at boot was never analyzed; move it to
    the failed dir so /retry_failed can see it instead of it sitting invisible.
    """
    moved = []
    still_pending = []
    for path in _pending_upload_items():
        # The sweep runs after READY=1 (watchdog armed): stamp per file so
        # a boot recovering a large backlog never reads as a boot-time hang.
        _stamp_progress()
        try:
            # Bytes are read lazily, only when the ledger has no entry.
            img_hash = _upload_file_ledger_hash(path)
        except OSError as e:
            log.warning(f"Could not read stranded pending upload {path}: {e}")
            still_pending.append(path)
            continue
        if database.meal_image_hash_exists(ALLOWED_CHAT_ID, img_hash):
            # A kill inside the save->discard window: the meal IS logged, only
            # the staged copy survived. Quietly drop it — flipping the row to
            # 'failed' and announcing a recovery would steer the user toward
            # a pointless (and duplicate-risking) /retry_failed.
            _discard_api_upload(path)
            database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "saved", source="startup_sweep")
            log.info(f"♻️ Discarded stranded staged copy of already-saved meal {img_hash}.")
            continue
        failed_path = _keep_failed_api_upload(path, img_hash)
        if failed_path == path:
            # The move failed; the file stayed behind in the pending dir.
            still_pending.append(path)
        database.mark_photo_hash_status(ALLOWED_CHAT_ID, img_hash, "failed", source="startup_sweep")
        moved.append(failed_path.name)

    # A crash between reserving a hash and finishing analysis leaves a
    # 'processing' row with no file behind it; phone retries then get
    # "duplicate" (and delete their queued copy) until the reservation goes
    # stale. Nothing can be legitimately in flight at boot, so release any
    # reservation that has no staged or failed file backing it.
    failed_prefixes = _failed_upload_hash_prefixes()
    # Files still in the pending dir (e.g. their bytes were unreadable above)
    # DO back their reservation — parse the hash prefix from the filename so
    # we never release a row whose file merely failed to read.
    pending_prefixes = set()
    for path in still_pending:
        match = _UPLOAD_NAME_HASH_RE.search(path.name)
        if match:
            pending_prefixes.add(match.group(1).lower())
    released = 0
    for img_hash in database.get_processing_photo_hashes(ALLOWED_CHAT_ID):
        prefix = _image_hash_prefix(img_hash)
        if prefix in failed_prefixes or prefix in pending_prefixes:
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
# An update that has killed the process this many times gets skipped, not
# retried forever. Three real attempts, then the ledger declares it poison.
POISON_UPDATE_MAX_ATTEMPTS = 3


def _poison_update_should_skip(bot: TelegramBot, update_id) -> bool:
    """Durable bounded retry for updates that kill the process outright.

    The per-update try/except in main() contains ordinary exceptions, but it
    cannot survive OOM/SIGKILL mid-update. This ledger can: every update's
    attempt count is persisted BEFORE processing, so an update that keeps
    dying arrives back (offset unconfirmed -> Telegram re-delivers) with a
    growing count and is eventually skipped instead of crash-looping the
    service forever (the 2026-07-16 failure mode, then only stopped by an
    in-code bug fix). Entries live in service_health.json as
    {'n': count, 'at': timestamp} (legacy bare-int counts are tolerated),
    pruned by RECENCY to 20 — pruning by numeric id would evict a small
    post-reset update_id on every sighting and crash-loop forever after a
    Telegram update_id reset. An update already in the processed ring
    (see _poison_update_clear) is a redelivery of completed work: skipped
    quietly, without counting an attempt.
    """
    if update_id is None:
        return False
    key = str(update_id)
    seen = {}

    def mutate(data):
        ring = data.get("processed_updates")
        if isinstance(ring, list) and key in ring:
            seen["already_processed"] = True
            return
        attempts = data.get("update_attempts")
        attempts = dict(attempts) if isinstance(attempts, dict) else {}
        prev = attempts.get(key)
        if isinstance(prev, dict) and isinstance(prev.get("n"), int):
            n = prev["n"] + 1
        elif isinstance(prev, int):  # legacy bare-count entry
            n = prev + 1
        else:
            n = 1
        seen["n"] = n
        attempts[key] = {"n": n, "at": service_health.timestamp()}
        if len(attempts) > 20:
            # Evict the LEAST RECENTLY SEEN entries, never the one just
            # written (legacy int entries have no 'at' and age out first).
            def _last_seen(k):
                value = attempts[k]
                return value.get("at", "") if isinstance(value, dict) else ""

            stale = sorted((k for k in attempts if k != key), key=_last_seen)
            for old in stale[:len(attempts) - 20]:
                attempts.pop(old, None)
        data["update_attempts"] = attempts

    service_health.update(mutate, SERVICE_HEALTH_PATH, warn=log.warning)
    if seen.get("already_processed"):
        log.info(f"Skipping already-processed update {update_id} (redelivery).")
        return True
    n = seen.get("n", 1)
    if n <= POISON_UPDATE_MAX_ATTEMPTS:
        return False
    log.error(f"☠️ Skipping poison update {update_id} after {n - 1} fatal attempts.")
    # NOTE: the count only ever reaches this threshold for updates whose
    # processing KILLED the process — _poison_update_clear() removes the
    # entry once an update completes, so healthy updates that merely share
    # re-delivered batches with a poison one never accumulate attempts.
    if n == POISON_UPDATE_MAX_ATTEMPTS + 1:  # notify once, not on every re-sight
        try:
            bot.send_message(
                ALLOWED_CHAT_ID,
                "⚠️ <b>I skipped a message that crashed me repeatedly.</b>\n\n"
                f"It failed {n - 1} times in a row, so I gave up on it to stay alive. "
                "Please rephrase it or split it into smaller requests.",
            )
        except Exception:
            log.error("Could not send the poison-update notice.")
    return True


def _poison_update_clear(update_id):
    """Forget an update's attempt count once the poll loop survived it.

    Without this, healthy updates sharing a re-delivered batch with a
    poison one would accumulate attempts in lockstep and eventually be
    skipped with a false "crashed me repeatedly" notice.

    Also records the update in the durable processed ring (newest 256), so
    a kill AFTER the clear but BEFORE the offset is confirmed cannot re-run
    the update's side effects on redelivery — _poison_update_should_skip
    recognizes ring members and skips them quietly.
    """
    if update_id is None:
        return
    key = str(update_id)

    def mutate(data):
        attempts = data.get("update_attempts")
        if isinstance(attempts, dict) and key in attempts:
            attempts.pop(key, None)
            data["update_attempts"] = attempts
        ring = data.get("processed_updates")
        ring = [str(x) for x in ring] if isinstance(ring, list) else []
        if key not in ring:
            ring.append(key)
        if len(ring) > 256:
            # Prune by insertion recency, NOT numeric value: after a Telegram
            # update_id reset DOWNWARD, numeric pruning would evict every new
            # small id on the call that added it (defeating at-most-once) and
            # pin 256 stale big ids forever — the same reset bug the attempts
            # ledger's recency pruning already guards against. Appends are
            # chronological, so a tail slice IS recency.
            ring = ring[-256:]
        data["processed_updates"] = ring

    service_health.update(mutate, SERVICE_HEALTH_PATH, warn=log.warning)


def _record_clean_shutdown():
    """Stamp an orderly exit so the next boot doesn't count as a crash."""

    def mutate(data):
        data["last_clean_shutdown"] = service_health.timestamp()

    service_health.update(mutate, SERVICE_HEALTH_PATH, warn=log.warning)


def _record_boot_and_maybe_alert(bot: TelegramBot):
    """Crash-loop tripwire: 3+ CRASH boots inside 10 minutes, alerted once.

    A boot following a clean shutdown (systemctl restart, deploys) is normal
    operation and never counts; only boots with no recent clean-shutdown
    stamp — crashes, OOM kills — feed the alarm, and the alert latches for
    10 minutes so a live crash loop doesn't also spam the chat.
    """
    now = datetime.now()
    state = {}

    def _within(data, key, seconds):
        try:
            return 0 <= (now - datetime.fromisoformat(data.get(key, ""))).total_seconds() <= seconds
        except (ValueError, TypeError):
            return False

    def mutate(data):
        clean_restart = _within(data, "last_clean_shutdown", 300)
        data.pop("last_clean_shutdown", None)  # consume the stamp
        boots = []
        for ts in data.get("recent_crash_boots") or []:
            try:
                if (now - datetime.fromisoformat(ts)).total_seconds() <= 600:
                    boots.append(ts)
            except (ValueError, TypeError):
                pass
        if not clean_restart:
            boots.append(now.isoformat(timespec="seconds"))
        data["recent_crash_boots"] = boots[-10:]
        state["n"] = len(boots)
        state["alert"] = len(boots) >= 3 and not _within(data, "last_boot_alert_at", 600)
        if state["alert"]:
            data["last_boot_alert_at"] = now.isoformat(timespec="seconds")

    service_health.update(mutate, SERVICE_HEALTH_PATH, warn=log.warning)
    if state.get("alert"):
        log.warning(f"⚠️ {state['n']} crash boots within 10 minutes — possible crash loop.")
        try:
            bot.send_message(
                ALLOWED_CHAT_ID,
                f"⚠️ <b>I have crash-restarted {state['n']} times in the last 10 minutes.</b>\n\n"
                "Something may be crashing me repeatedly. "
                "Check recent errors with <code>/logs 20</code>.",
            )
        except Exception:
            log.error("Could not send the rapid-restart alert.")


def _process_update(gemini_client, bot: TelegramBot, update: Dict):
    """Handle one Telegram update: callbacks, commands, photos, free text.

    Extracted from main() so the poll loop can wrap each update in the
    poison-update guard and a catch-all: before this existed, one update
    that raised took the process down BEFORE the offset was confirmed,
    and systemd restarted straight into a re-delivery crash loop
    (observed live 2026-07-16)."""
    callback_query = update.get("callback_query")
    if callback_query:
        handle_callback_query(gemini_client, bot, callback_query)
        return

    message = update.get("message")
    if not isinstance(message, dict):
        return

    # Structurally invalid messages are ignored, not raised: one malformed
    # update must never burn its whole batch in main()'s catch-all.
    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if chat_id is None:
        return
    sender = message.get("from")
    user = sender.get("first_name", "User") if isinstance(sender, dict) else "User"

    # ─── Security Check ──────────────────────────────
    if chat_id != ALLOWED_CHAT_ID:
        log.warning(f"Unauthorized access attempt from chat_id: {chat_id} (User: {user})")
        bot.send_message(
            chat_id,
            "🚫 You are not authorized to use this bot. This is a private instance.",
        )
        return

    text = message.get("text")
    if not isinstance(text, str):  # explicit null / non-string text
        text = ""
    text = text.strip()
    command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
    args = text.split()[1:] if text.startswith("/") else []

    # ─── Commands ────────────────────────────────────
    if command in {"/start", "/help"}:
        log.info(f"[{user}] /help")
        bot.send_message(chat_id, HELP_TEXT)
        return

    if command == "/commands":
        log.info(f"[{user}] /commands")
        bot.send_message(chat_id, COMMAND_MENU_TEXT)
        return

    if command == "/today":
        log.info(f"[{user}] /today")
        bot.send_message(chat_id, format_today_summary(chat_id))
        return

    if command == "/meals":
        log.info(f"[{user}] /meals")
        bot.send_message(chat_id, format_meals_list(chat_id))
        return

    if command == "/recent":
        limit = _parse_positive_int(args[0], 10, 1, 25) if args else 10
        log.info(f"[{user}] /recent {limit}")
        bot.send_message(chat_id, format_recent_meals(chat_id, limit=limit))
        return
                    
    if command == "/history":
        days = _parse_positive_int(args[0], 7, 1, 60) if args else 7
        log.info(f"[{user}] /history {days}")
        bot.send_message(chat_id, format_history(chat_id, days=days))
        return

    if command == "/ping_android":
        # Silent heartbeat ping
        # legacy — no known clients; watcher heartbeats via HTTP /ping
        database.update_android_heartbeat()
        return

    if command == "/status":
        log.info(f"[{user}] /status")
        bot.send_message(chat_id, format_operational_status())
        return

    if command == "/doctor":
        log.info(f"[{user}] /doctor")
        bot.send_message(chat_id, "🩺 Running self-check...")
        bot.send_message(chat_id, run_doctor(gemini_client))
        return

    if command == "/gemini":
        log.info(f"[{user}] /gemini")
        bot.send_message(chat_id, "🔎 Running Gemini probe...")
        bot.send_message(chat_id, run_gemini_probe(gemini_client))
        return

    if command == "/android":
        log.info(f"[{user}] /android")
        bot.send_message(chat_id, format_android_status())
        return

    if command == "/queue":
        limit = _parse_positive_int(args[0], 8, 1, 25) if args else 8
        log.info(f"[{user}] /queue {limit}")
        bot.send_message(chat_id, format_queue_status(limit))
        return

    if command == "/failed":
        log.info(f"[{user}] /failed")
        bot.send_message(chat_id, format_failed_uploads())
        return

    if command == "/retry_failed":
        selector = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else "latest"
        log.info(f"[{user}] /retry_failed {selector}")
        bot.send_message(chat_id, f"🔁 Retrying failed upload: <code>{escape(selector)}</code>")
        bot.send_message(chat_id, retry_failed_upload(gemini_client, selector))
        return

    if command == "/retry_all_failed":
        limit = _parse_positive_int(args[0], 3, 1, RETRY_ALL_FAILED_MAX) if args else 3
        log.info(f"[{user}] /retry_all_failed {limit}")
        bot.send_message(chat_id, f"🔁 Retrying up to {limit} failed saved upload(s)...")
        bot.send_message(chat_id, retry_all_failed_uploads(gemini_client, limit))
        return

    if command == "/clear_failed":
        confirmed = bool(args) and args[-1].lower() == "confirm"
        selector_parts = args[:-1] if confirmed else args
        selector = " ".join(selector_parts) or "latest"
        log.info(f"[{user}] /clear_failed {selector} confirmed={confirmed}")
        bot.send_message(chat_id, clear_failed_uploads(selector, confirmed=confirmed))
        return

    if command == "/vpn":
        log.info(f"[{user}] /vpn")
        bot.send_message(chat_id, format_vpn_status())
        return

    if command == "/report":
        selector = args[0] if args else "today"
        log.info(f"[{user}] /report {selector}")
        bot.send_message(chat_id, f"📊 Generating report for <code>{escape(selector)}</code>...")
        send_long_message(bot, chat_id, generate_report_for_command(selector))
        return

    if command == "/report_status":
        log.info(f"[{user}] /report_status")
        bot.send_message(chat_id, format_report_status())
        return

    if command == "/reports":
        limit = _parse_positive_int(args[0], 8, 1, 25) if args else 8
        log.info(f"[{user}] /reports {limit}")
        bot.send_message(chat_id, format_saved_reports(limit))
        return

    if command == "/logs":
        line_count = _parse_positive_int(args[0], 30, 1, 80) if args else 30
        log.info(f"[{user}] /logs {line_count}")
        send_long_message(bot, chat_id, format_recent_logs(line_count))
        return

    if command == "/config":
        log.info(f"[{user}] /config")
        bot.send_message(chat_id, format_safe_config())
        return

    if command == "/stats":
        log.info(f"[{user}] /stats")
        bot.send_message(chat_id, format_database_stats(chat_id))
        return

    # ─── Fitness & diet (pure DB + arithmetic, no Gemini) ──
    if command == "/weight":
        log.info(f"[{user}] /weight {' '.join(args)}".rstrip())
        _cmd_weight(bot, chat_id, args)
        return

    if command == "/diet":
        log.info(f"[{user}] /diet {' '.join(args)}".rstrip())
        _cmd_diet(bot, chat_id, args)
        return

    if command == "/macros":
        log.info(f"[{user}] /macros {' '.join(args)}".rstrip())
        _cmd_macros(bot, chat_id, args)
        return

    if command == "/workout":
        log.info(f"[{user}] /workout {' '.join(args)}".rstrip())
        _cmd_workout(bot, chat_id, args)
        return

    if command == "/train":
        log.info(f"[{user}] /train")
        bot.send_message(chat_id, format_train_recommendation(chat_id))
        return

    if command == "/activity":
        log.info(f"[{user}] /activity {' '.join(args)}".rstrip())
        _cmd_activity(bot, chat_id, args)
        return

    if command == "/train_run":
        log.info(f"[{user}] /train_run {' '.join(args)}".rstrip())
        _cmd_train_run(bot, chat_id, args)
        return

    if command == "/plan":
        log.info(f"[{user}] /plan")
        bot.send_message(chat_id, format_week_plan(chat_id))
        return

    if command == "/profile":
        log.info(f"[{user}] /profile")
        bot.send_message(chat_id, format_fitness_profile(chat_id))
        return

    # ─── Handle photos ────────────────────────────────
    if handle_photo_message(gemini_client, bot, chat_id, message, user):
        return

    # ─── Ignore non-text messages ─────────────────────
    if not text or text.startswith("/"):
        return

    # ─── Smart Text Handler ───────────────────────
    log.info(f"[{user}] Text: {text}")
    handle_text_message_safe(gemini_client, bot, chat_id, text)


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
    # Heartbeat BEFORE READY=1: the READY datagram is the moment systemd
    # starts enforcing WatchdogSec, so the petter must already be running.
    _start_watchdog_heartbeat()
    # READY before ANY Telegram send: under Type=notify systemd holds the
    # unit in "activating" and enforces TimeoutStartSec until READY=1
    # arrives, and everything below can send messages — the crash-loop
    # alert's sendMessage retries can stack past 120s, the stranded-upload
    # sweep touches many files and sends a summary (60s HTTP timeout each).
    # A slow boot must not be killed as a failed start.
    _sd_notify("READY=1")
    # Tripwire next: a boot that crashes inside the sweep/app build below
    # must still count as a crash boot, or a boot-time crash loop restarts
    # forever without ever feeding the alert.
    _record_boot_and_maybe_alert(bot)
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

    # systemd stop sends SIGTERM; raising KeyboardInterrupt on the main thread
    # reuses the existing shutdown path, and the interpreter then joins the
    # non-daemon upload threads so in-flight uploads finish before exit.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    _sweep_stranded_pending_uploads(bot)
    # Parked (slot-denied) photos survive a restart only as failed-dir files;
    # the in-memory drain queue does not. One boot notice keeps the "queued —
    # will analyze automatically" promise from becoming silent meal loss.
    leftover_failed = len(_failed_upload_items())
    if leftover_failed:
        bot.send_message(
            ALLOWED_CHAT_ID,
            f"📂 <b>{leftover_failed} saved upload(s) on disk from before the restart</b> "
            "(including any photos that were queued for auto-analysis).\n\n"
            "Drain them with <code>/retry_all_failed 3</code>.",
        )

    app = _build_api_app(bot, gemini_client)

    # Start Flask in a background thread
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False), daemon=True).start()
    log.info("🚀 Flask REST API started on port 5000")
    
    log.info("Bot is running! Listening for corrections and commands.")
    log.info("Press Ctrl+C to stop.\n")

    try:
        while True:
            updates = bot.get_updates(timeout=30)
            # Progress stamp once per poll iteration: the heartbeat thread
            # turns fresh stamps into WATCHDOG=1 pets, catching the hang
            # class Restart=always cannot see (process alive, loop stuck —
            # e.g. the old no-deadline Gemini hang). get_updates itself is
            # bounded (60s HTTP timeout; backoff sleeps are sliced with
            # their own stamps), so the clock stays fresh while healthy.
            _stamp_progress()
            maybe_warn_stale_android_heartbeat(bot)

            for update in updates:
                update_id = update.get("update_id")
                try:
                    if _poison_update_should_skip(bot, update_id):
                        continue
                    _process_update(gemini_client, bot, update)
                except Exception as e:
                    # Contain per-update failures: an escape here would kill
                    # the process pre-offset-confirm and crash-loop on the
                    # re-delivered update. The durable attempt ledger above
                    # backstops even hard kills (OOM/SIGKILL) this cannot see.
                    log.error(f"Unhandled error processing update {update_id}: {bot._redact(e)}")
                # Survived (processed or contained): forget the attempt count
                # so healthy batch-mates of a poison update never accumulate.
                # A skipped poison update `continue`s past this on purpose.
                _poison_update_clear(update_id)
                # Stamp per processed update too: a big redelivered batch of
                # slow updates could otherwise outlast the hang threshold
                # even though each one is making healthy progress.
                _stamp_progress()

    except KeyboardInterrupt:
        # SIGTERM is routed here too, so systemctl stop/restart and deploys
        # stamp a clean shutdown — the crash-loop tripwire ignores them.
        # Fresh progress stamp first: the interpreter still joins non-daemon
        # upload threads (bounded by TimeoutStopSec=180), and the daemon
        # heartbeat keeps petting through that join on this stamp's budget.
        _stamp_progress()
        _sd_notify("STOPPING=1")
        _record_clean_shutdown()
        log.info("\n👋 Bot stopped.")


if __name__ == "__main__":
    main()
