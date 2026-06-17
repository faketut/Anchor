"""Unit tests for the deep-investigation function-calling planner.

The OpenAI client is fully mocked — no network, no Splunk, no Qwen API key
required. We assert:
  * the planner loop dispatches each tool_call and feeds the observation back
  * an assistant message with no tool_calls finalises and returns
  * the step-budget cap forces a final answer when exceeded
  * dispatch routes unknown tools to an error stub instead of crashing
  * a tool that raises is converted to an error observation, not a stack trace
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from anchor import investigator
from anchor.models import (
    Anchor,
    DiffEntry,
    DriftRecord,
    Fingerprint,
    Scope,
    TimeRange,
)


# ---- fakes -----------------------------------------------------------------


def _fake_tool_call(name: str, args: dict, call_id: str = "call_x") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _fake_msg(content: str | None = None, tool_calls: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _fake_rsp(msg: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _fake_compare_result():
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    anchor = Anchor(
        id="aaaa1111-anchor",
        name="Healthy Week",
        created_at=now,
        created_by="test",
        time_range=TimeRange(start=now, end=now),
        scope=Scope(),
        fingerprint=Fingerprint(),
    )
    drift = DriftRecord(
        id="bbbb2222-drift",
        timestamp=now,
        anchor_id=anchor.id,
        compare_window=TimeRange(start=now, end=now),
        top_diffs=[],
    )
    diff = DiffEntry(
        signal="template:appeared:PaymentGatewayTimeout",
        kind="template",
        anchor_val=0,
        current_val=400,
        delta_pct=None,
        severity="HIGH",
        note="new",
    )
    from anchor.agent import CompareResult

    return CompareResult(
        anchor=anchor,
        drift=drift,
        top_diffs=[diff],
        summary="initial",
        hypothesis="placeholder",
        drill_in_spl=None,
        recalled=(),
    )


# ---- loop tests ------------------------------------------------------------


def test_investigate_runs_tool_then_finalises(monkeypatch):
    """One tool call, then a JSON answer — happy path."""
    rsps = iter(
        [
            _fake_rsp(
                _fake_msg(
                    content="Looking up precedents",
                    tool_calls=[
                        _fake_tool_call(
                            "recall_similar_drifts", {"signals": ["template:foo"]}, "c1"
                        )
                    ],
                )
            ),
            _fake_rsp(
                _fake_msg(
                    content=json.dumps(
                        {
                            "summary": "Matches drift abc12345 (resolved by rollback).",
                            "hypothesis": "deploy regression",
                            "evidence": ["recalled drift abc12345 with 100% overlap"],
                            "confidence": 0.85,
                        }
                    )
                )
            ),
        ]
    )

    class FakeClient:
        chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: next(rsps))
        )

    monkeypatch.setattr(investigator, "_make_client", lambda p: (FakeClient(), "fake-model"))
    monkeypatch.setattr(
        investigator,
        "_dispatch",
        lambda name, args: json.dumps([{"id": "abc12345", "outcome": "resolved"}]),
    )

    result = investigator.investigate(_fake_compare_result(), max_steps=5)
    assert len(result.steps) == 1
    assert result.steps[0].tool == "recall_similar_drifts"
    assert result.steps[0].args == {"signals": ["template:foo"]}
    assert result.hypothesis == "deploy regression"
    assert result.confidence == 0.85
    assert result.evidence == ["recalled drift abc12345 with 100% overlap"]
    assert not result.truncated


def test_investigate_truncates_at_max_steps(monkeypatch):
    """If the planner keeps calling tools past the cap, we force a finalise."""
    calls = {"n": 0}

    def _create(**kw):
        # Final forced call passes response_format; everything else returns a tool call.
        calls["n"] += 1
        if kw.get("response_format"):
            return _fake_rsp(
                _fake_msg(
                    content=json.dumps(
                        {"summary": "forced", "hypothesis": None, "evidence": []}
                    )
                )
            )
        return _fake_rsp(
            _fake_msg(
                content="more",
                tool_calls=[_fake_tool_call("list_recent_drifts", {}, f"c{calls['n']}")],
            )
        )

    class FakeClient:
        chat = SimpleNamespace(completions=SimpleNamespace(create=_create))

    monkeypatch.setattr(investigator, "_make_client", lambda p: (FakeClient(), "fake-model"))
    monkeypatch.setattr(investigator, "_dispatch", lambda name, args: "[]")

    result = investigator.investigate(_fake_compare_result(), max_steps=2)
    assert result.truncated is True
    assert len(result.steps) == 2
    assert result.summary == "forced"
    # 2 tool-calling round-trips + 1 forced finalise = 3 create() invocations
    assert calls["n"] == 3


def test_investigate_dispatch_error_becomes_observation(monkeypatch):
    """A tool that raises must yield a structured error observation, not crash."""

    def _boom(name, args):
        raise RuntimeError("splunk unreachable")

    rsps = iter(
        [
            _fake_rsp(
                _fake_msg(
                    tool_calls=[_fake_tool_call("run_spl", {"spl": "x", "earliest": "-1h", "latest": "now"})]
                )
            ),
            _fake_rsp(_fake_msg(content=json.dumps({"summary": "done"}))),
        ]
    )

    class FakeClient:
        chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: next(rsps))
        )

    monkeypatch.setattr(investigator, "_make_client", lambda p: (FakeClient(), "fake-model"))
    monkeypatch.setattr(investigator, "_dispatch", _boom)

    result = investigator.investigate(_fake_compare_result(), max_steps=3)
    assert len(result.steps) == 1
    obs = json.loads(result.steps[0].observation)
    assert "RuntimeError" in obs["error"]
    assert "splunk unreachable" in obs["error"]
    assert result.summary == "done"


def test_investigate_rejects_zero_max_steps():
    with pytest.raises(ValueError, match="max_steps"):
        investigator.investigate(_fake_compare_result(), max_steps=0)


def test_investigate_fires_step_callback_in_order(monkeypatch):
    """step_callback must be invoked once per step, in order, with the just-added step."""
    rsps = iter(
        [
            _fake_rsp(
                _fake_msg(
                    tool_calls=[_fake_tool_call("list_recent_drifts", {}, "c1")]
                )
            ),
            _fake_rsp(
                _fake_msg(
                    tool_calls=[
                        _fake_tool_call(
                            "recall_similar_drifts", {"signals": ["x"]}, "c2"
                        )
                    ]
                )
            ),
            _fake_rsp(_fake_msg(content=json.dumps({"summary": "done"}))),
        ]
    )

    class FakeClient:
        chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: next(rsps))
        )

    monkeypatch.setattr(investigator, "_make_client", lambda p: (FakeClient(), "fake-model"))
    monkeypatch.setattr(investigator, "_dispatch", lambda name, args: "[]")

    seen: list[tuple[int, str]] = []
    result = investigator.investigate(
        _fake_compare_result(),
        max_steps=5,
        step_callback=lambda s: seen.append((s.n, s.tool)),
    )
    assert seen == [(1, "list_recent_drifts"), (2, "recall_similar_drifts")]
    assert len(result.steps) == 2


# ---- dispatch tests --------------------------------------------------------


def test_dispatch_unknown_tool_returns_error_json():
    out = json.loads(investigator._dispatch("not_a_tool", {}))
    assert "unknown tool" in out["error"]


def test_dispatch_recall_similar_drifts_serializes_results(monkeypatch):
    """The dispatcher must convert (DriftRecord, similarity) tuples to JSON."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    fake_drift = DriftRecord(
        id="abcdef0123456789",
        timestamp=now,
        anchor_id="a1",
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
        engineer_confirmed_reason="rolled back deploy",
    )
    monkeypatch.setattr(
        "anchor.memory.recall_similar_drifts",
        lambda signals, k=5, min_similarity=0.1: [(fake_drift, 0.92)],
    )
    out = json.loads(investigator._dispatch("recall_similar_drifts", {"signals": ["x"]}))
    assert out[0]["id"] == "abcdef01"
    assert out[0]["similarity"] == 0.92
    assert out[0]["confirmed_reason"] == "rolled back deploy"
    assert out[0]["top_signals"] == ["template:appeared:Foo"]


