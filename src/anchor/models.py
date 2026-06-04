"""Pydantic models for fingerprints, anchors, drifts, and signal weights."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

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
    agent_hypothesis: Optional[str] = None
    engineer_confirmed_reason: Optional[str] = None
    outcome: Outcome = "unknown"
    suggested_spl: Optional[str] = None


class SignalWeight(BaseModel):
    signal_name: str  # matches DiffEntry.signal
    weight: float = 1.0
    confirmed_count: int = 0
    false_positive_count: int = 0
    last_updated: Optional[datetime] = None


class NarratorResponse(BaseModel):
    summary: str
    hypothesis: Optional[str] = None
    drill_in_spl: Optional[str] = None
