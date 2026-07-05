"""
Shared utilities for CalorieTracker.
"""

import json
import re


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
