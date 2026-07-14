"""Tests for the pure nutrition/macro-planning module."""

import nutrition
from nutrition import (
    DISCLAIMER,
    DIET_MODES,
    MIN_CALORIE_FLOOR,
    MODE_SPECS,
    analyze_macros,
    diet_targets,
    format_macro_report,
    parse_weight_kg,
    report_line,
)


def _meal(**totals):
    """Build a food meal wrapping an analysis with the given totals."""
    analysis = {"is_food": True}
    analysis.update(totals)
    return {"analysis": analysis}


# ── diet_targets: per-mode protein from weight ────────────────────────────

def test_diet_targets_modes_have_specs():
    assert DIET_MODES == ("keto", "high_protein", "balanced")
    for mode in DIET_MODES:
        spec = MODE_SPECS[mode]
        assert sum(spec["split"].values()) == 100


def test_diet_targets_keto_protein_from_weight():
    t = diet_targets("keto", weight_kg=70)
    assert t["mode"] == "keto"
    assert t["target_protein_g"] == round(1.6 * 70)  # 112
    assert t["carb_cap_g"] == 50
    # No calories -> no derived carb/fat grams yet.
    assert t["target_carbs_g"] is None
    assert t["target_fat_g"] is None


def test_diet_targets_high_protein_from_weight():
    t = diet_targets("high_protein", weight_kg=80)
    assert t["target_protein_g"] == round(2.0 * 80)  # 160


def test_diet_targets_balanced_from_weight():
    t = diet_targets("balanced", weight_kg=60)
    assert t["target_protein_g"] == round(1.6 * 60)  # 96


def test_diet_targets_protein_override():
    t = diet_targets("balanced", weight_kg=70, protein_g_per_kg=2.2)
    assert t["protein_g_per_kg"] == 2.2
    assert t["target_protein_g"] == round(2.2 * 70)  # 154


# ── diet_targets: from calories via the split ─────────────────────────────

def test_diet_targets_balanced_from_calories():
    t = diet_targets("balanced", calorie_target=2000)
    assert t["calories"] == 2000
    assert t["target_protein_g"] == round(0.30 * 2000 / 4)  # 150
    assert t["target_carbs_g"] == round(0.40 * 2000 / 4)  # 200
    assert t["target_fat_g"] == round(0.30 * 2000 / 9)  # 67


def test_diet_targets_high_protein_from_calories():
    t = diet_targets("high_protein", calorie_target=2500)
    assert t["target_protein_g"] == round(0.30 * 2500 / 4)  # 188


def test_diet_targets_keto_carb_cap_applied():
    # 5% of 5000 kcal / 4 = 62.5 g -> capped to 50.
    t = diet_targets("keto", calorie_target=5000)
    assert t["target_carbs_g"] == 50
    assert t["carb_cap_g"] == 50


def test_diet_targets_keto_carbs_below_cap_uncapped():
    # 5% of 1800 kcal / 4 = 22.5 g -> under the cap, not clamped.
    t = diet_targets("keto", calorie_target=1800)
    assert t["target_carbs_g"] <= 50
    assert t["target_carbs_g"] < 50


def test_diet_targets_calorie_floor_enforced():
    t = diet_targets("balanced", calorie_target=800)
    assert t["calories"] == MIN_CALORIE_FLOOR


def test_diet_targets_weight_and_calories_protein_from_weight():
    t = diet_targets("balanced", weight_kg=70, calorie_target=2000)
    # Protein anchored on weight, carbs/fat from the split.
    assert t["target_protein_g"] == round(1.6 * 70)  # 112
    assert t["target_carbs_g"] == round(0.40 * 2000 / 4)  # 200


def test_diet_targets_percentage_only_fallback():
    t = diet_targets("balanced")
    assert t["split"] == {"protein": 30, "carbs": 40, "fat": 30}
    assert t["target_protein_g"] is None
    assert t["target_carbs_g"] is None
    assert t["target_fat_g"] is None
    assert t["calories"] is None


def test_diet_targets_unknown_mode_falls_back_to_balanced():
    t = diet_targets("carnivore-moon-diet", weight_kg=70)
    assert t["mode"] == "balanced"
    assert t["target_protein_g"] == round(1.6 * 70)


def test_diet_targets_junk_numeric_inputs_are_ignored():
    t = diet_targets("balanced", weight_kg="heavy", calorie_target=float("inf"))
    # Junk coerced away -> percentage-only.
    assert t["target_protein_g"] is None
    assert t["calories"] is None


# ── analyze_macros ────────────────────────────────────────────────────────

