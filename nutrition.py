"""Diet-mode macro planning and analysis.

Pure module: stdlib + ``utils.safe_number`` only. It must never import
database/telegram/gemini so it stays trivially testable and side-effect free.

All macro/calorie inputs that originate from meal analysis are UNTRUSTED and
are coerced through :func:`utils.safe_number`, so a hostile or malformed meal
shape can never raise here.
"""

import html
import re
from typing import Optional

from utils import safe_number

DIET_MODES = ("keto", "high_protein", "balanced")

# Calorie yield per gram of each macronutrient (Atwater factors).
KCAL_PER_G = {"protein": 4, "carbs": 4, "fat": 9}

# Hard lower bound: we never hand back a calorie plan below this. Anything
# lower is clamped up to the floor.
MIN_CALORIE_FLOOR = 1200

DISCLAIMER = "Informational only — not medical advice."

# Per-mode plan definitions. ``split`` values are %kcal and sum to 100.
# ``carb_cap_g`` is a hard daily ceiling (only keto uses one).
MODE_SPECS = {
    "keto": {
        "label": "Keto",
        "protein_g_per_kg": 1.6,
        "carb_cap_g": 50,
        "split": {"protein": 23, "carbs": 5, "fat": 72},
    },
    "high_protein": {
        "label": "High-protein",
        "protein_g_per_kg": 2.0,
        "carb_cap_g": None,
        "split": {"protein": 30, "carbs": 40, "fat": 30},
    },
    "balanced": {
        "label": "Balanced",
        "protein_g_per_kg": 1.6,
        "carb_cap_g": None,
        "split": {"protein": 30, "carbs": 40, "fat": 30},
    },
}

_STATUS_BADGE = {"short": "⬇️", "ok": "✅", "over": "⬆️", "unknown": ""}


def _esc(value) -> str:
    """HTML-escape a value for safe inclusion in Telegram-HTML text."""
    return html.escape(str(value if value is not None else ""))


def _normalize_mode(mode) -> str:
    """Unknown / junk mode falls back to balanced."""
    # isinstance first: an unhashable junk mode (list/dict) would make the
    # dict-membership test itself raise, breaking the documented fallback.
    return mode if isinstance(mode, str) and mode in MODE_SPECS else "balanced"


def _pos(value) -> Optional[float]:
    """Coerce to a positive number or None (unknown).

    Junk / non-positive inputs become None so we can distinguish "not
    provided" from "provided but unusable".
    """
    num = safe_number(value, 0)
    return num if num > 0 else None


def _g(value) -> str:
    """Format a gram amount as an integer string like ``40g``."""
    return "{}g".format(int(round(safe_number(value))))


def format_split_line(split) -> str:
    """Render a P/C/F percentage split dict as the shared ``Split:`` line."""
    split = split if isinstance(split, dict) else {}
    return "Split: P {}% · C {}% · F {}%".format(
        int(round(safe_number(split.get("protein")))),
        int(round(safe_number(split.get("carbs")))),
        int(round(safe_number(split.get("fat")))),
    )


def diet_targets(mode, weight_kg=None, calorie_target=None, protein_g_per_kg=None) -> dict:
    """Compute macro targets for a diet mode.

    Protein is preferentially set from body weight
    (``protein_g_per_kg * weight_kg``), overridable via ``protein_g_per_kg``.
    When a calorie target is supplied, per-macro gram targets are derived from
    the mode's %kcal split (keto additionally caps carbs at 50 g). With neither
    weight nor calories known, only the percentage split is returned. An
    unrecognised mode is treated as balanced.
    """
    mode = _normalize_mode(mode)
    spec = MODE_SPECS[mode]
    split = dict(spec["split"])
    carb_cap = spec["carb_cap_g"]

    ppk = _pos(protein_g_per_kg) or spec["protein_g_per_kg"]
    weight = _pos(weight_kg)
    calories = _pos(calorie_target)

    result = {
        "mode": mode,
        "split": split,
        "protein_g_per_kg": ppk,
        "carb_cap_g": carb_cap,
        "calories": None,
        "target_protein_g": None,
        "target_carbs_g": None,
        "target_fat_g": None,
    }

    # Protein from body weight is the clinically grounded anchor; keep it even
    # when calories are also known.
    if weight:
        result["target_protein_g"] = round(ppk * weight)

    if calories:
        cal = max(calories, MIN_CALORIE_FLOOR)  # never plan below the floor
        result["calories"] = round(cal)

        p_g = (split["protein"] / 100.0) * cal / KCAL_PER_G["protein"]
        c_g = (split["carbs"] / 100.0) * cal / KCAL_PER_G["carbs"]
        f_g = (split["fat"] / 100.0) * cal / KCAL_PER_G["fat"]

        if carb_cap is not None and c_g > carb_cap:
            c_g = carb_cap

        if not weight:  # only fall back to split-derived protein without a weight
            result["target_protein_g"] = round(p_g)
        result["target_carbs_g"] = round(c_g)
        result["target_fat_g"] = round(f_g)

    return result


