"""Pydantic models for fingerprints, anchors, drifts, and signal weights."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["LOW", "MEDIUM", "HIGH"]
Outcome = Literal["resolved", "ongoing", "false_positive", "unknown"]


class TimeRange(BaseModel):
    start: datetime
    end: datetime


class Scope(BaseModel):
    indexes: list[str] = Field(default_factory=lambda: ["main"])
    sourcetypes: list[str] = Field(default_factory=list)  # empty = all


class LogPattern(BaseModel):
    template: str  # punct or drain3 signature
    frequency_pct: float
    example_raw: str
    sourcetype: str
    count: int


class MetricStats(BaseModel):
    p50: float
    p95: float
    p99: float
    mean: float
    stddev: float


class Fingerprint(BaseModel):
    event_volume: dict = Field(default_factory=dict)
    # { per_source: {sourcetype: count}, total: int, hourly_profile: [24 floats] }

    log_patterns: list[LogPattern] = Field(default_factory=list)  # top N
    error_rates: dict = Field(default_factory=dict)
    # { sourcetype: { error_count, warn_count, total } }

    key_metrics: dict[str, MetricStats] = Field(default_factory=dict)
    top_hosts: list[dict] = Field(default_factory=list)  # [{host, event_count}]


class Anchor(BaseModel):
    id: str
    name: str
    created_at: datetime
    created_by: str
    time_range: TimeRange
    scope: Scope
    version: int = 1
    fingerprint: Fingerprint


class DiffEntry(BaseModel):
    signal: str  # e.g. "volume:web", "template:PaymentTimeout", "metric:latency_p99"
    kind: Literal["volume", "template", "metric"]
    anchor_val: float | str | None
    current_val: float | str | None
    delta_pct: float | None = None
    severity: Severity
    note: str = ""  # short human-readable hint


class DriftRecord(BaseModel):
    id: str
    timestamp: datetime
    anchor_id: str
    compare_window: TimeRange
    top_diffs: list[DiffEntry]
    agent_hypothesis: str | None = None
    engineer_confirmed_reason: str | None = None
    outcome: Outcome = "unknown"
    suggested_spl: str | None = None
    # Optional: precomputed embedding of the drift's signal set (Qwen
    # text-embedding-v3 = 1024 dims). Populated when ANCHOR_SEMANTIC_RECALL=1.
    # Stored in the KV row so semantic recall has O(N) cosine instead of
    # re-embedding every past drift on every compare.
    signal_embedding: list[float] | None = None


class SignalWeight(BaseModel):
    # NOTE: field is named `signal_name` (not `signal`) to keep KV documents
    # compatible with rows written by earlier versions. DiffEntry uses
    # `.signal`; do not rename either without a KV migration.
    signal_name: str  # matches DiffEntry.signal
    weight: float = 1.0
    confirmed_count: int = 0
    false_positive_count: int = 0
    last_updated: datetime | None = None
    # Memory loop: track how often we see this signal and when it last fired.
    # Used by `anchor learned` and by weight decay (older weights drift back to 1.0).
    total_appearances: int = 0
    last_used_at: datetime | None = None


class NarratorResponse(BaseModel):
    summary: str
    hypothesis: str | None = None
    drill_in_spl: str | None = None


# ---- Deep investigation (function-calling planner) -------------------------


class InvestigationStep(BaseModel):
    """One iteration of the planner loop: the tool call it chose plus the
    truncated observation that came back."""

    n: int
    thought: str | None = None  # planner's free-text rationale, if any
    tool: str
    args: dict
    observation: str  # JSON-serialized + truncated for display


class InvestigationResult(BaseModel):
    """Output of a deep investigation: the full reasoning trace plus the
    planner's final structured conclusion."""

    steps: list[InvestigationStep] = Field(default_factory=list)
    summary: str
    hypothesis: str | None = None
    evidence: list[str] = Field(default_factory=list)
    confidence: float | None = None
    truncated: bool = False  # True if max_steps hit before a final answer
