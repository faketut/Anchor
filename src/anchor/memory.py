"""Memory layer: persistence for anchors, drift history, and signal weights."""
from __future__ import annotations

import getpass
import json
import uuid
from datetime import datetime, timezone

from .models import Anchor, DiffEntry, DriftRecord, Fingerprint, Outcome, Scope, SignalWeight, TimeRange
from .splunk_client import ensure_collections, kv_all, kv_get, kv_insert, kv_query, kv_update

# ---- Anchors ---------------------------------------------------------------


def save_anchor(name: str, start: datetime, end: datetime, scope: Scope, fp: Fingerprint) -> Anchor:
    ensure_collections()
    anchor = Anchor(
        id=str(uuid.uuid4()),
        name=name,
        created_at=datetime.now(timezone.utc),
        created_by=getpass.getuser(),
        time_range=TimeRange(start=start, end=end),
        scope=scope,
        fingerprint=fp,
    )
    doc = json.loads(anchor.model_dump_json())
    doc["_key"] = anchor.id
    kv_insert("anchors", doc)
    return anchor


def get_anchor(anchor_id: str) -> Anchor | None:
    doc = kv_get("anchors", anchor_id)
    if not doc:
        return None
    return Anchor.model_validate(doc)


def list_anchors() -> list[Anchor]:
    return [Anchor.model_validate(d) for d in kv_all("anchors")]


def latest_anchor() -> Anchor | None:
    anchors = list_anchors()
    if not anchors:
        return None
    return max(anchors, key=lambda a: a.created_at)


# ---- Drift history ---------------------------------------------------------


def save_drift(
    anchor_id: str,
    compare_window: TimeRange,
    top_diffs: list[DiffEntry],
    hypothesis: str | None,
    suggested_spl: str | None,
) -> DriftRecord:
    ensure_collections()
    rec = DriftRecord(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        anchor_id=anchor_id,
        compare_window=compare_window,
        top_diffs=top_diffs,
        agent_hypothesis=hypothesis,
        suggested_spl=suggested_spl,
        outcome="unknown",
    )
    doc = json.loads(rec.model_dump_json())
    doc["_key"] = rec.id
    kv_insert("drift_history", doc)
    return rec


def get_drift(drift_id: str) -> DriftRecord | None:
    doc = kv_get("drift_history", drift_id)
    return DriftRecord.model_validate(doc) if doc else None


def update_drift_outcome(drift_id: str, outcome: Outcome, reason: str | None) -> DriftRecord | None:
    rec = get_drift(drift_id)
    if not rec:
        return None
    rec.outcome = outcome
    rec.engineer_confirmed_reason = reason
    doc = json.loads(rec.model_dump_json())
    kv_update("drift_history", drift_id, doc)
    return rec


def list_drifts(*, outcome: Outcome | None = None, limit: int = 50) -> list[DriftRecord]:
    query = {"outcome": outcome} if outcome else None
    docs = kv_query("drift_history", query)
    drifts = [DriftRecord.model_validate(d) for d in docs]
    drifts.sort(key=lambda r: r.timestamp, reverse=True)
    return drifts[:limit]


# ---- Signal weights --------------------------------------------------------

WEIGHT_DELTA = 0.1
WEIGHT_MIN = 0.1
WEIGHT_MAX = 3.0


def get_weights() -> dict[str, SignalWeight]:
    ensure_collections()
    out: dict[str, SignalWeight] = {}
    for d in kv_all("signal_weights"):
        try:
            w = SignalWeight.model_validate(d)
            out[w.signal_name] = w
        except Exception:
            continue
    return out


def _upsert_weight(name: str, delta: float, confirmed_inc: int, fp_inc: int) -> SignalWeight:
    existing = kv_query("signal_weights", {"signal_name": name})
    if existing:
        w = SignalWeight.model_validate(existing[0])
        w.weight = max(WEIGHT_MIN, min(WEIGHT_MAX, w.weight + delta))
        w.confirmed_count += confirmed_inc
        w.false_positive_count += fp_inc
        w.last_updated = datetime.now(timezone.utc)
        key = existing[0].get("_key")
        kv_update("signal_weights", key, json.loads(w.model_dump_json()))
        return w
    w = SignalWeight(
        signal_name=name,
        weight=max(WEIGHT_MIN, min(WEIGHT_MAX, 1.0 + delta)),
        confirmed_count=confirmed_inc,
        false_positive_count=fp_inc,
        last_updated=datetime.now(timezone.utc),
    )
    doc = json.loads(w.model_dump_json())
    kv_insert("signal_weights", doc)
    return w


def apply_feedback(drift: DriftRecord, outcome: Outcome) -> None:
    """Update signal weights for each diff in the drift based on outcome."""
    if outcome == "resolved":
        for d in drift.top_diffs:
            _upsert_weight(d.signal, +WEIGHT_DELTA, confirmed_inc=1, fp_inc=0)
    elif outcome == "false_positive":
        for d in drift.top_diffs:
            _upsert_weight(d.signal, -WEIGHT_DELTA, confirmed_inc=0, fp_inc=1)
    # unknown / ongoing → no weight change


# ---- Blind spots -----------------------------------------------------------


def recurring_blind_spots(min_count: int = 3) -> list[tuple[str, int]]:
    """Signals appearing in ≥min_count unresolved drifts."""
    counts: dict[str, int] = {}
    for drift in list_drifts(outcome="unknown", limit=500):
        for d in drift.top_diffs:
            counts[d.signal] = counts.get(d.signal, 0) + 1
    return sorted([(s, c) for s, c in counts.items() if c >= min_count], key=lambda x: -x[1])