def test_truncate_caps_long_observations():
    long = "x" * 5000
    out = investigator._truncate(long, limit=100)
    assert out.startswith("x" * 100)
    assert "truncated" in out
    assert len(out) < 200


def test_dispatch_get_drift_details_strips_signal_embedding(monkeypatch):
    """B3: the 1024-dim embedding must never appear in a planner observation."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    fake_drift = DriftRecord(
        id="cafebabe-0000-0000-0000-000000000000",
        timestamp=now,
        anchor_id="a1",
        compare_window=TimeRange(start=now, end=now),
        top_diffs=[
            DiffEntry(
                signal="template:x", kind="template",
                anchor_val=0, current_val=1,
                delta_pct=None, severity="HIGH",
            )
        ],
        outcome="resolved",
        signal_embedding=[0.123] * 1024,
    )
    monkeypatch.setattr("anchor.memory.get_drift", lambda _id: fake_drift)
    raw = investigator._dispatch("get_drift_details", {"drift_id": "cafebabe"})
    out = json.loads(raw)
    assert "signal_embedding" not in out
    assert out["outcome"] == "resolved"


def test_dispatch_missing_required_args_returns_error():
    """get_drift_details / run_spl with absent args must not KeyError."""
    out = json.loads(investigator._dispatch("get_drift_details", {}))
    assert "drift_id" in out["error"]

    out = json.loads(investigator._dispatch("run_spl", {"spl": "x"}))
    assert "earliest" in out["error"] or "latest" in out["error"]
