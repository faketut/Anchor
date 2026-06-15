"""Unit tests for the pure diff engine. No Splunk or LLM required."""
from __future__ import annotations

import pytest

from anchor.diff import diff_all, metric_diff, template_diff, volume_diff
from anchor.models import Fingerprint, LogPattern, MetricStats, SignalWeight


# ---- Fixtures --------------------------------------------------------------


def _fp(
    *,
    per_source: dict[str, int] | None = None,
    hourly: list[float] | None = None,
    patterns: list[LogPattern] | None = None,
    metrics: dict[str, MetricStats] | None = None,
) -> Fingerprint:
    total = sum((per_source or {}).values())
    return Fingerprint(
        event_volume={
            "per_source": per_source or {},
            "total": total,
            "hourly_profile": hourly or [100.0] * 24,
        },
        log_patterns=patterns or [],
        error_rates={},
        key_metrics=metrics or {},
        top_hosts=[],
    )


def _pat(template: str, freq: float, count: int, sourcetype: str = "web") -> LogPattern:
    return LogPattern(
        template=template,
        frequency_pct=freq,
        example_raw=f"example for {template}",
        sourcetype=sourcetype,
        count=count,
    )


# ---- volume_diff -----------------------------------------------------------


def test_volume_diff_flags_increase_by_delta() -> None:
    anchor = _fp(per_source={"web": 1000}, hourly=[100.0] * 24)
    current = _fp(per_source={"web": 5000}, hourly=[100.0] * 24)

    diffs = volume_diff(anchor, current)

    assert len(diffs) == 1
    d = diffs[0]
    assert d.signal == "volume:web"
    assert d.kind == "volume"
    assert d.delta_pct == 400.0
    # 400% ≥ HIGH_DELTA (200) → HIGH severity by delta alone.
    assert d.severity == "HIGH"


def test_volume_diff_handles_appeared_source() -> None:
    anchor = _fp(per_source={"web": 100})
    current = _fp(per_source={"web": 100, "auth": 50})

    diffs = {d.signal: d for d in volume_diff(anchor, current)}

    assert "volume:auth" in diffs
    assert diffs["volume:auth"].anchor_val == 0
    assert diffs["volume:auth"].current_val == 50
    # Newly-appeared sources expose delta_pct=None (no magic 1000 sentinel).
    assert diffs["volume:auth"].delta_pct is None
    assert diffs["volume:auth"].severity == "HIGH"


def test_volume_diff_skips_double_zero() -> None:
    anchor = _fp(per_source={"web": 100})
    current = _fp(per_source={"web": 100})

    # Both have zero of "auth" implicitly — should not produce an entry
    diffs = volume_diff(anchor, current)
    assert all(d.signal != "volume:auth" for d in diffs)


# ---- template_diff ---------------------------------------------------------


def test_template_diff_detects_appeared() -> None:
    anchor = _fp(patterns=[_pat("INFO :req=:", 80.0, 800)])
    current = _fp(
        patterns=[
            _pat("INFO :req=:", 70.0, 700),
            _pat("ERROR :PaymentGatewayTimeout:", 30.0, 412),
        ]
    )

    diffs = template_diff(anchor, current)
    appeared = [d for d in diffs if d.signal.startswith("template:appeared:")]

    assert len(appeared) == 1
    assert "PaymentGatewayTimeout" in appeared[0].signal
    assert appeared[0].severity == "HIGH"  # count > 10
    assert appeared[0].anchor_val == 0.0
    assert appeared[0].current_val == 412


def test_template_diff_detects_disappeared() -> None:
    anchor = _fp(
        patterns=[
            _pat("INFO :req=:", 80.0, 800),
            _pat("INFO :heartbeat:", 20.0, 200),
        ]
    )
    current = _fp(patterns=[_pat("INFO :req=:", 100.0, 1000)])

    diffs = template_diff(anchor, current)
    disappeared = [d for d in diffs if d.signal.startswith("template:disappeared:")]

    assert len(disappeared) == 1
    assert "heartbeat" in disappeared[0].signal
    assert disappeared[0].delta_pct == -100.0


def test_template_diff_detects_frequency_shift() -> None:
    anchor = _fp(patterns=[_pat("INFO :req=:", 80.0, 800), _pat("WARN :retry:", 5.0, 50)])
    current = _fp(patterns=[_pat("INFO :req=:", 30.0, 300), _pat("WARN :retry:", 50.0, 500)])

    diffs = template_diff(anchor, current)
    shifted = [d for d in diffs if d.signal.startswith("template:shifted:")]

    signals = {d.signal for d in shifted}
    assert any("INFO" in s for s in signals)
    assert any("WARN" in s for s in signals)


