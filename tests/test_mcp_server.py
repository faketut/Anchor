"""Unit tests for the Anchor MCP server's tool dispatcher.

The `mcp` package is an optional dependency (declared in `[mcp]` extra). To
avoid forcing dev environments to install it, we shim it into sys.modules
with minimal stubs before importing the server module. This lets us test
_call() — the pure dispatcher — without the asyncio/stdio server layer.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone

import pytest


# ---- shim the optional `mcp` package ---------------------------------------


def _install_mcp_stub() -> None:
    if "mcp" in sys.modules:  # pragma: no cover  — real install present
        return
    mcp_pkg = types.ModuleType("mcp")
    server_pkg = types.ModuleType("mcp.server")
    stdio_pkg = types.ModuleType("mcp.server.stdio")
    types_pkg = types.ModuleType("mcp.types")

    class _FakeServer:
        def __init__(self, name: str) -> None:
            self.name = name

        def list_tools(self):
            return lambda fn: fn

        def call_tool(self):
            return lambda fn: fn

    class _FakeTool:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class _FakeTextContent:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    server_pkg.Server = _FakeServer
    stdio_pkg.stdio_server = lambda *a, **kw: None  # never called in tests
    types_pkg.Tool = _FakeTool
    types_pkg.TextContent = _FakeTextContent

    sys.modules["mcp"] = mcp_pkg
    sys.modules["mcp.server"] = server_pkg
    sys.modules["mcp.server.stdio"] = stdio_pkg
    sys.modules["mcp.types"] = types_pkg


_install_mcp_stub()

from anchor import mcp_server  # noqa: E402  — after stub install


# ---- fixtures --------------------------------------------------------------


@pytest.fixture
def fake_anchor():
    from anchor.models import Anchor, Fingerprint, Scope, TimeRange

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return Anchor(
        id="aaaa1111-fake-anchor",
        name="Healthy",
        created_at=now,
        created_by="test",
        time_range=TimeRange(start=now, end=now),
        scope=Scope(),
        fingerprint=Fingerprint(),
    )


@pytest.fixture
def fake_drift():
    from anchor.models import DiffEntry, DriftRecord, TimeRange

    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    return DriftRecord(
        id="bbbb2222-fake-drift",
        timestamp=now,
        anchor_id="aaaa1111",
        compare_window=TimeRange(start=now, end=now),
        top_diffs=[
            DiffEntry(
                signal="template:appeared:Foo",
                kind="template",
                anchor_val=0,
                current_val=10,
                delta_pct=None,
                severity="HIGH",
            )
        ],
        outcome="resolved",
        engineer_confirmed_reason="rolled back",
    )


# ---- dispatcher tests ------------------------------------------------------


def test_list_tools_advertises_all_documented_tools():
    names = {t["name"] for t in mcp_server._TOOLS}
    expected = {
        "anchor.list_anchors",
        "anchor.capture_anchor",
        "anchor.compare",
        "anchor.deep_compare",
        "anchor.recall",
        "anchor.feedback",
        "anchor.list_history",
        "anchor.learned_signals",
    }
    assert expected.issubset(names)


def test_dispatch_list_anchors(monkeypatch, fake_anchor):
    monkeypatch.setattr(mcp_server.agent, "all_anchors", lambda: [fake_anchor])
    out = mcp_server._call("anchor.list_anchors", {})
    assert len(out) == 1
    assert out[0]["id"] == "aaaa1111-fake-anchor"


def test_dispatch_recall(monkeypatch, fake_drift):
    captured = {}

    def _fake(signals, k=3, min_similarity=0.15):
        captured["signals"] = signals
        captured["k"] = k
        captured["min_similarity"] = min_similarity
        return [(fake_drift, 0.42)]

    monkeypatch.setattr(mcp_server, "recall_similar_drifts", _fake)
    out = mcp_server._call(
        "anchor.recall",
        {"signals": ["template:Foo"], "k": 5, "min_similarity": 0.2},
    )
    assert captured == {"signals": ["template:Foo"], "k": 5, "min_similarity": 0.2}
    assert out[0]["similarity"] == 0.42
    assert out[0]["drift"]["id"] == "bbbb2222-fake-drift"


def test_dispatch_compare_serializes_compare_result(monkeypatch, fake_anchor, fake_drift):
    from anchor.agent import CompareResult

    cr = CompareResult(
        anchor=fake_anchor,
        drift=fake_drift,
        top_diffs=fake_drift.top_diffs,
        summary="things broke",
        hypothesis="deploy regression",
        drill_in_spl="search index=main error",
        recalled=((fake_drift, 0.99),),
    )
    monkeypatch.setattr(mcp_server.agent, "compare", lambda *a, **kw: cr)
    out = mcp_server._call(
        "anchor.compare",
        {"start": "2026-06-14T00:00", "end": "2026-06-14T12:00"},
    )
    assert out["summary"] == "things broke"
    assert out["hypothesis"] == "deploy regression"
    assert out["drill_in_spl"] == "search index=main error"
    assert out["top_diffs"][0]["signal"] == "template:appeared:Foo"
    assert out["recalled"][0]["similarity"] == 0.99


def test_dispatch_deep_compare_includes_investigation(monkeypatch, fake_anchor, fake_drift):
    from anchor.agent import CompareResult
    from anchor.models import InvestigationResult, InvestigationStep

    cr = CompareResult(
        anchor=fake_anchor,
        drift=fake_drift,
        top_diffs=fake_drift.top_diffs,
        summary="initial",
        hypothesis=None,
        drill_in_spl=None,
        recalled=(),
    )
    invest = InvestigationResult(
        steps=[
            InvestigationStep(
                n=1, tool="recall_similar_drifts",
                args={"signals": ["template:Foo"]},
                observation="[]",
            )
        ],
        summary="refined summary",
        hypothesis="refined hypothesis",
        evidence=["e1"],
        confidence=0.75,
    )
    monkeypatch.setattr(mcp_server.agent, "deep_compare", lambda *a, **kw: (cr, invest))
    out = mcp_server._call(
        "anchor.deep_compare",
        {"start": "2026-06-14T00:00", "end": "2026-06-14T12:00", "max_steps": 3},
    )
    assert out["summary"] == "initial"
    assert out["investigation"]["summary"] == "refined summary"
    assert out["investigation"]["confidence"] == 0.75
    assert out["investigation"]["steps"][0]["tool"] == "recall_similar_drifts"


def test_dispatch_feedback_threads_args(monkeypatch, fake_drift):
    seen = {}

    def _fake(drift_id, outcome, reason):
        seen["drift_id"] = drift_id
        seen["outcome"] = outcome
        seen["reason"] = reason
        return fake_drift

    monkeypatch.setattr(mcp_server.agent, "submit_feedback", _fake)
    out = mcp_server._call(
        "anchor.feedback",
        {"drift_id": "bbbb", "outcome": "resolved", "reason": "rolled back"},
    )
    assert seen == {"drift_id": "bbbb", "outcome": "resolved", "reason": "rolled back"}
    assert out["id"] == "bbbb2222-fake-drift"


def test_dispatch_unknown_tool_raises():
    with pytest.raises(ValueError, match="Unknown tool"):
        mcp_server._call("anchor.no_such_thing", {})


def test_iso_parses_z_suffix():
    dt = mcp_server._iso("2026-06-14T12:00:00Z")
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.hour == 12


def test_iso_promotes_naive_datetime_to_utc():
    """B2: a naive ISO string must be silently promoted to UTC, not crash."""
    dt = mcp_server._iso("2026-06-14T12:00:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_dispatch_recall_strips_signal_embedding(monkeypatch, fake_drift):
    """B3: anchor.recall must not return 1024-dim embeddings to MCP clients."""
    fake_drift.signal_embedding = [0.42] * 1024
    monkeypatch.setattr(
        mcp_server, "recall_similar_drifts",
        lambda signals, k=3, min_similarity=0.15: [(fake_drift, 0.5)],
    )
    out = mcp_server._call("anchor.recall", {"signals": ["template:Foo"]})
    assert "signal_embedding" not in out[0]["drift"]
