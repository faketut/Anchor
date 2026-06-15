"""Diff engine: pure functions comparing an anchor fingerprint to a current one."""
from __future__ import annotations

import hashlib

from .models import DiffEntry, Fingerprint, Severity, SignalWeight

# Severity thresholds (after weight multiplication)
HIGH_DELTA = 200.0  # +/- 200% change
MED_DELTA = 100.0
LOW_DELTA = 50.0


def _severity_from_delta(abs_pct: float) -> Severity:
    if abs_pct >= HIGH_DELTA:
        return "HIGH"
    if abs_pct >= MED_DELTA:
        return "MEDIUM"
    return "LOW"


def _pct_change(anchor: float, current: float) -> float | None:
    """Percent change from anchor to current. ``None`` means "new" / undefined
    (anchor was zero and current is positive). Callers should treat ``None``
    as HIGH severity and render it as ``new`` in UI rather than as a
    misleading magic number.
    """
    if anchor == 0:
        return None if current > 0 else 0.0
    return ((current - anchor) / anchor) * 100.0


# ---- Volume diff -----------------------------------------------------------


def volume_diff(anchor: Fingerprint, current: Fingerprint) -> list[DiffEntry]:
    out: list[DiffEntry] = []
    a_src = anchor.event_volume.get("per_source", {})
    c_src = current.event_volume.get("per_source", {})
    for src in set(a_src) | set(c_src):
        a = float(a_src.get(src, 0))
        c = float(c_src.get(src, 0))
        if a == 0 and c == 0:
            continue
        delta = _pct_change(a, c)
        # Newly-appeared source: severity is HIGH and delta_pct is None
        # (we don't fabricate a magic percent for divide-by-zero).
        if delta is None:
            out.append(
                DiffEntry(
                    signal=f"volume:{src}",
                    kind="volume",
                    anchor_val=a,
                    current_val=c,
                    delta_pct=None,
                    severity="HIGH",
                    note="new sourcetype",
                )
            )
            continue
        sev = _severity_from_delta(abs(delta))
        out.append(
            DiffEntry(
                signal=f"volume:{src}",
                kind="volume",
                anchor_val=a,
                current_val=c,
                delta_pct=round(delta, 1),
                severity=sev,
                note="",
            )
        )
    return out


# ---- Template diff ---------------------------------------------------------


def template_diff(anchor: Fingerprint, current: Fingerprint) -> list[DiffEntry]:
    a_map = {p.template: p for p in anchor.log_patterns}
    c_map = {p.template: p for p in current.log_patterns}
    out: list[DiffEntry] = []

    # Appeared (in current, not in anchor) — high signal
    for t in set(c_map) - set(a_map):
        p = c_map[t]
        out.append(
            DiffEntry(
                signal=f"template:appeared:{_short(t)}",
                kind="template",
                anchor_val=0.0,
                current_val=p.count,
                delta_pct=None,
                severity="HIGH" if p.count > 10 else "MEDIUM",
                note=f"new pattern ({p.sourcetype}): {p.example_raw[:80]}",
            )
        )

    # Disappeared (in anchor, not in current)
    for t in set(a_map) - set(c_map):
        p = a_map[t]
        out.append(
            DiffEntry(
                signal=f"template:disappeared:{_short(t)}",
                kind="template",
                anchor_val=p.count,
                current_val=0.0,
                delta_pct=-100.0,
                severity="MEDIUM",
                note=f"missing pattern ({p.sourcetype}): {p.example_raw[:80]}",
            )
        )

    # Shifted (in both, frequency moved >2x)
    for t in set(a_map) & set(c_map):
        a_freq = a_map[t].frequency_pct
        c_freq = c_map[t].frequency_pct
        if a_freq == 0 and c_freq == 0:
            continue
        delta = _pct_change(a_freq, c_freq)
        if delta is None or abs(delta) < LOW_DELTA:
            continue
        out.append(
            DiffEntry(
                signal=f"template:shifted:{_short(t)}",
                kind="template",
                anchor_val=round(a_freq, 3),
                current_val=round(c_freq, 3),
                delta_pct=round(delta, 1),
                severity=_severity_from_delta(abs(delta)),
                note=f"freq shift ({a_map[t].sourcetype})",
            )
        )

    return out


def _short(template: str, n: int = 32) -> str:
    """Short, stable, collision-resistant signal-id fragment from a template.

    Two distinct templates that share a 32-char prefix used to collapse to
    the same signal name; now we append a 6-char MD5 suffix so each template
    gets a unique id.
    """
    suffix = hashlib.md5(template.encode("utf-8", errors="replace")).hexdigest()[:6]
    head = template[:n] if len(template) <= n else template[:n] + "..."
    return f"{head}#{suffix}"


# ---- Metric diff -----------------------------------------------------------


def metric_diff(anchor: Fingerprint, current: Fingerprint) -> list[DiffEntry]:
    out: list[DiffEntry] = []
    for name, a_stats in anchor.key_metrics.items():
        c_stats = current.key_metrics.get(name)
        if c_stats is None:
            continue
        for pct in ("p50", "p95", "p99"):
            a_val = getattr(a_stats, pct)
            c_val = getattr(c_stats, pct)
            if a_val == 0 and c_val == 0:
                continue
            delta = _pct_change(a_val, c_val)
            if delta is None or abs(delta) < LOW_DELTA:
                continue
            out.append(
                DiffEntry(
                    signal=f"metric:{name}:{pct}",
                    kind="metric",
                    anchor_val=round(a_val, 3),
                    current_val=round(c_val, 3),
                    delta_pct=round(delta, 1),
                    severity=_severity_from_delta(abs(delta)),
                    note="",
                )
            )
    return out


# ---- Combine + rank --------------------------------------------------------


SEV_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def diff_all(
    anchor: Fingerprint,
    current: Fingerprint,
    weights: dict[str, SignalWeight] | None = None,
    *,
    limit: int = 20,
) -> list[DiffEntry]:
    """Produce a ranked list of diffs, severity-weighted by signal_weights."""
    weights = weights or {}
    entries = volume_diff(anchor, current) + template_diff(anchor, current) + metric_diff(anchor, current)

    def rank(e: DiffEntry) -> float:
        base = SEV_ORDER[e.severity]
        w = weights.get(e.signal, SignalWeight(signal_name=e.signal)).weight
        # Also factor in delta magnitude as a tiebreaker
        mag = abs(e.delta_pct or 0.0) / 100.0
        return base * w + mag * 0.01

    entries.sort(key=rank, reverse=True)
    return entries[:limit]
