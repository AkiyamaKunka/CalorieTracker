"""Golden-vector replay: shared/vectors/*.json must match Python behavior.

scripts/generate_shared_vectors.py computes every vector's expected output by
calling the real Python functions (the reference implementation for the Dart
port). This suite replays EVERY case in EVERY vector file through those same
implementations, so:

  - changing shared behavior without regenerating the vectors fails here
    (the stale file no longer matches the code), and
  - regenerating with changed behavior shows up as a reviewable JSON diff.

A final drift gate rebuilds each file in memory and compares the full
structure against what's on disk, so added/removed cases and header changes
can't slip through either.
"""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "android"))
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_bot  # noqa: E402
import upload_photo  # noqa: E402
import utils  # noqa: E402
import generate_shared_vectors as gen  # noqa: E402

VECTORS_DIR = ROOT / "shared" / "vectors"
VECTOR_FILES = [
    "coerce.json",
    "nl_normalize.json",
    "captured_at.json",
    "mismatch.json",
    "report_formulas.json",
]


def _load(name):
    path = VECTORS_DIR / name
    assert path.exists(), f"missing vector file {path} — run scripts/generate_shared_vectors.py"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _cases(name):
    payload = _load(name)
    return [pytest.param(payload, case, id=case["id"]) for case in payload["cases"]]


def _raises_marker(expected):
    return isinstance(expected, dict) and expected.get("__raises__") is True


# ─── coerce.json ───────────────────────────────────────────────────

@pytest.mark.parametrize("payload,case", _cases("coerce.json"))
def test_coerce_vectors(payload, case):
    fn = case["fn"]
    value = gen.decode_value(case["input"])
    expected = gen.decode_value(case["expected"])

    if fn == "safe_number":
        if "default" in case:
            actual = utils.safe_number(value, gen.decode_value(case["default"]))
        else:
            actual = utils.safe_number(value)
    elif fn == "parse_boolish":
        actual = utils.parse_boolish(value)
    elif fn == "safe_food_items":
        actual = utils.safe_food_items(value)
    elif fn == "parse_ai_json":
        if _raises_marker(case["expected"]):
            with pytest.raises(Exception):
                utils.parse_ai_json(value)
            return
        actual = utils.parse_ai_json(value)
    else:
        pytest.fail(f"unknown fn {fn!r} in coerce.json")

    assert actual == expected, f"{fn}({value!r})"


# ─── nl_normalize.json ─────────────────────────────────────────────

@pytest.mark.parametrize("payload,case", _cases("nl_normalize.json"))
def test_nl_normalize_vectors(payload, case):
    # deepcopy: _normalize_nl_actions mutates delete dicts when merging.
    actual = telegram_bot._normalize_nl_actions(copy.deepcopy(case["input"]))
    assert actual == case["expected"]


def test_nl_normalize_header_matches_module_cap():
    assert _load("nl_normalize.json")["max_actions"] == telegram_bot.NL_MAX_ACTIONS


# ─── captured_at.json ──────────────────────────────────────────────

@pytest.mark.parametrize("payload,case", _cases("captured_at.json"))
def test_captured_at_vectors(payload, case):
    fn = case["fn"]
    if fn == "captured_at_from_filename":
        assert upload_photo._captured_at_from_filename(case["input"]) == case["expected"]
    elif fn == "captured_at_valid":
        actual = gen.captured_at_valid(
            case["captured_at"], case["now"], payload["max_age_days"])
        assert actual == case["expected_valid"]
    else:
        pytest.fail(f"unknown fn {fn!r} in captured_at.json")


def test_captured_at_header_matches_module_max_age():
    # The vectors were generated under the default CAPTURED_AT_MAX_AGE_DAYS;
    # if the module (env-driven) value drifts, the boundary cases are stale.
    assert _load("captured_at.json")["max_age_days"] == telegram_bot.CAPTURED_AT_MAX_AGE_DAYS


# ─── mismatch.json ─────────────────────────────────────────────────

@pytest.mark.parametrize("payload,case", _cases("mismatch.json"))
def test_mismatch_vectors(payload, case):
    actual = utils.meal_calorie_mismatch(gen.decode_value(case["input"]))
    assert actual == case["expected"]


# ─── report_formulas.json ──────────────────────────────────────────

@pytest.mark.parametrize("payload,case", _cases("report_formulas.json"))
def test_report_formula_vectors(payload, case):
    fn = case["fn"]
    value = gen.decode_value(case["input"])
    if fn == "day_calorie_totals":
        assert gen.day_calorie_totals(value) == gen.decode_value(case["expected"])
    elif fn == "typical_day_median":
        assert gen.typical_day_median(value) == case["expected"]
    elif fn == "report_prior_avg":
        totals = gen.report_prior_day_totals(value)
        expected = gen.decode_value(case["expected"])
        assert totals == expected["day_totals"]
        assert gen.report_seven_day_avg(totals) == expected["avg"]
    else:
        pytest.fail(f"unknown fn {fn!r} in report_formulas.json")


# ─── drift gate ────────────────────────────────────────────────────

@pytest.mark.parametrize("name", VECTOR_FILES)
def test_vector_files_are_freshly_generated(name):
    """Rebuild each vector file in memory and compare with what's on disk.

    Catches everything the per-case replay can't: cases added/removed from
    the generator, header changes, or a hand-edited vector file. On failure,
    run scripts/generate_shared_vectors.py and review the diff.
    """
    fresh = gen.BUILDERS[name]()
    # Round-trip through JSON so -0.0/int-float notation quirks compare the
    # way a consumer will actually read them.
    fresh = json.loads(json.dumps(fresh, ensure_ascii=False))
    assert fresh == _load(name), (
        f"shared/vectors/{name} is stale — regenerate with "
        "scripts/generate_shared_vectors.py and review the diff"
    )
