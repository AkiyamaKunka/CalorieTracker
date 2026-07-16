"""Tests for scripts/journal_latency_report.py — deterministic, no network.

The fixture is a synthetic journalctl short-precise dump that exercises every
pairing rule: boot timing, a single-flight Claude chain, an overlapping
(concurrent) pair whose FIFO pairing must be flagged, a PID change that
abandons open chains, and NL-text handling.
"""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "journal_latency_report",
    Path(__file__).resolve().parent.parent / "scripts" / "journal_latency_report.py",
)
jlr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(jlr)

FIXTURE = """\
Jul 15 20:58:08.716864 host systemd[1]: Started Calorie Tracker Telegram Bot.
Jul 15 20:58:12.025249 host python3[100]: 20:58:12 [INFO] 🤖 CalorieTracker Bot (Corrections Mode)
Jul 15 20:58:12.064528 host python3[100]: 20:58:12 [INFO] Bot is running! Listening for corrections and commands.
Jul 15 21:29:08.018696 host python3[100]: 21:29:08 [INFO] 🔍 Analyzing food from Android API upload in background...
Jul 15 21:29:30.185103 host python3[100]: 21:29:30 [INFO] ✅ Photo analyzed by Claude (subscription).
Jul 15 21:29:30.597522 host python3[100]: 21:29:30 [INFO]   ✅ API Food: test meal (~620 kcal)
Jul 15 22:00:00.000000 host python3[100]: 22:00:00 [INFO] 🔍 Analyzing food from iPhone API upload in background...
Jul 15 22:00:01.000000 host python3[100]: 22:00:01 [INFO] 🔍 Analyzing food from iPhone API upload in background...
Jul 15 22:00:05.000000 host python3[100]: 22:00:05 [INFO]   ⏭️ API Upload: Not food
Jul 15 22:00:09.000000 host python3[100]: 22:00:09 [INFO]   ✅ API Food: second meal (~100 kcal)
Jul 15 22:30:00.000000 host python3[100]: 22:30:00 [INFO] [U] Text: I weighed 71.8 kg
Jul 15 22:30:02.500000 host python3[100]: 22:30:02 [INFO]   ⚖️ NL weight logged: 71.8 kg
Jul 15 22:31:00.000000 host python3[100]: 22:31:00 [INFO] [U] Text: what should I run today?
Jul 15 22:31:00.250000 host python3[100]: 22:31:00 [INFO]   🏃 Deterministic run query answered (no Gemini spend)
Jul 15 23:00:00.000000 host python3[100]: 23:00:00 [INFO] 🔍 Analyzing food from Android API upload in background...
Jul 15 23:57:23.633858 host systemd[1]: Started Calorie Tracker Telegram Bot.
Jul 15 23:57:27.317455 host python3[200]: 23:57:27 [INFO] 🤖 CalorieTracker Bot (Corrections Mode)
Jul 15 23:57:27.347025 host python3[200]: 23:57:27 [INFO] Bot is running! Listening for corrections and commands.
Jul 15 23:58:00.000000 host python3[200]: 23:58:00 [INFO]   ✅ API Food: ghost meal from dead pid (~1 kcal)
"""


def _run(tmp_path):
    p = tmp_path / "journal.log"
    p.write_text(FIXTURE, encoding="utf-8")
    return jlr.analyze(str(p), 2026)


def test_boot_durations(tmp_path):
    boots, _, _ = _run(tmp_path)
    durations = [(r - s).total_seconds() for s, r, _ in boots if s]
    assert len(durations) == 2
    assert abs(durations[0] - 3.347664) < 1e-4
    assert abs(durations[1] - 3.713167) < 1e-4


def test_single_flight_claude_chain(tmp_path):
    _, chains, _ = _run(tmp_path)
    clean = [c for c in chains if not c.concurrent]
    assert len(clean) == 1
    c = clean[0]
    assert c.kind == "upload"
    assert c.analyzed[1] == "claude"
    assert abs((c.analyzed[0] - c.start).total_seconds() - 22.166407) < 1e-4
    assert abs((c.end[0] - c.start).total_seconds() - 22.578826) < 1e-4
    assert c.end[1] == "food"


def test_overlapping_chains_are_flagged_concurrent(tmp_path):
    _, chains, _ = _run(tmp_path)
    conc = [c for c in chains if c.concurrent]
    assert len(conc) == 2
    # FIFO pairing: first start -> first terminal (5s), second -> second (8s)
    assert abs((conc[0].end[0] - conc[0].start).total_seconds() - 5.0) < 1e-6
    assert abs((conc[1].end[0] - conc[1].start).total_seconds() - 8.0) < 1e-6


def test_pid_change_abandons_open_chains(tmp_path):
    _, chains, _ = _run(tmp_path)
    # The 23:00:00 chain on pid 100 never completes; the terminal line on
    # pid 200 must NOT be paired with it (3 completed chains total, and the
    # pid-200 orphan terminal is dropped because pid 200 has no open chain).
    assert len(chains) == 3
    assert all(c.end is not None for c in chains)


def test_text_nl_and_deterministic_split(tmp_path):
    _, _, texts = _run(tmp_path)
    assert len(texts) == 2
    nl = [t for t in texts if "Deterministic" not in t[2]]
    det = [t for t in texts if "Deterministic" in t[2]]
    assert len(nl) == 1 and len(det) == 1
    assert abs((nl[0][1] - nl[0][0]).total_seconds() - 2.5) < 1e-6
    assert abs((det[0][1] - det[0][0]).total_seconds() - 0.25) < 1e-6


def test_percentile_nearest_rank():
    vals = sorted([1.0, 2.0, 3.0, 4.0, 5.0])
    assert jlr.pctl(vals, 50) == 3.0
    assert jlr.pctl(vals, 95) == 5.0
    assert jlr.pctl([7.5], 95) == 7.5
    assert jlr.pctl([], 50) is None
