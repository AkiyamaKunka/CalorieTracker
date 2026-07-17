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
