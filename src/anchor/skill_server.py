"""Minimal HTTP shim that exposes Anchor as a Qwen Application Center skill.

The OpenAPI spec at `deploy/qwen_skill/anchor-skill.yaml` defines the contract
this shim implements. Routes mirror the MCP server but over plain HTTP +
Bearer auth so Qwen Cloud's Custom Skill mechanism can invoke them.

Run locally:

    pip install -e '.[skill]'
    ANCHOR_SKILL_TOKEN=secret uvicorn anchor.skill_server:app --host 0.0.0.0 --port 8080

On Alibaba Cloud ECS: same command, then upload `anchor-skill.yaml` (after
editing `servers[0].url` to your ECS public address) to:
  Qwen Cloud Console > Application Center > Custom Skills > Create
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

try:  # pragma: no cover  — optional dependency
    from fastapi import Depends, FastAPI, HTTPException, Query
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from pydantic import BaseModel, Field
except ModuleNotFoundError as exc:  # pragma: no cover
    raise ModuleNotFoundError(
        "Skill server requires the 'skill' extra. Install with:\n"
        "    pip install -e '.[skill]'"
    ) from exc

from . import agent
from .memory import recall_similar_drifts
from .models import Outcome, Scope


# ---- auth ------------------------------------------------------------------


_EXPECTED_TOKEN = os.getenv("ANCHOR_SKILL_TOKEN", "")
_bearer = HTTPBearer(auto_error=False)


def _require_token(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    """Reject if ANCHOR_SKILL_TOKEN is set and the request doesn't match it.

    When the env var is empty we run open — convenient for `localhost` dev,
    REQUIRED to be set on any internet-reachable host.
    """
    if not _EXPECTED_TOKEN:
        return
    if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != _EXPECTED_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


# ---- request models --------------------------------------------------------


class CaptureRequest(BaseModel):
    name: str
    start: datetime
    end: datetime
    indexes: list[str] = Field(default_factory=lambda: ["main"])
    sourcetypes: list[str] = Field(default_factory=list)
    metrics: list[str] | None = None


class CompareRequest(BaseModel):
    anchor_id: str | None = None
    start: datetime
    end: datetime
    focus: str | None = None
    metrics: list[str] | None = None
    provider: str | None = None
    deep: bool = False
    max_steps: int | None = None


class RecallRequest(BaseModel):
    signals: list[str]
    k: int = 3
    min_similarity: float = 0.15


class FeedbackRequest(BaseModel):
    drift_id: str
    outcome: Outcome
    reason: str | None = None


# ---- app -------------------------------------------------------------------


app = FastAPI(
    title="Anchor SRE MemoryAgent",
    description="Healthy-baseline drift agent for Splunk, exposed as a Qwen Application Center skill.",
    version="1.0.0",
)


def _compare_to_dict(cr) -> dict[str, Any]:
    return {
        "anchor": cr.anchor.model_dump(mode="json"),
        "drift": cr.drift.model_dump(mode="json"),
        "summary": cr.summary,
        "hypothesis": cr.hypothesis,
        "drill_in_spl": cr.drill_in_spl,
        "top_diffs": [d.model_dump(mode="json") for d in cr.top_diffs],
        "recalled": [
            {"drift": d.model_dump(mode="json"), "similarity": round(sim, 3)}
            for d, sim in cr.recalled
        ],
    }


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/anchors", dependencies=[Depends(_require_token)])
def list_anchors() -> list[dict]:
    return [a.model_dump(mode="json") for a in agent.all_anchors()]


@app.post("/anchors", dependencies=[Depends(_require_token)])
def capture_anchor(req: CaptureRequest) -> dict:
    scope = Scope(indexes=req.indexes, sourcetypes=req.sourcetypes)
    a = agent.capture_anchor(
        name=req.name, start=req.start, end=req.end,
        scope=scope, metric_fields=req.metrics,
    )
    return a.model_dump(mode="json")


@app.post("/compare", dependencies=[Depends(_require_token)])
def compare(req: CompareRequest) -> dict:
    if req.deep:
        base, invest = agent.deep_compare(
            req.anchor_id, req.start, req.end,
            focus=req.focus, metric_fields=req.metrics,
            provider=req.provider, max_steps=req.max_steps,
        )
        out = _compare_to_dict(base)
        out["investigation"] = invest.model_dump(mode="json")
        return out
    cr = agent.compare(
        req.anchor_id, req.start, req.end,
        focus=req.focus, metric_fields=req.metrics, provider=req.provider,
    )
    return _compare_to_dict(cr)


@app.post("/recall", dependencies=[Depends(_require_token)])
def recall(req: RecallRequest) -> list[dict]:
    rows = recall_similar_drifts(req.signals, k=req.k, min_similarity=req.min_similarity)
    return [
        {"drift": d.model_dump(mode="json"), "similarity": round(sim, 3)}
        for d, sim in rows
    ]


@app.post("/feedback", dependencies=[Depends(_require_token)])
def feedback(req: FeedbackRequest) -> dict:
    updated = agent.submit_feedback(req.drift_id, req.outcome, req.reason)
    return updated.model_dump(mode="json")


@app.get("/history", dependencies=[Depends(_require_token)])
def history(
    limit: int = Query(20, ge=1, le=100),
    unresolved_only: bool = Query(False),
) -> list[dict]:
    rows = agent.list_history(unresolved_only=unresolved_only, limit=limit)
    return [r.model_dump(mode="json") for r in rows]


@app.get("/learned", dependencies=[Depends(_require_token)])
def learned() -> list[dict]:
    return [w.model_dump(mode="json") for w in agent.learned_signals()]
