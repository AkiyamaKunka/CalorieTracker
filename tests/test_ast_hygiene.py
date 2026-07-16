"""AST hygiene guard (from the S9 sweep) for bug classes pyflakes misses.

Enforces the unambiguous ones repo-wide — mutable default arguments and
truly-bare `except:` — so a future edit can't reintroduce them. Broad
`except Exception: pass` is deliberately NOT enforced here: the one such
site (daily_report's best-effort failure alert) is a legitimate pattern
where swallowing is correct.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PY_FILES = sorted(ROOT.glob("*.py")) + sorted(ROOT.glob("android/*.py"))


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_no_mutable_default_arguments(path):
    tree = ast.parse(path.read_text(), filename=path.name)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in node.args.defaults + node.args.kw_defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    offenders.append(f"{node.name}() @L{node.lineno}")
                elif (isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                      and default.func.id in {"list", "dict", "set"}):
                    offenders.append(f"{node.name}() @L{node.lineno}")
    assert not offenders, f"mutable default args in {path.name}: {offenders}"


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_no_bare_except(path):
    tree = ast.parse(path.read_text(), filename=path.name)
    bare = [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and node.type is None]
    assert not bare, f"bare 'except:' in {path.name} at lines {bare}"


_HTTP_VERBS = {"get", "post", "put", "delete", "head", "patch", "request"}


def _is_outbound_http_call(node: ast.Call) -> bool:
    """Matches requests.<verb>(...) and <...>.session.<verb>(...) call sites."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in _HTTP_VERBS):
        return False
    owner = func.value
    if isinstance(owner, ast.Name):
        return owner.id == "requests" or owner.id.endswith("session")
    return isinstance(owner, ast.Attribute) and owner.attr.endswith("session")


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_outbound_http_calls_always_pass_a_timeout(path):
    """requests has NO default timeout: a call without timeout= blocks forever
    on a black-holed connection. On the bot's main polling thread that is a
    permanent hang systemd Restart=always can never see (the process stays
    alive), so every direct requests/session call must pass an explicit
    timeout. (The google-genai 0.3.0 SDK's internal no-timeout requests are
    pinned separately in test_telegram_bot.py — this guard covers our code.)"""
    tree = ast.parse(path.read_text(), filename=path.name)
    offenders = [
        f"L{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_outbound_http_call(node)
        and not any(kw.arg == "timeout" for kw in node.keywords)
    ]
    assert not offenders, f"requests call without timeout= in {path.name}: {offenders}"