def profile_diet_targets(mode, weight_kg, profile) -> dict:
    """Mode targets overlaid with the profile's explicit gram targets.

    Explicit gram targets from '/diet target …' win over mode-derived ones,
    so the daily report and /macros always agree.
    """
    targets = diet_targets(
        mode,
        weight_kg=weight_kg,
        calorie_target=(profile or {}).get("target_calories"),
        protein_g_per_kg=(profile or {}).get("protein_g_per_kg"),
    )
    for key in ("target_protein_g", "target_carbs_g", "target_fat_g"):
        stored = safe_number((profile or {}).get(key), None)
        if stored is not None:
            targets[key] = stored
    return targets


def _iter_analyses(meals):
    """Yield each meal's analysis dict, tolerating any junk container shape."""
    if not isinstance(meals, list):
        return
    for meal in meals:
        if not isinstance(meal, dict):
            continue
        analysis = meal.get("analysis")
        if not isinstance(analysis, dict):
            continue
        if analysis.get("is_food") is False:  # skip explicit non-food entries
            continue
        yield analysis


def _status(consumed, target) -> str:
    """short / ok / over relative to ``target`` with a 10% (min 5 g) band."""
    if target is None:
        return "unknown"
    tol = max(5, 0.1 * target)
    if consumed < target - tol:
        return "short"
    if consumed > target + tol:
        return "over"
    return "ok"


def _protein_food(short_g) -> str:
    """A concrete, non-medical food suggestion sized to the protein shortfall."""
    if short_g >= 35:
        return "a chicken breast (~40g)"
    if short_g >= 20:
        return "a can of tuna (~25g)"
    if short_g >= 10:
        return "Greek yogurt (~17g)"
    return "an egg (~6g)"


