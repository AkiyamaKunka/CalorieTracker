"""Metamorphic property tests (from the S11 hunt).

Deterministic, seeded, stdlib-only. These assert relationships that must
hold for ALL inputs, not just chosen examples — round-trips, idempotence,
range-bounding, claimed invariants — catching regressions example tests miss.
"""
import json
import random
from datetime import timedelta

import database
import utils

SEED = 20260711


def _rng():
    return random.Random(SEED)


def _rand_json_value(rng, depth=0):
    if depth > 3:
        return rng.choice([1, "x", True, None, 1.5])
    kind = rng.randint(0, 6)
    if kind == 0:
        return rng.randint(-10**6, 10**6)
    if kind == 1:
        return round(rng.uniform(-1000, 1000), 3)
    if kind == 2:
        return rng.choice(["", "food", "a b c", "café ☕", 'quote"s', "back\\slash"])
    if kind == 3:
        return rng.choice([True, False, None])
    if kind == 4:
        return [_rand_json_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {f"k{i}": _rand_json_value(rng, depth + 1) for i in range(rng.randint(0, 3))}


def test_parse_ai_json_roundtrips_and_tolerates_fences_and_prose():
    rng = _rng()
    for _ in range(800):
        obj = {f"key{i}": _rand_json_value(rng) for i in range(rng.randint(0, 4))}
        s = json.dumps(obj)
        assert utils.parse_ai_json(s) == obj
        assert utils.parse_ai_json(f"```json\n{s}\n```") == obj
        assert utils.parse_ai_json(f"Here you go: {s}") == obj


def test_safe_number_is_idempotent_bounded_and_passes_valid_through():
    rng = _rng()
    samples = [
        lambda: rng.randint(-10**12, 10**12),
        lambda: round(rng.uniform(-2e9, 2e9), 2),
        lambda: rng.choice([float("inf"), float("nan"), "5", [], {}, None,
                            True, False, 10**400, -10**400, 999_999_999, 1_000_000_001]),
    ]
    for _ in range(1500):
        x = rng.choice(samples)()
        n = utils.safe_number(x)
        assert utils.safe_number(n) == n                       # idempotent
        assert isinstance(n, (int, float)) and not isinstance(n, bool)
        assert -1e9 < n < 1e9 or n == 0                        # bounded
        if isinstance(x, (int, float)) and not isinstance(x, bool) and -1e9 < x < 1e9:
            assert n == x                                       # valid passthrough


def test_parse_timezone_offset_round_trips_and_rejects_out_of_range():
    for sign in "+-":
        for hh in range(0, 16):
            for mm in (0, 15, 30, 45, 59, 60):
                s = f"{sign}{hh:02d}{mm:02d}"
                off = database.parse_timezone_offset(s)
                if hh > 14 or mm > 59:
                    assert off is None, s
                else:
                    expect = (1 if sign == "+" else -1) * timedelta(hours=hh, minutes=mm)
                    assert off == expect, f"{s} -> {off}"


def test_meal_calorie_mismatch_invariant_holds_when_it_fires():
    rng = _rng()
    for _ in range(1500):
        total = rng.choice([rng.randint(0, 5000), rng.uniform(0, 5000), None, "x", []])
        items = [{"estimated_calories": rng.choice(
            [rng.randint(0, 2000), rng.uniform(0, 2000), None, "y"])}
            for _ in range(rng.randint(0, 5))]
        v = utils.meal_calorie_mismatch({"total_calories": total, "food_items": items})
        assert v is None or (isinstance(v, int) and v > 0)
        if v is not None:
            t = utils.safe_number(total)
            assert abs(t - v) > max(100, 0.2 * max(t, v))       # claimed invariant


def test_telegram_message_chunks_are_bounded_and_content_preserving():
    rng = _rng()
    for _ in range(800):
        lines = ["x" * rng.randint(0, 60) for _ in range(rng.randint(0, 40))]
        text = "\n".join(lines)
        limit = rng.choice([50, 100, 3900])
        chunks = utils.telegram_message_chunks(text, limit)
        assert len(chunks) >= 1
        assert all(len(c) <= limit for c in chunks)
        # every non-whitespace char survives, in order
        assert "".join("".join(chunks).split()) == "".join(text.split())
