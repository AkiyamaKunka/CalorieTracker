import sys
import os
import base64
import json
import logging
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import claude_analyzer
import database
import telegram_bot
import utils


GOOD_ANALYSIS = {"is_food": True, "meal_description": "Latte", "total_calories": 150,
                 "total_protein_g": 8, "total_carbs_g": 12, "total_fat_g": 8,
                 "food_items": []}


def _envelope(result_text, is_error=False, turns=None):
    env = {"type": "result", "subtype": "success",
           "is_error": is_error, "result": result_text}
    if turns is not None:
        env["num_turns"] = turns
    return json.dumps(env)


def _configure(monkeypatch, enabled="1", cli="/usr/local/bin/claude", dispatch=None):
    monkeypatch.setenv("CLAUDE_ANALYZER_ENABLED", enabled)
    # Hermetic dispatch: default to the module default (stream) regardless of
    # any ambient CLAUDE_ANALYZER_DISPATCH in the developer's environment.
    if dispatch is None:
        monkeypatch.delenv("CLAUDE_ANALYZER_DISPATCH", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_ANALYZER_DISPATCH", dispatch)
    monkeypatch.setattr(claude_analyzer.shutil, "which", lambda *a, **k: cli)


def _fake_run(monkeypatch, stdout="", returncode=0, stderr="", raise_timeout=False):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 120))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(claude_analyzer.subprocess, "run", run)
    return calls


def test_disabled_by_default_no_subprocess(monkeypatch):
    monkeypatch.delenv("CLAUDE_ANALYZER_ENABLED", raising=False)
    calls = _fake_run(monkeypatch)
    assert claude_analyzer.is_configured() is False
    assert claude_analyzer.analyze_food_photo(b"jpegbytes") is None
    assert calls == []


def test_enabled_but_cli_missing_is_unconfigured(monkeypatch):
    _configure(monkeypatch, cli=None)
    assert claude_analyzer.is_configured() is False


def test_successful_analysis_with_fenced_json(monkeypatch):
    _configure(monkeypatch)
    fenced = "```json\n" + json.dumps(GOOD_ANALYSIS) + "\n```"
    calls = _fake_run(monkeypatch, stdout=_envelope(fenced))

    result = claude_analyzer.analyze_food_photo(b"jpegbytes")

    assert result == GOOD_ANALYSIS
    assert len(calls) == 1                    # single-turn: no fallback spent
    cmd = calls[0]
    assert cmd[0] == "/usr/local/bin/claude"
    assert "-p" in cmd and "--output-format" in cmd
    # Default dispatch is the single-turn stream path — image on stdin.
    assert "--input-format" in cmd


def test_successful_analysis_plain_json(monkeypatch):
    _configure(monkeypatch)
    _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    assert claude_analyzer.analyze_food_photo(b"x") == GOOD_ANALYSIS


def test_model_override_passed_through(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("CLAUDE_ANALYZER_MODEL", "claude-sonnet-5")
    calls = _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    claude_analyzer.analyze_food_photo(b"x")
    assert "--model" in calls[0]
    assert calls[0][calls[0].index("--model") + 1] == "claude-sonnet-5"


@pytest.mark.parametrize("stdout,returncode,stderr", [
    ("", 1, "Claude AI usage limit reached — resets 3pm"),   # rate window
    ("", 1, "Invalid OAuth token"),                          # bad auth
    ("not json at all", 0, ""),                              # garbage envelope
    (json.dumps({"type": "result", "is_error": True, "result": "boom"}), 0, ""),
    (_envelope("no json here"), 0, ""),                      # unparseable result
    (_envelope(json.dumps({"note": "missing contract"})), 0, ""),  # no is_food
    (_envelope(""), 0, ""),                                  # empty result
])
def test_failure_modes_return_none(monkeypatch, stdout, returncode, stderr):
    _configure(monkeypatch)
    _fake_run(monkeypatch, stdout=stdout, returncode=returncode, stderr=stderr)
    assert claude_analyzer.analyze_food_photo(b"x") is None


def test_timeout_returns_none(monkeypatch):
    _configure(monkeypatch)
    _fake_run(monkeypatch, raise_timeout=True)
    assert claude_analyzer.analyze_food_photo(b"x") is None


def test_temp_file_cleaned_up(monkeypatch, tmp_path):
    _configure(monkeypatch, dispatch="file")
    seen = {}

    def run(cmd, **kwargs):
        prompt = cmd[cmd.index("-p") + 1]
        # Extract the temp path from the prompt's first line.
        seen["path"] = prompt.split("Read the image file at ", 1)[1].split(" and analyze", 1)[0]
        assert os.path.exists(seen["path"])          # exists during the run
        return SimpleNamespace(returncode=0, stdout=_envelope(json.dumps(GOOD_ANALYSIS)), stderr="")

    monkeypatch.setattr(claude_analyzer.subprocess, "run", run)
    claude_analyzer.analyze_food_photo(b"jpegbytes")
    assert not os.path.exists(seen["path"])          # removed afterwards


def test_timeout_env_clamped(monkeypatch):
    monkeypatch.setenv("CLAUDE_ANALYZER_TIMEOUT_SECONDS", "999999")
    assert claude_analyzer._timeout_seconds() == 600
    monkeypatch.setenv("CLAUDE_ANALYZER_TIMEOUT_SECONDS", "junk")
    assert claude_analyzer._timeout_seconds() == 120


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers for the hardening suite below
# ─────────────────────────────────────────────────────────────────────────────

def _prompt_image_path(cmd):
    prompt = cmd[cmd.index("-p") + 1]
    return prompt.split("Read the image file at ", 1)[1].split(" and analyze", 1)[0]


def _fake_run_capture(monkeypatch, stdout="", returncode=0, stderr=""):
    """Like _fake_run but also records the kwargs subprocess.run received."""
    captured = []

    def run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(claude_analyzer.subprocess, "run", run)
    return captured


def _redirect_tempdir(monkeypatch, tmp_path):
    """Route NamedTemporaryFile into tmp_path so leftovers are observable."""
    monkeypatch.setattr(claude_analyzer.tempfile, "tempdir", str(tmp_path))


def _leftover_temp_files(tmp_path):
    return sorted(p.name for p in tmp_path.glob("ct_claude_*"))


# ─────────────────────────────────────────────────────────────────────────────
# Envelope hardening: hostile / degenerate CLI output shapes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stdout", [
    pytest.param("[]", id="envelope-json-array"),
    pytest.param("true", id="envelope-json-scalar"),
    pytest.param(json.dumps({"type": "result", "subtype": "success", "is_error": False}),
                 id="result-key-missing"),
    pytest.param(json.dumps({"type": "result", "is_error": False, "result": {"is_food": True}}),
                 id="result-not-a-string"),
    pytest.param(json.dumps({"type": "result", "is_error": False, "result": None}),
                 id="result-json-null-value"),
    pytest.param(_envelope("[1, 2, 3]"), id="result-json-array"),
    pytest.param(_envelope("null"), id="result-literal-null"),
    pytest.param(_envelope("   \n\t "), id="result-whitespace-only"),
    pytest.param(_envelope(json.dumps({"analysis": {"is_food": True}})),
                 id="is_food-nested-not-top-level"),
    pytest.param(json.dumps({"type": "result", "subtype": "error_during_execution",
                             "is_error": True, "result": json.dumps(GOOD_ANALYSIS)}),
                 id="error-envelope-wins-over-good-payload"),
    pytest.param(_envelope('Here {"a": 1} and later {"is_food": true}'),
                 id="two-json-objects-in-prose"),
    pytest.param("{" * 100_000, id="giant-garbage-stdout"),
    pytest.param(_envelope("A" * 1_000_000), id="giant-non-json-result"),
])
def test_hostile_envelopes_return_none_and_leave_no_temp_files(monkeypatch, tmp_path, stdout):
    _configure(monkeypatch)
    _redirect_tempdir(monkeypatch, tmp_path)
    _fake_run(monkeypatch, stdout=stdout)
    assert claude_analyzer.analyze_food_photo(b"img") is None
    assert _leftover_temp_files(tmp_path) == []


