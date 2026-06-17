"""Tests for the Qwen embedding helpers and the semantic recall path."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from anchor import embedding, memory
from anchor.models import DiffEntry, DriftRecord, TimeRange


# ---- pure helpers ----------------------------------------------------------


def test_cosine_identical_vectors_is_one():
    v = [1.0, 2.0, 3.0]
    assert abs(embedding.cosine(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal_is_zero():
    assert embedding.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_handles_empty_and_mismatched():
    assert embedding.cosine([], []) == 0.0
    assert embedding.cosine([1.0, 2.0], [3.0]) == 0.0
    assert embedding.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_partial_overlap():
    # 45 degrees -> cos = sqrt(2)/2
    assert math.isclose(embedding.cosine([1.0, 0.0], [1.0, 1.0]), math.sqrt(2) / 2)


def _patched_config(**overrides):
    """Build a stand-in for CONFIG. CONFIG is a frozen dataclass so monkeypatch
    can't mutate fields — swap the whole object instead."""
    defaults = dict(
        qwen_api_key="",
        qwen_embed_model="text-embedding-v3",
        qwen_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        semantic_recall=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_embed_signals_returns_none_without_api_key(monkeypatch):
    monkeypatch.setattr(embedding, "CONFIG", _patched_config(qwen_api_key=""))
    assert embedding.embed_signals(["x", "y"]) is None


def test_embed_signals_returns_none_on_empty():
    assert embedding.embed_signals([]) is None


def test_embed_signals_calls_openai_when_configured(monkeypatch):
    """With API key + model set, embed_signals must hit the OpenAI-compat endpoint."""
    monkeypatch.setattr(
        embedding,
        "CONFIG",
        _patched_config(qwen_api_key="fake", qwen_embed_model="text-embedding-v3"),
    )
    captured = {}

    class FakeEmbeddings:
        def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])

    class FakeClient:
        def __init__(self, **kw):
            captured["client_kw"] = kw
            self.embeddings = FakeEmbeddings()

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    out = embedding.embed_signals(["b", "a"])  # sorted to make embedding stable
    assert out == [0.1, 0.2, 0.3]
    assert captured["model"] == "text-embedding-v3"
    # sorted + deduped + joined by newline
    assert captured["input"] == "a\nb"


def test_embed_signals_falls_back_to_none_on_exception(monkeypatch, capsys):
    monkeypatch.setattr(
        embedding,
        "CONFIG",
        _patched_config(qwen_api_key="fake", qwen_embed_model="text-embedding-v3"),
    )

    class FakeClient:
        def __init__(self, **kw):
            self.embeddings = SimpleNamespace(
                create=lambda **kw: (_ for _ in ()).throw(RuntimeError("network"))
            )

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    assert embedding.embed_signals(["x"]) is None
    err = capsys.readouterr().err
    assert "embed_signals failed" in err
    assert "RuntimeError" in err


# ---- semantic recall integration ------------------------------------------


def _mk_drift(id_suffix: str, signals: list[str], emb: list[float] | None = None):
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    return DriftRecord(
        id=f"00000000-0000-0000-0000-{id_suffix:>012}",
        timestamp=now,
        anchor_id="a1",
        compare_window=TimeRange(start=now, end=now),
        top_diffs=[
            DiffEntry(
                signal=s, kind="template", anchor_val=0, current_val=10,
                delta_pct=None, severity="HIGH",
            )
            for s in signals
        ],
        outcome="resolved",
        engineer_confirmed_reason="root cause",
        signal_embedding=emb,
    )


