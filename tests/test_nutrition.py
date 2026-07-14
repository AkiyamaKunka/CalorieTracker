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


# ═══════════════════════════════════════════════════════════════════════════
# Deep behavioral tests (append-only hardening pass).
# ═══════════════════════════════════════════════════════════════════════════

import pytest


# ── invariants: protein scaling and keto cap ──────────────────────────────

@pytest.mark.parametrize("mode", DIET_MODES)
def test_protein_target_scales_exactly_linearly_with_weight(mode):
    # Doubling body weight must exactly double the protein plan in every mode
    # — any drift here silently mis-doses the primary training macro.
    light = diet_targets(mode, weight_kg=70)
    heavy = diet_targets(mode, weight_kg=140)
    assert heavy["target_protein_g"] == 2 * light["target_protein_g"]


@pytest.mark.parametrize(
    "calorie_target",
    [1, 800, 1200, 1201, 2000, 3999, 4000, 4001, 8000, 1e6, 5e8],
)
def test_keto_carb_target_never_exceeds_cap_for_any_calorie_target(calorie_target):
    # The 50g keto cap is the whole point of the mode: no calorie budget,
    # however huge (5% of 5e8 kcal would be 6.25M grams), may pierce it.
    t = diet_targets("keto", calorie_target=calorie_target)
    assert t["target_carbs_g"] is not None
    assert 0 < t["target_carbs_g"] <= MODE_SPECS["keto"]["carb_cap_g"]


@pytest.mark.parametrize("mode", DIET_MODES)
def test_targets_monotonic_in_calorie_target(mode):
    # A bigger calorie budget must never shrink any macro allowance.
    prev = None
    for cal in (1200, 1500, 2000, 2500, 3000, 4000, 5000, 8000):
        t = diet_targets(mode, calorie_target=cal)
        cur = (t["target_protein_g"], t["target_carbs_g"], t["target_fat_g"], t["calories"])
        if prev is not None:
            for before, after in zip(prev, cur):
                assert after >= before, "target shrank going {} -> {} kcal in {}".format(prev, cur, mode)
        prev = cur


def test_calorie_floor_yields_exact_floor_plan():
    # An 800 kcal crash-diet request must produce the identical plan to 1200:
    # derived gram targets come from the floored calories, not the raw ask.
    assert diet_targets("keto", calorie_target=800) == diet_targets(
        "keto", calorie_target=MIN_CALORIE_FLOOR
    )


@pytest.mark.parametrize("bad_weight", [-70, 0, True, float("nan")])
def test_diet_targets_nonpositive_or_bool_weight_gives_no_protein_target(bad_weight):
    # bool True is classic JSON junk that int-coerces to 1 if unguarded.
    t = diet_targets("high_protein", weight_kg=bad_weight)
    assert t["target_protein_g"] is None


@pytest.mark.parametrize("bad_override", [-3, 0, True, "lots"])
def test_diet_targets_junk_protein_override_falls_back_to_mode_default(bad_override):
    # A junk per-kg override must not zero out the protein plan.
    t = diet_targets("high_protein", weight_kg=70, protein_g_per_kg=bad_override)
    assert t["protein_g_per_kg"] == 2.0
    assert t["target_protein_g"] == 140


# ── metamorphic: protein status walk ──────────────────────────────────────

def test_protein_status_walks_short_ok_over_monotonically():
    # As consumed protein rises 0..300g against a fixed 144g target the
    # status must pass short -> ok -> over without ever regressing, and the
    # shortfall suggestion must appear exactly while short (never a nag for
    # extra protein — benign for a runner).
    targets = diet_targets("high_protein", weight_kg=72)  # 144g target
    order = {"short": 0, "ok": 1, "over": 2}
    walk = []
    for grams in range(0, 301):
        result = analyze_macros([_meal(total_protein_g=grams)], targets)
        status = result["macros"]["protein"]["status"]
        joined = " ".join(result["suggestions"])
        if status == "short":
            assert "protein short" in joined
        else:
            assert "protein" not in joined
        walk.append(order[status])
    assert walk == sorted(walk)      # monotone: no status flip-flops
    assert set(walk) == {0, 1, 2}    # all three states actually reached


def test_protein_tolerance_band_boundaries_at_ten_percent():
    # 144g target -> ±14.4g band; pin the exact integers where status flips
    # so a tolerance change can't slip through unnoticed.
    targets = {"target_protein_g": 144}

    def status(grams):
        return analyze_macros([_meal(total_protein_g=grams)], targets)["macros"]["protein"]["status"]

    assert status(129) == "short"
    assert status(130) == "ok"
    assert status(158) == "ok"
    assert status(159) == "over"


