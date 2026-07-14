"""
Shared utilities for CalorieTracker.
"""

import json
import re
from typing import List, Optional


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


def telegram_message_chunks(text: str, limit: int = 3900) -> List[str]:
    """Split a Telegram message into <=limit chunks without breaking normal lines."""
    chunks = []
    current = ""
    for line in str(text).splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            for start in range(0, len(line), limit):
                chunks.append(line[start:start + limit].rstrip())
            continue

        if current and len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line

    if current:
        chunks.append(current.rstrip())
    return chunks or [""]
