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
  CLAUDE_ANALYZER_MODEL             optional --model override (claude backend)
  CLAUDE_ANALYZER_TIMEOUT_SECONDS   per-analysis wall clock (default: 120)
  CLAUDE_ANALYZER_EXTRA_FLAGS       optional extra CLI flags (shlex-split)
  CLAUDE_ANALYZER_DISPATCH          'stream' (default) or 'file' (see above)
  CLAUDE_CODE_OAUTH_TOKEN           subscription token (read by the CLI itself)
  GLM_PLAN_KEY                      Zhipu GLM Coding Plan key → 'glm' backend
  DOUBAO_PLAN_KEY                   Volcengine Agent Plan key → 'doubao' backend

Subscription backends (the [backend] parameter):
  The SAME Claude Code CLI can run against two other vendors' subscription
  plans — both officially document Claude Code as a supported tool, so this
  is their sanctioned use, not impersonation (facts verified 2026-07-30):
    glm     Zhipu GLM Coding Plan: ANTHROPIC_BASE_URL=
            https://open.bigmodel.cn/api/anthropic + plan key as the auth
            token. That endpoint is TEXT-ONLY, so photos go through Zhipu's
            OFFICIAL vision MCP server (@z_ai/mcp-server, GLM-4.6V) reading
            a local temp file — needs npx on this machine.
    doubao  Volcengine Ark Agent Plan: ANTHROPIC_BASE_URL=
            https://ark.cn-beijing.volces.com/api/plan + plan key. Direct
            API calls with a plan key are a documented ban risk — the plan
            is valid ONLY through whitelisted tools, and running the real
            Claude Code CLI is exactly that. Image input over this endpoint
            is undocumented: the stream dispatch sends the photo as a
            base64 block and a rejection surfaces as an ordinary failed run
            (→ None → the caller's fallback), never a crash.
  Both backends scrub CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY from the
  child env so a vendor run can never accidentally spend the Anthropic
  subscription or an API key.
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

class AnalyzerBusy(Exception):
    """Another photo owns the single-flight CLI right now.

    Distinct from a None return, which means the run HAPPENED and failed
    (timeout, junk reply, blind GLM run) — those are terminal for this
    photo, and the app must not spend three more 120 s CLI runs on them.
    The endpoints map busy → retryable 503, terminal → non-retryable.
    """


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


# ─── Subscription backends ────────────────────────────────────────────
SUBSCRIPTION_BACKENDS = ("claude", "glm", "doubao")

_BACKEND_BASE_URLS = {
    "glm": "https://open.bigmodel.cn/api/anthropic",
    "doubao": "https://ark.cn-beijing.volces.com/api/plan",
}
_BACKEND_KEY_ENVS = {"glm": "GLM_PLAN_KEY", "doubao": "DOUBAO_PLAN_KEY"}
_BACKEND_LABELS = {
    "claude": "Claude (subscription)",
    "glm": "GLM (coding plan)",
    "doubao": "Doubao (agent plan)",
}


def _backend_key(backend: str) -> Optional[str]:
    env_name = _BACKEND_KEY_ENVS.get(backend)
    if not env_name:
        return None
    key = (os.environ.get(env_name) or "").strip()
    return key or None


def _npx_path() -> Optional[str]:
    return shutil.which("npx")


def backend_available(backend: str, for_photo: bool = False) -> bool:
    """Can [backend] serve a request right now? 'claude' rides the existing
    knobs; the vendor plans need their key — and, for GLM photos only, npx
    (the official vision MCP server is an npm package)."""
    if not is_configured():
        return False
    if backend == "claude":
        return True
    if backend not in _BACKEND_KEY_ENVS:
        return False
    if _backend_key(backend) is None:
        return False
    if backend == "glm" and for_photo and _npx_path() is None:
        return False
    return True


def backend_status() -> Dict[str, str]:
    """Per-backend one-phrase state for /api/auth_check — the phone's
    'Test connection' shows these so a missing server-side plan key is
    diagnosed from the couch, not from ssh."""
    status: Dict[str, str] = {"claude": status_label()}
    for backend in ("glm", "doubao"):
        if not is_enabled():
            status[backend] = "off"
        elif _cli_path() is None:
            status[backend] = "enabled, CLI missing"
        elif _backend_key(backend) is None:
            status[backend] = f"no key ({_BACKEND_KEY_ENVS[backend]})"
        elif backend == "glm" and _npx_path() is None:
            status[backend] = "key set, npx missing (vision MCP needs node)"
        else:
            status[backend] = "ready"
    return status


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


def _append_model_and_extra_flags(cmd: list, backend: str = "claude") -> None:
    """Shared argv tail: optional --model override, then extra flags last.

    The --model override is CLAUDE-ONLY: the vendor plans map Anthropic
    model names to their own models server-side (GLM: sonnet→glm-5.2;
    Doubao: per plan tier), and an Anthropic model id pushed at them is at
    best ignored and at worst a 400 on every photo.
    """
    if backend == "claude":
        model = (os.environ.get("CLAUDE_ANALYZER_MODEL") or "").strip()
        if model:
            cmd += ["--model", model]
    cmd += _extra_flags()


def _cli_env(backend: str = "claude") -> Dict[str, str]:
    # The CLI phones home for updates/telemetry on every run; both knobs
    # shave that off (and are harmless no-ops on CLIs that predate them).
    env = dict(os.environ)
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    if backend in _BACKEND_BASE_URLS:
        env["ANTHROPIC_BASE_URL"] = _BACKEND_BASE_URLS[backend]
        env["ANTHROPIC_AUTH_TOKEN"] = _backend_key(backend) or ""
        # Scrub the Anthropic credentials: a vendor-plan run must never be
        # able to fall through to (and spend) the Claude subscription or an
        # API key — and the CLI prefers some of these over AUTH_TOKEN.
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        env.pop("ANTHROPIC_API_KEY", None)
        # Zhipu's own Claude Code setup doc prescribes a long API timeout;
        # harmless for Ark. Never override an operator's explicit value.
        env.setdefault("API_TIMEOUT_MS", "3000000")
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
    text = (proc.stderr or proc.stdout or "").strip()
    # Redact BEFORE truncating: a CLI that echoes a bad argument back into
    # stderr (e.g. "MCP config file not found: {...Z_AI_API_KEY...}") must
    # not put a plan key into the server log — and truncating first could
    # leave a recognizable key fragment straddling the cut.
    for backend in _BACKEND_KEY_ENVS:
        key = _backend_key(backend)
        if key:
            text = text.replace(key, "[redacted-plan-key]")
    snippet = text[:300]
    if "limit" in snippet.lower():
        log.warning(f"Claude CLI usage window likely exhausted: {snippet}")
    else:
        log.warning(f"Claude CLI exited {proc.returncode}: {snippet}")


def _finish_analysis(
    envelope: Dict, result_text: str, start: float,
    backend: str = "claude",
) -> Optional[Dict]:
    """is_food-contract validation + success instrumentation, shared by all
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
    label = _BACKEND_LABELS.get(backend, backend)
    log.info(
        f"✅ Photo analyzed by {label} in {wall:.1f}s "
        f"(cli={envelope.get('duration_ms')}ms "
        f"api={envelope.get('duration_api_ms')}ms "
        f"turns={envelope.get('num_turns')})."
    )
    return analysis


def _attempt_stream(
    cli: str, image_bytes: bytes, env: Dict[str, str], start: float,
    prompt: Optional[str] = None, backend: str = "claude",
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
    _append_model_and_extra_flags(cmd, backend)
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
        return _finish_analysis(envelope, text, start, backend), False
    except subprocess.TimeoutExpired:
        log.warning(f"Claude CLI timed out after {_timeout_seconds()}s.")
        return None, False
    except Exception as e:
        log.warning(f"Claude analyzer failed unexpectedly: {type(e).__name__}: {e}")
        return None, False


def _zai_mcp_config(key: str) -> str:
    """Inline --mcp-config JSON for Zhipu's OFFICIAL vision MCP server
    (docs.bigmodel.cn coding-plan vision-mcp doc): the coding-plan key IS
    the intended credential, Z_AI_MODE=ZHIPU selects the mainland platform."""
    return json.dumps({
        "mcpServers": {
            "zai": {
                "command": "npx",
                "args": ["-y", "@z_ai/mcp-server"],
                "env": {"Z_AI_API_KEY": key, "Z_AI_MODE": "ZHIPU"},
            }
        }
    })


def _attempt_glm_vision(
    cli: str, image_bytes: bytes, env: Dict[str, str], start: float,
    prompt: Optional[str] = None,
) -> Optional[Dict]:
    """GLM-backend photo dispatch: temp file + the official zai vision MCP.

    The GLM coding-plan Anthropic endpoint is TEXT-ONLY — an image content
    block never reaches a vision model there. Zhipu's sanctioned photo path
    for plan users is their first-party vision MCP server (GLM-4.6V) reading
    a LOCAL file, so this dispatch mirrors _attempt_file's temp-file shape
    but allows ONLY the zai MCP tools — never Read. A steered prompt could
    at worst point the vision tool at another IMAGE on this machine; it
    cannot read .env or any text secret, which is the exfiltration class the
    allow_file_fallback rule exists to stop.
    """
    key = _backend_key("glm")
    if key is None:
        return None
    if _npx_path() is None:
        log.warning("GLM vision needs npx (node) for @z_ai/mcp-server — "
                    "not installed on this machine.")
        return None
    tmp_path = None
    cfg_path = None
    work_dir = None
    try:
        # A dedicated EMPTY working directory: the image and the config
        # used to share /tmp, so the run's cwd contained the plan-key file.
        work_dir = tempfile.mkdtemp(prefix="ct_glm_run_")
        with tempfile.NamedTemporaryFile(
            prefix="ct_glm_", suffix=".jpg", delete=False, dir=work_dir
        ) as tmp:
            tmp_path = tmp.name
            tmp.write(image_bytes)
        # The mcp-config goes through a 0600 TEMP FILE, never inline argv:
        # inline JSON put the plan key into /proc/<pid>/cmdline for the
        # whole run, and a CLI that echoes a rejected argument into stderr
        # would hand it to _log_cli_exit (review 2026-07-30).
        # The config lives OUTSIDE work_dir on purpose (see above).
        with tempfile.NamedTemporaryFile(
            prefix="ct_glm_cfg_", suffix=".json", mode="w", delete=False
        ) as cfg:
            cfg_path = cfg.name
            cfg.write(_zai_mcp_config(key))

        cmd = [
            cli, "-p",
            (f"Use the zai vision tool to analyze the image file at "
             f"{tmp_path}, then answer.\n\n"
             f"{prompt or FOOD_DETECTION_PROMPT}"),
            "--output-format", "json",
            "--mcp-config", cfg_path,
            "--allowedTools", "mcp__zai",
            # --allowedTools is an AUTO-APPROVE list, not a disable list:
            # the built-in read-only tools stay available to a prompt that
            # carries caller-influenced text (the app's dietary profile).
            # Name them explicitly forbidden, and run in a scratch cwd —
            # the previous cwd held the temp MCP CONFIG with the plan key.
            "--disallowedTools", "Read,Glob,Grep,LS,Bash,Write,Edit,WebFetch",
            "--strict-mcp-config",
        ]
        _append_model_and_extra_flags(cmd, "glm")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_timeout_seconds(),
            cwd=work_dir,
            env=env,
        )
        if proc.returncode != 0:
            _log_cli_exit(proc)
            return None
        envelope = json.loads(proc.stdout)
        text = _result_text(envelope)
        if text is None:
            log.warning("GLM CLI returned an unusable result envelope.")
            return None
        # PROVE the vision tool actually ran. If the MCP server fails
        # after launch (npm registry unreachable, startup crash) the CLI
        # continues WITHOUT the tool and the model answers about an image
        # it never saw — and the prompt's "unclear → not food" rule makes
        # that a contract-valid wrong verdict. A real tool round-trip is
        # ≥2 turns; a single-turn envelope here is a blind run.
        turns = envelope.get("num_turns")
        if not isinstance(turns, int) or turns < 2:
            log.warning(
                f"GLM vision run had no tool round-trip (num_turns={turns})"
                " — the zai MCP server likely failed to start; discarding "
                "the blind answer.")
            return None
        return _finish_analysis(envelope, text, start, "glm")
    except subprocess.TimeoutExpired:
        log.warning(f"GLM CLI timed out after {_timeout_seconds()}s.")
        return None
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"Could not parse GLM CLI output: {e}")
        return None
    except Exception as e:
        log.warning(f"GLM analyzer failed unexpectedly: {type(e).__name__}: {e}")
        return None
    finally:
        for p in (tmp_path, cfg_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        if work_dir:
            try:
                os.rmdir(work_dir)
            except OSError:
                pass


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


def analyze_text_prompt(prompt: str, backend: str = "claude",
                        raise_on_busy: bool = False) -> Optional[Dict]:
    """Run an arbitrary JSON-answering prompt through the CLI (subscription).

    Used by the app-facing /api/text_intent endpoint so the phone's NL
    corrections cost no API money either. Same discipline as the photo path:
    single turn, no tools, serialized by _CLI_LOCK, and a contended CLI
    returns None (caller decides — the app falls back to its own provider)
    rather than queueing behind a 120 s photo run. Text works on ALL three
    backends: the vendor plans' Anthropic endpoints are text-native.
    """
    if backend not in SUBSCRIPTION_BACKENDS:
        return None
    if not backend_available(backend):
        return None
    cli = _cli_path()
    if cli is None or not (prompt or "").strip():
        return None
    if not _CLI_LOCK.acquire(blocking=False):
        log.info("Claude CLI busy — text intent declined.")
        if raise_on_busy:
            raise AnalyzerBusy()
        return None
    start = time.time()
    try:
        cmd = [cli, "-p", "--output-format", "json", "--tools", ""]
        _append_model_and_extra_flags(cmd, backend)
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_timeout_seconds(),
            env=_cli_env(backend),
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
    image_bytes: bytes,
    prompt: Optional[str] = None,
    allow_file_fallback: bool = True,
    backend: str = "claude",
    raise_on_busy: bool = False,
) -> Optional[Dict]:
    """Analyze a food photo via the Claude Code CLI; None means 'use Gemini'.

    [prompt] overrides the built-in FOOD_DETECTION_PROMPT (the API endpoint
    composes one from the app's dietary profile).

    [allow_file_fallback] MUST be False whenever any part of the prompt came
    from a network caller. The stream dispatch runs with `--tools ""`, but
    the file fallback necessarily enables the Read tool to load the temp
    image — and a caller who can steer that prompt could read arbitrary
    files (e.g. the .env holding the subscription token) and have the
    content returned inside the analysis JSON. Reaching the fallback does
    not even need the rollback knob: a prompt that produces no output makes
    the stream attempt look like a CLI-shape failure.

    [backend] selects whose subscription pays: 'claude' (default, the
    existing path), 'glm' (coding plan; photos via the official vision MCP),
    or 'doubao' (agent plan; stream dispatch only — its Read-file fallback
    is refused unconditionally since image-block support there is already
    the experiment, and a Read-enabled retry adds the exfiltration surface
    on top of an unknown).
    """
    if backend not in SUBSCRIPTION_BACKENDS:
        return None
    if not backend_available(backend, for_photo=True):
        return None
    cli = _cli_path()
    if cli is None or not image_bytes:
        return None

    # Contended-CLI bypass: another photo already owns the (up to 120s) CLI
    # run. Don't queue behind it — Gemini answers this photo in seconds.
    if not _CLI_LOCK.acquire(blocking=False):
        log.info("Claude CLI busy — falling back to Gemini for this photo.")
        # Default False keeps the TELEGRAM contract ("None → use Gemini")
        # byte-identical; only the API endpoints opt in, because only they
        # need to tell the app "retry in seconds" from "do not retry".
        if raise_on_busy:
            raise AnalyzerBusy()
        return None

    # One photo = one CLI occupancy: the lock is held across BOTH attempts
    # of the stream→file fallback chain, so a burst can never stack the
    # memory-heavy CLI even mid-fallback.
    start = time.time()
    try:
        env = _cli_env(backend)
        if backend == "glm":
            # Text-only Anthropic endpoint: the stream dispatch cannot
            # carry the image; the vision MCP path is the ONLY photo path.
            return _attempt_glm_vision(cli, image_bytes, env, start, prompt)
        if backend == "doubao":
            analysis, _ = _attempt_stream(
                cli, image_bytes, env, start, prompt, backend)
            return analysis
        if _dispatch_mode() == "stream":
            analysis, retry_via_file = _attempt_stream(
                cli, image_bytes, env, start, prompt)
            if not retry_via_file:
                return analysis
            if not allow_file_fallback:
                # Caller-influenced prompt: refuse the Read-enabled path.
                log.warning(
                    "Claude stream dispatch failed for an API-originated "
                    "request — NOT falling back to the Read-tool path."
                )
                return None
            log.warning(
                "Claude stream-json dispatch failed — retrying once via the "
                "two-turn Read-file path."
            )
        return _attempt_file(cli, image_bytes, env, start, prompt)
    finally:
        _CLI_LOCK.release()