def test_empty_stdout_with_stderr_only_crash(monkeypatch, tmp_path):
    """A CLI that dies writing only to stderr (but exits 0) must not crash the bot."""
    _configure(monkeypatch)
    _redirect_tempdir(monkeypatch, tmp_path)
    _fake_run(monkeypatch, stdout="", stderr="FATAL ERROR: JS heap out of memory", returncode=0)
    assert claude_analyzer.analyze_food_photo(b"img") is None
    assert _leftover_temp_files(tmp_path) == []


@pytest.mark.parametrize("result_text", [
    pytest.param("Sure! Here is the analysis:\n" + json.dumps(GOOD_ANALYSIS) + "\nHope that helps.",
                 id="prose-wrapped"),
    pytest.param("```\n" + json.dumps(GOOD_ANALYSIS) + "\n```", id="fence-no-language-tag"),
    pytest.param(json.dumps(GOOD_ANALYSIS, indent=2), id="pretty-printed"),
])
def test_tolerated_result_wrappers_still_parse(monkeypatch, result_text):
    _configure(monkeypatch)
    _fake_run(monkeypatch, stdout=_envelope(result_text))
    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS


def test_real_shaped_envelope_with_extra_fields_and_padding(monkeypatch):
    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 4200, "num_turns": 1, "session_id": "abc-123",
        "total_cost_usd": 0.0, "usage": {"input_tokens": 9000},
        "result": json.dumps(GOOD_ANALYSIS),
    })
    _configure(monkeypatch)
    _fake_run(monkeypatch, stdout="\n " + envelope + "\n")
    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS


def test_non_food_minimal_contract_passes_through(monkeypatch):
    _configure(monkeypatch)
    _fake_run(monkeypatch, stdout=_envelope('{"is_food": false}'))
    assert claude_analyzer.analyze_food_photo(b"img") == {"is_food": False}


def test_success_log_signal_fires_only_on_success(monkeypatch, caplog):
    """'✅ Photo analyzed by Claude' is the health/log signal — it must be accurate."""
    _configure(monkeypatch)
    _fake_run(monkeypatch, stdout="not json at all")
    with caplog.at_level(logging.INFO, logger="claude_analyzer"):
        assert claude_analyzer.analyze_food_photo(b"img") is None
    assert "Photo analyzed by Claude" not in caplog.text

    caplog.clear()
    _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    with caplog.at_level(logging.INFO, logger="claude_analyzer"):
        assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    assert "Photo analyzed by Claude" in caplog.text


# ─────────────────────────────────────────────────────────────────────────────
# Contract validation: what a hostile "analysis" does downstream
# ─────────────────────────────────────────────────────────────────────────────

def test_hostile_analysis_passes_through_like_gemini_path(monkeypatch):
    """Parity check: neither the Claude nor the Gemini seam sanitizes before
    save_meal — both store the parsed dict raw and rely on read-time guards
    (utils.safe_number / safe_food_items). Pin the guards that make that safe."""
    hostile = ('{"is_food": true, "total_calories": -50000, "total_fat_g": Infinity, '
               '"food_items": "not a list", "meal_description": "<script>alert(1)</script>"}')
    _configure(monkeypatch)
    _fake_run(monkeypatch, stdout=_envelope(hostile))
    analysis = claude_analyzer.analyze_food_photo(b"img")

    assert analysis["is_food"] is True
    assert analysis["total_calories"] == -50000          # passed through untouched
    assert analysis["total_fat_g"] == float("inf")       # json.loads accepts Infinity
    assert analysis["food_items"] == "not a list"

    # Downstream read-time guards that make the pass-through survivable:
    assert utils.safe_food_items(analysis) == []          # renderers iterate nothing
    assert utils.safe_number(analysis["total_fat_g"]) == 0   # inf neutralized
    # safe_number itself stays sign-agnostic; the daily-total accumulators
    # (format_daily_totals / _daily_calorie_totals / _daily_average_meals)
    # clamp each per-meal contribution with max(0, ...) so a negative
    # hallucinated total can't subtract from the day.
    assert utils.safe_number(analysis["total_calories"]) == -50000


def test_string_is_food_is_coerced_or_rejected(monkeypatch):
    _configure(monkeypatch)
    _fake_run(monkeypatch, stdout=_envelope('{"is_food": "false"}'))
    analysis = claude_analyzer.analyze_food_photo(b"img")
    assert analysis is None or isinstance(analysis.get("is_food"), bool)


# ─────────────────────────────────────────────────────────────────────────────
# Subprocess lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def test_subprocess_gets_clamped_timeout_list_args_and_temp_cwd(monkeypatch, tmp_path):
    _configure(monkeypatch, dispatch="file")
    _redirect_tempdir(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_ANALYZER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CLAUDE_ANALYZER_MODEL", raising=False)
    captured = _fake_run_capture(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))

    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    (cmd, kwargs), = captured
    assert isinstance(cmd, list)                      # argv list — no shell string
    assert kwargs.get("shell") is not True            # and never shell=True
    assert kwargs["timeout"] == 120                   # default wall clock
    assert kwargs.get("text") is True
    assert kwargs["cwd"] == str(tmp_path)             # CLI runs beside the image


def test_timeout_env_lower_bound_and_float_strings(monkeypatch):
    monkeypatch.setenv("CLAUDE_ANALYZER_TIMEOUT_SECONDS", "0")
    assert claude_analyzer._timeout_seconds() == 10
    monkeypatch.setenv("CLAUDE_ANALYZER_TIMEOUT_SECONDS", "-5")
    assert claude_analyzer._timeout_seconds() == 10
    monkeypatch.setenv("CLAUDE_ANALYZER_TIMEOUT_SECONDS", "12.5")
    assert claude_analyzer._timeout_seconds() == 120  # int() rejects floats → default


def test_timeout_path_cleans_temp_file(monkeypatch, tmp_path):
    _configure(monkeypatch, dispatch="file")
    _redirect_tempdir(monkeypatch, tmp_path)
    _fake_run(monkeypatch, raise_timeout=True)
    assert claude_analyzer.analyze_food_photo(b"img") is None
    assert _leftover_temp_files(tmp_path) == []


def test_binary_vanishing_mid_flight_cleans_temp_file(monkeypatch, tmp_path):
    """which() said yes, exec said no — the analyzer must degrade, not crash."""
    _configure(monkeypatch, dispatch="file")
    _redirect_tempdir(monkeypatch, tmp_path)

    def run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(claude_analyzer.subprocess, "run", run)
    assert claude_analyzer.analyze_food_photo(b"img") is None
    assert _leftover_temp_files(tmp_path) == []