def test_keto_cap_is_hard_boundary_no_tolerance_band():
    # Unlike the 10% band elsewhere, the keto cap is hard: exactly 50g rides
    # the line without a scolding, 51g flags immediately.
    targets = diet_targets("keto", weight_kg=70, calorie_target=2000)
    at_cap = analyze_macros([_meal(total_carbs_g=50)], targets)
    assert at_cap["macros"]["carbs"]["status"] == "ok"
    assert not any("cap" in s for s in at_cap["suggestions"])
    over = analyze_macros([_meal(total_carbs_g=51)], targets)
    assert over["macros"]["carbs"]["status"] == "over"
    assert any("over the 50g cap" in s for s in over["suggestions"])


# ── realism: a plausible day, checked by hand ─────────────────────────────

def test_real_day_high_protein_runner_numbers_match_hand_calculation():
    # A 72 kg high-protein runner's actual day (eggs, chicken+rice, salmon+
    # salad). Every expected number below was computed by hand, not by
    # re-running the code under test.
    targets = diet_targets("high_protein", weight_kg=72, calorie_target=2200)
    assert targets["target_protein_g"] == 144   # 2.0 g/kg × 72
    assert targets["target_carbs_g"] == 220     # 40% of 2200 / 4
    assert targets["target_fat_g"] == 73        # 30% of 2200 / 9 = 73.3

    meals = [
        _meal(total_protein_g=18, total_carbs_g=2, total_fat_g=15, total_calories=215),   # 3 eggs
        _meal(total_protein_g=66, total_carbs_g=45, total_fat_g=8, total_calories=640),   # chicken + rice
        _meal(total_protein_g=40, total_carbs_g=10, total_fat_g=26, total_calories=480),  # salmon + salad
    ]
    result = analyze_macros(meals, targets)

    assert result["consumed"] == {"protein_g": 124, "carbs_g": 57, "fat_g": 49, "calories": 1335}
    assert result["macros"]["protein"]["status"] == "short"
    assert result["macros"]["protein"]["delta_g"] == -20
    # macro kcal: 496 P + 228 C + 441 F = 1165 -> 43% / 20% / 38%.
    assert result["actual_split"] == {"protein": 43, "carbs": 20, "fat": 38}

    joined = " ".join(result["suggestions"])
    assert "~20g protein short — add a can of tuna" in joined
    assert "~163g carbs short" in joined
    assert "~24g fat short" in joined
    assert "~865kcal under target" in joined

    report = format_macro_report(result, targets, "high_protein")
    assert "Calories: <b>1,335</b> / 2,200 kcal" in report  # thousands separators
    assert "Protein: 124g / 144g ⬇️" in report


def test_report_line_flags_protein_first_when_multiple_macros_off():
    # Protein outranks carbs/fat in the one-liner — the signal a
    # high-protein user acts on first.
    targets = {"target_protein_g": 144, "target_carbs_g": 100, "target_fat_g": 60}
    result = analyze_macros(
        [_meal(total_protein_g=50, total_carbs_g=200, total_fat_g=60)], targets
    )
    line = report_line(result, targets, "high_protein")
    assert "— protein short" in line
    assert "carbs" not in line


# ── HTML injection / rendering safety ─────────────────────────────────────

def test_format_macro_report_escapes_hostile_suggestion_strings():
    # Suggestions can be attacker-influenced via a tampered analysis dict;
    # unescaped HTML would break Telegram-HTML parsing (message send fails).
    hostile = {"suggestions": ["<script>alert(1)</script> & 'quotes' <b>x</b>"]}
    report = format_macro_report(hostile, {}, "balanced")
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert "&amp;" in report
    assert "&#x27;" in report


def test_format_macro_report_disclaimer_exactly_once_with_many_suggestions():
    # The legal disclaimer must appear once and last no matter how long the
    # suggestions list grows.
    crowded = {"suggestions": ["tip number {}".format(i) for i in range(40)]}
    report = format_macro_report(crowded, {}, "keto")
    assert report.count(DISCLAIMER) == 1
    assert report.strip().endswith(DISCLAIMER + "</i>")


@pytest.mark.parametrize(
    "hostile_mode",
    ["<b>keto</b>", "keto<script>", "balanced' onload='x"],
)
def test_hostile_mode_strings_never_reach_rendered_output(hostile_mode):
    # Unknown modes normalize to balanced; the raw mode text must never leak
    # into HTML-parsed output.
    report = format_macro_report({}, {}, hostile_mode)
    assert hostile_mode not in report
    assert "Balanced" in report
    line = report_line({}, {}, hostile_mode)
    assert hostile_mode not in line
    assert "<" not in line


def test_format_macro_report_junk_macro_status_and_values_render_safely():
    # A forged status must not inject markup, and junk grams render as 0g.
    weird = {"macros": {"protein": {"consumed_g": "<b>", "target_g": None,
                                    "status": "<img src=x>"}}}
    report = format_macro_report(weird, {}, "balanced")
    assert "<img" not in report
    assert "Protein: 0g" in report


# ── junk resilience beyond existing coverage ──────────────────────────────

