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