def test_cli_uninstalled_between_configured_check_and_run(monkeypatch):
    """is_configured() → True, then the CLI disappears before analyze runs."""
    monkeypatch.setenv("CLAUDE_ANALYZER_ENABLED", "1")
    answers = iter(["/usr/local/bin/claude"])
    monkeypatch.setattr(claude_analyzer.shutil, "which",
                        lambda *a, **k: next(answers, None))
    calls = _fake_run(monkeypatch)

    assert claude_analyzer.is_configured() is True     # present at the gate
    assert claude_analyzer.analyze_food_photo(b"img") is None  # gone at run time
    assert calls == []                                 # and no subprocess launched


def test_empty_image_bytes_short_circuit(monkeypatch, tmp_path):
    _configure(monkeypatch)
    _redirect_tempdir(monkeypatch, tmp_path)
    calls = _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    assert claude_analyzer.analyze_food_photo(b"") is None
    assert calls == []
    assert _leftover_temp_files(tmp_path) == []


# ─────────────────────────────────────────────────────────────────────────────
# Temp file lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def test_concurrent_analyses_never_stack_cli_runs(monkeypatch, tmp_path):
    """A burst (e.g. a Telegram album) must never stack two memory-heavy CLI
    subprocesses. The old mechanics QUEUED the second caller behind the full
    CLI run; the invariant it pinned — one CLI at a time, distinct temp
    files, nothing leaked — now holds via the contended-CLI bypass: while
    one run is in flight, a second caller returns None immediately (its
    photo falls through to Gemini) without launching a subprocess. Pinned on
    the file path, whose temp-file lifecycle is the risky part."""
    _configure(monkeypatch, dispatch="file")
    _redirect_tempdir(monkeypatch, tmp_path)
    seen = []
    first_run_started = threading.Event()
    release_first_run = threading.Event()

    def run(cmd, **kwargs):
        path = _prompt_image_path(cmd)
        assert claude_analyzer._CLI_LOCK.locked()
        assert os.path.exists(path)
        seen.append(path)
        first_run_started.set()
        # Hold the CLI "in flight" until the contended caller has finished.
        assert release_first_run.wait(timeout=30)
        return SimpleNamespace(returncode=0,
                               stdout=_envelope(json.dumps(GOOD_ANALYSIS)), stderr="")

    monkeypatch.setattr(claude_analyzer.subprocess, "run", run)
    first_result = {}
    worker = threading.Thread(
        target=lambda: first_result.update(result=claude_analyzer.analyze_food_photo(b"img"))
    )
    worker.start()
    try:
        assert first_run_started.wait(timeout=30)
        # Deterministically contended: the first run is provably in flight.
        assert claude_analyzer.analyze_food_photo(b"img2") is None
        assert len(seen) == 1                          # no second subprocess
    finally:
        release_first_run.set()
        worker.join(timeout=30)

    assert first_result["result"] == GOOD_ANALYSIS     # the holder is unaffected
    assert _leftover_temp_files(tmp_path) == []        # everything cleaned up


def test_temp_file_not_leaked_when_write_fails(monkeypatch, tmp_path):
    _configure(monkeypatch, dispatch="file")
    _redirect_tempdir(monkeypatch, tmp_path)
    real_ntf = tempfile.NamedTemporaryFile

    def failing_ntf(*args, **kwargs):
        inner = real_ntf(*args, **kwargs)

        class Proxy:
            name = inner.name

            def write(self, data):
                raise OSError(28, "No space left on device")

        class CM:
            def __enter__(self):
                return Proxy()

            def __exit__(self, *exc):
                inner.close()
                return False

        return CM()

    monkeypatch.setattr(claude_analyzer.tempfile, "NamedTemporaryFile", failing_ntf)
    calls = _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))

    assert claude_analyzer.analyze_food_photo(b"payload") is None  # degrades to Gemini
    assert calls == []                                 # CLI never launched
    assert _leftover_temp_files(tmp_path) == []        # ← leaks today (the bug)


# ─────────────────────────────────────────────────────────────────────────────
# Env safety
# ─────────────────────────────────────────────────────────────────────────────

def test_model_env_injection_stays_one_argv_element(monkeypatch):
    _configure(monkeypatch)
    hostile = "opus; rm -rf /; $(reboot) && `halt`"
    monkeypatch.setenv("CLAUDE_ANALYZER_MODEL", hostile)
    captured = _fake_run_capture(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))

    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    (cmd, kwargs), = captured
    assert cmd[cmd.index("--model") + 1] == hostile    # whole string, one element
    assert kwargs.get("shell") is not True             # list-args, never a shell


def test_whitespace_only_model_env_adds_no_flag(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("CLAUDE_ANALYZER_MODEL", "   ")
    captured = _fake_run_capture(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    (cmd, _), = captured
    assert "--model" not in cmd


def test_blank_bin_env_falls_back_to_claude(monkeypatch):
    monkeypatch.setenv("CLAUDE_ANALYZER_BIN", "")
    seen = []

    def which(name):
        seen.append(name)
        return "/usr/local/bin/claude"

    monkeypatch.setattr(claude_analyzer.shutil, "which", which)
    assert claude_analyzer._cli_path() == "/usr/local/bin/claude"
    assert seen == ["claude"]


def test_non_executable_bin_is_unconfigured(monkeypatch, tmp_path):
    """CLAUDE_ANALYZER_BIN at a real file without +x must disable the analyzer."""
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho hi\n")
    fake.chmod(0o644)
    monkeypatch.setenv("CLAUDE_ANALYZER_ENABLED", "1")
    monkeypatch.setenv("CLAUDE_ANALYZER_BIN", str(fake))
    assert claude_analyzer.is_configured() is False
    assert claude_analyzer.analyze_food_photo(b"img") is None


@pytest.mark.parametrize("value", ["maybe", "2", "enabled", " ", "-1", "tru"])
def test_undocumented_enabled_spellings_stay_disabled(monkeypatch, value):
    monkeypatch.setenv("CLAUDE_ANALYZER_ENABLED", value)
    monkeypatch.setattr(claude_analyzer.shutil, "which", lambda *a, **k: "/usr/local/bin/claude")
    assert claude_analyzer.is_configured() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on", "y"])
def test_documented_enabled_spellings_enable(monkeypatch, value):
    monkeypatch.setenv("CLAUDE_ANALYZER_ENABLED", value)
    monkeypatch.setattr(claude_analyzer.shutil, "which", lambda *a, **k: "/usr/local/bin/claude")
    assert claude_analyzer.is_configured() is True


# ─────────────────────────────────────────────────────────────────────────────
# Single-turn stream dispatch (default) and the stream→file fallback chain
# ─────────────────────────────────────────────────────────────────────────────

# The exact single-turn invocation verified live on the production VM
# (CLI 2.1.210): --input-format stream-json ERRORS unless the output format
# matches, --verbose is required for stream output with -p, and --tools ""
# forbids tool turns so the answer must land in turn one.
STREAM_BASE_CMD = [
    "/usr/local/bin/claude", "-p",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--verbose",
    "--tools", "",
]


def _fake_run_script(monkeypatch, responses):
    """Scripted subprocess.run: responses[i] answers call i as
    (returncode, stdout), or the string 'timeout'. Records (cmd, kwargs) and
    asserts _CLI_LOCK is held for EVERY call — one photo must equal one CLI
    occupancy even across the stream→file fallback chain."""
    records = []

    def run(cmd, **kwargs):
        assert claude_analyzer._CLI_LOCK.locked(), "CLI ran outside the lock"
        index = len(records)
        records.append((cmd, kwargs))
        assert index < len(responses), "unexpected extra subprocess call"
        response = responses[index]
        if response == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 120))
        returncode, stdout = response
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(claude_analyzer.subprocess, "run", run)
    return records