def test_recall_uses_cosine_when_semantic_enabled(monkeypatch):
    """When CONFIG.semantic_recall=True and embeddings exist, ranking must be cosine."""
    # past[0] has token overlap = 0 with current ("payment timeout" vs "billing-svc 500")
    # but a near-parallel embedding -> should still win.
    past = [
        _mk_drift("aaaa", ["billing-svc:status_500"], emb=[1.0, 1.0, 0.0]),  # cos ~1.0
        _mk_drift("bbbb", ["payment:PaymentGatewayTimeout"], emb=[0.0, 1.0, 1.0]),  # cos ~0.5
    ]
    monkeypatch.setattr(memory, "list_drifts", lambda limit=500: past)
    monkeypatch.setattr(memory, "CONFIG", _patched_config(semantic_recall=True))
    monkeypatch.setattr(memory, "embed_signals", lambda sigs: [1.0, 1.0, 0.0])

    result = memory.recall_similar_drifts(
        ["payment:PaymentGatewayTimeout"], k=2, min_similarity=0.0
    )
    ids = [d.id[-4:] for d, _ in result]
    assert ids == ["aaaa", "bbbb"]  # semantically-closer one ranks first
    # Sanity: top similarity is 1.0 because vectors are identical
    assert math.isclose(result[0][1], 1.0, abs_tol=1e-6)


def test_recall_falls_back_to_jaccard_when_no_embeddings(monkeypatch):
    """Even with semantic_recall=True, no embeddings -> Jaccard path."""
    past = [
        _mk_drift("cccc", ["sig:a", "sig:b"]),
        _mk_drift("dddd", ["sig:x"]),
    ]
    monkeypatch.setattr(memory, "list_drifts", lambda limit=500: past)
    monkeypatch.setattr(memory, "CONFIG", _patched_config(semantic_recall=True))
    # Should not be called because no candidate has an embedding.
    monkeypatch.setattr(
        memory, "embed_signals", lambda sigs: (_ for _ in ()).throw(AssertionError())
    )
    result = memory.recall_similar_drifts(["sig:a", "sig:b"], k=2, min_similarity=0.0)
    assert result[0][0].id.endswith("cccc")  # full overlap wins


def test_recall_falls_back_when_embedding_call_fails(monkeypatch):
    """If embed_signals returns None for the current query, ranking degrades to Jaccard."""
    past = [
        _mk_drift("eeee", ["sig:a"], emb=[1.0, 0.0]),
        _mk_drift("ffff", ["sig:b"], emb=[0.0, 1.0]),
    ]
    monkeypatch.setattr(memory, "list_drifts", lambda limit=500: past)
    monkeypatch.setattr(memory, "CONFIG", _patched_config(semantic_recall=True))
    monkeypatch.setattr(memory, "embed_signals", lambda sigs: None)
    # Jaccard: "sig:a" matches first past drift exactly.
    result = memory.recall_similar_drifts(["sig:a"], k=2, min_similarity=0.0)
    assert result[0][0].id.endswith("eeee")


def test_save_drift_embeds_when_semantic_recall_enabled(monkeypatch):
    """save_drift must call embed_signals iff CONFIG.semantic_recall is True."""
    monkeypatch.setattr(memory, "CONFIG", _patched_config(semantic_recall=True))
    monkeypatch.setattr(memory, "ensure_collections", lambda: None)
    inserted = {}
    monkeypatch.setattr(memory, "kv_insert", lambda coll, doc: inserted.setdefault("doc", doc))
    monkeypatch.setattr(memory, "embed_signals", lambda sigs: [0.5, 0.5])

    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    diffs = [
        DiffEntry(
            signal="template:Foo", kind="template", anchor_val=0,
            current_val=1, delta_pct=None, severity="HIGH",
        )
    ]
    rec = memory.save_drift(
        anchor_id="a1",
        compare_window=TimeRange(start=now, end=now),
        top_diffs=diffs,
        hypothesis=None,
        suggested_spl=None,
    )
    assert rec.signal_embedding == [0.5, 0.5]
    assert inserted["doc"]["signal_embedding"] == [0.5, 0.5]


def test_save_drift_skips_embedding_when_disabled(monkeypatch):
    monkeypatch.setattr(memory, "CONFIG", _patched_config(semantic_recall=False))
    monkeypatch.setattr(memory, "ensure_collections", lambda: None)
    monkeypatch.setattr(memory, "kv_insert", lambda coll, doc: None)

    def _boom(sigs):
        raise AssertionError("embed_signals should not be called when semantic_recall=False")

    monkeypatch.setattr(memory, "embed_signals", _boom)
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    rec = memory.save_drift(
        anchor_id="a1",
        compare_window=TimeRange(start=now, end=now),
        top_diffs=[],
        hypothesis=None,
        suggested_spl=None,
    )
    assert rec.signal_embedding is None
