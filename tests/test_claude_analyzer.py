import sys
import os
import json
import subprocess
from types import SimpleNamespace

import pytest

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import claude_analyzer


GOOD_ANALYSIS = {"is_food": True, "meal_description": "Latte", "total_calories": 150,
                 "total_protein_g": 8, "total_carbs_g": 12, "total_fat_g": 8,
                 "food_items": []}


def _envelope(result_text, is_error=False):
    return json.dumps({"type": "result", "subtype": "success",
                       "is_error": is_error, "result": result_text})


def _configure(monkeypatch, enabled="1", cli="/usr/local/bin/claude"):
    monkeypatch.setenv("CLAUDE_ANALYZER_ENABLED", enabled)
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
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "/usr/local/bin/claude"
    assert "-p" in cmd and "--output-format" in cmd
    # The prompt must point Claude at the temp image file.
    prompt = cmd[cmd.index("-p") + 1]
    assert "Read the image file at" in prompt and ".jpg" in prompt


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
    _configure(monkeypatch)
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