def _assert_file_cmd_shape(cmd):
    """The old two-turn shape: temp-file wrapper prompt + Read tool."""
    assert "--allowedTools" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == "Read"
    prompt = cmd[cmd.index("-p") + 1]
    assert "Read the image file at" in prompt and ".jpg" in prompt
    assert "--input-format" not in cmd


def test_stream_default_cmd_shape_exact(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.delenv("CLAUDE_ANALYZER_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_ANALYZER_EXTRA_FLAGS", raising=False)
    calls = _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))

    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    assert calls == [STREAM_BASE_CMD]          # pinned exactly, element for element


def test_stream_stdin_carries_prompt_and_image_base64(monkeypatch):
    _configure(monkeypatch)
    captured = _fake_run_capture(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    image = b"\xff\xd8\xffjpeg-bytes"

    assert claude_analyzer.analyze_food_photo(image) == GOOD_ANALYSIS

    (_, kwargs), = captured
    message = json.loads(kwargs["input"])      # exactly one JSON line on stdin
    assert message["type"] == "user"
    assert message["message"]["role"] == "user"
    text_block, image_block = message["message"]["content"]
    assert text_block["type"] == "text"
    assert text_block["text"].startswith("Analyze the attached image.")
    assert claude_analyzer.FOOD_DETECTION_PROMPT in text_block["text"]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(image_block["source"]["data"]) == image


def test_stream_path_creates_no_temp_file_and_no_cwd(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.delenv("CLAUDE_ANALYZER_TIMEOUT_SECONDS", raising=False)

    def no_temp(*args, **kwargs):
        raise AssertionError("stream dispatch must not create a temp file")

    monkeypatch.setattr(claude_analyzer.tempfile, "NamedTemporaryFile", no_temp)
    captured = _fake_run_capture(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))

    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    (_, kwargs), = captured
    assert "cwd" not in kwargs                 # no temp dir to sit beside
    assert kwargs["timeout"] == 120            # same clamped wall clock
    assert kwargs.get("text") is True
    assert kwargs["env"]["DISABLE_AUTOUPDATER"] == "1"   # same env knobs


def test_stream_result_line_parsed_amid_noise(monkeypatch, caplog):
    """The result envelope is the LAST {"type":"result"} JSONL line; every
    other line — non-JSON noise, init/assistant events, truncated JSON,
    blanks — is tolerated."""
    _configure(monkeypatch)
    stdout = "\n".join([
        "npm warn deprecated something",
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": []}}),
        '{"type": "assistant", "truncated": ',
        "",
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "num_turns": 1, "duration_ms": 8200, "duration_api_ms": 7300,
                    "result": json.dumps(GOOD_ANALYSIS)}),
    ])
    calls = _fake_run(monkeypatch, stdout=stdout)

    with caplog.at_level(logging.INFO, logger="claude_analyzer"):
        assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS

    assert len(calls) == 1
    lines = [r.message for r in caplog.records if "Photo analyzed by Claude" in r.message]
    assert len(lines) == 1 and "turns=1" in lines[0]


@pytest.mark.parametrize("first_response", [
    pytest.param((1, ""), id="nonzero-exit"),
    pytest.param((0, "noise only\n" + json.dumps({"type": "system"})), id="no-result-line"),
    pytest.param((0, _envelope("boom", is_error=True)), id="error-envelope"),
    pytest.param((0, json.dumps({"type": "result", "is_error": False, "result": ""})),
                 id="empty-result-text"),
])
def test_cli_shape_failure_falls_back_once_to_file_path(monkeypatch, tmp_path,
                                                        caplog, first_response):
    """A CLI-shape failure on the stream attempt (what an older CLI without
    stream-json support produces) retries ONCE via the two-turn Read-file
    path — on the SAME lock hold (asserted inside _fake_run_script)."""
    _configure(monkeypatch)
    _redirect_tempdir(monkeypatch, tmp_path)
    records = _fake_run_script(monkeypatch, [
        first_response,
        (0, _envelope(json.dumps(GOOD_ANALYSIS))),       # fallback succeeds
    ])

    with caplog.at_level(logging.WARNING, logger="claude_analyzer"):
        assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS

    assert len(records) == 2
    stream_cmd, stream_kwargs = records[0]
    assert "--input-format" in stream_cmd
    assert "input" in stream_kwargs                      # image went via stdin
    file_cmd, file_kwargs = records[1]
    _assert_file_cmd_shape(file_cmd)
    assert file_kwargs["cwd"] == str(tmp_path)           # CLI beside the image
    assert "retrying once" in caplog.text                # logged exactly the switch
    assert claude_analyzer._CLI_LOCK.locked() is False   # released on the way out
    assert _leftover_temp_files(tmp_path) == []


def test_fallback_failure_returns_none(monkeypatch, tmp_path):
    """Both attempts fail -> None (Gemini), exactly two CLI spawns, no leaks."""
    _configure(monkeypatch)
    _redirect_tempdir(monkeypatch, tmp_path)
    records = _fake_run_script(monkeypatch, [(1, ""), (1, "")])

    assert claude_analyzer.analyze_food_photo(b"img") is None
    assert len(records) == 2
    assert claude_analyzer._CLI_LOCK.locked() is False
    assert _leftover_temp_files(tmp_path) == []


@pytest.mark.parametrize("result_text", [
    pytest.param("total nonsense, no json anywhere", id="unparseable"),
    pytest.param(json.dumps({"note": "no is_food contract"}), id="missing-contract"),
])
def test_contract_failure_returns_none_without_fallback(monkeypatch, result_text):
    """The model ANSWERED (a turn was spent) but with junk JSON. That is an
    is_food-contract failure, not a CLI-shape failure: go straight to Gemini
    and never double-spend the subscription on a model that replied."""
    _configure(monkeypatch)
    records = _fake_run_script(monkeypatch, [(0, _envelope(result_text))])

    assert claude_analyzer.analyze_food_photo(b"img") is None
    assert len(records) == 1                   # no second CLI spawn


def test_stream_timeout_is_terminal_no_fallback(monkeypatch):
    """A timeout means the model was slow, not that the dispatch shape was
    wrong — a file-path retry would double the wall clock for one photo."""
    _configure(monkeypatch)
    records = _fake_run_script(monkeypatch, ["timeout"])

    assert claude_analyzer.analyze_food_photo(b"img") is None
    assert len(records) == 1
    assert claude_analyzer._CLI_LOCK.locked() is False


def test_dispatch_file_skips_stream_entirely(monkeypatch, tmp_path):
    """CLAUDE_ANALYZER_DISPATCH=file is the rollback knob: the old two-turn
    path runs directly, with no stream attempt spent first."""
    _configure(monkeypatch, dispatch="file")
    _redirect_tempdir(monkeypatch, tmp_path)
    records = _fake_run_script(monkeypatch, [(0, _envelope(json.dumps(GOOD_ANALYSIS)))])

    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    assert len(records) == 1
    file_cmd, file_kwargs = records[0]
    _assert_file_cmd_shape(file_cmd)
    assert "input" not in file_kwargs          # nothing rides stdin on this path
    assert _leftover_temp_files(tmp_path) == []


def test_unknown_dispatch_value_defaults_to_stream(monkeypatch):
    _configure(monkeypatch, dispatch="TURBO")
    calls = _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    assert "--input-format" in calls[0]


# ─────────────────────────────────────────────────────────────────────────────
# Real-subprocess integration (no mocked subprocess.run)
# ─────────────────────────────────────────────────────────────────────────────

