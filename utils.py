"""
Shared utilities for CalorieTracker.
"""

import json
import re
from typing import List


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
    return value


def meal_calorie_mismatch(analysis: dict):
    """Return the item-calorie sum when it contradicts the stored meal total.

    Gemini sometimes hallucinates a meal total that disagrees with its own
    per-item breakdown (observed in production: items summing to 135 kcal
    under a 1335 kcal total). Returns the item sum when the disagreement
    exceeds max(100 kcal, 20%), else None. Missing or non-numeric data is
    treated as consistent — a crashed warning would be worse than the bug
    it flags.
    """
    items = (analysis or {}).get("food_items") or []
    item_sum = 0
    counted = 0
    for item in items:
        cal = _as_number((item or {}).get("estimated_calories"))
        if cal is not None:
            item_sum += cal
            counted += 1
    total = _as_number((analysis or {}).get("total_calories"))
    if not counted or item_sum <= 0 or total is None:
        return None
    if abs(total - item_sum) > max(100, 0.2 * max(total, item_sum)):
        return int(item_sum)
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
