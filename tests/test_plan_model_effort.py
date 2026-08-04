"""Per-request model/effort choice for the Claude plan (2026-08-05).

Closed whitelists, claude-only: these values become CLI argv, so the
endpoints must 400 anything unlisted BEFORE a CLI run exists, and the
vendor plans (which map model names server-side) must reject overrides.
"""
import base64
import json

import claude_analyzer
import telegram_bot

from test_api_analyze_endpoints import client  # noqa: F401

ANALYSIS = {"is_food": True, "total_calories": 450}


def _photo_payload(**over):
    p = {"image_b64": base64.b64encode(b"jpegbytes").decode()}
    p.update(over)
    return p


def _stub(monkeypatch):
    captured = {}

    def fake(b, p=None, allow_file_fallback=True, backend="claude",
             raise_on_busy=False, model=None, effort=None):
        captured.update(model=model, effort=effort, backend=backend)
        return dict(ANALYSIS)

    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(claude_analyzer, "analyze_food_photo", fake)
    return captured


def test_valid_model_and_effort_reach_the_analyzer(client, monkeypatch):
    captured = _stub(monkeypatch)
    resp = client.http.post("/api/analyze_photo",
                            headers={"X-API-Key": "secret-key"},
                            json=_photo_payload(model="haiku", effort="low"))
    assert resp.status_code == 200
    assert captured["model"] == "haiku"
    assert captured["effort"] == "low"


def test_absent_choice_means_server_default(client, monkeypatch):
    captured = _stub(monkeypatch)
    resp = client.http.post("/api/analyze_photo",
                            headers={"X-API-Key": "secret-key"},
                            json=_photo_payload())
    assert resp.status_code == 200
    assert captured["model"] is None and captured["effort"] is None


def test_unlisted_values_are_400_not_argv(client, monkeypatch):
    captured = _stub(monkeypatch)
    for bad in ({"model": "opus-4.5-preview"}, {"model": "--tools"},
                {"effort": "max"}, {"effort": "9000"}):
        resp = client.http.post("/api/analyze_photo",
                                headers={"X-API-Key": "secret-key"},
                                json=_photo_payload(**bad))
        assert resp.status_code == 400, bad
    assert not captured, "no CLI call may happen for a rejected choice"


def test_vendor_plans_reject_overrides(client, monkeypatch):
    captured = _stub(monkeypatch)
    resp = client.http.post("/api/analyze_photo",
                            headers={"X-API-Key": "secret-key"},
                            json=_photo_payload(backend="glm", model="opus"))
    assert resp.status_code == 400
    assert not captured


def test_argv_carries_the_choice_only_for_claude():
    cmd = ["claude", "-p"]
    claude_analyzer._append_model_and_extra_flags(
        cmd, "claude", model="haiku", effort="high")
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert cmd[cmd.index("--effort") + 1] == "high"
    cmd2 = ["claude", "-p"]
    claude_analyzer._append_model_and_extra_flags(
        cmd2, "glm", model="haiku", effort="high")
    assert "--model" not in cmd2 and "--effort" not in cmd2


def test_argv_never_takes_unlisted_values_even_if_called_directly():
    # Defense in depth: even if endpoint validation regressed, the argv
    # builder itself refuses off-whitelist values.
    cmd = ["claude", "-p"]
    claude_analyzer._append_model_and_extra_flags(
        cmd, "claude", model="--dangerously-skip-permissions", effort="rm")
    assert "--dangerously-skip-permissions" not in cmd
    assert "rm" not in cmd