@pytest.mark.parametrize(
    "junk_value",
    [True, False, {"value": 50}, {"total_protein_g": 50}, [50], "50"],
)
def test_analyze_macros_bool_container_and_string_totals_count_as_zero(junk_value):
    # bools and nested containers are classic hostile-Gemini JSON shapes;
    # True must not int-coerce to 1 and "50" must not string-coerce.
    targets = diet_targets("balanced", weight_kg=70)
    result = analyze_macros(
        [_meal(total_protein_g=junk_value, total_carbs_g=junk_value,
               total_fat_g=junk_value, total_calories=junk_value)],
        targets,
    )
    assert result["consumed"] == {"protein_g": 0, "carbs_g": 0, "fat_g": 0, "calories": 0}


def test_analyze_macros_negative_grams_never_raise_and_still_render():
    # Negative totals are physically impossible but a hostile payload can
    # supply them; the pipeline must survive end to end.
    targets = diet_targets("keto", weight_kg=70, calorie_target=2000)
    result = analyze_macros(
        [_meal(total_protein_g=-50, total_carbs_g=100, total_fat_g=-5,
               total_calories=-300)],
        targets,
    )
    report = format_macro_report(result, targets, "keto")
    assert DISCLAIMER in report
    assert isinstance(report_line(result, targets, "keto"), str)


@pytest.mark.xfail(
    strict=False,
    reason="BUG: negative gram totals yield actual_split percentages outside 0..100 (e.g. P -100%)",
)
def test_actual_split_percentages_stay_within_zero_and_hundred():
    # Hostile meal with negative protein grams currently produces
    # actual_split {'protein': -100, 'carbs': 200, 'fat': 0}, which renders
    # as "Split: P -100% · C 200% · F 0%" in a user-facing report.
    result = analyze_macros(
        [_meal(total_protein_g=-50, total_carbs_g=100, total_fat_g=0)], {}
    )
    for pct in result["actual_split"].values():
        assert 0 <= pct <= 100


@pytest.mark.parametrize(
    "p,c,f",
    [(124, 57, 49), (1, 1, 1), (200, 5, 160), (30, 400, 10), (0.3, 0.3, 0.3)],
)
def test_actual_split_sums_to_100_within_rounding(p, c, f):
    # The rendered P/C/F split must always read as a sane percentage
    # breakdown (sum 100 ± rounding of three terms).
    result = analyze_macros(
        [_meal(total_protein_g=p, total_carbs_g=c, total_fat_g=f)], {}
    )
    assert abs(sum(result["actual_split"].values()) - 100) <= 2


# ── parse_weight_kg: realistic phrasings, adversarial near-misses ─────────

@pytest.mark.parametrize("text,expected", [
    ("159 lbs", 72.1),
    ("159lb", 72.1),
    ("down to 158.5 lbs today", 71.9),
    ("weighed myself: 72.5", 72.5),
    ("my weight: 72", 72.0),
    ("I am 72.5 KG", 72.5),          # case-insensitive units
    ("165 POUNDS", 74.8),
    ("72kg after a 5 km run", 72.0),  # the 5 km must not shadow the weight
])
def test_parse_weight_kg_realistic_phrasings(text, expected):
    assert parse_weight_kg(text) == expected


@pytest.mark.parametrize("text", [
    "ran 100 km this week",       # distance, not mass
    "ate 300 g of rice",          # food grams
    "drank 500 ml of water",
    "my dog weighs 8 kg",         # weigh-keyword but out-of-bounds value
    "did a 5k in 21:30",
    "10,000 steps today",
    "the flight is 72 minutes",   # bare number without weigh-keyword
    "72,5 kg",                    # European decimal comma: pinned unsupported
])
def test_parse_weight_kg_rejects_non_bodyweight_numbers(text):
    assert parse_weight_kg(text) is None


def test_parse_weight_kg_unit_number_beats_keyword_bare_number():
    # "weighed in at 159 lbs": the lb reading (72.1 kg) must win — the
    # bare-number keyword path would misread 159 as kg, which is in bounds.
    assert parse_weight_kg("weighed in at 159 lbs") == 72.1


def test_parse_weight_kg_gym_context_parses_documented_limitation():
    # Pinned limitation: no gym-context awareness, so lifted weights with
    # units parse as body weight. If this ever changes, the NL weight intent
    # in telegram_bot needs a re-review too.
    assert parse_weight_kg("72 kg dumbbell press") == 72.0
    assert parse_weight_kg("benched 225 lbs") == round(225 * 0.45359237, 1)  # 102.1


@pytest.mark.parametrize("text,expected", [
    ("30kg", 30.0),        # inclusive lower bound
    ("300kg", 300.0),      # inclusive upper bound
    ("29.9kg", None),
    ("300.1kg", None),
    ("66 lbs", None),      # 29.9 kg after conversion: bound applies post-conversion
    ("67 lbs", 30.4),
])
def test_parse_weight_kg_bounds_inclusive_and_applied_after_conversion(text, expected):
    assert parse_weight_kg(text) == expected