def test_template_diff_ignores_small_shifts() -> None:
    # 10% shift < LOW_DELTA (50%) → no diff entry
    anchor = _fp(patterns=[_pat("INFO :req=:", 80.0, 800)])
    current = _fp(patterns=[_pat("INFO :req=:", 88.0, 880)])

    diffs = template_diff(anchor, current)
    assert not [d for d in diffs if d.signal.startswith("template:shifted:")]


# ---- metric_diff -----------------------------------------------------------


def test_metric_diff_flags_p99_spike() -> None:
    anchor = _fp(metrics={"latency_ms": MetricStats(p50=80, p95=120, p99=150, mean=85, stddev=20)})
    current = _fp(metrics={"latency_ms": MetricStats(p50=90, p95=400, p99=1500, mean=200, stddev=300)})

    diffs = metric_diff(anchor, current)
    by_sig = {d.signal: d for d in diffs}

    assert "metric:latency_ms:p99" in by_sig
    p99 = by_sig["metric:latency_ms:p99"]
    assert p99.delta_pct == 900.0  # (1500-150)/150 = 900%
    assert p99.severity == "HIGH"


def test_metric_diff_skips_metrics_not_in_anchor() -> None:
    anchor = _fp(metrics={"latency_ms": MetricStats(p50=80, p95=120, p99=150, mean=85, stddev=20)})
    current = _fp(
        metrics={
            "latency_ms": MetricStats(p50=80, p95=120, p99=150, mean=85, stddev=20),
            "qps": MetricStats(p50=10, p95=20, p99=30, mean=12, stddev=5),
        }
    )

    diffs = metric_diff(anchor, current)
    assert not any("qps" in d.signal for d in diffs)


# ---- diff_all + signal weights --------------------------------------------


def test_diff_all_combines_and_ranks_by_severity() -> None:
    anchor = _fp(
        per_source={"web": 100},
        patterns=[_pat("INFO :req=:", 100.0, 100)],
        metrics={"latency_ms": MetricStats(p50=80, p95=120, p99=150, mean=85, stddev=20)},
    )
    current = _fp(
        per_source={"web": 100},  # no volume diff
        patterns=[
            _pat("INFO :req=:", 100.0, 100),
            _pat("ERROR :Timeout:", 30.0, 30),  # appeared → HIGH
        ],
        metrics={"latency_ms": MetricStats(p50=82, p95=125, p99=160, mean=87, stddev=22)},  # tiny shift
    )

    result = diff_all(anchor, current)
    assert result, "expected at least one diff"
    # HIGH signal should rank first
    assert result[0].severity == "HIGH"
    assert "appeared" in result[0].signal


def test_signal_weights_downrank_false_positive() -> None:
    """A signal previously marked false_positive (weight < 1.0) should rank lower."""
    anchor = _fp(
        patterns=[_pat("INFO :req=:", 50.0, 500), _pat("WARN :noisy:", 50.0, 500)]
    )
    # Both shift by the same magnitude → equal raw severity
    current = _fp(
        patterns=[_pat("INFO :req=:", 10.0, 100), _pat("WARN :noisy:", 90.0, 900)]
    )

    # Find the actual shifted signal names produced by the diff engine
    raw = template_diff(anchor, current)
    shifted = [d for d in raw if d.signal.startswith("template:shifted:")]
    assert len(shifted) == 2

    # Downweight the "noisy" signal heavily
    noisy_sig = next(d.signal for d in shifted if "noisy" in d.signal)
    info_sig = next(d.signal for d in shifted if "INFO" in d.signal)
    weights = {
        noisy_sig: SignalWeight(signal_name=noisy_sig, weight=0.2),
        info_sig: SignalWeight(signal_name=info_sig, weight=2.5),
    }

    ranked = diff_all(anchor, current, weights=weights)
    # INFO signal (weight 2.5) should now outrank noisy (weight 0.2)
    info_idx = next(i for i, d in enumerate(ranked) if d.signal == info_sig)
    noisy_idx = next(i for i, d in enumerate(ranked) if d.signal == noisy_sig)
    assert info_idx < noisy_idx


def test_diff_all_respects_limit() -> None:
    # Generate 30 appeared templates
    anchor = _fp()
    patterns = [_pat(f"T{i}", 1.0, 100) for i in range(30)]
    current = _fp(patterns=patterns)

    result = diff_all(anchor, current, limit=10)
    assert len(result) == 10