def _install_fake_cli(tmp_path, monkeypatch, body):
    script = tmp_path / "fake_claude.sh"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("CLAUDE_ANALYZER_ENABLED", "1")
    monkeypatch.setenv("CLAUDE_ANALYZER_BIN", str(script))
    monkeypatch.delenv("CLAUDE_ANALYZER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CLAUDE_ANALYZER_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_ANALYZER_DISPATCH", raising=False)
    return script


def test_real_subprocess_end_to_end_success_and_injection_inert(monkeypatch, tmp_path):
    envelope_file = tmp_path / "envelope.json"
    envelope_text = _envelope(json.dumps(GOOD_ANALYSIS))
    assert "'" not in envelope_text
    envelope_file.write_text(envelope_text)
    args_file = tmp_path / "args.txt"
    pwned = tmp_path / "pwned"

    _install_fake_cli(
        tmp_path, monkeypatch,
        f'printf \'%s\\n\' "$@" > "{args_file}"\ncat "{envelope_file}"\n',
    )
    # This end-to-end pins the FILE dispatch (temp file + --allowedTools Read);
    # the stream sibling below covers the default stdin dispatch.
    monkeypatch.setenv("CLAUDE_ANALYZER_DISPATCH", "file")
    monkeypatch.setenv("CLAUDE_ANALYZER_MODEL", f"opus; touch {pwned}")
    _redirect_tempdir(monkeypatch, tmp_path)

    assert claude_analyzer.is_configured() is True
    assert claude_analyzer.analyze_food_photo(b"real-bytes") == GOOD_ANALYSIS

    args = args_file.read_text().splitlines()
    assert f"opus; touch {pwned}" in args      # hostile model arrived as ONE argv
    assert not pwned.exists()                  # and was never shell-interpreted
    assert "--allowedTools" in args
    assert "Read" in args
    assert _leftover_temp_files(tmp_path) == []


def test_real_subprocess_stream_end_to_end(monkeypatch, tmp_path):
    """Default dispatch against a REAL subprocess: the prompt and the image
    (as base64) arrive on the child's stdin in one stream-json user message,
    and no temp image file is ever created."""
    envelope_file = tmp_path / "envelope.json"
    envelope_file.write_text(_envelope(json.dumps(GOOD_ANALYSIS)))
    args_file = tmp_path / "args.txt"
    stdin_file = tmp_path / "stdin.json"

    _install_fake_cli(
        tmp_path, monkeypatch,
        f'printf \'%s\\n\' "$@" > "{args_file}"\n'
        f'cat > "{stdin_file}"\n'
        f'cat "{envelope_file}"\n',
    )
    _redirect_tempdir(monkeypatch, tmp_path)

    image = b"real-stream-bytes"
    assert claude_analyzer.analyze_food_photo(image) == GOOD_ANALYSIS

    args = args_file.read_text().splitlines()
    assert "--input-format" in args and "--output-format" in args
    assert args.count("stream-json") == 2      # both format flags
    assert "--verbose" in args                 # -p streams only with --verbose
    assert "--allowedTools" not in args        # no Read-tool turn

    message = json.loads(stdin_file.read_text())
    text_block, image_block = message["message"]["content"]
    assert "Analyze the attached image." in text_block["text"]
    assert base64.b64decode(image_block["source"]["data"]) == image
    assert _leftover_temp_files(tmp_path) == []


def test_real_subprocess_timeout_kills_hung_cli(monkeypatch, tmp_path):
    """A hung CLI must be killed AND reaped (no zombie) within the wall clock."""
    pid_file = tmp_path / "cli.pid"
    _install_fake_cli(tmp_path, monkeypatch, f'echo $$ > "{pid_file}"\nsleep 30\n')
    monkeypatch.setattr(claude_analyzer, "_timeout_seconds", lambda: 1)
    _redirect_tempdir(monkeypatch, tmp_path)

    start = time.monotonic()
    assert claude_analyzer.analyze_food_photo(b"img") is None
    elapsed = time.monotonic() - start
    assert elapsed < 8, f"analyzer blocked ~{elapsed:.1f}s; timeout did not fire promptly"

    assert pid_file.exists(), "fake CLI never started"
    pid = int(pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                        # dead and reaped — kill(0) on a zombie succeeds
    assert _leftover_temp_files(tmp_path) == []


# ─────────────────────────────────────────────────────────────────────────────
# Integration seam: telegram_bot.analyze_food_photo_with_retries (read-only)
# ─────────────────────────────────────────────────────────────────────────────

GEMINI_ANALYSIS = {"is_food": True, "meal_description": "Ramen", "total_calories": 550,
                   "total_protein_g": 25, "total_carbs_g": 70, "total_fat_g": 18,
                   "food_items": []}


def _patch_claude(monkeypatch, configured, result=None, must_not_run=False):
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: configured)
    if must_not_run:
        def analyze(image_bytes):
            raise AssertionError("claude_analyzer.analyze_food_photo must not run")
    else:
        def analyze(image_bytes):
            return result
    monkeypatch.setattr(claude_analyzer, "analyze_food_photo", analyze)


def _patch_gemini_once(monkeypatch, result=None):
    calls = []

    def once(client, image_bytes, *args, **kwargs):
        # Extra args are the normalized-bytes/prep-cache plumbing of the
        # single-decode pipeline; these tests only track WHETHER Gemini ran.
        calls.append(image_bytes)
        return result

    monkeypatch.setattr(telegram_bot, "_analyze_food_photo_once", once)
    return calls


def _health_path(monkeypatch, tmp_path):
    path = tmp_path / "service_health.json"
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", path)
    return path


def _activate_gemini_pause(path, hours=2):
    now = datetime.now()
    path.write_text(json.dumps({"gemini": {
        "quota_pause_until": (now + timedelta(hours=hours)).isoformat(timespec="seconds"),
        "quota_pause_set_at": now.isoformat(timespec="seconds"),
        "quota_pause_reason": "test pause",
    }}))


def test_claude_success_skips_gemini_and_records_no_gemini_health(monkeypatch, tmp_path):
    health = _health_path(monkeypatch, tmp_path)
    _patch_claude(monkeypatch, True, dict(GOOD_ANALYSIS))
    gemini_calls = _patch_gemini_once(monkeypatch, GEMINI_ANALYSIS)

    result = telegram_bot.analyze_food_photo_with_retries(object(), b"img")

    # Claude successes are tagged with an inert provenance marker.
    assert result == {**GOOD_ANALYSIS, "analyzed_by": "claude"}
    assert gemini_calls == []          # Claude success must skip Gemini entirely
    assert not health.exists()         # and must not claim a Gemini health success


def test_claude_failure_always_falls_back_to_gemini(monkeypatch, tmp_path):
    health = _health_path(monkeypatch, tmp_path)
    _patch_claude(monkeypatch, True, None)
    gemini_calls = _patch_gemini_once(monkeypatch, GEMINI_ANALYSIS)

    result = telegram_bot.analyze_food_photo_with_retries(object(), b"img")

    assert result == GEMINI_ANALYSIS
    assert len(gemini_calls) == 1
    events = json.loads(health.read_text())["gemini"]["events"]
    assert events[-1]["ok"] is True    # the success is credited to Gemini, not Claude


def test_claude_disabled_never_invoked(monkeypatch, tmp_path):
    _health_path(monkeypatch, tmp_path)
    _patch_claude(monkeypatch, False, must_not_run=True)
    gemini_calls = _patch_gemini_once(monkeypatch, GEMINI_ANALYSIS)

    assert telegram_bot.analyze_food_photo_with_retries(object(), b"img") == GEMINI_ANALYSIS
    assert len(gemini_calls) == 1