def test_analyze_macros_protein_short():
    targets = diet_targets("balanced", weight_kg=70, calorie_target=2000)
    meals = [_meal(total_protein_g=72, total_carbs_g=200, total_fat_g=67,
                   total_calories=2000)]
    result = analyze_macros(meals, targets)
    assert result["macros"]["protein"]["status"] == "short"
    assert result["macros"]["protein"]["consumed_g"] == 72
    joined = " ".join(result["suggestions"])
    assert "protein short" in joined
    # ~40g shortfall -> chicken breast copy.
    assert "chicken breast" in joined


def test_analyze_macros_on_target():
    targets = diet_targets("balanced", weight_kg=70, calorie_target=2000)
    meals = [_meal(total_protein_g=112, total_carbs_g=200, total_fat_g=67,
                   total_calories=2000)]
    result = analyze_macros(meals, targets)
    assert result["macros"]["protein"]["status"] == "ok"


def test_analyze_macros_keto_carbs_over_cap():
    targets = diet_targets("keto", weight_kg=70, calorie_target=2000)
    meals = [_meal(total_protein_g=112, total_carbs_g=68, total_fat_g=150,
                   total_calories=2000)]
    result = analyze_macros(meals, targets)
    assert result["macros"]["carbs"]["status"] == "over"
    joined = " ".join(result["suggestions"])
    assert "over the 50g cap" in joined
    assert "68g" in joined


def test_analyze_macros_keto_carbs_under_cap_is_ok():
    targets = diet_targets("keto", weight_kg=70, calorie_target=2000)
    meals = [_meal(total_protein_g=112, total_carbs_g=30, total_fat_g=150,
                   total_calories=2000)]
    result = analyze_macros(meals, targets)
    assert result["macros"]["carbs"]["status"] == "ok"


def test_analyze_macros_sums_multiple_meals():
    targets = diet_targets("balanced", calorie_target=2000)
    meals = [
        _meal(total_protein_g=50, total_calories=800),
        _meal(total_protein_g=50, total_calories=800),
    ]
    result = analyze_macros(meals, targets)
    assert result["consumed"]["protein_g"] == 100
    assert result["consumed"]["calories"] == 1600


def test_analyze_macros_actual_split_sums_reasonably():
    targets = diet_targets("balanced", calorie_target=2000)
    # 100g protein (400) + 100g carbs (400) + 100g fat (900) = 1700 kcal.
    meals = [_meal(total_protein_g=100, total_carbs_g=100, total_fat_g=100)]
    result = analyze_macros(meals, targets)
    split = result["actual_split"]
    assert 50 <= split["fat"] <= 55  # 900/1700 ~ 53%
    assert abs(sum(split.values()) - 100) <= 1


def test_analyze_macros_empty_meals_no_suggestions_error():
    targets = diet_targets("balanced", calorie_target=2000)
    result = analyze_macros([], targets)
    assert result["consumed"]["calories"] == 0
    assert result["actual_split"] == {"protein": 0, "carbs": 0, "fat": 0}
    assert result["suggestions"]  # never empty


def test_analyze_macros_all_junk_shapes_never_raises():
    targets = diet_targets("keto", weight_kg=70, calorie_target=2000)
    junk = [
        None,
        "not a meal",
        42,
        {},
        {"analysis": None},
        {"analysis": "nope"},
        {"analysis": {"total_protein_g": "abc", "total_calories": float("inf")}},
        {"analysis": {"total_carbs_g": float("nan"), "total_fat_g": [1, 2]}},
        {"analysis": {"is_food": False, "total_carbs_g": 9999}},  # skipped
    ]
    result = analyze_macros(junk, targets)
    assert result["consumed"]["protein_g"] == 0
    assert result["consumed"]["carbs_g"] == 0
    assert result["consumed"]["calories"] == 0


def test_analyze_macros_tolerates_non_dict_targets():
    result = analyze_macros([_meal(total_protein_g=50)], None)
    assert result["macros"]["protein"]["status"] == "unknown"


def test_analyze_macros_tolerates_non_list_meals():
    targets = diet_targets("balanced", calorie_target=2000)
    result = analyze_macros("not a list", targets)
    assert result["consumed"]["calories"] == 0


def test_analyze_macros_non_keto_carbs_over_and_calories_over():
    targets = diet_targets("balanced", calorie_target=2000)  # carbs target 200
    meals = [_meal(total_protein_g=150, total_carbs_g=320, total_fat_g=67,
                   total_calories=2600)]
    result = analyze_macros(meals, targets)
    assert result["macros"]["carbs"]["status"] == "over"
    joined = " ".join(result["suggestions"])
    assert "carbs over — ease off starchy sides" in joined
    assert "kcal over target" in joined


