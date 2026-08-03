"""Regression pins for the 2026-08-03 pressure-test campaign (wave 1).

Each test reproduces a bug an adversarial fuzzer found and an independent
verifier confirmed against the pre-fix code. A failure here means a fixed
bug has returned.
"""

import nutrition
import telegram_bot


class TestCompoundDeleteMerge:
    def test_two_bare_int_deletes_merge_instead_of_vanishing(self):
        out = telegram_bot._normalize_nl_actions(
            {
                "intent": "multi",
                "actions": [
                    {"intent": "delete", "meal_indices": 0},
                    {"intent": "delete", "meal_indices": 1},
                ],
            }
        )
        deletes = [a for a in out if a.get("intent") == "delete"]
        assert len(deletes) == 1
        assert deletes[0]["meal_indices"] == [0, 1]

    def test_list_plus_bare_int_keeps_every_target(self):
        out = telegram_bot._normalize_nl_actions(
            {
                "intent": "multi",
                "actions": [
                    {"intent": "delete", "meal_indices": [0, 1]},
                    {"intent": "delete", "meal_indices": 4},
                ],
            }
        )
        deletes = [a for a in out if a.get("intent") == "delete"]
        assert deletes[0]["meal_indices"] == [0, 1, 4]

    def test_bool_is_not_honored_as_an_index(self):
        # bool is an int subclass; True must not become "delete meal 1".
        out = telegram_bot._normalize_nl_actions(
            {
                "intent": "multi",
                "actions": [
                    {"intent": "delete", "meal_indices": [2]},
                    {"intent": "delete", "meal_indices": True},
                ],
            }
        )
        deletes = [a for a in out if a.get("intent") == "delete"]
        assert deletes[0]["meal_indices"] == [2]


class TestNutritionContract:
    def test_normalize_mode_survives_unhashable_junk(self):
        # Docstring: "Unknown / junk mode falls back to balanced."
        assert nutrition.diet_targets([]) is not None
        assert nutrition.diet_targets({}) is not None
        assert nutrition._normalize_mode([1, 2]) == "balanced"
        assert nutrition._normalize_mode({"a": 1}) == "balanced"
        assert nutrition._normalize_mode("keto") == "keto"

    def test_analyze_macros_survives_non_numeric_carb_cap(self):
        # Docstring: "Never raises on malformed meals or targets."
        meals = [{"analysis": {"is_food": True, "total_carbs_g": 100}}]
        for junk in ("50", [], {}, True, float("nan"), float("inf")):
            result = nutrition.analyze_macros(meals, {"carb_cap_g": junk})
            assert isinstance(result, dict)

    def test_analyze_macros_numeric_carb_cap_still_caps(self):
        meals = [{"analysis": {"is_food": True, "total_carbs_g": 100}}]
        result = nutrition.analyze_macros(meals, {"carb_cap_g": 50})
        assert result["macros"]["carbs"]["status"] == "over"


class TestWeightParsing:
    def test_overweight_does_not_arm_the_keyword(self):
        assert nutrition.parse_weight_kg("overweight, ate 200 calories") is None
        assert nutrition.parse_weight_kg("I am underweight 200") is None

    def test_real_weigh_ins_still_parse(self):
        assert nutrition.parse_weight_kg("I weigh 72.5 kg") == 72.5
        assert nutrition.parse_weight_kg("weighed 81.6") == 81.6
        assert nutrition.parse_weight_kg("180 lb") == 81.6

    def test_fullwidth_digits_normalize_but_exotic_digits_reject(self):
        # Chinese-IME fullwidth digits are a supported input on BOTH
        # platforms; other Unicode digit sets are rejected on BOTH
        # (previously Python \d accepted Arabic-Indic digits the Dart
        # port rejected — same text logged a weigh-in on the server and
        # nothing on the phone).
        assert nutrition.parse_weight_kg("７２．５kg") == 72.5
        assert nutrition.parse_weight_kg("weigh ８１") == 81.0
        assert nutrition.parse_weight_kg("٧٢kg") is None
        assert nutrition.parse_weight_kg("weigh ٧٢") is None