def test_gemini_quota_pause_does_not_block_claude_primary(monkeypatch, tmp_path):
    """A Gemini-only pause must not gate the Claude-first hop in with_retries."""
    health = _health_path(monkeypatch, tmp_path)
    _activate_gemini_pause(health)
    _patch_claude(monkeypatch, True, dict(GOOD_ANALYSIS))
    gemini_calls = _patch_gemini_once(monkeypatch, GEMINI_ANALYSIS)

    result = telegram_bot.analyze_food_photo_with_retries(object(), b"img")

    assert result == {**GOOD_ANALYSIS, "analyzed_by": "claude"}
    assert gemini_calls == []


def test_pause_still_gates_gemini_after_claude_failure(monkeypatch, tmp_path):
    health = _health_path(monkeypatch, tmp_path)
    _activate_gemini_pause(health)
    _patch_claude(monkeypatch, True, None)
    gemini_calls = _patch_gemini_once(monkeypatch, GEMINI_ANALYSIS)

    assert telegram_bot.analyze_food_photo_with_retries(object(), b"img") is None
    assert gemini_calls == []          # paused Gemini is never spent


def test_retry_failed_upload_uses_claude_during_gemini_pause(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "meals.db")
    database.init_db()
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    health = _health_path(monkeypatch, tmp_path)
    _activate_gemini_pause(health)
    _patch_claude(monkeypatch, True, dict(GOOD_ANALYSIS))
    gemini_calls = _patch_gemini_once(monkeypatch, GEMINI_ANALYSIS)

    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    photo = failed_dir / "20260101T000000_abcdef123456.jpg"
    photo.write_bytes(b"failed-upload-bytes")

    result = telegram_bot._retry_failed_upload_path(object(), photo)

    assert result["status"] == "logged", (
        f"expected the Claude-first retry to log the meal, got {result['status']!r}"
    )
    assert gemini_calls == []
    assert not photo.exists()          # logged uploads are discarded


# ── Observability helpers: is_enabled / status_label ─────────────────

def test_is_enabled_tracks_only_the_knob(monkeypatch):
    monkeypatch.delenv("CLAUDE_ANALYZER_ENABLED", raising=False)
    assert claude_analyzer.is_enabled() is False

    monkeypatch.setenv("CLAUDE_ANALYZER_ENABLED", "1")
    monkeypatch.setattr(claude_analyzer.shutil, "which", lambda *a, **k: None)
    # Knob on, CLI absent: enabled but not configured.
    assert claude_analyzer.is_enabled() is True
    assert claude_analyzer.is_configured() is False


@pytest.mark.parametrize("enabled,cli,expected", [
    (None, "/usr/local/bin/claude", "off"),
    ("0", "/usr/local/bin/claude", "off"),
    ("1", None, "enabled, CLI missing"),
    ("1", "/usr/local/bin/claude", "enabled"),
])
def test_status_label_states(monkeypatch, enabled, cli, expected):
    if enabled is None:
        monkeypatch.delenv("CLAUDE_ANALYZER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_ANALYZER_ENABLED", enabled)
    monkeypatch.setattr(claude_analyzer.shutil, "which", lambda *a, **k: cli)

    assert claude_analyzer.status_label() == expected


def test_cli_runs_hold_the_serialization_lock(monkeypatch):
    """The Node CLI is memory-heavy; a burst of photo threads (e.g. a
    Telegram album) must serialize on _CLI_LOCK around the subprocess."""
    _configure(monkeypatch)
    locked_during_run = []

    def run(cmd, **kwargs):
        locked_during_run.append(claude_analyzer._CLI_LOCK.locked())
        return SimpleNamespace(returncode=0, stdout=_envelope(json.dumps(dict(GOOD_ANALYSIS))),
                               stderr="")

    monkeypatch.setattr(claude_analyzer.subprocess, "run", run)

    assert claude_analyzer.analyze_food_photo(b"jpegbytes") is not None
    assert locked_during_run == [True]
    assert claude_analyzer._CLI_LOCK.locked() is False  # released on the way out


def test_cli_lock_released_after_timeout(monkeypatch):
    """A TimeoutExpired inside the lock must not leave it held — queued
    photo threads would deadlock forever."""
    _configure(monkeypatch)
    _fake_run(monkeypatch, raise_timeout=True)

    assert claude_analyzer.analyze_food_photo(b"jpegbytes") is None
    assert claude_analyzer._CLI_LOCK.locked() is False


def test_contended_cli_lock_bypasses_to_gemini_without_subprocess(monkeypatch, tmp_path, caplog):
    """Lock held by another photo -> immediate None (caller falls back to
    Gemini), no subprocess launch, no temp file, and the holder's lock is
    left exactly as it was."""
    _configure(monkeypatch)
    _redirect_tempdir(monkeypatch, tmp_path)
    calls = _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))

    assert claude_analyzer._CLI_LOCK.acquire(blocking=False)
    try:
        with caplog.at_level(logging.INFO, logger="claude_analyzer"):
            assert claude_analyzer.analyze_food_photo(b"img") is None
    finally:
        claude_analyzer._CLI_LOCK.release()

    assert calls == []                                  # no subprocess
    assert _leftover_temp_files(tmp_path) == []         # not even a temp file
    assert "Claude CLI busy — falling back to Gemini" in caplog.text


def test_free_cli_lock_runs_normally_and_releases(monkeypatch):
    """Lock free -> the normal single run happens and the lock is returned."""
    _configure(monkeypatch)
    calls = _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))

    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    assert len(calls) == 1
    assert claude_analyzer._CLI_LOCK.locked() is False


# ─────────────────────────────────────────────────────────────────────────────
# CLI instrumentation + safe env (duration logging, env knobs, extra flags)
# ─────────────────────────────────────────────────────────────────────────────

def test_subprocess_env_inherits_and_adds_safety_knobs(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("CT_TEST_MARKER_ENV", "carried-through")
    captured = _fake_run_capture(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))

    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    (_, kwargs), = captured
    env = kwargs["env"]
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    # The current process env rides along (the CLI needs PATH, HOME, token…).
    assert env["CT_TEST_MARKER_ENV"] == "carried-through"


def test_extra_flags_env_appended_shlex_split(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv(
        "CLAUDE_ANALYZER_EXTRA_FLAGS",
        '--strict-mcp-config --no-session-persistence --note "two words"',
    )
    calls = _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))

    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    cmd = calls[0]
    assert cmd[-4:] == ["--strict-mcp-config", "--no-session-persistence",
                        "--note", "two words"]          # shlex: quoted arg stays one element


def test_extra_flags_absent_or_malformed_add_nothing(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.delenv("CLAUDE_ANALYZER_EXTRA_FLAGS", raising=False)
    monkeypatch.delenv("CLAUDE_ANALYZER_MODEL", raising=False)
    calls = _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    assert calls[0][-2:] == ["--tools", ""]   # stream base argv unchanged

    # Unbalanced quote: degrade to no extra flags, never fail the photo.
    monkeypatch.setenv("CLAUDE_ANALYZER_EXTRA_FLAGS", '--broken "unclosed')
    assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    assert calls[1][-2:] == ["--tools", ""]


def test_success_log_includes_wall_and_envelope_durations(monkeypatch, caplog):
    _configure(monkeypatch)
    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 4200, "duration_api_ms": 3100, "num_turns": 1,
        "result": json.dumps(GOOD_ANALYSIS),
    })
    _fake_run(monkeypatch, stdout=envelope)

    with caplog.at_level(logging.INFO, logger="claude_analyzer"):
        assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS

    lines = [r.message for r in caplog.records if "Photo analyzed by Claude" in r.message]
    assert len(lines) == 1
    assert "(subscription) in " in lines[0]
    assert "cli=4200ms api=3100ms" in lines[0]
    # num_turns is the proof of which dispatch answered (1 = single-turn).
    assert "turns=1" in lines[0]