def analyze_macros(meals, targets) -> dict:
    """Compare consumed macros (summed from meals) against ``targets``.

    Returns consumed totals, the actual %kcal split, per-macro delta/status
    versus target, and a list of specific, non-medical suggestions. Never
    raises on malformed meals or targets.
    """
    targets = targets if isinstance(targets, dict) else {}

    consumed = {"protein": 0.0, "carbs": 0.0, "fat": 0.0, "calories": 0.0}
    for analysis in _iter_analyses(meals):
        # Negative totals are physically impossible; clamp so a hostile
        # payload can't push actual_split outside 0..100.
        consumed["protein"] += max(0.0, safe_number(analysis.get("total_protein_g")))
        consumed["carbs"] += max(0.0, safe_number(analysis.get("total_carbs_g")))
        consumed["fat"] += max(0.0, safe_number(analysis.get("total_fat_g")))
        consumed["calories"] += max(0.0, safe_number(analysis.get("total_calories")))

    # Actual macro split by calories contributed.
    macro_kcal = {
        "protein": consumed["protein"] * KCAL_PER_G["protein"],
        "carbs": consumed["carbs"] * KCAL_PER_G["carbs"],
        "fat": consumed["fat"] * KCAL_PER_G["fat"],
    }
    total_macro_kcal = sum(macro_kcal.values())
    if total_macro_kcal > 0:
        actual_split = {k: round(v / total_macro_kcal * 100) for k, v in macro_kcal.items()}
    else:
        actual_split = {"protein": 0, "carbs": 0, "fat": 0}

    # Numeric-or-None: the docstring promises "never raises on malformed
    # targets", but a string carb_cap_g reached the `>` comparison raw.
    carb_cap = targets.get("carb_cap_g")
    if isinstance(carb_cap, bool) or not isinstance(carb_cap, (int, float)):
        carb_cap = None
    elif carb_cap != carb_cap or carb_cap in (float("inf"), float("-inf")):
        carb_cap = None
    macros = {}
    suggestions = []

    # ── Protein ───────────────────────────────────────────────
    p_consumed = round(consumed["protein"], 1)
    p_target = targets.get("target_protein_g")
    p_status = _status(p_consumed, p_target)
    macros["protein"] = {
        "consumed_g": p_consumed,
        "target_g": p_target,
        "delta_g": round(p_consumed - p_target, 1) if p_target is not None else None,
        "status": p_status,
    }
    if p_status == "short":
        short = int(round(p_target - p_consumed))
        if short > 0:
            suggestions.append(
                "~{}g protein short — add {}".format(short, _protein_food(short))
            )

    # ── Carbs (keto uses the hard cap; others use the split target) ──
    c_consumed = round(consumed["carbs"], 1)
    c_target = targets.get("target_carbs_g")
    if carb_cap is not None:
        effective = c_target if c_target is not None else carb_cap
        c_status = "over" if c_consumed > carb_cap else "ok"
        macros["carbs"] = {
            "consumed_g": c_consumed,
            "target_g": effective,
            "delta_g": round(c_consumed - carb_cap, 1),
            "status": c_status,
        }
        if c_status == "over":
            suggestions.append(
                "carbs {}g over the {}g cap — cut rice/bread".format(
                    int(round(c_consumed)), carb_cap
                )
            )
    else:
        c_status = _status(c_consumed, c_target)
        macros["carbs"] = {
            "consumed_g": c_consumed,
            "target_g": c_target,
            "delta_g": round(c_consumed - c_target, 1) if c_target is not None else None,
            "status": c_status,
        }
        if c_status == "over":
            over = int(round(c_consumed - c_target))
            suggestions.append("~{}g carbs over — ease off starchy sides".format(over))
        elif c_status == "short":
            short = int(round(c_target - c_consumed))
            suggestions.append("~{}g carbs short — add fruit or whole grains".format(short))

    # ── Fat ───────────────────────────────────────────────────
    f_consumed = round(consumed["fat"], 1)
    f_target = targets.get("target_fat_g")
    f_status = _status(f_consumed, f_target)
    macros["fat"] = {
        "consumed_g": f_consumed,
        "target_g": f_target,
        "delta_g": round(f_consumed - f_target, 1) if f_target is not None else None,
        "status": f_status,
    }
    if f_status == "over":
        over = int(round(f_consumed - f_target))
        suggestions.append("~{}g fat over — trim oils and fatty cuts".format(over))
    elif f_status == "short":
        short = int(round(f_target - f_consumed))
        suggestions.append("~{}g fat short — add avocado, olive oil, or nuts".format(short))

    # ── Calories ──────────────────────────────────────────────
    cal_consumed = round(consumed["calories"])
    cal_target = targets.get("calories")
    cal_status = _status(cal_consumed, cal_target)
    calories = {
        "consumed": cal_consumed,
        "target": cal_target,
        "delta": round(cal_consumed - cal_target) if cal_target is not None else None,
        "status": cal_status,
    }
    if cal_target is not None:
        if cal_status == "over":
            suggestions.append(
                "~{}kcal over target — lighten the next meal".format(
                    int(round(cal_consumed - cal_target))
                )
            )
        elif cal_status == "short":
            suggestions.append(
                "~{}kcal under target — room for a light snack".format(
                    int(round(cal_target - cal_consumed))
                )
            )

    if not suggestions:
        suggestions.append("Macros look on track — nice work.")

    return {
        "mode": targets.get("mode"),
        "consumed": {
            "protein_g": p_consumed,
            "carbs_g": c_consumed,
            "fat_g": f_consumed,
            "calories": cal_consumed,
        },
        "actual_split": actual_split,
        "macros": macros,
        "calories": calories,
        "suggestions": suggestions,
    }


def format_macro_report(analysis_result, targets, mode) -> str:
    """Render a multi-line Telegram-HTML macro block ending with the disclaimer."""
    analysis_result = analysis_result if isinstance(analysis_result, dict) else {}
    mode = _normalize_mode(mode)
    label = MODE_SPECS[mode]["label"]

    macros = analysis_result.get("macros") or {}
    cal = analysis_result.get("calories") or {}
    actual_split = analysis_result.get("actual_split") or {}
    suggestions = analysis_result.get("suggestions") or []

    lines = ["<b>🥗 Macro check — {}</b>".format(_esc(label))]

    c_consumed = int(round(safe_number(cal.get("consumed"))))
    c_target = cal.get("target")
    if c_target:
        lines.append("Calories: <b>{:,}</b> / {:,} kcal".format(c_consumed, int(round(c_target))))
    else:
        lines.append("Calories: <b>{:,}</b> kcal".format(c_consumed))

    for name, glyph in (("protein", "Protein"), ("carbs", "Carbs"), ("fat", "Fat")):
        m = macros.get(name) if isinstance(macros.get(name), dict) else {}
        m = m or {}
        badge = _STATUS_BADGE.get(m.get("status", ""), "")
        target_g = m.get("target_g")
        if target_g is not None:
            line = "{}: {} / {} {}".format(glyph, _g(m.get("consumed_g")), _g(target_g), badge)
        else:
            line = "{}: {} {}".format(glyph, _g(m.get("consumed_g")), badge)
        lines.append(line.rstrip())

    if actual_split:
        lines.append(format_split_line(actual_split))

    if suggestions:
        lines.append("")
        for s in suggestions:
            lines.append("• {}".format(_esc(s)))

    lines.append("")
    lines.append("<i>{}</i>".format(_esc(DISCLAIMER)))
    return "\n".join(lines)


