"""
Claude-first food photo analysis via the Claude Code CLI.

Runs the analysis on the user's Claude subscription (headless `claude -p`
with a CLAUDE_CODE_OAUTH_TOKEN minted by `claude setup-token`) instead of a
metered API key. This module is a best-effort PRIMARY: any failure — CLI
absent, token invalid, usage-window exhausted, timeout, malformed output —
returns None and the caller falls back to Gemini, so the bot never degrades
below today's behavior.

Configuration (env / .env):
  CLAUDE_ANALYZER_ENABLED           truthy to enable (default: off)
  CLAUDE_ANALYZER_BIN               CLI binary (default: "claude")
  CLAUDE_ANALYZER_MODEL             optional --model override
  CLAUDE_ANALYZER_TIMEOUT_SECONDS   per-analysis wall clock (default: 120)
  CLAUDE_ANALYZER_EXTRA_FLAGS       optional extra CLI flags (shlex-split)
  CLAUDE_CODE_OAUTH_TOKEN           subscription token (read by the CLI itself)
"""

import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from config import FOOD_DETECTION_PROMPT
from utils import parse_ai_json, parse_boolish

log = logging.getLogger("claude_analyzer")

# The CLI is a full Node process (hundreds of MB RSS). Single-flight: only
# one run at a time, and a contended caller does NOT queue — it returns None
# immediately so the photo falls through to Gemini instead of waiting out
# another photo's full CLI run (measured ~42-63s cards on album bursts). The
# subprocess timeout bounds how long the one holder keeps the lock.
_CLI_LOCK = threading.Lock()


def _timeout_seconds() -> int:
    try:
        value = int(os.environ.get("CLAUDE_ANALYZER_TIMEOUT_SECONDS", "120"))
    except ValueError:
        return 120
    return min(max(value, 10), 600)


def _cli_path() -> Optional[str]:
    return shutil.which(os.environ.get("CLAUDE_ANALYZER_BIN", "claude") or "claude")


def is_enabled() -> bool:
    """The CLAUDE_ANALYZER_ENABLED knob alone, ignoring CLI presence."""
    return parse_boolish(os.environ.get("CLAUDE_ANALYZER_ENABLED")) is True


def is_configured() -> bool:
    """Enabled by knob AND the CLI is actually installed.

    The OAuth token is deliberately not required here: the CLI may hold its
    own stored login, and a missing/expired token simply fails the run —
    which the caller already treats as "fall back to Gemini".
    """
    return is_enabled() and _cli_path() is not None


def status_label() -> str:
    """One-phrase analyzer state for /config and /doctor."""
    if not is_enabled():
        return "off"
    if _cli_path() is None:
        return "enabled, CLI missing"
    return "enabled"


def _build_prompt(image_path: str) -> str:
    return (
        f"Read the image file at {image_path} and analyze it.\n\n"
        f"{FOOD_DETECTION_PROMPT}"
    )


def _extra_flags() -> list:
    """CLAUDE_ANALYZER_EXTRA_FLAGS, shlex-split into argv elements.

    A malformed value (unbalanced quote) degrades to no extra flags — the
    analyzer must keep working rather than fail every photo on a typo.
    """
    raw = (os.environ.get("CLAUDE_ANALYZER_EXTRA_FLAGS") or "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError as e:
        log.warning(f"Ignoring malformed CLAUDE_ANALYZER_EXTRA_FLAGS: {e}")
        return []


def analyze_food_photo(image_bytes: bytes) -> Optional[Dict]:
    """Analyze a food photo via the Claude Code CLI; None means 'use Gemini'."""
    if not is_configured():
        return None
    cli = _cli_path()
    if cli is None or not image_bytes:
        return None

    # Contended-CLI bypass: another photo already owns the (up to 120s) CLI
    # run. Don't queue behind it — Gemini answers this photo in seconds.
    if not _CLI_LOCK.acquire(blocking=False):
        log.info("Claude CLI busy — falling back to Gemini for this photo.")
        return None

    tmp_path = None
    start = time.time()
    try:
        with tempfile.NamedTemporaryFile(
            prefix="ct_claude_", suffix=".jpg", delete=False
        ) as tmp:
            # Record the path before writing so a failed write (e.g. ENOSPC)
            # still gets the file cleaned up by the finally block.
            tmp_path = tmp.name
            tmp.write(image_bytes)

        cmd = [
            cli, "-p", _build_prompt(tmp_path),
            "--output-format", "json",
            "--allowedTools", "Read",
        ]
        model = (os.environ.get("CLAUDE_ANALYZER_MODEL") or "").strip()
        if model:
            cmd += ["--model", model]
        cmd += _extra_flags()

        # The CLI phones home for updates/telemetry on every run; both knobs
        # shave that off (and are harmless no-ops on CLIs that predate them).
        env = dict(os.environ)
        env["DISABLE_AUTOUPDATER"] = "1"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_timeout_seconds(),
            cwd=str(Path(tmp_path).parent),
            env=env,
        )
        if proc.returncode != 0:
            snippet = (proc.stderr or proc.stdout or "").strip()[:300]
            if "limit" in snippet.lower():
                log.warning(f"Claude CLI usage window likely exhausted: {snippet}")
            else:
                log.warning(f"Claude CLI exited {proc.returncode}: {snippet}")
            return None

        # `claude -p --output-format json` wraps the reply in a result envelope.
        envelope = json.loads(proc.stdout)
        if not isinstance(envelope, dict) or envelope.get("is_error"):
            log.warning("Claude CLI returned an error envelope.")
            return None
        result_text = envelope.get("result")
        if not isinstance(result_text, str) or not result_text.strip():
            log.warning("Claude CLI envelope had no result text.")
            return None

        analysis = parse_ai_json(result_text)
        if not isinstance(analysis, dict) or "is_food" not in analysis:
            log.warning("Claude analysis JSON missing the is_food contract.")
            return None
        # The CLI has no JSON mode, so is_food may arrive as a quoted
        # "false" — truthy downstream. Coerce to a real bool or reject.
        if not isinstance(analysis["is_food"], bool):
            coerced = parse_boolish(analysis["is_food"])
            if coerced is None:
                log.warning("Claude analysis is_food was not boolean-like.")
                return None
            analysis["is_food"] = coerced
        # Envelope timings split CLI overhead from API time — the evidence
        # trail for tuning startup flags without touching the model choice.
        wall = time.time() - start
        duration_ms = envelope.get("duration_ms")
        duration_api_ms = envelope.get("duration_api_ms")
        log.info(
            f"✅ Photo analyzed by Claude (subscription) in {wall:.1f}s "
            f"(cli={duration_ms}ms api={duration_api_ms}ms)."
        )
        return analysis
    except subprocess.TimeoutExpired:
        log.warning(f"Claude CLI timed out after {_timeout_seconds()}s.")
        return None
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"Could not parse Claude CLI output: {e}")
        return None
    except Exception as e:
        log.warning(f"Claude analyzer failed unexpectedly: {type(e).__name__}: {e}")
        return None
    finally:
        _CLI_LOCK.release()
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
