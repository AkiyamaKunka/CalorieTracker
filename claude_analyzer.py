"""
Claude-first food photo analysis via the Claude Code CLI.

Runs the analysis on the user's Claude subscription (headless `claude -p`
with a CLAUDE_CODE_OAUTH_TOKEN minted by `claude setup-token`) instead of a
metered API key. This module is a best-effort PRIMARY: any failure — CLI
absent, token invalid, usage-window exhausted, timeout, malformed output —
returns None and the caller falls back to Gemini, so the bot never degrades
below today's behavior.

Dispatch modes (CLAUDE_ANALYZER_DISPATCH):
  stream (default)  Single model turn: the image travels on stdin as a
                    base64 content block via `--input-format stream-json`,
                    so no temp file and no Read-tool round trip. Measured on
                    the production VM: ~8.2-8.4s wall vs 16.0s for the
                    two-turn path, num_turns=1, same-quality output. If the
                    attempt fails for CLI-shape reasons (older CLIs without
                    stream-json support exit nonzero here), the analyzer
                    logs once and retries ONCE via the file path below —
                    within the same _CLI_LOCK hold.
  file              Two model turns: the image is written to a temp file and
                    the model Reads it back. Forces the old path only —
                    a rollback knob, never combined with a stream attempt.

Configuration (env / .env):
  CLAUDE_ANALYZER_ENABLED           truthy to enable (default: off)
  CLAUDE_ANALYZER_BIN               CLI binary (default: "claude")
  CLAUDE_ANALYZER_MODEL             optional --model override
  CLAUDE_ANALYZER_TIMEOUT_SECONDS   per-analysis wall clock (default: 120)
  CLAUDE_ANALYZER_EXTRA_FLAGS       optional extra CLI flags (shlex-split)
  CLAUDE_ANALYZER_DISPATCH          'stream' (default) or 'file' (see above)
  CLAUDE_CODE_OAUTH_TOKEN           subscription token (read by the CLI itself)
"""

import base64
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
from typing import Dict, Optional, Tuple

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


def _dispatch_mode() -> str:
    """'file' forces the two-turn temp-file path (the rollback knob);
    anything else — unset, 'stream', or a typo — takes the stream default."""
    raw = (os.environ.get("CLAUDE_ANALYZER_DISPATCH") or "").strip().lower()
    return "file" if raw == "file" else "stream"


def _build_prompt(image_path: str, prompt: Optional[str] = None) -> str:
    return (
        f"Read the image file at {image_path} and analyze it.\n\n"
        f"{prompt or FOOD_DETECTION_PROMPT}"
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


def _append_model_and_extra_flags(cmd: list) -> None:
    """Shared argv tail: optional --model override, then extra flags last."""
    model = (os.environ.get("CLAUDE_ANALYZER_MODEL") or "").strip()
    if model:
        cmd += ["--model", model]
    cmd += _extra_flags()


def _cli_env() -> Dict[str, str]:
    # The CLI phones home for updates/telemetry on every run; both knobs
    # shave that off (and are harmless no-ops on CLIs that predate them).
    env = dict(os.environ)
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def _stream_stdin(image_bytes: bytes, prompt: Optional[str] = None) -> str:
    """One stream-json user message: the analysis prompt plus the image as a
    base64 content block. Callers pass already-normalized JPEG bytes (the
    single-decode photo pipeline), hence the fixed image/jpeg media type."""
    message = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Analyze the attached image.\n\n"
                            f"{prompt or FOOD_DETECTION_PROMPT}",
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    },
                },
            ],
        },
    }
    return json.dumps(message) + "\n"


def _parse_stream_result(stdout: str) -> Optional[Dict]:
    """The LAST {"type": "result"} line of the stream-json JSONL output —
    the same envelope shape as `--output-format json` (is_error, result,
    duration_ms, duration_api_ms, num_turns). Non-JSON lines and result-less
    events (init/assistant messages, stray warnings) are tolerated noise."""
    envelope = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and candidate.get("type") == "result":
            envelope = candidate
    return envelope


def _result_text(envelope) -> Optional[str]:
    """The result string of a non-error envelope; None for any other shape."""
    if not isinstance(envelope, dict) or envelope.get("is_error"):
        return None
    text = envelope.get("result")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


def _log_cli_exit(proc) -> None:
    snippet = (proc.stderr or proc.stdout or "").strip()[:300]
    if "limit" in snippet.lower():
        log.warning(f"Claude CLI usage window likely exhausted: {snippet}")
    else:
        log.warning(f"Claude CLI exited {proc.returncode}: {snippet}")


def _finish_analysis(envelope: Dict, result_text: str, start: float) -> Optional[Dict]:
    """is_food-contract validation + success instrumentation, shared by both
    dispatch paths. None here means the MODEL answered junk — callers must
    treat it as terminal and never retry-spend on a model that answered."""
    try:
        analysis = parse_ai_json(result_text)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"Could not parse Claude analysis JSON: {e}")
        return None
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
    # Envelope timings split CLI overhead from API time, and num_turns
    # proves which dispatch actually answered (1 = single-turn stream) —
    # the evidence trail for tuning without touching the model choice.
    wall = time.time() - start
    log.info(
        f"✅ Photo analyzed by Claude (subscription) in {wall:.1f}s "
        f"(cli={envelope.get('duration_ms')}ms "
        f"api={envelope.get('duration_api_ms')}ms "
        f"turns={envelope.get('num_turns')})."
    )
    return analysis


