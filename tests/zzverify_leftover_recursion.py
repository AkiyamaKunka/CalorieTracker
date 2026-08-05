"""zzverify: does a nested-array JSON bomb under the char cap 500 the
/api/analyze_leftover endpoint? (delete after verification)"""
import base64

import claude_analyzer
import pytest

from test_api_analyze_endpoints import client, _meals, _ledger_rows  # noqa: F401


def _stub(monkeypatch):
    monkeypatch.setattr(claude_analyzer, "is_configured", lambda: True)
    monkeypatch.setattr(claude_analyzer, "analyze_leftover_photo",
                        lambda *a, **kw: {"same_meal": True,
                                          "confidence": 0.9,
                                          "leftover_fraction": 0.4,
                                          "items": []})


@pytest.mark.parametrize("depth", [100, 500, 900, 1200])
def test_zzverify_nested_array_bomb(client, mon