def test_success_log_tolerates_missing_envelope_durations(monkeypatch, caplog):
    """Older CLIs may omit the duration fields; the success line must still land."""
    _configure(monkeypatch)
    _fake_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    with caplog.at_level(logging.INFO, logger="claude_analyzer"):
        assert claude_analyzer.analyze_food_photo(b"img") == GOOD_ANALYSIS
    lines = [r.message for r in caplog.records if "Photo analyzed by Claude" in r.message]
    assert len(lines) == 1
    assert "cli=Nonems api=Nonems" in lines[0]
    assert "turns=None" in lines[0]


def test_timeout_bounds_lock_hold_even_with_pipe_holding_grandchild(monkeypatch, tmp_path):
    """Pins the _CLI_LOCK boundedness claim against a REAL subprocess.

    The comment on _CLI_LOCK promises 'the subprocess timeout bounds how long
    any holder keeps the lock'. The Claude CLI is a Node process that can
    spawn children; if such a grandchild inherits the stdout pipe and
    outlives the kill, some subprocess APIs block until pipe EOF — a forever
    lock hold. On POSIX, subprocess.run() kills the direct child and then
    only wait()s (it does NOT drain the pipes again), so the hold really is
    bounded. This test fails loudly if that load-bearing behavior ever
    changes (e.g. a refactor to Popen+communicate)."""
    pidfile = tmp_path / "grandchild.pid"
    cli = tmp_path / "fake_claude"
    cli.write_text(
        "#!/bin/sh\n"
        "sleep 30 &\n"                      # grandchild inherits the stdout pipe
        'echo $! > "%s"\n' % pidfile +
        # exec: the shell BECOMES this sleep, so the timeout kill reaches it
        # and only the deliberate background grandchild outlives the kill.
        "exec sleep 30\n"
    )
    cli.chmod(0o755)
    monkeypatch.setenv("CLAUDE_ANALYZER_ENABLED", "1")
    monkeypatch.setenv("CLAUDE_ANALYZER_BIN", str(cli))
    monkeypatch.delenv("CLAUDE_ANALYZER_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_ANALYZER_DISPATCH", raising=False)
    monkeypatch.setattr(claude_analyzer, "_timeout_seconds", lambda: 1)

    result_box = {}

    def run_analysis():
        result_box["result"] = claude_analyzer.analyze_food_photo(b"\xff\xd8\xff jpeg")

    worker = threading.Thread(target=run_analysis)
    start = time.monotonic()
    worker.start()
    worker.join(timeout=8)
    hung = worker.is_alive()
    try:
        assert not hung, (
            "analyze_food_photo still running 8s after a 1s subprocess timeout — "
            "the _CLI_LOCK hold is NOT bounded by the timeout"
        )
        assert result_box["result"] is None
        assert claude_analyzer._CLI_LOCK.locked() is False
        assert time.monotonic() - start < 8
    finally:
        # Reap the deliberate orphan so nothing outlives the test; in the
        # hung case this also closes the pipe and unblocks the worker.
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text().strip()), 9)
            except (ValueError, OSError):
                pass
        worker.join(timeout=35)


# ─── Subscription backends: glm / doubao ride the SAME CLI ──────────────

def _configure_backend(monkeypatch, backend, key="sk-plan"):
    _configure(monkeypatch)
    # Hermetic: ambient developer env must not leak flags into the argv
    # the assertions below inspect.
    monkeypatch.delenv("CLAUDE_ANALYZER_EXTRA_FLAGS", raising=False)
    monkeypatch.delenv("CLAUDE_ANALYZER_MODEL", raising=False)
    monkeypatch.setenv(
        {"glm": "GLM_PLAN_KEY", "doubao": "DOUBAO_PLAN_KEY"}[backend], key)
    # Prove the scrub: these must NOT reach a vendor-plan child process.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "anthropic-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-api-secret")


def _capture_run(monkeypatch, stdout="", returncode=0, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env"),
                      "input": kwargs.get("input")})
        return SimpleNamespace(returncode=returncode, stdout=stdout,
                               stderr=stderr)

    monkeypatch.setattr(claude_analyzer.subprocess, "run", run)
    return calls


def test_doubao_photo_streams_with_plan_env_and_never_file_falls_back(
        monkeypatch):
    _configure_backend(monkeypatch, "doubao")
    # Nonzero exit — for claude this would arm the file fallback; for
    # doubao the fallback must NOT run (image support there is already the
    # experiment; a Read-enabled retry adds exfiltration on top).
    calls = _capture_run(monkeypatch, returncode=1)
    out = claude_analyzer.analyze_food_photo(
        b"jpeg", allow_file_fallback=True, backend="doubao")
    assert out is None
    assert len(calls) == 1
    env = calls[0]["env"]
    assert env["ANTHROPIC_BASE_URL"] == \
        "https://ark.cn-beijing.volces.com/api/plan"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-plan"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    # Stream shape: the image rides stdin, not a temp file.
    assert "--input-format" in calls[0]["cmd"]


