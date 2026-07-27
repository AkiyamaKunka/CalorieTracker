#!/usr/bin/env python3
"""Generate golden behavior vectors from the Python reference implementation.

The Python server is the reference for all behavior shared with the Dart app.
This script calls the REAL Python functions (utils, telegram_bot,
android/upload_photo, plus pure extractions of DB-coupled report formulas)
and records input -> expected-output pairs as JSON under shared/vectors/.
pytest (tests/test_shared_vectors.py) replays every vector through the same
implementations, so a behavior change without regeneration fails the suite;
`flutter test` replays the same files through the Dart ports.

Deterministic by construction: no network, no clocks (validation cases carry
an explicit `now`), no randomness. Re-running on unchanged code is a no-op.

Value encoding (documented in every file header): JSON cannot represent
NaN/Infinity floats, so those inputs are encoded as
    {"__special__": "nan" | "inf" | "-inf"}
and decoded before the function call. An expected side of a case that raises
is encoded as {"__raises__": true}.

For functions that need DB/state (telegram_bot._daily_calorie_totals, the
typical-day median in format_daily_summary, daily_report's 7-day-average
filter, telegram_bot._parse_captured_at's clock comparison), the pure
sub-logic is extracted below — same approach the Dart port took — and the
extraction, not the vector, is what must be kept in sync by eye with the
DB-coupled original.
"""
import copy
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "android"))

import telegram_bot  # noqa: E402
import upload_photo  # noqa: E402
import nutrition
import utils  # noqa: E402

VECTORS_DIR = ROOT / "shared" / "vectors"

GENERATOR = "scripts/generate_shared_vectors.py"
ENCODING_NOTE = (
    "Inputs/outputs are plain JSON except: {\"__special__\": \"nan\"|\"inf\"|\"-inf\"} "
    "encodes a non-JSON float, and an expected of {\"__raises__\": true} means the "
    "call raises. Decode specials before calling the implementation."
)

_SPECIALS = {"nan": float("nan"), "inf": float("inf"), "-inf": float("-inf")}


