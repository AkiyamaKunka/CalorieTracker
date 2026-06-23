"""
Shared utilities for CalorieTracker.
"""

import json


def parse_ai_json(text: str) -> dict:
    """Parse JSON from a Gemini response, stripping markdown fences if present."""
    content = text.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines)
    return json.loads(content)
