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