def test_glm_photo_uses_the_official_vision_mcp(monkeypatch):
    import stat
    _configure_backend(monkeypatch, "glm")
    monkeypatch.setenv("CLAUDE_ANALYZER_MODEL", "claude-opus-5")
    captured = {}

    def run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        # The config temp file exists only DURING the run; read it here.
        cfg = cmd[cmd.index("--mcp-config") + 1]
        with open(cfg) as f:
            captured["cfg"] = f.read()
        captured["cfg_mode"] = stat.S_IMODE(os.stat(cfg).st_mode)
        return SimpleNamespace(
            returncode=0,
            stdout=_envelope(json.dumps(GOOD_ANALYSIS), turns=2),
            stderr="")

    monkeypatch.setattr(claude_analyzer.subprocess, "run", run)
    out = claude_analyzer.analyze_food_photo(b"jpeg", backend="glm")
    assert out == GOOD_ANALYSIS
    cmd = captured["cmd"]
    # The key must NEVER be an argv element (argv is world-readable via
    # ps//proc for the whole run) — it lives in a 0600 temp file.
    assert all("sk-plan" not in str(a) for a in cmd)
    mcp = json.loads(captured["cfg"])
    zai = mcp["mcpServers"]["zai"]
    assert zai["args"] == ["-y", "@z_ai/mcp-server"]
    assert zai["env"]["Z_AI_API_KEY"] == "sk-plan"
    assert zai["env"]["Z_AI_MODE"] == "ZHIPU"
    assert captured["cfg_mode"] == 0o600
    cfg_path = cmd[cmd.index("--mcp-config") + 1]
    assert not os.path.exists(cfg_path), "config temp file must be cleaned"
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__zai"
    flag_values = {cmd[i]: cmd[i + 1]
                   for i in range(len(cmd) - 1) if cmd[i].startswith("--")}
    assert "Read" not in flag_values.get("--allowedTools", ""), \
        "Read must never be enabled on this path"
    assert "--tools" not in cmd, "the no-tools text flag would kill the MCP"
    # --allowedTools is an AUTO-APPROVE list, not a disable list: the
    # built-ins stay reachable to a prompt carrying caller text (the app's
    # dietary profile) unless they are named forbidden (review 2026-07-31).
    denied = flag_values.get("--disallowedTools", "")
    for tool in ("Read", "Glob", "Grep", "LS", "Bash", "Write"):
        assert tool in denied, f"{tool} must be explicitly disallowed"
    assert "--strict-mcp-config" in cmd
    # And the run's cwd must NOT be the directory holding the plan key.
    assert captured["cwd"] != os.path.dirname(cfg_path), \
        "the working directory must not contain the mcp-config file"
    # Anthropic model ids must not be pushed at a vendor plan.
    assert "--model" not in cmd
    env = captured["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://open.bigmodel.cn/api/anthropic"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_glm_photo_without_npx_spends_nothing(monkeypatch):
    _configure_backend(monkeypatch, "glm")
    cli = "/usr/local/bin/claude"
    monkeypatch.setattr(
        claude_analyzer.shutil, "which",
        lambda name=None, *a, **k: None if name == "npx" else cli)
    calls = _capture_run(monkeypatch)
    assert claude_analyzer.analyze_food_photo(b"jpeg", backend="glm") is None
    assert calls == []


def test_vendor_backend_without_key_spends_nothing(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.delenv("GLM_PLAN_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_PLAN_KEY", raising=False)
    calls = _capture_run(monkeypatch)
    assert claude_analyzer.analyze_food_photo(b"jpeg", backend="glm") is None
    assert claude_analyzer.analyze_text_prompt("hi", backend="doubao") is None
    assert calls == []


def test_unknown_backend_is_refused_outright(monkeypatch):
    _configure(monkeypatch)
    calls = _capture_run(monkeypatch)
    assert claude_analyzer.analyze_food_photo(b"jpeg", backend="gpt") is None
    assert claude_analyzer.analyze_text_prompt("hi", backend="") is None
    assert calls == []


def test_text_intent_on_glm_uses_plan_env_but_plain_text_turn(monkeypatch):
    _configure_backend(monkeypatch, "glm")
    monkeypatch.setenv("CLAUDE_ANALYZER_MODEL", "claude-opus-5")
    calls = _capture_run(
        monkeypatch, stdout=_envelope(json.dumps({"intent": "x"})))
    out = claude_analyzer.analyze_text_prompt("fix meal 2", backend="glm")
    assert out == {"result": {"intent": "x"}}
    cmd = calls[0]["cmd"]
    assert "--mcp-config" not in cmd, "text turns need no vision MCP"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--model" not in cmd
    assert calls[0]["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-plan"


def test_claude_backend_still_gets_the_model_override(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("CLAUDE_ANALYZER_MODEL", "claude-opus-5")
    calls = _capture_run(
        monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    assert claude_analyzer.analyze_food_photo(b"jpeg") == GOOD_ANALYSIS
    assert "--model" in calls[0]["cmd"]


def test_backend_status_names_the_missing_knob(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.delenv("GLM_PLAN_KEY", raising=False)
    monkeypatch.setenv("DOUBAO_PLAN_KEY", "k")
    status = claude_analyzer.backend_status()
    assert status["claude"] == "enabled"
    assert "GLM_PLAN_KEY" in status["glm"]
    assert status["doubao"] == "ready"


def test_doubao_never_file_falls_back_even_under_dispatch_file(monkeypatch):
    # CLAUDE_ANALYZER_DISPATCH=file is the claude-backend rollback knob; a
    # doubao run must ignore it — the Read-file path is both the untested
    # image experiment AND the exfiltration surface.
    _configure_backend(monkeypatch, "doubao")
    monkeypatch.setenv("CLAUDE_ANALYZER_DISPATCH", "file")
    calls = _capture_run(monkeypatch, returncode=1)
    assert claude_analyzer.analyze_food_photo(
        b"jpeg", backend="doubao") is None
    assert len(calls) == 1
    assert "--input-format" in calls[0]["cmd"], "stream shape, never file"
    assert "--allowedTools" not in calls[0]["cmd"]


def test_backend_paths_respect_the_single_flight_lock(monkeypatch):
    _configure_backend(monkeypatch, "glm")
    monkeypatch.setenv("DOUBAO_PLAN_KEY", "sk-plan")
    calls = _capture_run(monkeypatch)
    assert claude_analyzer._CLI_LOCK.acquire(blocking=False)
    try:
        # A contended CLI must decline instantly on EVERY backend — the
        # vendor paths spawn the same memory-heavy Node process.
        assert claude_analyzer.analyze_food_photo(
            b"jpeg", backend="glm") is None
        assert claude_analyzer.analyze_food_photo(
            b"jpeg", backend="doubao") is None
        assert claude_analyzer.analyze_text_prompt(
            "hi", backend="glm") is None
        assert calls == []
    finally:
        claude_analyzer._CLI_LOCK.release()


def test_plan_key_never_reaches_log_output(monkeypatch, caplog):
    # The GLM key is embedded in the --mcp-config argv JSON; a failing run
    # logs stderr/stdout snippets — none of OUR log lines may carry argv.
    _configure_backend(monkeypatch, "glm", key="sk-SECRET-PLAN")
    calls = _capture_run(monkeypatch, returncode=1)
    with caplog.at_level(logging.DEBUG, logger="claude_analyzer"):
        assert claude_analyzer.analyze_food_photo(
            b"jpeg", backend="glm") is None
    assert calls, "the CLI must actually have been invoked"
    assert "sk-SECRET-PLAN" not in caplog.text


def test_glm_blind_run_without_tool_roundtrip_is_discarded(monkeypatch):
    # If the zai MCP server dies after launch (npm registry unreachable),
    # the CLI answers in ONE turn about an image the model never saw — and
    # the prompt's "unclear → not food" rule makes that a contract-valid
    # WRONG verdict. num_turns >= 2 is the proof a tool round-trip ran.
    _configure_backend(monkeypatch, "glm")
    _capture_run(monkeypatch,
                 stdout=_envelope(json.dumps({"is_food": False}), turns=1))
    assert claude_analyzer.analyze_food_photo(b"jpeg", backend="glm") is None
    _capture_run(monkeypatch, stdout=_envelope(json.dumps(GOOD_ANALYSIS)))
    assert claude_analyzer.analyze_food_photo(b"jpeg", backend="glm") is None
    _capture_run(monkeypatch,
                 stdout=_envelope(json.dumps(GOOD_ANALYSIS), turns=3))
    assert claude_analyzer.analyze_food_photo(
        b"jpeg", backend="glm") == GOOD_ANALYSIS


def test_cli_echoed_plan_key_is_redacted_from_logs(monkeypatch, caplog):
    # An older CLI that rejects --mcp-config as a file path echoes the
    # VALUE into stderr — which _log_cli_exit snippets into the server
    # log. The key must be scrubbed before the snippet is cut.
    _configure_backend(monkeypatch, "glm", key="sk-SECRET-PLAN")
    _capture_run(
        monkeypatch, returncode=1,
        stderr=("error: MCP config file not found: "
                "{\"mcpServers\":{\"zai\":{\"env\":{\"Z_AI_API_KEY\":"
                "\"sk-SECRET-PLAN\"}}}}"))
    with caplog.at_level(logging.WARNING, logger="claude_analyzer"):
        assert claude_analyzer.analyze_food_photo(
            b"jpeg", backend="glm") is None
    assert "sk-SECRET-PLAN" not in caplog.text
    assert "redacted-plan-key" in caplog.text
