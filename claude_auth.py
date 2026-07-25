"""Phone-initiated OAuth re-connect for the server's Claude subscription.

The ONLY sanctioned way to authenticate a Claude subscription is Claude
Code's own OAuth Authorization-Code+PKCE flow (`claude setup-token`): the
user signs in on Anthropic's official page and the minted token authorizes
Claude Code — the genuine registered client — on THIS machine. These
helpers let the phone drive that flow's two human steps remotely:

    start_session()      spawn `claude setup-token` under a PTY, harvest
                         the official authorize URL from its output
    complete_session()   paste the user's code into the waiting process,
                         harvest the minted token

The PKCE verifier lives inside the setup-token process between those two
steps, which is why the process must stay alive and why there is exactly
ONE session at a time (spawning a second would orphan the first's
verifier). The token is applied to os.environ (the CLI child inherits it
on the next analysis — no service restart) and persisted to .env for the
next boot. It is never logged and never returned to the caller.
"""

import fcntl
import logging
import os
import pty
import re
import select
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger("claude_auth")

SESSION_TTL_SECONDS = 600  # the authorize page's code is short-lived anyway
_COMPLETE_TIMEOUT_SECONDS = 60

_AUTHORIZE_URL_RE = re.compile(r"https://[^\s\x1b]*oauth[^\s\x1b]*", re.I)
_TOKEN_RE = re.compile(r"sk-ant-oat[0-9A-Za-z_\-]+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

_lock = threading.Lock()
_session: Optional[Dict] = None  # {proc, fd, url, started_at}


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _read_available(fd: int, timeout: float) -> str:
    """Read whatever the PTY has within [timeout] seconds (non-blocking)."""
    chunks = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.05, deadline - time.monotonic())
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            continue
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data.decode("utf-8", "replace"))
    return "".join(chunks)


def _kill_session(sess: Dict) -> None:
    try:
        os.close(sess["fd"])
    except OSError:
        pass
    proc = sess["proc"]
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _expired(sess: Dict) -> bool:
    return time.monotonic() - sess["started_at"] > SESSION_TTL_SECONDS


def cancel_session() -> bool:
    """Kill any in-flight session; True if one existed."""
    global _session
    with _lock:
        sess, _session = _session, None
    if sess is None:
        return False
    _kill_session(sess)
    return True


def session_active() -> bool:
    with _lock:
        return _session is not None and not _expired(_session)


def start_session(cli_path: str, env: Dict[str, str]) -> Dict:
    """Spawn `claude setup-token` and return {'url': ...} or {'error': ...}.

    Replaces any prior session — its PKCE verifier dies with its process,
    so a stale session's code can never be completed (by design: one
    in-flight consent at a time keeps the attack surface at one code).
    """
    cancel_session()
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            [cli_path, "setup-token"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
            start_new_session=True,  # its own group: SIGTERM cleans up node
        )
    except OSError as e:
        os.close(master)
        os.close(slave)
        return {"error": f"could not launch the CLI: {e}"}
    os.close(slave)
    # Non-blocking master so a wedged CLI cannot hang the Flask worker.
    fcntl.fcntl(master, fcntl.F_SETFL,
                fcntl.fcntl(master, fcntl.F_GETFL) | os.O_NONBLOCK)

    output = ""
    deadline = time.monotonic() + 30
    url = None
    while time.monotonic() < deadline:
        output += _read_available(master, 1.0)
        m = _AUTHORIZE_URL_RE.search(_strip_ansi(output))
        if m:
            url = m.group(0).rstrip(").,")
            break
        if proc.poll() is not None:
            break
    if url is None:
        _kill_session({"proc": proc, "fd": master})
        # Output may embed escape sequences but never secrets at this stage.
        log.warning("setup-token produced no authorize URL (exit=%s)",
                    proc.poll())
        return {"error": "the CLI did not produce a sign-in URL"}

    global _session
    with _lock:
        _session = {
            "proc": proc,
            "fd": master,
            "url": url,
            "started_at": time.monotonic(),
        }
    log.info("Claude re-auth session started (URL harvested).")
    return {"url": url}


def complete_session(code: str, apply_token) -> Dict:
    """Paste [code] into the waiting CLI; on success call apply_token(tok).

    Returns {'ok': True} or {'error': ...}. The token itself never leaves
    this module except through [apply_token].
    """
    global _session
    with _lock:
        sess = _session
        _session = None  # one attempt per session: codes are one-shot
    if sess is None:
        return {"error": "no sign-in in progress — start again"}
    if _expired(sess):
        _kill_session(sess)
        return {"error": "the sign-in expired — start again"}

    code = (code or "").strip()
    if not code or len(code) > 512 or any(c.isspace() for c in code):
        _kill_session(sess)
        return {"error": "that does not look like a valid code"}

    try:
        os.write(sess["fd"], (code + "\n").encode())
    except OSError as e:
        _kill_session(sess)
        return {"error": f"could not hand the code to the CLI: {e}"}

    output = ""
    deadline = time.monotonic() + _COMPLETE_TIMEOUT_SECONDS
    token = None
    while time.monotonic() < deadline:
        output += _read_available(sess["fd"], 1.0)
        m = _TOKEN_RE.search(_strip_ansi(output))
        if m:
            token = m.group(0)
            break
        if sess["proc"].poll() is not None and not token:
            # Process ended; one last drain, then give up.
            output += _read_available(sess["fd"], 0.5)
            m = _TOKEN_RE.search(_strip_ansi(output))
            token = m.group(0) if m else None
            break
    _kill_session(sess)
    if token is None:
        log.warning("setup-token completion produced no token "
                    "(likely a wrong/expired code).")
        return {"error": "sign-in failed — the code was not accepted"}
    apply_token(token)
    log.info("Claude subscription token refreshed via phone re-auth.")
    return {"ok": True}


def apply_token_to_env(token: str, env_path: Optional[Path] = None) -> None:
    """Make [token] effective NOW (os.environ → next CLI child inherits)
    and persist it to .env for the next boot. Never logs the value."""
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
    path = env_path or Path(__file__).resolve().parent / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines(True) \
            if path.exists() else []
        key = "CLAUDE_CODE_OAUTH_TOKEN="
        replaced = False
        for i, line in enumerate(lines):
            if line.strip().startswith(key):
                lines[i] = f"{key}{token}\n"
                replaced = True
                break
        if not replaced:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{key}{token}\n")
        path.write_text("".join(lines), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as e:
        # The in-memory token still works until restart; say so honestly.
        log.error("Token active for this run but .env update failed: %s", e)
