"""Smoke test for scripts/bench_latency_components.py.

The harness is a measurement tool, not product code — this test only pins
that it stays runnable and deterministic in shape: no network, scratch-dir
isolation, and every expected component key present with sane timings.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bench_latency_components as bench


EXPECTED_TIMED_COMPONENTS = {
    "md5_hash",
    "stage_write",
    "stage_read",
    "prepare_gemini",
    "compress_echo",
    "db_today_summary",
    "db_food_card",
    "db_dup_check",
    "nl_context_build",
    "truncate_html",
}


def test_bench_harness_runs_and_reports_all_components(tmp_path, monkeypatch):
    import database
    import telegram_bot as tb

    # run_benchmarks reassigns these module globals; monkeypatch restores the
    # originals after the test so the suite's own isolation is untouched.
    monkeypatch.setattr(database, "DB_PATH", database.DB_PATH)
    monkeypatch.setattr(tb, "SERVICE_HEALTH_PATH", tb.SERVICE_HEALTH_PATH)

    results = bench.run_benchmarks(tmp_path, repeats=1, photo_size=(640, 480))

    assert results["synth_photo_bytes"] > 0
    assert results["echo_bytes"] > 0
    for name in EXPECTED_TIMED_COMPONENTS:
        timing = results[name]
        assert timing["min_ms"] >= 0
        assert timing["median_ms"] >= timing["min_ms"]
        assert timing["max_ms"] >= timing["median_ms"]

    # The scratch DB must have been created inside the sandbox dir.
    assert (tmp_path / "bench_meals.db").exists()


def test_synthetic_photo_is_deterministic():
    a = bench.build_synthetic_photo((320, 240))
    b = bench.build_synthetic_photo((320, 240))
    assert a == b
