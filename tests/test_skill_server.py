"""Tests for the FastAPI skill server.

Skipped entirely when `fastapi` (the `[skill]` extra) is not installed, so
lean dev environments don't need to pull in starlette + uvicorn.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

# Skip the whole module if the [skill] extra isn't installed.
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from anchor import skill_server  # noqa: E402
from anchor.agent import CompareResult  # noqa: E402
from anchor.models import (  # noqa: E402
    Anchor,
    DiffEntry,
    DriftRecord,
    Fingerprint,
    Scope,
    TimeRange,
)


# ---- fixtures --------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(skill_server.app)


@pytest.fixture
def fake_anchor() -> Anchor:
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
def fake_drift() -> DriftRecord:
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    return DriftRecord(
        id="bbbb2222-fake-drift",
        timestamp=now,
        anchor_id="aaaa1111",
        compare_window=TimeRange(start=now, end=now),
        top_diffs=[
            DiffEntry(
                signal="template:appeared:Foo", kind="template",
                anchor_val=0, current_val=10,
                delta_pct=None, severity="HIGH",
            )
        ],
        outcome="resolved",
        engineer_confirmed_reason="rolled back",
        signal_embedding=[0.42] * 1024,
    )


# ---- meta ------------------------------------------------------------------


def test_healthz_is_open(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_SKILL_TOKEN", "secret")
    rsp = client.get("/healthz")
    assert rsp.status_code == 200
    assert rsp.json() == {"status": "ok"}


# ---- auth ------------------------------------------------------------------


def test_protected_route_401_without_token(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_SKILL_TOKEN", "secret")
    rsp = client.get("/anchors")
    assert rsp.status_code == 401


def test_protected_route_401_with_wrong_token(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_SKILL_TOKEN", "secret")
    rsp = client.get("/anchors", headers={"Authorization": "Bearer nope"})
    assert rsp.status_code == 401


def test_protected_route_200_with_correct_token(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_SKILL_TOKEN", "secret")
    monkeypatch.setattr(skill_server.agent, "all_anchors", lambda: [])
    rsp = client.get("/anchors", headers={"Authorization": "Bearer secret"})
    assert rsp.status_code == 200
    assert rsp.json() == []


def test_protected_route_open_when_token_unset(client, monkeypatch):
    """When ANCHOR_SKILL_TOKEN is empty, no auth header is required.

    Convenient for localhost dev; flagged as REQUIRED to set in deploy README.
    """
    monkeypatch.delenv("ANCHOR_SKILL_TOKEN", raising=False)
    monkeypatch.setattr(skill_server.agent, "all_anchors", lambda: [])
    rsp = client.get("/anchors")
    assert rsp.status_code == 200


def test_token_check_is_per_request(client, monkeypatch):
    """B1: rotating ANCHOR_SKILL_TOKEN must take effect without restart."""
    monkeypatch.setattr(skill_server.agent, "all_anchors", lambda: [])
    monkeypatch.setenv("ANCHOR_SKILL_TOKEN", "first")
    assert client.get("/anchors", headers={"Authorization": "Bearer first"}).status_code == 200
    monkeypatch.setenv("ANCHOR_SKILL_TOKEN", "second")
    assert client.get("/anchors", headers={"Authorization": "Bearer first"}).status_code == 401
    assert client.get("/anchors", headers={"Authorization": "Bearer second"}).status_code == 200


# ---- compare happy path ----------------------------------------------------


def test_compare_happy_path(client, monkeypatch, fake_anchor, fake_drift):
    monkeypatch.delenv("ANCHOR_SKILL_TOKEN", raising=False)
    cr = CompareResult(
        anchor=fake_anchor,
        drift=fake_drift,
        top_diffs=fake_drift.top_diffs,
        summary="things broke",
        hypothesis="deploy regression",
        drill_in_spl="search index=main error",
        recalled=((fake_drift, 0.99),),
    )
    monkeypatch.setattr(skill_server.agent, "compare", lambda *a, **kw: cr)
    rsp = client.post(
        "/compare",
        json={"start": "2026-06-14T00:00:00Z", "end": "2026-06-14T12:00:00Z"},
    )
    assert rsp.status_code == 200
    body = rsp.json()
    assert body["summary"] == "things broke"
    assert body["hypothesis"] == "deploy regression"
    # B3: embeddings must be stripped from recalled drifts
    assert "signal_embedding" not in body["recalled"][0]["drift"]
    assert "signal_embedding" not in body["drift"]


# ---- naive datetime normalisation -----------------------------------------


def test_compare_accepts_naive_datetime(client, monkeypatch, fake_anchor, fake_drift):
    """B2: naive ISO strings get silently promoted to UTC, request succeeds."""
    monkeypatch.delenv("ANCHOR_SKILL_TOKEN", raising=False)
    captured = {}

    def _fake_compare(anchor_id, start, end, **kw):
        captured["start"] = start
        captured["end"] = end
        return CompareResult(
            anchor=fake_anchor, drift=fake_drift,
            top_diffs=[], summary="ok", hypothesis=None,
            drill_in_spl=None, recalled=(),
        )

    monkeypatch.setattr(skill_server.agent, "compare", _fake_compare)
    rsp = client.post(
        "/compare",
        json={"start": "2026-06-14T00:00:00", "end": "2026-06-14T12:00:00"},
    )
    assert rsp.status_code == 200
    assert captured["start"].tzinfo is not None
    assert captured["end"].tzinfo is not None


# ---- recall strips embedding -----------------------------------------------


def test_recall_strips_signal_embedding(client, monkeypatch, fake_drift):
    monkeypatch.delenv("ANCHOR_SKILL_TOKEN", raising=False)
    monkeypatch.setattr(
        skill_server, "recall_similar_drifts",
        lambda signals, k=3, min_similarity=0.15: [(fake_drift, 0.7)],
    )
    rsp = client.post("/recall", json={"signals": ["template:Foo"], "k": 3})
    assert rsp.status_code == 200
    body = rsp.json()
    assert body[0]["similarity"] == 0.7
    assert "signal_embedding" not in body[0]["drift"]
