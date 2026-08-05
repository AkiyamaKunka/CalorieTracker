"""
Shared utilities for CalorieTracker.
"""

import json
import logging
import re
from typing import List, Optional

import requests

from config import PUSHPLUS_TOKEN


def parse_ai_json(text: str) -> dict:
    """Parse JSON from a Gemini response, tolerating common markdown wrappers."""
    content = (text or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start_candidates = [pos for pos in (content.find("{"), content.find("[")) if pos != -1]
        if not start_candidates:
            raise

        start = min(start_candidates)
        end_obj = content.rfind("}")
        end_arr = content.rfind("]")
        end = max(end_obj, end_arr)
        if end <= start:
            raise

        return json.loads(content[start:end + 1])


def _as_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Magnitude guard: absurd values from hallucinated JSON (400-digit ints,
    # 1e400 -> inf) overflow int/float arithmetic downstream, and inf/NaN are
    # never real calorie counts. NaN fails this range test too.
    if not (-1e9 < value < 1e9):
        return None
    return value


def safe_number(value, default=0):
    """Bounded numeric coercion for untrusted analysis fields.

    Returns `default` for anything _as_number rejects (non-numerics, bools,
    inf/NaN, absurd magnitudes) so accumulations can never raise.
    """
    number = _as_number(value)
    return default if number is None else number


def safe_food_items(analysis):
    """The analysis's food_items as a list of dicts, tolerating any shape.

    Gemini's JSON mode guarantees syntax, not schema: food_items may be a
    scalar, and entries may be strings. Renderers iterate this instead of
    trusting the raw field.
    """
    if not isinstance(analysis, dict):
        return []
    items = analysis.get("food_items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def meal_calorie_mismatch(analysis: dict):
    """Return the item-calorie sum when it contradicts the stored meal total.

    Gemini sometimes hallucinates a meal total that disagrees with its own
    per-item breakdown (observed in production: items summing to 135 kcal
    under a 1335 kcal total). Returns the item sum when the disagreement
    exceeds max(100 kcal, 20%), else None. Missing or non-numeric data is
    treated as consistent — a crashed warning would be worse than the bug
    it flags.
    """
    item_sum = 0
    counted = 0
    for item in safe_food_items(analysis):
        cal = _as_number(item.get("estimated_calories"))
        if cal is not None:
            item_sum += cal
            counted += 1
    total = _as_number(analysis.get("total_calories")) if isinstance(analysis, dict) else None
    if not counted or item_sum <= 0 or total is None:
        return None
    if abs(total - item_sum) > max(100, 0.2 * max(total, item_sum)):
        return int(item_sum)
    return None


def parse_boolish(value) -> Optional[bool]:
    """Tri-state boolean parse: True/False for documented spellings, else None."""
    if value is None:
        return None

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return None


def push_wechat(title: str, content: str, topic: Optional[str] = None) -> bool:
    """Send a plain-text WeChat notification via PushPlus. Never raises.

    Shared by the daily report and the bot's Telegram-outage alert — WeChat
    is the channel that still reaches the user when Telegram itself is down.
    Template "txt" renders `content` literally, so callers may pass user
    text without HTML-escaping it for PushPlus. Returns False (no-op)
    without PUSHPLUS_TOKEN, True only on a confirmed code-200 delivery.
    """
    if not PUSHPLUS_TOKEN:
        return False
    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "txt",
    }
    if topic is not None:
        payload["topic"] = topic
    try:
        response = requests.post("https://www.pushplus.plus/send", json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        if result.get("code") == 200:
            return True
        logging.getLogger("calorie_bot").warning(f"PushPlus API error: {result}")
        return False
    except Exception as e:
        # Alert paths call this while already handling an outage; a push
        # failure must degrade to False, never become a second crash.
        logging.getLogger("calorie_bot").warning(f"PushPlus send failed: {e}")
        return False


def utf16_len(text: str) -> int:
    """Telegram measures message limits in UTF-16 code units, not codepoints."""
    return len(text.encode("utf-16-le")) // 2


def telegram_message_chunks(text: str, limit: int = 3900) -> List[str]:
    """Split a Telegram message into <=limit chunks without breaking normal lines.

    ``limit`` counts UTF-16 code units (Telegram's yardstick): astral emoji
    are 2 units each. Overlong lines are split codepoint-by-codepoint, so a
    boundary can never fall inside a surrogate pair.
    """
    chunks = []
    current = ""
    current_units = 0
    for line in str(text).splitlines(keepends=True):
        line_units = utf16_len(line)
        if line_units > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
                current_units = 0
            piece = ""
            piece_units = 0
            for ch in line:
                ch_units = 2 if ord(ch) > 0xFFFF else 1
                if piece_units + ch_units > limit:
                    chunks.append(piece.rstrip())
                    piece = ""
                    piece_units = 0
                piece += ch
                piece_units += ch_units
            if piece:
                chunks.append(piece.rstrip())
            continue

        if current and current_units + line_units > limit:
            chunks.append(current.rstrip())
            current = line
            current_units = line_units
        else:
            current += line
            current_units += line_units

    if current:
        chunks.append(current.rstrip())
    return chunks or [""]


def compact_leftover_original(raw) -> "Optional[str]":
    """Re-sanitize the app's compact original-analysis JSON for the
    leftover prompt (2026-08-05).

    The client already sends a whitelisted compact form, but this server
    NEVER embeds network JSON into a CLI prompt verbatim: the object is
    parsed and REBUILT from a field whitelist (numbers coerced, names
    stringified and length-capped), so no smuggled keys or oversized
    strings ride into the prompt. None = unusable input (caller 400s).
    """
    import shared_generated

    if isinstance(raw, (dict,)):
        parsed = raw
    else:
        if not isinstance(raw, str) or not raw.strip():
            return None
        if len(raw) > shared_generated.API_LEFTOVER_MAX_CHARS:
            return None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError, RecursionError):
            # RecursionError: a deeply nested array bomb under the size
            # cap crashed the endpoint with a 500 (pressure-test find).
            return None
    if not isinstance(parsed, dict):
        return None
    try:
        # Force any lurking deep structure through a bounded round-trip
        # BEFORE fields are str()'d into the compact.
        json.dumps(parsed)
    except (RecursionError, ValueError, TypeError):
        return None

    def _num(v):
        n = safe_number(v, 0)
        return n if isinstance(n, (int, float)) else 0

    compact = {
        "meal_description": str(parsed.get("meal_description", "Meal"))[:300],
        "total_calories": _num(parsed.get("total_calories")),
        "total_protein_g": _num(parsed.get("total_protein_g")),
        "total_carbs_g": _num(parsed.get("total_carbs_g")),
        "total_fat_g": _num(parsed.get("total_fat_g")),
        "food_items": [
            {
                "name": str(item.get("name", "?"))[:200],
                "estimated_calories": _num(item.get("estimated_calories")),
            }
            for item in safe_food_items(parsed)[:30]
        ],
    }
    out = json.dumps(compact, ensure_ascii=False)
    if len(out) > shared_generated.API_LEFTOVER_MAX_CHARS:
        compact["food_items"] = []
        out = json.dumps(compact, ensure_ascii=False)
    return out


def compact_recent_meals(raw) -> "Optional[list]":
    """Sanitize the app's recent-meals list for the AUTO leftover check
    (2026-08-05): parsed and REBUILT from a whitelist per meal — same
    posture as compact_leftover_original. None = unusable (caller 400s);
    a valid-but-empty list is fine (no check requested).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    out = []
    for meal in raw[:5]:
        if not isinstance(meal, dict):
            return None
        out.append({
            "time": str(meal.get("time", ""))[:20],
            "meal_description": str(meal.get("meal_description", "Meal"))[:300],
            "total_calories": safe_number(meal.get("total_calories"), 0),
            "food_items": [
                {
                    "name": str(item.get("name", "?"))[:200],
                    "estimated_calories": safe_number(
                        item.get("estimated_calories"), 0),
                }
                for item in safe_food_items(meal)[:30]
            ],
        })
    return out


def build_recent_meals_block(compacts) -> str:
    """The RECENT MEALS prompt block — BYTE-IDENTICAL to the Dart
    formatRecentMealsBlock (core/leftover_logic.dart), pinned by
    tests: same analysis text on both platforms or the model behaves
    differently per provider path.
    """
    if not compacts:
        return ""
    lines = ["", "RECENT MEALS (today, for the leftover check):"]
    for i, c in enumerate(compacts):
        items = c.get("food_items") or []
        bits = ", ".join(
            f"{it.get('name', '?')} (~{round(safe_number(it.get('estimated_calories'), 0))} kcal)"
            for it in items
        )
        line = (
            f"[{i}] {c.get('time', '')} — {c.get('meal_description', 'Meal')} "
            f"(~{round(safe_number(c.get('total_calories'), 0))} kcal)"
        )
        if bits:
            line += f": {bits}"
        lines.append(line)
    return "\n".join(lines)
