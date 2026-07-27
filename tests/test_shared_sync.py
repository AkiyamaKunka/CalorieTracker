"""Drift gate: the generated shared bindings must match shared/ sources.

scripts/sync_shared.py generates shared_generated.py (server) and
app/lib/core/shared_generated.dart (mobile app) from shared/prompts/*.txt
and shared/constants.json. Editing any of those without regenerating —
or hand-editing a generated file — fails here AND in the app's suite.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_shared_bindings_are_in_sync():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_shared.py"), "--check"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"stale shared bindings:\n{proc.stdout}{proc.stderr}"


def test_config_prompts_come_from_shared():
    import config
    import shared_generated

    assert config.TEXT_HANDLER_PROMPT == shared_generated.TEXT_HANDLER_PROMPT_TEMPLATE
    base = config.FOOD_DETECTION_PROMPT
    marker = "\n\nUser's Dietary Profile / Cultural Context:"
    if marker in base:
        base = base.split(marker)[0]
    assert base == shared_generated.FOOD_DETECTION_PROMPT_RAW


def test_prompt_admits_order_screenshots_and_nutrition_labels():
    """A calorie tracker logs what was EATEN, not only what was photographed:
    takeout order screenshots and nutrition labels are evidence of a meal
    (user request 2026-07-27). Both sides read this from shared/, so pinning
    it here pins the app too."""
    import config
    p = config.FOOD_DETECTION_PROMPT
    for phrase in ["TEXTUAL EVIDENCE", "order confirmation", "NUTRITION LABEL",
                   "meal_description"]:
        assert phrase in p, phrase


def test_prompt_refuses_meals_without_evidence_of_eating():
    """The counterweight: a phantom meal corrupts the log worse than a
    missing one, so menus/ads/recipes/social feeds must stay is_food false."""
    import config
    p = config.FOOD_DETECTION_PROMPT
    for phrase in ["menus", "adverts", "recipes", "social feed",
                   "cancelled order", "phantom meal"]:
        assert phrase in p, phrase
