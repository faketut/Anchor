"""Orchestrator: ties fingerprint extraction, diffing, narration, and memory together."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, NamedTuple, TYPE_CHECKING

from .diff import diff_all
from .fingerprint import extract_fingerprint
from .memory import (
    apply_feedback,
    bump_appearance,
    delete_drift,
    get_anchor,
    get_drift,
    get_weights,
    latest_anchor,
    list_anchors,
    list_drifts,
    purge_drifts,
    recall_similar_drifts,
    recurring_blind_spots,
    save_anchor,
    save_drift,
    update_drift_outcome,
)
from .models import Anchor, DiffEntry, DriftRecord, Outcome, Scope, SignalWeight, TimeRange
from .narrator import narrate

if TYPE_CHECKING:  # pragma: no cover  — type-only import
    from .models import InvestigationResult, InvestigationStep


class CompareResult(NamedTuple):
    anchor: Anchor
    drift: DriftRecord
    top_diffs: list[DiffEntry]
    summary: str
    hypothesis: str | None
    drill_in_spl: str | None
    # Immutable default — NamedTuple defaults are class-level, so use a tuple
    # to avoid the shared-mutable-default footgun.
    recalled: tuple[tuple[DriftRecord, float], ...] = ()

    def to_dict(self) -> dict:
        """JSON-safe dict for HTTP / MCP responses.

        Strips `signal_embedding` from recalled drifts — 1024 floats per
        record is wasted bytes for the consumer and the planner can't use
        them anyway.
        """
        exclude = {"signal_embedding"}
        return {
            "anchor": self.anchor.model_dump(mode="json"),
            "drift": self.drift.model_dump(mode="json", exclude=exclude),
            "summary": self.summary,
            "hypothesis": self.hypothesis,
            "drill_in_spl": self.drill_in_spl,
            "top_diffs": [d.model_dump(mode="json") for d in self.top_diffs],
            "recalled": [
                {
                    "drift": d.model_dump(mode="json", exclude=exclude),
                    "similarity": round(sim, 3),
                }
                for d, sim in self.recalled
            ],
        }


# ---- ANCHOR ----------------------------------------------------------------


def capture_anchor(
    name: str,
    start: datetime,
    end: datetime,
    scope: Scope,
    metric_fields: list[str] | None = None,
) -> Anchor:
    fp = extract_fingerprint(start, end, scope, metric_fields=metric_fields)
    return save_anchor(name, start, end, scope, fp)


# ---- COMPARE ---------------------------------------------------------------


def compare(
    anchor_id: str | None,
    start: datetime,
    end: datetime,
    focus: str | None = None,
    metric_fields: list[str] | None = None,
    provider: str | None = None,
) -> CompareResult:
    anchor = get_anchor(anchor_id) if anchor_id else latest_anchor()
    if anchor is None:
        raise ValueError("No anchor found. Run `anchor capture` first.")

    # Re-extract using the anchor's own scope (apples-to-apples)
    current_fp = extract_fingerprint(
        start, end, anchor.scope, metric_fields=metric_fields or list(anchor.fingerprint.key_metrics)
    )

    weights = get_weights()
    diffs = diff_all(anchor.fingerprint, current_fp, weights=weights, limit=15)

    # Memory loop: record that these signals fired again, and recall similar
    # past drifts (resolved or false-positive) to feed into the narrator.
    bump_appearance([d.signal for d in diffs])
    recalled = recall_similar_drifts([d.signal for d in diffs], k=3)

    narration = narrate(diffs, focus, anchor.name, past_incidents=recalled, provider=provider)
    drift = save_drift(
        anchor_id=anchor.id,
        compare_window=TimeRange(start=start, end=end),
        top_diffs=diffs,
        hypothesis=narration.hypothesis,
        suggested_spl=narration.drill_in_spl,
    )
    return CompareResult(
        anchor=anchor,
        drift=drift,
        top_diffs=diffs,
        summary=narration.summary,
        hypothesis=narration.hypothesis,
        drill_in_spl=narration.drill_in_spl,
        recalled=tuple(recalled),
    )


# ---- FEEDBACK --------------------------------------------------------------


def submit_feedback(drift_id: str, outcome: Outcome, reason: str | None) -> DriftRecord:
    drift = get_drift(drift_id)
    if drift is None:
        raise ValueError(f"Drift {drift_id} not found")
    updated = update_drift_outcome(drift_id, outcome, reason)
    apply_feedback(updated or drift, outcome)
    return updated or drift


# ---- INTROSPECTION ---------------------------------------------------------


def list_history(unresolved_only: bool = False, limit: int = 50) -> list[DriftRecord]:
    if unresolved_only:
        return list_drifts(outcome="unknown", limit=limit)
    return list_drifts(limit=limit)


def blind_spots(min_count: int = 3) -> list[tuple[str, int]]:
    return recurring_blind_spots(min_count=min_count)


def all_anchors() -> list[Anchor]:
    return list_anchors()


def learned_signals() -> list[SignalWeight]:
    """All known signal weights, sorted by deviation from the 1.0 default (most learned first)."""
    weights = get_weights().values()
    return sorted(weights, key=lambda w: abs(w.weight - 1.0), reverse=True)


# ---- DESTRUCTIVE ------------------------------------------------------------


def remove_drift(drift_id: str) -> bool:
    """Delete a single drift record. Returns True if it existed."""
    return delete_drift(drift_id)


def remove_drifts(outcome: Outcome | None = None) -> int:
    """Bulk-delete drift records, optionally filtered by outcome. Returns count removed."""
    return purge_drifts(outcome=outcome)


# ---- DEEP COMPARE (function-calling investigation) -------------------------


def deep_compare(
    anchor_id: str | None,
    start: datetime,
    end: datetime,
    focus: str | None = None,
    metric_fields: list[str] | None = None,
    provider: str | None = None,
    max_steps: int | None = None,
    step_callback: "Callable[[InvestigationStep], None] | None" = None,
) -> tuple[CompareResult, "InvestigationResult"]:
    """Run a normal `compare`, then drive a function-calling planner over the
    result. Returns the original CompareResult alongside the investigation.

    `step_callback` is forwarded to the planner so callers can render each
    reasoning step as it lands (useful for CLI live trace).

    Imported lazily to avoid a hard dependency on `openai` for the base flow.
    """
    from .investigator import investigate  # local import: optional openai client

    base = compare(
        anchor_id, start, end,
        focus=focus, metric_fields=metric_fields, provider=provider,
    )
    invest = investigate(
        base, provider=provider, max_steps=max_steps, step_callback=step_callback,
    )
    return base, invest
