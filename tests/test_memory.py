"""Unit tests for pure-function memory helpers (decay, recall).

These don't touch Splunk — they exercise the math/logic directly. For
`decay_weights` and `bump_appearance` (which talk to KV), see integration
tests with a real Splunk sandbox.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from anchor import memory
from anchor.memory import _jaccard, decay_weights, recall_similar_drifts
from anchor.models import DiffEntry, DriftRecord, SignalWeight, TimeRange


# ---- Jaccard ---------------------------------------------------------------


def test_jaccard_identical_sets_is_one() -> None:
    assert _jaccard({"a", "b", "c"}, {"a", "b", "c"}) == 1.0


def test_jaccard_disjoint_is_zero() -> None:
    assert _jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_two_thirds_overlap() -> None:
    # intersection = {a,b} size 2; union = {a,b,c,d} size 4 → 0.5
    assert _jaccard({"a", "b", "c"}, {"a", "b", "d"}) == 0.5


def test_jaccard_both_empty_is_zero() -> None:
    assert _jaccard(set(), set()) == 0.0


# ---- recall_similar_drifts (uses list_drifts internally) -------------------


def _drift(
    drift_id: str,
    signals: list[str],
    outcome: str = "resolved",
    reason: str = "",
) -> DriftRecord:
    now = datetime.now(timezone.utc)
    return DriftRecord(
        id=drift_id,
        timestamp=now,
        anchor_id="anchor-1",
        compare_window=TimeRange(start=now - timedelta(hours=1), end=now),
        top_diffs=[
            DiffEntry(
                signal=s,
                kind="metric",
                anchor_val=1.0,
                current_val=2.0,
                delta_pct=100.0,
                severity="MEDIUM",
            )
            for s in signals
        ],
        outcome=outcome,  # type: ignore[arg-type]
        engineer_confirmed_reason=reason or None,
    )


def test_recall_ranks_by_overlap(monkeypatch) -> None:
    """Recall should rank higher-overlap drifts first, all else equal."""
    fakes = [
        _drift("d-low", ["metric:cpu", "metric:mem"]),                    # 1/4 overlap
        _drift("d-high", ["metric:latency:p99", "metric:latency:p95"]),   # 2/2 → high
        _drift("d-mid", ["metric:latency:p99", "metric:cpu"]),            # 1/3 overlap
    ]
    monkeypatch.setattr("anchor.memory.list_drifts", lambda **_: fakes)

    out = recall_similar_drifts(
        ["metric:latency:p99", "metric:latency:p95"], k=3, min_similarity=0.0
    )
    ids = [d.id for d, _ in out]
    assert ids[0] == "d-high"  # full overlap wins
    # d-mid (1/3 ≈ 0.33) ranks above d-low (1/4 = 0.25)
    assert ids.index("d-mid") < ids.index("d-low")


def test_recall_filters_by_outcome(monkeypatch) -> None:
    """Only drifts with confirmed ground-truth outcomes should be recalled."""
    fakes = [
        _drift("d-unknown", ["metric:latency:p99"], outcome="unknown"),
        _drift("d-ongoing", ["metric:latency:p99"], outcome="ongoing"),
        _drift("d-resolved", ["metric:latency:p99"], outcome="resolved"),
    ]
    monkeypatch.setattr("anchor.memory.list_drifts", lambda **_: fakes)

    out = recall_similar_drifts(["metric:latency:p99"], k=5, min_similarity=0.0)
    ids = [d.id for d, _ in out]
    assert ids == ["d-resolved"]


def test_recall_respects_min_similarity(monkeypatch) -> None:
    fakes = [_drift("d-low", ["metric:cpu", "metric:mem", "metric:disk"])]
    monkeypatch.setattr("anchor.memory.list_drifts", lambda **_: fakes)

    # current shares 0 of 4 → similarity 0 → below default min
    out = recall_similar_drifts(["metric:latency:p99"], k=5, min_similarity=0.15)
    assert out == []


def test_recall_empty_current_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        "anchor.memory.list_drifts", lambda **_: [_drift("d", ["x"])],
    )
    assert recall_similar_drifts([], k=5) == []


# ---- decay_weights ---------------------------------------------------------


def _weight_doc(name: str, weight: float, last_updated: datetime | None) -> dict:
    w = SignalWeight(
        signal_name=name,
        weight=weight,
        last_updated=last_updated,
    )
    doc = json.loads(w.model_dump_json())
    doc["_key"] = name
    return doc


def _patch_kv(monkeypatch, store: dict[str, dict]) -> list[tuple[str, dict]]:
    """Wire kv_all + kv_update against an in-memory dict. Returns a captured
    list of (key, doc) updates so tests can assert what was written.
    """
    updates: list[tuple[str, dict]] = []
    monkeypatch.setattr("anchor.memory.kv_all", lambda name: list(store.values()))

    def _update(name: str, key: str, doc: dict) -> None:
        store[key] = {**doc, "_key": key}
        updates.append((key, doc))

    monkeypatch.setattr("anchor.memory.kv_update", _update)
    return updates


def test_decay_pulls_old_weight_toward_one(monkeypatch) -> None:
    """A 60-day-old weight of 2.0 with half-life 30d should land at 1.25."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    store = {"sig": _weight_doc("sig", 2.0, now - timedelta(days=60))}
    _patch_kv(monkeypatch, store)

    touched = decay_weights(now, half_life_days=30.0)
    assert touched == 1
    # 1.0 + (2.0 - 1.0) * 0.5 ** (60/30) = 1.0 + 0.25 = 1.25
    assert abs(store["sig"]["weight"] - 1.25) < 1e-6


