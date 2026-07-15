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
  CLAUDE_CODE_OAUTH_TOKEN           subscription token (read by the CLI itself)
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional

from config import FOOD_DETECTION_PROMPT
from utils import parse_ai_json, parse_boolish

log = logging.getLogger("claude_analyzer")


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


def analyze_food_photo(image_bytes: bytes) -> Optional[Dict]:
    """Analyze a food photo via the Claude Code CLI; None means 'use Gemini'."""
    if not is_configured():
        return None
    cli = _cli_path()
    if cli is None or not image_bytes:
        return None

    tmp_path = None
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

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_timeout_seconds(),
            cwd=str(Path(tmp_path).parent),
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
        log.info("✅ Photo analyzed by Claude (subscription).")
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
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
