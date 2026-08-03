"""The calorie-evidence priority ladder (user feature 2026-08-03).

The ladder lives in shared/prompts/estimation_priority.txt (the editable
"skill" file) and is spliced into the photo prompt at sync time. These
tests pin the CONTRACT: the ladder reaches both platform bindings, in
order, with the splice machinery intact — so a future edit that breaks
the placeholder or comment-stripping fails loudly here instead of
silently shipping a prompt with no ladder.
"""

from pathlib import Path

import shared_generated

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "shared" / "prompts" / "estimation_priority.txt"
DART = ROOT / "app" / "lib" / "core" / "shared_generated.dart"


def test_ladder_reaches_the_python_binding_in_order():
    p = shared_generated.FOOD_DETECTION_PROMPT_RAW
    i1 = p.find("Priority 1 — A NUTRITION LABEL")
    i2 = p.find("Priority 2 — A PRINTED WEIGHT")
    i3 = p.find("Priority 3 — A BRAND, CHAIN LOGO, OR MERCHANT")
    i4 = p.find("Priority 4 — VISUAL ESTIMATION")
    assert -1 not in (i1, i2, i3, i4)
    assert i1 < i2 < i3 < i4, "the ladder must keep its order"
    # The fallback ladder ends where the calibration rules begin.
    assert i4 < p.find("PORTION SIZE (the Priority-4 fallback)")


def test_ladder_reaches_the_dart_binding_identically():
    dart = DART.read_text(encoding="utf-8")
    for marker in (
        "Priority 1 — A NUTRITION LABEL",
        "Priority 2 — A PRINTED WEIGHT",
        "Priority 3 — A BRAND, CHAIN LOGO, OR MERCHANT",
        "Priority 4 — VISUAL ESTIMATION",
    ):
        assert marker in dart, f"Dart binding is missing: {marker}"


def test_no_placeholder_or_comment_lines_leak_into_the_prompt():
    p = shared_generated.FOOD_DETECTION_PROMPT_RAW
    assert "<<ESTIMATION_PRIORITY>>" not in p
    assert "TO ADD A PRIORITY" not in p, "'#' doc lines must be stripped"


def test_skill_file_documents_how_to_extend():
    text = SKILL.read_text(encoding="utf-8")
    assert "TO ADD A PRIORITY" in text
    assert "sync_shared.py" in text


def test_china_specific_label_rules_survive():
    # kJ-per-100g labels are the norm on Chinese packaging; losing the
    # conversion rule would silently 4x-inflate labeled foods.
    p = shared_generated.FOOD_DETECTION_PROMPT_RAW
    assert "kJ ÷ 4.184" in p
    assert "净含量" in p