def test_decay_skips_recent_weights(monkeypatch) -> None:
    """Weights touched within DECAY_SKIP_RECENT_HOURS must be untouched."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    store = {"sig": _weight_doc("sig", 2.0, now - timedelta(hours=1))}
    _patch_kv(monkeypatch, store)

    touched = decay_weights(now)
    assert touched == 0
    assert store["sig"]["weight"] == 2.0


def test_decay_skips_weights_without_last_updated(monkeypatch) -> None:
    """A weight with no last_updated has no age to compute decay against."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    store = {"sig": _weight_doc("sig", 2.0, None)}
    _patch_kv(monkeypatch, store)

    touched = decay_weights(now)
    assert touched == 0
    assert store["sig"]["weight"] == 2.0


def test_decay_half_life_exact(monkeypatch) -> None:
    """Age == half_life → factor 0.5, weight 2.0 → 1.5."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    store = {"sig": _weight_doc("sig", 2.0, now - timedelta(days=30))}
    _patch_kv(monkeypatch, store)

    decay_weights(now, half_life_days=30.0)
    assert abs(store["sig"]["weight"] - 1.5) < 1e-6


def test_decay_noop_when_weight_already_one(monkeypatch) -> None:
    """Weight == 1.0 has no opinion to forget; no update should be issued."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    store = {"sig": _weight_doc("sig", 1.0, now - timedelta(days=365))}
    updates = _patch_kv(monkeypatch, store)

    touched = decay_weights(now)
    assert touched == 0
    assert updates == []


def test_maybe_decay_throttles_within_interval(monkeypatch) -> None:
    """_maybe_decay_weights must not invoke decay twice within DECAY_MIN_INTERVAL_HOURS."""
    monkeypatch.setattr(memory, "_last_decay_run", None)
    calls: list[datetime] = []

    def _fake_decay(now, half_life_days=memory.DECAY_HALF_LIFE_DAYS):
        calls.append(now)
        return 0

    monkeypatch.setattr(memory, "decay_weights", _fake_decay)

    memory._maybe_decay_weights()
    memory._maybe_decay_weights()
    memory._maybe_decay_weights()
    assert len(calls) == 1