def encode_value(value):
    """Recursively encode a Python value as JSON-safe vector data."""
    if isinstance(value, float):
        if value != value:
            return {"__special__": "nan"}
        if value == float("inf"):
            return {"__special__": "inf"}
        if value == float("-inf"):
            return {"__special__": "-inf"}
        return value
    if isinstance(value, dict):
        return {key: encode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [encode_value(item) for item in value]
    return value


def decode_value(value):
    """Inverse of encode_value."""
    if isinstance(value, dict):
        if set(value) == {"__special__"}:
            return _SPECIALS[value["__special__"]]
        return {key: decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


# ─── Pure extractions of DB/clock-coupled logic ────────────────────
# Each mirrors the named production code path exactly; the vectors pin the
# extraction, and the extraction must be kept in sync with the original.

def captured_at_valid(captured_at, now, max_age_days):
    """Pure mirror of telegram_bot._parse_captured_at's validation rule.

    The original compares against database.user_local_now(); here `now` is an
    explicit 'YYYY-MM-DD HH:MM:SS' wall-clock string. Both boundaries are
    inclusive: exactly now+1h and exactly now-max_age_days are valid.
    """
    try:
        captured = datetime.strptime((captured_at or "").strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False
    now_dt = datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
    if captured > now_dt + timedelta(hours=1):
        return False
    if captured < now_dt - timedelta(days=max_age_days):
        return False
    return True


def day_calorie_totals(meals):
    """Pure mirror of telegram_bot._daily_calorie_totals' aggregation loop.

    Input: meal rows [{"date": str, "analysis": dict}]. Non-food meals are
    skipped; hostile calorie values coerce through utils.safe_number and
    negative values clamp to 0 so they can never subtract from a day.
    """
    totals = {}
    for m in meals:
        analysis = m.get("analysis", {})
        if not analysis.get("is_food"):
            continue
        d = m.get("date", "")
        totals[d] = totals.get(d, 0) + max(0, utils.safe_number(analysis.get("total_calories")))
    return totals


def typical_day_median(day_totals):
    """Pure mirror of format_daily_summary's typical-day rule (telegram_bot).

    Median of the prior days' totals; fewer than 2 logged days -> None
    (median over sparse data would just echo one day).
    """
    if len(day_totals) < 2:
        return None
    return int(statistics.median(day_totals.values()))


def report_prior_day_totals(meals):
    """Pure mirror of daily_report's 7-day-average filter + aggregation.

    Unlike day_calorie_totals this uses the report's own bounds check
    (0 < cal < 1e9, bools excluded, falsy -> 0 via `or 0`), matching
    daily_report.py line-for-line.
    """
    totals = {}
    for m in meals:
        analysis = m.get("analysis", {})
        if not analysis.get("is_food"):
            continue
        cal = analysis.get("total_calories") or 0
        if not (
            isinstance(cal, (int, float))
            and not isinstance(cal, bool)
            and 0 < cal < 1e9
        ):
            continue
        day = m.get("date")
        totals[day] = totals.get(day, 0) + cal
    return totals


def report_seven_day_avg(day_totals):
    """Pure mirror of daily_report's average rule: >=2 days -> round(mean).

    Python round() is banker's rounding (round-half-even) — a case below
    pins that so the Dart port cannot silently use round-half-up.
    """
    if len(day_totals) < 2:
        return None
    return round(sum(day_totals.values()) / len(day_totals))


# ─── coerce.json ───────────────────────────────────────────────────

def _expected_or_raises(fn, *args):
    try:
        return encode_value(fn(*args))
    except Exception:
        return {"__raises__": True}


def build_coerce():
    cases = []

    def sn(case_id, value, expected_note=None, **kwargs):
        args = (value, kwargs["default"]) if "default" in kwargs else (value,)
        case = {"id": f"safe_number_{case_id}", "fn": "safe_number",
                "input": encode_value(value),
                "expected": encode_value(utils.safe_number(*args))}
        if "default" in kwargs:
            case["default"] = encode_value(kwargs["default"])
        cases.append(case)

    sn("int", 500)
    sn("zero", 0)
    sn("negative_int", -1)
    sn("float", 12.5)
    sn("negative_float", -0.25)
    sn("bool_true", True)
    sn("bool_false", False)
    sn("numeric_string", "500")
    sn("float_string", "12.5")
    sn("none", None)
    sn("list", [500])
    sn("dict", {"value": 500})
    sn("magnitude_upper_bound_exact", 10**9)
    sn("magnitude_upper_bound_float", 1e9)
    sn("magnitude_just_under_upper", 999999999)
    sn("magnitude_lower_bound_exact", -(10**9))
    sn("magnitude_just_over_lower", -999999999)
    sn("huge_int", 10**18)
    sn("huge_float", 1e18)
    sn("inf", float("inf"))
    sn("neg_inf", float("-inf"))
    sn("nan", float("nan"))
    sn("none_with_default", None, default=7)
    sn("string_with_none_default", "500", default=None)
    sn("bool_with_default", True, default=-1)
    sn("valid_ignores_default", 250, default=99)

    def pb(case_id, value):
        cases.append({"id": f"parse_boolish_{case_id}", "fn": "parse_boolish",
                      "input": encode_value(value),
                      "expected": utils.parse_boolish(value)})

    pb("one_str", "1")
    pb("true_str", "true")
    pb("true_upper", "TRUE")
    pb("yes_padded", "  yes  ")
    pb("y", "y")
    pb("on", "on")
    pb("on_mixed_case", "On")
    pb("zero_str", "0")
    pb("false_str", "false")
    pb("no", "no")
    pb("n", "n")
    pb("off", "off")
    pb("off_upper", "OFF")
    pb("int_one", 1)
    pb("int_zero", 0)
    pb("bool_true", True)
    pb("bool_false", False)
    pb("none", None)
    pb("empty", "")
    pb("maybe", "maybe")
    pb("two", "2")
    pb("float_one", 1.0)
    pb("t_alone", "t")
    pb("list", [])

    def sfi(case_id, analysis):
        cases.append({"id": f"safe_food_items_{case_id}", "fn": "safe_food_items",
                      "input": encode_value(analysis),
                      "expected": encode_value(utils.safe_food_items(analysis))})

    sfi("normal", {"food_items": [{"name": "rice", "estimated_calories": 200}]})
    sfi("not_a_dict_list", [{"name": "rice"}])
    sfi("not_a_dict_str", "rice")
    sfi("none", None)
    sfi("number", 42)
    sfi("missing_key", {"total_calories": 500})
    sfi("items_scalar", {"food_items": "rice"})
    sfi("items_number", {"food_items": 5})
    sfi("items_dict", {"food_items": {"name": "rice"}})
    sfi("items_none", {"food_items": None})
    sfi("items_empty", {"food_items": []})
    sfi("items_mixed", {"food_items": [
        {"name": "rice"}, "junk", 3, None, {"name": "soup", "estimated_calories": 90}, [],
    ]})

    def paj(case_id, text):
        cases.append({"id": f"parse_ai_json_{case_id}", "fn": "parse_ai_json",
                      "input": text,
                      "expected": _expected_or_raises(utils.parse_ai_json, text)})

    paj("plain_object", '{"a": 1}')
    paj("plain_array", '[1, 2]')
    paj("fenced_json", '```json\n{"a": 1}\n```')
    paj("fenced_no_lang", '```\n[1, 2]\n```')
    paj("fenced_upper", '```JSON\n{"is_food": true}\n```')
    paj("fenced_padded", '  ```json\n{"a": 1}\n```  ')
    paj("prose_wrapped_object", 'Here is the result: {"a": 1} hope that helps')
    paj("prose_wrapped_array", 'the actions are [1, 2] as requested')
    paj("prose_nested", 'x {"a": [1, 2], "b": {"c": 3}} y')
    paj("bare_intent_array", '[{"intent": "delete", "meal_indices": [2]}]')
    paj("whitespace_padded", '   {"a": 1}   ')
    paj("no_json", 'no json here')
    paj("empty", '')
    paj("none", None)
    paj("unterminated_object", '{"broken": ')
    paj("close_before_open", '}{')
    paj("fence_only", '```json\n```')

    return {"generator": GENERATOR, "encoding": ENCODING_NOTE, "cases": cases}


# ─── nl_normalize.json ─────────────────────────────────────────────

def build_nl_normalize():
    cases = []

    def nl(case_id, result):
        expected = telegram_bot._normalize_nl_actions(copy.deepcopy(result))
        cases.append({"id": case_id, "fn": "_normalize_nl_actions",
                      "input": encode_value(result),
                      "expected": encode_value(expected)})

    corr = {"intent": "correction", "meal_index": 1, "reason": "wrong dish",
            "analysis": {"is_food": True, "meal_description": "roast duck rice",
                         "total_calories": 780}}
    dele = {"intent": "delete", "meal_indices": [2], "reason": "user asked"}
    new_meal = {"intent": "new_meal",
                "analysis": {"is_food": True, "meal_description": "oatmeal",
                             "total_calories": 320}}
    log_weight = {"intent": "log_weight", "weight_kg": 71.4}
    log_activity = {"intent": "log_activity", "calories_burned": 450,
                    "steps": 8000, "distance_km": 5}
    chat = {"intent": "chat", "reply": "Sounds like a balanced day!"}

    # Single intents.
    nl("single_correction", corr)
    nl("single_delete", dele)
    nl("single_new_meal", new_meal)
    nl("single_log_weight", log_weight)
    nl("single_log_activity", log_activity)
    nl("single_chat", chat)
    nl("single_unknown_intent", {"intent": "dance", "reply": "?"})
    nl("single_no_intent_key", {"reply": "hello"})
    nl("single_intent_number", {"intent": 3, "reply": "?"})

    # Designed multi shape.
    nl("multi_two_actions", {"intent": "multi", "actions": [corr, dele]})
    nl("multi_three_actions",
       {"intent": "multi", "actions": [new_meal, log_weight, log_activity]})
    nl("multi_empty_actions", {"intent": "multi", "actions": []})
    nl("multi_actions_not_list", {"intent": "multi", "actions": {"intent": "delete"}})
    nl("multi_actions_scalar", {"intent": "multi", "actions": "delete meal 2"})
    nl("multi_with_non_map_entries",
       {"intent": "multi", "actions": [corr, "junk", 3, None, dele, [new_meal]]})
    nl("multi_all_non_map_entries", {"intent": "multi", "actions": ["a", 1, None]})

    # Bare arrays — including the exact 2026-07-16 production compound.
    nl("bare_array_production_correction_delete", [
        {"intent": "correction", "meal_index": 1,
         "reason": "第二顿饭改为烧鸭饭",
         "analysis": {"is_food": True, "meal_description": "烧鸭饭",
                      "total_calories": 780, "total_protein_g": 32,
                      "total_carbs_g": 88, "total_fat_g": 30,
                      "food_items": [
                          {"name": "烧鸭", "estimated_calories": 380},
                          {"name": "米饭", "estimated_calories": 400},
                      ]}},
        {"intent": "delete", "meal_indices": [2], "reason": "用户要求删除第三顿饭"},
    ])
    nl("bare_array_single", [corr])
    nl("bare_array_empty", [])
    nl("bare_array_with_non_map_entries", [corr, "junk", 7, None, dele])
    nl("bare_array_all_non_map", ["only", "strings", 1, None])
    nl("bare_array_nested_list_dropped", [[corr]])

    # Scalar / unusable top-level shapes.
    nl("scalar_string", "hello")
    nl("scalar_number", 42)
    nl("scalar_null", None)
    nl("scalar_bool", True)

    # Wrapper precedence: a recognized single intent must beat a
    # hallucinated actions list; anything unrecognized must honor actions.
    nl("recognized_single_wins_over_actions",
       {"intent": "correction", "meal_index": 0, "analysis": corr["analysis"],
        "actions": [dele, new_meal]})
    nl("unrecognized_intent_wrapper_uses_actions",
       {"intent": "compound_request", "actions": [corr, dele]})
    nl("no_intent_wrapper_uses_actions", {"actions": [new_meal]})
    nl("chat_intent_wrapper_uses_actions",
       {"intent": "chat", "reply": "doing both", "actions": [corr, dele]})

    # Unhashable-intent equivalents (JSON stand-ins: numbers/objects/arrays
    # as intent must read as "no recognized intent", never raise).
    nl("number_intent_wrapper_uses_actions",
       {"intent": 3.14, "actions": [dele]})
    nl("object_intent_wrapper_uses_actions",
       {"intent": {"kind": "multi"}, "actions": [corr]})
    nl("array_intent_wrapper_uses_actions",
       {"intent": ["correction"], "actions": [new_meal]})
    nl("bool_intent_wrapper_uses_actions",
       {"intent": True, "actions": [log_weight]})

    # Cap at NL_MAX_ACTIONS.
    seven_meals = [dict(new_meal, note=i) for i in range(7)]
    nl("cap_bare_array_seven_actions", seven_meals)
    nl("cap_multi_six_actions", {"intent": "multi", "actions": [
        corr, new_meal, log_weight, log_activity, dele,
        {"intent": "delete", "meal_indices": [4]},
    ]})
    nl("cap_drops_second_delete_before_merge", [
        dele, new_meal, new_meal, log_weight, log_activity,
        {"intent": "delete", "meal_indices": [4]},
    ])
    nl("cap_then_merge_deletes", [
        {"intent": "delete", "meal_indices": [0]},
        {"intent": "delete", "meal_indices": [1]},
        new_meal, log_weight, log_activity, corr,
    ])

    # Duplicate-delete merge.
    nl("merge_two_deletes", [
        {"intent": "delete", "meal_indices": [0], "reason": "first"},
        {"intent": "delete", "meal_indices": [2, 3], "reason": "second"},
    ])
    nl("merge_three_deletes", [
        {"intent": "delete", "meal_indices": [0]},
        {"intent": "delete", "meal_indices": [1]},
        {"intent": "delete", "meal_indices": [5]},
    ])
    nl("merge_keeps_first_delete_position", [
        corr,
        {"intent": "delete", "meal_indices": [1]},
        new_meal,
        {"intent": "delete", "meal_indices": [2]},
    ])
    nl("merge_scalar_indices_contribute_nothing", [
        {"intent": "delete", "meal_indices": 3},
        {"intent": "delete", "meal_indices": [1]},
    ])
    nl("merge_missing_indices_contribute_nothing", [
        {"intent": "delete"},
        {"intent": "delete", "meal_indices": [0, 1]},
    ])

    # Scalar meal_indices on a single delete is preserved (handled downstream).
    nl("single_delete_scalar_indices_passthrough",
       {"intent": "delete", "meal_indices": 2})
    nl("single_delete_hostile_indices_passthrough",
       {"intent": "delete", "meal_indices": "all"})

    return {
        "generator": GENERATOR,
        "encoding": ENCODING_NOTE,
        "max_actions": telegram_bot.NL_MAX_ACTIONS,
        "cases": cases,
    }


# ─── captured_at.json ──────────────────────────────────────────────

def build_captured_at():
    cases = []

    def fname(case_id, name):
        cases.append({"id": f"filename_{case_id}", "fn": "captured_at_from_filename",
                      "input": name,
                      "expected": upload_photo._captured_at_from_filename(name)})

    fname("standard_img", "IMG_20260716_193042.jpg")
    fname("hash_prefixed", "a3f9c2d81b04__IMG_20260715_193042.jpg")
    fname("no_prefix", "20260716_193042.jpg")
    fname("vid_prefix", "VID_20260716_193042.mp4")
    fname("pixel_millis_suffix", "PXL_20260716_193042123.jpg")
    fname("trailing_extra_digit", "IMG_20260716_1930421.jpg")
    fname("no_extension", "IMG_20260716_193042")
    fname("digit_before_prefix", "9IMG_20260716_193042.jpg")
    fname("first_of_two_timestamps", "IMG_20260101_000000_20260716_193042.jpg")
    fname("year_boundary_low", "IMG_00010101_000000.jpg")
    fname("year_boundary_high", "IMG_99991231_235959.jpg")
    fname("year_rollover_eve", "IMG_20261231_235959.jpg")
    fname("year_rollover_day", "IMG_20270101_000000.jpg")
    fname("leap_day_valid", "IMG_20240229_080000.jpg")
    fname("leap_day_invalid", "IMG_20260229_080000.jpg")
    fname("month_13", "IMG_20261332_120000.jpg")
    fname("feb_31", "IMG_20260231_120000.jpg")
    fname("month_00", "IMG_20260016_120000.jpg")
    fname("day_00", "IMG_20260700_120000.jpg")
    fname("hour_25", "IMG_20260716_250000.jpg")
    fname("minute_60", "IMG_20260716_196042.jpg")
    fname("second_61", "IMG_20260716_193061.jpg")
    fname("second_60_leap", "IMG_20260716_193060.jpg")
    fname("dash_separator", "IMG_20260716-193042.jpg")
    fname("date_split_by_underscore", "IMG_2026_0716.jpg")
    fname("seven_digit_date", "IMG_2026071_193042.jpg")
    fname("five_digit_time", "IMG_20260716_19304.jpg")
    fname("nine_digit_date_run", "IMG_120260716_193042.jpg")
    fname("plain_name", "photo.jpg")
    fname("empty", "")
    fname("none", None)

    max_age_days = telegram_bot.CAPTURED_AT_MAX_AGE_DAYS
    now = "2026-07-16 12:00:00"

    def valid(case_id, captured_at, case_now=now):
        cases.append({
            "id": f"validation_{case_id}", "fn": "captured_at_valid",
            "captured_at": captured_at, "now": case_now,
            "expected_valid": captured_at_valid(captured_at, case_now, max_age_days),
        })

    valid("exactly_now", "2026-07-16 12:00:00")
    valid("one_second_future", "2026-07-16 12:00:01")
    valid("plus_one_hour_inclusive", "2026-07-16 13:00:00")
    valid("plus_one_hour_one_second", "2026-07-16 13:00:01")
    valid("plus_one_day", "2026-07-17 12:00:00")
    valid("yesterday", "2026-07-15 20:15:30")
    valid("forty_four_days_old", "2026-06-02 12:00:00")
    valid("minus_45_days_inclusive", "2026-06-01 12:00:00")
    valid("minus_45_days_one_second", "2026-06-01 11:59:59")
    valid("forty_six_days_old", "2026-05-31 12:00:00")
    valid("padded_whitespace_valid", "  2026-07-16 11:00:00  ")
    valid("iso_t_separator_invalid", "2026-07-16T11:00:00")
    valid("missing_seconds_invalid", "2026-07-16 11:00")
    valid("date_only_invalid", "2026-07-16")
    valid("empty_invalid", "")
    valid("junk_invalid", "yesterday lunch")
    valid("midnight_now_window", "2026-01-01 00:30:00", case_now="2026-01-01 00:00:00")

    return {
        "generator": GENERATOR,
        "encoding": ENCODING_NOTE,
        "max_age_days": max_age_days,
        "cases": cases,
    }


# ─── mismatch.json ─────────────────────────────────────────────────

def build_mismatch():
    cases = []

    def mm(case_id, analysis):
        cases.append({"id": case_id, "fn": "meal_calorie_mismatch",
                      "input": encode_value(analysis),
                      "expected": utils.meal_calorie_mismatch(analysis)})

    def items(*cals):
        return [{"name": f"item{i}", "estimated_calories": c}
                for i, c in enumerate(cals)]

    # The observed production hallucination: items summing to 135 kcal under
    # a 1335 kcal meal total (and its inverse).
    mm("production_135_vs_1335",
       {"is_food": True, "meal_description": "beef noodle soup",
        "total_calories": 1335, "food_items": items(45, 45, 45)})
    mm("inverse_1335_vs_135",
       {"total_calories": 135, "food_items": items(445, 445, 445)})

    # Consistent meals.
    mm("consistent_exact", {"total_calories": 600, "food_items": items(200, 400)})
    mm("consistent_close", {"total_calories": 620, "food_items": items(200, 400)})

    # Threshold edges: flag only when |total - sum| > max(100, 20% of larger).
    mm("edge_diff_exactly_100", {"total_calories": 500, "food_items": items(150, 250)})
    mm("edge_diff_101", {"total_calories": 501, "food_items": items(150, 250)})
    mm("edge_exactly_20_percent", {"total_calories": 1000, "food_items": items(400, 400)})
    mm("edge_just_over_20_percent", {"total_calories": 1000, "food_items": items(400, 399)})
    mm("edge_20_percent_of_item_sum", {"total_calories": 800, "food_items": items(500, 500)})

    # Missing / degenerate data reads as consistent.
    mm("no_items", {"total_calories": 500})
    mm("empty_items", {"total_calories": 500, "food_items": []})
    mm("items_without_calories",
       {"total_calories": 500, "food_items": [{"name": "rice"}, {"name": "soup"}]})
    mm("item_sum_zero", {"total_calories": 500, "food_items": items(0, 0)})
    mm("item_sum_negative", {"total_calories": 500, "food_items": items(-50, 20)})
    mm("total_missing", {"food_items": items(200, 300)})
    mm("total_string", {"total_calories": "1335", "food_items": items(45, 45, 45)})
    mm("total_bool", {"total_calories": True, "food_items": items(45, 45, 45)})
    mm("total_huge", {"total_calories": 1e18, "food_items": items(45, 45, 45)})
    mm("total_nan", {"total_calories": float("nan"), "food_items": items(45, 45, 45)})
    mm("analysis_not_dict_list", [1, 2, 3])
    mm("analysis_none", None)

    # Hostile item values are skipped, not summed.
    mm("hostile_items_skipped",
       {"total_calories": 1335,
        "food_items": items(45, "800", 45) + [
            {"name": "huge", "estimated_calories": 1e18},
            {"name": "inf", "estimated_calories": float("inf")},
            {"name": "nan", "estimated_calories": float("nan")},
            {"name": "bool", "estimated_calories": True},
            "not-a-dict",
            {"name": "ok", "estimated_calories": 45},
        ]})
    mm("negative_total_vs_positive_sum",
       {"total_calories": -500, "food_items": items(150, 150)})
    mm("float_sum_truncates_to_int",
       {"total_calories": 800, "food_items": items(175.35, 175.35)})

    # Corrected-meal suppression is CONSUMER behavior: format_meal_response
    # skips the warning for corrected meals before ever calling this pure
    # function, so the vector for a corrected meal is identical to the
    # uncorrected one — pinned here so the Dart port suppresses in the
    # consumer too, not inside the pure function.
    mm("corrected_flag_is_ignored_by_pure_fn",
       {"corrected": True, "total_calories": 1335, "food_items": items(45, 45, 45)})

    return {"generator": GENERATOR, "encoding": ENCODING_NOTE, "cases": cases}


# ─── report_formulas.json ──────────────────────────────────────────

def build_report_formulas():
    cases = []

    def meal(day, cal, is_food=True, **extra):
        analysis = {"is_food": is_food, "total_calories": cal}
        analysis.update(extra)
        return {"date": day, "analysis": analysis}

    def dt_case(case_id, meals):
        cases.append({"id": f"day_totals_{case_id}", "fn": "day_calorie_totals",
                      "input": encode_value(meals),
                      "expected": encode_value(day_calorie_totals(meals))})

    dt_case("two_days", [
        meal("2026-07-14", 500), meal("2026-07-14", 700), meal("2026-07-15", 1800),
    ])
    dt_case("non_food_skipped", [
        meal("2026-07-14", 500), meal("2026-07-14", 900, is_food=False),
        {"date": "2026-07-14", "analysis": {"total_calories": 300}},
    ])
    dt_case("truthy_is_food_counts", [
        meal("2026-07-14", 500, is_food="yes"), meal("2026-07-14", 200, is_food=0),
        meal("2026-07-14", 100, is_food=1),
    ])
    dt_case("negative_clamped_to_zero", [
        meal("2026-07-14", -400), meal("2026-07-14", 600),
    ])
    dt_case("hostile_values_coerce_to_zero", [
        meal("2026-07-14", "640"), meal("2026-07-14", 1e18),
        meal("2026-07-14", float("inf")), meal("2026-07-14", float("nan")),
        meal("2026-07-14", True), meal("2026-07-14", None),
        meal("2026-07-14", 250),
    ])
    dt_case("missing_date_buckets_under_empty", [
        {"analysis": {"is_food": True, "total_calories": 400}},
        meal("2026-07-14", 500),
    ])
    dt_case("float_calories_accumulate", [
        meal("2026-07-14", 250.5), meal("2026-07-14", 250.25),
    ])
    dt_case("empty", [])

    def med_case(case_id, day_totals):
        cases.append({"id": f"typical_median_{case_id}", "fn": "typical_day_median",
                      "input": encode_value(day_totals),
                      "expected": typical_day_median(day_totals)})

    med_case("two_days", {"2026-07-14": 1800, "2026-07-15": 2200})
    med_case("even_count_averages_middle_pair",
             {"d1": 1000, "d2": 1500, "d3": 2000, "d4": 3000})
    med_case("even_count_truncates_half", {"d1": 1001, "d2": 1002})
    med_case("odd_count", {"d1": 1200, "d2": 2600, "d3": 1900})
    med_case("order_independent", {"d1": 2600, "d2": 1200, "d3": 1900})
    med_case("single_day_null", {"2026-07-14": 1800})
    med_case("empty_null", {})

    def avg_case(case_id, meals):
        totals = report_prior_day_totals(meals)
        cases.append({"id": f"prior_avg_{case_id}", "fn": "report_prior_avg",
                      "input": encode_value(meals),
                      "expected": {"day_totals": encode_value(totals),
                                   "avg": report_seven_day_avg(totals)}})

    avg_case("two_days", [
        meal("2026-07-14", 1400), meal("2026-07-14", 600), meal("2026-07-15", 1800),
    ])
    avg_case("single_day_null_avg", [
        meal("2026-07-14", 900), meal("2026-07-14", 700),
    ])
    avg_case("empty", [])
    avg_case("filter_zero_and_negative", [
        meal("2026-07-14", 0), meal("2026-07-14", -300), meal("2026-07-14", 500),
        meal("2026-07-15", 700),
    ])
    avg_case("filter_upper_bound", [
        meal("2026-07-14", 1e9), meal("2026-07-14", 999999999),
        meal("2026-07-15", 1e9 - 1),
    ])
    avg_case("filter_non_numeric_and_bool", [
        meal("2026-07-14", "800"), meal("2026-07-14", True),
        meal("2026-07-14", None), meal("2026-07-14", 400),
        meal("2026-07-15", 600, is_food="truthy-string"),
    ])
    avg_case("filter_non_food", [
        meal("2026-07-14", 500), meal("2026-07-15", 800, is_food=False),
        meal("2026-07-15", 300),
    ])
    avg_case("bankers_rounding_half_to_even_down", [
        meal("2026-07-14", 1000), meal("2026-07-15", 1001),
    ])
    avg_case("bankers_rounding_half_to_even_up", [
        meal("2026-07-14", 1001), meal("2026-07-15", 1002),
    ])
    avg_case("float_calories", [
        meal("2026-07-14", 620.5), meal("2026-07-15", 700.25),
    ])

    return {"generator": GENERATOR, "encoding": ENCODING_NOTE, "cases": cases}


# ─── entry point ───────────────────────────────────────────────────

def build_weight():
    """parse_weight_kg — two hand-ported implementations with a rounding
    trap: Python's round(kg, 1) is half-to-EVEN on the exact binary value,
    Dart's .round() is half-away-from-zero, and multiplying by 10 first
    MANUFACTURES ties. Measured divergence before pinning: 72.25kg stored as
    72.2 by the server and 72.3 by the app; 29.95kg refused by one and
    accepted by the other. One weigh-in, two numbers, depending on client."""
    cases = []

    def case(case_id, text):
        cases.append({
            "id": f"parse_weight_{case_id}",
            "fn": "parse_weight_kg",
            "input": encode_value(text),
            "expected": encode_value(nutrition.parse_weight_kg(text)),
        })

    # Exact binary ties (.25/.5/.75) — half-to-even territory.
    case("tie_quarter_down", "72.25kg")
    case("tie_three_quarter_up", "72.75kg")
    case("tie_half_exact", "72.5kg")
    # Decimal .x5 values that are NOT binary ties.
    for v in ["72.35", "72.45", "72.55", "68.65", "65.15", "71.05"]:
        case(f"near_tie_{v.replace('.', '_')}", f"{v}kg")
    # Range boundaries, incl. values that round ACROSS them.
    case("below_floor", "29.95kg")
    case("at_floor", "30.0kg")
    case("above_ceiling", "300.05kg")
    case("at_ceiling", "300.04kg")
    # Units and conversion.
    case("pounds_int", "159 lb")
    case("pounds_decimal", "160.5 lb")
    case("pounds_word", "161.3 pounds")
    case("kg_spaced", "72.4 kg")
    case("integer_kg", "80kg")
    # Keyword path (bare number, no unit).
    case("keyword_bare", "I weigh 72.25")
    case("keyword_weight", "weight 68.65")
    # Rejections.
    case("no_number", "I feel heavy today")
    case("comma_decimal", "72,5kg")
    case("not_a_string", 72.5)
    case("empty", "")
    case("absurd_high", "900kg")
    case("absurd_low", "3kg")
    return {"cases": cases}


BUILDERS = {
    "coerce.json": build_coerce,
    "nl_normalize.json": build_nl_normalize,
    "captured_at.json": build_captured_at,
    "mismatch.json": build_mismatch,
    "report_formulas.json": build_report_formulas,
    "weight.json": build_weight,
}


def build_all():
    return {name: builder() for name, builder in BUILDERS.items()}


def main():
    VECTORS_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in build_all().items():
        path = VECTORS_DIR / name
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"{name}: {len(payload['cases'])} cases")


if __name__ == "__main__":
    main()
