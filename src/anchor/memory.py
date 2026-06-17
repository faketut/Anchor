"""Memory layer: persistence for anchors, drift history, and signal weights."""
from __future__ import annotations

import getpass
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

from .config import CONFIG
from .embedding import cosine, embed_signals
from .models import Anchor, DiffEntry, DriftRecord, Fingerprint, Outcome, Scope, SignalWeight, TimeRange
from .splunk_client import ensure_collections, kv_all, kv_delete, kv_get, kv_insert, kv_query, kv_update

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
    embedding: list[float] | None = None
    if CONFIG.semantic_recall:
        # Best-effort: embed the signal set so future recall can do cosine
        # instead of Jaccard. embed_signals already swallows failures.
        embedding = embed_signals([d.signal for d in top_diffs])
    rec = DriftRecord(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        anchor_id=anchor_id,
        compare_window=compare_window,
        top_diffs=top_diffs,
        agent_hypothesis=hypothesis,
        suggested_spl=suggested_spl,
        outcome="unknown",
        signal_embedding=embedding,
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


def delete_drift(drift_id: str) -> bool:
    """Delete a single drift record by id. Returns True if a row was removed."""
    if get_drift(drift_id) is None:
        return False
    kv_delete("drift_history", drift_id)
    return True


def purge_drifts(*, outcome: Outcome | None = None) -> int:
    """Delete all drift records, optionally filtered by outcome. Returns count removed."""
    query = {"outcome": outcome} if outcome else None
    docs = kv_query("drift_history", query)
    removed = 0
    for d in docs:
        key = d.get("_key")
        if not key:
            continue
        kv_delete("drift_history", key)
        removed += 1
    return removed


# ---- Signal weights --------------------------------------------------------

WEIGHT_DELTA = 0.1
WEIGHT_MIN = 0.1
WEIGHT_MAX = 3.0

# Memory decay: weights drift halfway back to 1.0 every `DECAY_HALF_LIFE_DAYS`
# of inactivity. Implements Track-1's "timely forgetting of outdated information".
DECAY_HALF_LIFE_DAYS = 30.0
DECAY_SKIP_RECENT_HOURS = 24.0  # don't decay weights touched in last 24h
DECAY_MIN_INTERVAL_HOURS = 1.0  # re-run decay at most once per hour per process

# Module-level guard so long-running processes still re-decay periodically
# without doing it on every single get_weights() call.
_last_decay_run: datetime | None = None


def get_weights() -> dict[str, SignalWeight]:
    ensure_collections()
    _maybe_decay_weights()
    out: dict[str, SignalWeight] = {}
    for d in kv_all("signal_weights"):
        try:
            w = SignalWeight.model_validate(d)
            out[w.signal_name] = w
        except Exception:
            continue
    return out


def _maybe_decay_weights() -> None:
    """Run decay_weights at most once per DECAY_MIN_INTERVAL_HOURS per process."""
    global _last_decay_run
    now = datetime.now(timezone.utc)
    if _last_decay_run is not None:
        elapsed = (now - _last_decay_run).total_seconds() / 3600.0
        if elapsed < DECAY_MIN_INTERVAL_HOURS:
            return
    _last_decay_run = now
    try:
        decay_weights(now)
    except Exception as e:
        # Decay is best-effort — never fail callers because of it.
        # Emit a breadcrumb so operators can spot a persistent failure.
        print(f"anchor: weight decay skipped ({e!r})", file=sys.stderr)


def decay_weights(now: datetime, half_life_days: float = DECAY_HALF_LIFE_DAYS) -> int:
    """Pull each weight halfway back to 1.0 per `half_life_days` of inactivity.

    Skips weights touched within the last DECAY_SKIP_RECENT_HOURS (so freshly
    learned weights aren't immediately washed out). Returns the count of
    weights modified. Pure-ish: no logging, no LLM, no Splunk SPL.
    """
    if half_life_days <= 0:
        return 0
    skip_cutoff = now - timedelta(hours=DECAY_SKIP_RECENT_HOURS)
    touched = 0
    for d in kv_all("signal_weights"):
        try:
            w = SignalWeight.model_validate(d)
        except Exception:
            continue
        if w.last_updated and w.last_updated > skip_cutoff:
            continue
        if w.last_updated is None:
            continue
        age_days = (now - w.last_updated).total_seconds() / 86400.0
        factor = 0.5 ** (age_days / half_life_days)
        new_weight = 1.0 + (w.weight - 1.0) * factor
        if abs(new_weight - w.weight) < 1e-6:
            continue
        w.weight = max(WEIGHT_MIN, min(WEIGHT_MAX, new_weight))
        w.last_updated = now
        key = d.get("_key") or w.signal_name
        kv_update("signal_weights", key, json.loads(w.model_dump_json()))
        touched += 1
    return touched


def bump_appearance(signals: list[str], now: datetime | None = None) -> None:
    """Record that these signals appeared in a compare's top_diffs.

    Creates a weight row at 1.0 for unseen signals so `anchor learned` can
    show "we've watched this N times but never confirmed/denied it".

    Implementation note: pulls every existing weight in a single ``kv_all``
    call, then issues at most one write per signal. The old per-signal
    ``kv_query`` did N round-trips against Splunk; on a remote ECS that
    visibly stalled ``anchor compare``.
    """
    if not signals:
        return
    now = now or datetime.now(timezone.utc)
    existing: dict[str, tuple[str | None, SignalWeight]] = {}
    for d in kv_all("signal_weights"):
        try:
            w = SignalWeight.model_validate(d)
        except Exception:
            continue
        existing[w.signal_name] = (d.get("_key"), w)
    for name in signals:
        if name in existing:
            key, w = existing[name]
            w.total_appearances += 1
            w.last_used_at = now
            if key:
                kv_update("signal_weights", key, json.loads(w.model_dump_json()))
        else:
            w = SignalWeight(
                signal_name=name,
                weight=1.0,
                total_appearances=1,
                last_used_at=now,
                last_updated=now,
            )
            kv_insert("signal_weights", json.loads(w.model_dump_json()))
            # Track so duplicate signals in one batch don't double-insert.
            existing[name] = (None, w)


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
    """Update signal weights for each diff in the drift based on outcome.

    Also opportunistically runs weight decay so long-running processes (e.g. a
    daemon issuing many `feedback` calls) still age out stale opinions.
    """
    _maybe_decay_weights()
    if outcome == "resolved":
        for d in drift.top_diffs:
            _upsert_weight(d.signal, +WEIGHT_DELTA, confirmed_inc=1, fp_inc=0)
    elif outcome == "false_positive":
        for d in drift.top_diffs:
            _upsert_weight(d.signal, -WEIGHT_DELTA, confirmed_inc=0, fp_inc=1)
    # unknown / ongoing → no weight change


# ---- Memory recall ---------------------------------------------------------


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def recall_similar_drifts(
    current_signals: list[str],
    *,
    k: int = 3,
    min_similarity: float = 0.15,
    outcomes: tuple[Outcome, ...] = ("resolved", "false_positive"),
) -> list[tuple[DriftRecord, float]]:
    """Return up to k past drifts most similar to `current_signals`.

    Ranking strategy:
      * If CONFIG.semantic_recall is on AND the embedding call succeeds for
        the current signals AND at least one past drift has a stored
        signal_embedding, rank by cosine similarity. This catches semantic
        matches that Jaccard misses (e.g. "PaymentGatewayTimeout" vs
        "upstream payment failure").
      * Otherwise (or as a fallback for drifts missing embeddings) rank by
        Jaccard overlap of signals in top_diffs.

    Only considers drifts whose outcome ∈ `outcomes` (default: resolved or
    false_positive — i.e. drifts with confirmed ground truth). This is the
    "recalling critical memories within limited context windows" capability
    for the MemoryAgent track.
    """
    if not current_signals:
        return []

    candidates = [d for d in list_drifts(limit=500) if d.outcome in outcomes]
    if not candidates:
        return []

    current_set = set(current_signals)
    use_semantic = CONFIG.semantic_recall and any(d.signal_embedding for d in candidates)
    current_embedding: list[float] | None = None
    if use_semantic:
        current_embedding = embed_signals(current_signals)
        if current_embedding is None:
            use_semantic = False  # embedding call failed — fall back

    scored: list[tuple[DriftRecord, float]] = []
    for drift in candidates:
        sim: float
        if use_semantic and drift.signal_embedding:
            sim = cosine(current_embedding, drift.signal_embedding)  # type: ignore[arg-type]
        else:
            past_signals = {d.signal for d in drift.top_diffs}
            sim = _jaccard(current_set, past_signals)
        if sim < min_similarity:
            continue
        scored.append((drift, sim))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


# ---- Blind spots -----------------------------------------------------------


def recurring_blind_spots(min_count: int = 3) -> list[tuple[str, int]]:
    """Signals appearing in ≥min_count unresolved drifts."""
    counts: dict[str, int] = {}
    for drift in list_drifts(outcome="unknown", limit=500):
        for d in drift.top_diffs:
            counts[d.signal] = counts.get(d.signal, 0) + 1
    return sorted([(s, c) for s, c in counts.items() if c >= min_count], key=lambda x: -x[1])
