"""Phone-driven OAuth re-connect (claude_auth + its endpoints).

The PTY session machinery is exercised against a SCRIPTED fake CLI (a tiny
python script that mimics setup-token's dialogue), so URL harvesting, code
paste, token extraction, one-shot semantics and expiry run for real. The
contract that matters:

  * the token never appears in any HTTP response or log line;
  * one session at a time, one completion attempt per session;
  * .env update is atomic-ish, chmod 600, and effective via os.environ.
"""
import os
import stat
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import claude_analyzer
import claude_auth
import database
import telegram_bot


def make_fake_cli(tmp_path, behavior="ok"):
    """A stand-in for `claude setup-token` speaking the same dialogue."""
    script = tmp_path / "fake_setup_token.py"
    script.write_text(textwrap.dedent(f"""
        import sys, time
        behavior = {behavior!r}
        if behavior == "no_url":
            print("something went wrong before any url")
            sys.exit(1)
        print("Visit: https://claude.ai/oauth/authorize?code=true&x=1")
        sys.stdout.flush()
        line = sys.stdin.readline().strip()
        if behavior == "bad_code" or line != "good-code#state":
            print("Invalid code.")
            sys.exit(1)
        print("Success! Your token: sk-ant-oat01-FAKE_TOKEN_abc123")
    """))
    wrapper = tmp_path / "fakecli"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    wrapper.chmod(0o755)
    return str(wrapper)


@pytest.fixture(autouse=True)
def clean_sessions():
    claude_auth.cancel_session()
    yield
    claude_auth.cancel_session()


def test_full_flow_start_complete_applies_token(tmp_path):
    cli = make_fake_cli(tmp_path)
    started = claude_auth.start_session(cli, dict(os.environ))
    assert started.get("url", "").startswith(
        "https://claude.ai/oauth/authorize")
    applied = []
    done = claude_auth.complete_session("good-code#state", applied.append)
    assert done == {"ok": True}
    assert applied == ["sk-ant-oat01-FAKE_TOKEN_abc123"]


def test_wrong_code_fails_without_applying(tmp_path):
    cli = make_fake_cli(tmp_path)
    claude_auth.start_session(cli, dict(os.environ))
    applied = []
    done = claude_auth.complete_session("wrong", applied.append)
    assert "error" in done
    assert applied == []


def test_sessions_are_one_shot_and_singular(tmp_path):
    cli = make_fake_cli(tmp_path)
    claude_auth.start_session(cli, dict(os.environ))
    # A second start replaces the first (its verifier died with it).
    claude_auth.start_session(cli, dict(os.environ))
    done = claude_auth.complete_session("good-code#state", lambda t: None)
    assert done == {"ok": True}
    # The consumed session cannot be completed twice.
    again = claude_auth.complete_session("good-code#state", lambda t: None)
    assert "error" in again


def test_no_url_from_cli_is_a_clean_error(tmp_path):
    cli = make_fake_cli(tmp_path, behavior="no_url")
    started = claude_auth.start_session(cli, dict(os.environ))
    assert "error" in started
    assert not claude_auth.session_active()


def test_expired_session_refuses_completion(tmp_path, monkeypatch):
    cli = make_fake_cli(tmp_path)
    claude_auth.start_session(cli, dict(os.environ))
    monkeypatch.setattr(claude_auth, "SESSION_TTL_SECONDS", -1)
    done = claude_auth.complete_session("good-code#state", lambda t: None)
    assert "error" in done and "expired" in done["error"]


def test_whitespace_or_giant_codes_are_rejected_before_the_pty(tmp_path):
    cli = make_fake_cli(tmp_path)
    for bad in ["has space", "x" * 600, "", "  "]:
        claude_auth.start_session(cli, dict(os.environ))
        done = claude_auth.complete_session(bad, lambda t: None)
        assert "error" in done, repr(bad)


def test_apply_token_updates_env_file_and_process(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nCLAUDE_CODE_OAUTH_TOKEN=old-token\nMORE=2\n")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    claude_auth.apply_token_to_env("sk-ant-oat01-NEW", env_path=env)
    text = env.read_text()
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-NEW" in text
    assert "old-token" not in text
    assert "OTHER=1" in text and "MORE=2" in text  # neighbours untouched
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-NEW"
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600


def test_apply_token_appends_when_key_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OTHER=1")  # no trailing newline on purpose
    claude_auth.apply_token_to_env("sk-ant-oat01-NEW", env_path=env)
    assert env.read_text().endswith("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-NEW\n")


# ── endpoint layer ────────────────────────────────────────────────


class FakeBot:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))
        return {"ok": True}

    def _redact(self, e):
        return str(e)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "auth.db")
    database.init_db()
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "secret-key")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR",
                        tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR",
                        tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH",
                        tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "maybe_warn_android_vpn_inactive",
                        lambda *a, **kw: None)
    app = telegram_bot._build_api_app(FakeBot(), object())
    return app.test_client()


def test_start_requires_auth_and_cli(client, monkeypatch):
    assert client.post("/api/claude_auth/start").status_code == 401
    monkeypatch.setattr(claude_analyzer, "_cli_path", lambda: None)
    resp = client.post("/api/claude_auth/start",
                       headers={"X-API-Key": "secret-key"})
    assert resp.status_code == 503


def test_start_and_complete_via_http_never_leak_the_token(
        client, monkeypatch, tmp_path):
    cli = make_fake_cli(tmp_path)
    monkeypatch.setattr(claude_analyzer, "_cli_path", lambda: cli)
    applied = []
    monkeypatch.setattr(claude_auth, "apply_token_to_env",
                        lambda tok: applied.append(tok))
    h = {"X-API-Key": "secret-key"}
    started = client.post("/api/claude_auth/start", headers=h)
    assert started.status_code == 200
    body = started.get_json()
    assert body["url"].startswith("https://claude.ai/oauth/authorize")

    done = client.post("/api/claude_auth/complete", headers=h,
                       json={"code": "good-code#state"})
    assert done.status_code == 200
    payload = done.get_json()
    assert payload["ok"] is True
    assert applied == ["sk-ant-oat01-FAKE_TOKEN_abc123"]
    # The token must never appear in ANY response body.
    assert "sk-ant-oat" not in started.get_data(as_text=True)
    assert "sk-ant-oat" not in done.get_data(as_text=True)


def test_complete_without_start_is_a_clean_502(client):
    resp = client.post("/api/claude_auth/complete",
                       headers={"X-API-Key": "secret-key"},
                       json={"code": "whatever"})
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "complete_failed"


def test_cancel_reports_whether_a_session_existed(
        client, monkeypatch, tmp_path):
    h = {"X-API-Key": "secret-key"}
    assert client.post("/api/claude_auth/cancel",
                       headers=h).get_json()["cancelled"] is False
    cli = make_fake_cli(tmp_path)
    monkeypatch.setattr(claude_analyzer, "_cli_path", lambda: cli)
    client.post("/api/claude_auth/start", headers=h)
    assert client.post("/api/claude_auth/cancel",
                       headers=h).get_json()["cancelled"] is True