def report_line(analysis_result, targets, mode) -> str:
    """One compact, HTML-safe summary line for the daily report."""
    analysis_result = analysis_result if isinstance(analysis_result, dict) else {}
    mode = _normalize_mode(mode)
    label = MODE_SPECS[mode]["label"]
    macros = analysis_result.get("macros") or {}
    cal = analysis_result.get("calories") or {}

    def grams(name):
        m = macros.get(name) if isinstance(macros.get(name), dict) else {}
        return int(round(safe_number((m or {}).get("consumed_g"))))

    tail = ""
    for name in ("protein", "carbs", "fat"):
        m = macros.get(name) if isinstance(macros.get(name), dict) else {}
        if (m or {}).get("status") in ("short", "over"):
            tail = " — {} {}".format(name, m["status"])
            break

    return "🥗 {}: {}g P · {}g C · {}g F ({:,} kcal){}".format(
        label,
        grams("protein"),
        grams("carbs"),
        grams("fat"),
        int(round(safe_number(cal.get("consumed")))),
        tail,
    )


# The (?<![\w.]) lookbehind stops the number from starting mid-token, so
# scientific notation like "1e72" can't have its exponent read as 72 kg.
# The (?![a-z]) lookahead stops the unit from matching mid-token, so the
# "kilo" prefix of "kilometers"/"kilometres" can't be read as kilograms.
_WEIGHT_UNIT_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(kgs?|kilograms?|kilos?|lbs?|pounds?)(?![a-z])",
    re.IGNORECASE | re.ASCII,
)
# Bare number guarded by an explicit weigh(ed/s/t) keyword, interpreted as kg.
# (?<![a-z]): 'overweight'/'underweight' must not arm the keyword — the bare
# number after them is almost never a weigh-in. re.ASCII on both: Python \d
# otherwise matches Unicode digits that float() parses but the Dart port's
# ASCII \d rejects — same text logged a weigh-in on the server and nothing on
# the phone (pressure-test finds, 2026-08-03).
_WEIGHT_KEYWORD_RE = re.compile(
    r"(?<![a-z])weigh(?:ed|s|t)?\D{0,12}?(?<![\w.])(\d+(?:\.\d+)?)",
    re.IGNORECASE | re.ASCII,
)
_LB_TO_KG = 0.45359237
MIN_WEIGHT_KG = 30
MAX_WEIGHT_KG = 300


_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９．", "0123456789.")


def parse_weight_kg(text) -> Optional[float]:
    """Extract a plausible body weight in kg from free text.

    Understands explicit kg/lb units ("72.5kg", "159 lb" -> converted), and a
    bare number when preceded by a weigh-keyword. Fullwidth digits from a
    Chinese IME (７２．５) normalize to ASCII first — the regexes are ASCII-only
    so the Dart port parses the same texts identically. Values outside
    30..300 kg (or no match) return None.
    """
    if not isinstance(text, str):
        return None
    text = text.translate(_FULLWIDTH_DIGITS)

    for num_str, unit in _WEIGHT_UNIT_RE.findall(text):
        try:
            value = float(num_str)
        except ValueError:
            continue
        unit = unit.lower()
        kg = value * _LB_TO_KG if unit.startswith(("lb", "pound")) else value
        kg = round(kg, 1)
        if MIN_WEIGHT_KG <= kg <= MAX_WEIGHT_KG:
            return kg

    match = _WEIGHT_KEYWORD_RE.search(text)
    if match:
        kg = round(float(match.group(1)), 1)
        if MIN_WEIGHT_KG <= kg <= MAX_WEIGHT_KG:
            return kg

    return None