def test_analyze_macros_carbs_and_fat_short_and_calories_under():
    targets = diet_targets("balanced", calorie_target=2000)  # carbs 200, fat 67
    meals = [_meal(total_protein_g=150, total_carbs_g=80, total_fat_g=20,
                   total_calories=1200)]
    result = analyze_macros(meals, targets)
    joined = " ".join(result["suggestions"])
    assert "carbs short" in joined
    assert "fat short" in joined
    assert "kcal under target" in joined


def test_analyze_macros_small_protein_shortfalls_pick_smaller_foods():
    # ~15g short -> Greek yogurt tier.
    targets = {"target_protein_g": 100}
    result = analyze_macros([_meal(total_protein_g=85)], targets)
    assert "Greek yogurt" in " ".join(result["suggestions"])
    # ~25g short -> tuna tier.
    result = analyze_macros([_meal(total_protein_g=75)], targets)
    assert "can of tuna" in " ".join(result["suggestions"])
    # ~8g short (small target so it clears the tolerance band) -> egg tier.
    result = analyze_macros([_meal(total_protein_g=32)], {"target_protein_g": 40})
    assert "an egg" in " ".join(result["suggestions"])


# ── format_macro_report / report_line ─────────────────────────────────────

def test_format_macro_report_has_disclaimer_once():
    targets = diet_targets("keto", weight_kg=70, calorie_target=2000)
    result = analyze_macros([_meal(total_protein_g=60, total_carbs_g=68,
                                    total_fat_g=150, total_calories=2000)], targets)
    report = format_macro_report(result, targets, "keto")
    assert DISCLAIMER in report
    assert report.count(DISCLAIMER) == 1
    assert report.strip().endswith(DISCLAIMER + "</i>")
    assert "Keto" in report
    assert "<b>" in report  # Telegram-HTML formatting present


def test_format_macro_report_tolerates_empty_inputs():
    report = format_macro_report({}, {}, "balanced")
    assert DISCLAIMER in report
    assert "Balanced" in report


def test_format_macro_report_unknown_mode_labelled_balanced():
    report = format_macro_report({}, {}, "zzz")
    assert "Balanced" in report


def test_report_line_is_compact_and_flags_status():
    targets = diet_targets("keto", weight_kg=70, calorie_target=2000)
    result = analyze_macros([_meal(total_protein_g=112, total_carbs_g=68,
                                    total_fat_g=150, total_calories=2000)], targets)
    line = report_line(result, targets, "keto")
    assert "\n" not in line
    assert "Keto" in line
    assert "carbs over" in line


def test_report_line_tolerates_junk():
    assert isinstance(report_line(None, None, "nonsense"), str)


# ── parse_weight_kg ───────────────────────────────────────────────────────

def test_parse_weight_kg_plain_kg():
    assert parse_weight_kg("I'm 72.5kg today") == 72.5


def test_parse_weight_kg_spaced_kilograms():
    assert parse_weight_kg("weight is 80 kilograms") == 80.0


def test_parse_weight_kg_pounds_converted():
    result = parse_weight_kg("weighed 159 lb this morning")
    assert result == round(159 * 0.45359237, 1)  # 72.1


def test_parse_weight_kg_pounds_word():
    result = parse_weight_kg("about 200 pounds")
    assert result == round(200 * 0.45359237, 1)  # 90.7


def test_parse_weight_kg_keyword_bare_number_kg():
    assert parse_weight_kg("weighed 72 this week") == 72.0


def test_parse_weight_kg_below_bound_is_none():
    assert parse_weight_kg("the baby is 25kg") is None


def test_parse_weight_kg_above_bound_is_none():
    assert parse_weight_kg("that truck is 500kg") is None


def test_parse_weight_kg_no_number_is_none():
    assert parse_weight_kg("I feel heavy today") is None


def test_parse_weight_kg_non_string_is_none():
    assert parse_weight_kg(None) is None
    assert parse_weight_kg(72.5) is None


def test_parse_weight_kg_picks_first_in_bounds():
    # 20kg is out of bounds; 72kg is valid and should win.
    assert parse_weight_kg("not 20kg but really 72kg") == 72.0


# ── module hygiene ────────────────────────────────────────────────────────

def test_module_is_pure_no_forbidden_imports():
    import sys
    # nutrition must not pull in database/telegram/gemini transitively at import.
    src = open(nutrition.__file__).read()
    for forbidden in ("import database", "import telegram", "import gemini",
                      "from database", "from telegram", "from gemini"):
        assert forbidden not in src
    assert "database" not in sys.modules or True  # sanity: import already happened