def _attempt_stream(
    cli: str, image_bytes: bytes, env: Dict[str, str], start: float,
    prompt: Optional[str] = None,
) -> Tuple[Optional[Dict], bool]:
    """Single-turn dispatch: image on stdin, no temp file, no tool turns.

    Returns (analysis, retry_via_file). retry_via_file is True only for
    CLI-shape failures — nonzero exit, no result line, unusable envelope —
    the cases an older CLI without stream-json support produces. A timeout
    or a model that answered junk is terminal: retrying would double-spend
    the wall clock / subscription for the same photo.
    """
    cmd = [
        cli, "-p",
        # stdin carries the image; --input-format stream-json ERRORS unless
        # the output format matches, and -p only streams with --verbose.
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--tools", "",  # no tools: the answer must land in turn one
    ]
    _append_model_and_extra_flags(cmd)
    try:
        proc = subprocess.run(
            cmd,
            input=_stream_stdin(image_bytes, prompt),
            capture_output=True,
            text=True,
            timeout=_timeout_seconds(),
            env=env,
        )
        if proc.returncode != 0:
            _log_cli_exit(proc)
            return None, True
        envelope = _parse_stream_result(proc.stdout)
        if envelope is None:
            log.warning("Claude CLI stream output had no result line.")
            return None, True
        text = _result_text(envelope)
        if text is None:
            log.warning("Claude CLI stream result envelope was unusable.")
            return None, True
        return _finish_analysis(envelope, text, start), False
    except subprocess.TimeoutExpired:
        log.warning(f"Claude CLI timed out after {_timeout_seconds()}s.")
        return None, False
    except Exception as e:
        log.warning(f"Claude analyzer failed unexpectedly: {type(e).__name__}: {e}")
        return None, False


def _attempt_file(
    cli: str, image_bytes: bytes, env: Dict[str, str], start: float,
    prompt: Optional[str] = None,
) -> Optional[Dict]:
    """Two-turn dispatch: temp image file + a Read-tool prompt. The
    compatibility fallback for CLIs without stream-json support, and the
    forced path under CLAUDE_ANALYZER_DISPATCH=file."""
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
            cli, "-p", _build_prompt(tmp_path, prompt),
            "--output-format", "json",
            "--allowedTools", "Read",
        ]
        _append_model_and_extra_flags(cmd)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_timeout_seconds(),
            cwd=str(Path(tmp_path).parent),
            env=env,
        )
        if proc.returncode != 0:
            _log_cli_exit(proc)
            return None

        # `claude -p --output-format json` wraps the reply in a result envelope.
        envelope = json.loads(proc.stdout)
        text = _result_text(envelope)
        if text is None:
            log.warning("Claude CLI returned an unusable result envelope.")
            return None
        return _finish_analysis(envelope, text, start)
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


def analyze_text_prompt(prompt: str) -> Optional[Dict]:
    """Run an arbitrary JSON-answering prompt through the CLI (subscription).

    Used by the app-facing /api/text_intent endpoint so the phone's NL
    corrections cost no API money either. Same discipline as the photo path:
    single turn, no tools, serialized by _CLI_LOCK, and a contended CLI
    returns None (caller decides — the app falls back to its own provider)
    rather than queueing behind a 120 s photo run.
    """
    if not is_configured():
        return None
    cli = _cli_path()
    if cli is None or not (prompt or "").strip():
        return None
    if not _CLI_LOCK.acquire(blocking=False):
        log.info("Claude CLI busy — text intent declined.")
        return None
    start = time.time()
    try:
        cmd = [cli, "-p", "--output-format", "json", "--tools", ""]
        _append_model_and_extra_flags(cmd)
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_timeout_seconds(),
            env=_cli_env(),
        )
        if proc.returncode != 0:
            _log_cli_exit(proc)
            return None
        try:
            envelope = json.loads(proc.stdout or "{}")
        except ValueError:
            log.warning("Claude CLI text output was not JSON.")
            return None
        text = _result_text(envelope)
        if text is None:
            log.warning("Claude CLI text envelope was unusable.")
            return None
        parsed = parse_ai_json(text)
        if not isinstance(parsed, (dict, list)):
            log.warning("Claude text reply was not a JSON object/array.")
            return None
        log.info(
            "Claude text intent ok in %.1fs (turns=%s)",
            time.time() - start,
            envelope.get("num_turns"),
        )
        return {"result": parsed}
    except subprocess.TimeoutExpired:
        log.warning(f"Claude CLI text intent timed out after {_timeout_seconds()}s.")
        return None
    except Exception as e:
        log.warning(f"Claude text intent failed: {type(e).__name__}: {e}")
        return None
    finally:
        _CLI_LOCK.release()


def analyze_food_photo(
    image_bytes: bytes, prompt: Optional[str] = None
) -> Optional[Dict]:
    """Analyze a food photo via the Claude Code CLI; None means 'use Gemini'.

    [prompt] overrides the built-in FOOD_DETECTION_PROMPT — the mobile app
    sends its own copy (same shared/ source) WITH the user's dietary-profile
    appendix, which the server has no other way to know about.
    """
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

    # One photo = one CLI occupancy: the lock is held across BOTH attempts
    # of the stream→file fallback chain, so a burst can never stack the
    # memory-heavy CLI even mid-fallback.
    start = time.time()
    try:
        env = _cli_env()
        if _dispatch_mode() == "stream":
            analysis, retry_via_file = _attempt_stream(
                cli, image_bytes, env, start, prompt)
            if not retry_via_file:
                return analysis
            log.warning(
                "Claude stream-json dispatch failed — retrying once via the "
                "two-turn Read-file path."
            )
        return _attempt_file(cli, image_bytes, env, start, prompt)
    finally:
        _CLI_LOCK.release()
