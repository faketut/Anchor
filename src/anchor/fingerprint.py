"""Fingerprint extractor — runs SPL against a window and produces a structured summary."""
from __future__ import annotations

import re
from datetime import datetime

from .models import Fingerprint, LogPattern, MetricStats, Scope
from .splunk_client import run_search

TOP_PATTERNS = 50
TOP_HOSTS = 20

# Allow only Splunk-safe identifier chars in scope tokens (indexes, sourcetypes,
# metric field names). The CLI is the trust boundary, but a defence-in-depth
# whitelist closes the door on SPL injection via --index 'foo;|...'.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_*\-]+$")


def _safe_token(s: str, kind: str) -> str:
    if not _TOKEN_RE.match(s):
        raise ValueError(f"unsafe {kind} token: {s!r}")
    return s


# ---- SPL builders ----------------------------------------------------------


def _index_filter(scope: Scope) -> str:
    indexes = [_safe_token(i, "index") for i in scope.indexes] or ["main"]
    sourcetypes = [_safe_token(s, "sourcetype") for s in scope.sourcetypes]
    idx = " OR ".join(f"index={i}" for i in indexes)
    if sourcetypes:
        st = " OR ".join(f"sourcetype={s}" for s in sourcetypes)
        return f"({idx}) ({st})"
    return f"({idx})"

def _spl_volume(scope: Scope) -> str:
    base = _index_filter(scope)
    return f"{base} | stats count by sourcetype"


def _spl_hourly(scope: Scope) -> str:
    base = _index_filter(scope)
    return f"{base} | bin _time span=1h | stats count by _time"


def _spl_patterns(scope: Scope) -> str:
    base = _index_filter(scope)
    # `punct` is a default field Splunk extracts: punctuation skeleton of the event.
    # Cheap, zero-deps log-template proxy. Group, count, keep an example raw.
    return (
        f"{base} | eval _punct=if(isnull(punct),\"<none>\",punct) "
        f"| stats count, values(sourcetype) as sourcetype, values(_raw) as examples by _punct "
        f"| sort -count | head {TOP_PATTERNS} "
        f"| eval example=mvindex(examples,0), sourcetype=mvindex(sourcetype,0)"
    )


def _spl_error_rates(scope: Scope) -> str:
    base = _index_filter(scope)
    return (
        f"{base} | eval _lvl=case("
        f"match(_raw,\"(?i)\\\\berror\\\\b|\\\\bfatal\\\\b|\\\\bexception\\\\b\"),\"error\","
        f"match(_raw,\"(?i)\\\\bwarn(ing)?\\\\b\"),\"warn\","
        f"true(),\"info\") "
        f"| stats count as total, "
        f"sum(eval(if(_lvl==\"error\",1,0))) as errors, "
        f"sum(eval(if(_lvl==\"warn\",1,0))) as warns "
        f"by sourcetype"
    )


def _spl_metrics(scope: Scope, metric_fields: list[str]) -> str:
    base = _index_filter(scope)
    if not metric_fields:
        return ""
    safe = [_safe_token(m, "metric") for m in metric_fields]
    aggs = ", ".join(
        f"perc50({m}) as {m}_p50, perc95({m}) as {m}_p95, perc99({m}) as {m}_p99, "
        f"avg({m}) as {m}_mean, stdev({m}) as {m}_stddev"
        for m in safe
    )
    return f"{base} | stats {aggs}"


def _spl_top_hosts(scope: Scope) -> str:
    base = _index_filter(scope)
    return f"{base} | top limit={TOP_HOSTS} host"


# ---- Extractor -------------------------------------------------------------


def extract_fingerprint(
    start: datetime,
    end: datetime,
    scope: Scope,
    *,
    metric_fields: list[str] | None = None,
) -> Fingerprint:
    metric_fields = metric_fields or []

    # Volume per source
    vol_rows = run_search(_spl_volume(scope), start, end)
    per_source = {r["sourcetype"]: int(r.get("count", 0)) for r in vol_rows if "sourcetype" in r}
    total = sum(per_source.values())

    # Hourly profile (24 buckets, hour-of-day average)
    hourly_rows = run_search(_spl_hourly(scope), start, end)
    hourly = [0.0] * 24
    counts_per_hour: dict[int, list[int]] = {h: [] for h in range(24)}
    for r in hourly_rows:
        try:
            ts = datetime.fromisoformat(r["_time"].replace("Z", "+00:00"))
            counts_per_hour[ts.hour].append(int(float(r.get("count", 0))))
        except Exception:
            continue
    for h, vals in counts_per_hour.items():
        hourly[h] = (sum(vals) / len(vals)) if vals else 0.0

    # Log templates (top N punct patterns)
    pat_rows = run_search(_spl_patterns(scope), start, end)
    patterns: list[LogPattern] = []
    for r in pat_rows:
        count = int(r.get("count", 0))
        if total > 0:
            freq = (count / total) * 100
        else:
            freq = 0.0
        patterns.append(
            LogPattern(
                template=r.get("_punct", "<none>") or "<none>",
                frequency_pct=round(freq, 4),
                example_raw=(r.get("example", "") or "")[:500],
                sourcetype=r.get("sourcetype", "") or "",
                count=count,
            )
        )

    # Error rates
    err_rows = run_search(_spl_error_rates(scope), start, end)
    error_rates = {
        r["sourcetype"]: {
            "error_count": int(float(r.get("errors", 0))),
            "warn_count": int(float(r.get("warns", 0))),
            "total": int(float(r.get("total", 0))),
        }
        for r in err_rows
        if "sourcetype" in r
    }

    # Key metrics
    key_metrics: dict[str, MetricStats] = {}
    if metric_fields:
        mrows = run_search(_spl_metrics(scope, metric_fields), start, end)
        if mrows:
            row = mrows[0]
            for m in metric_fields:
                try:
                    key_metrics[m] = MetricStats(
                        p50=float(row.get(f"{m}_p50", 0) or 0),
                        p95=float(row.get(f"{m}_p95", 0) or 0),
                        p99=float(row.get(f"{m}_p99", 0) or 0),
                        mean=float(row.get(f"{m}_mean", 0) or 0),
                        stddev=float(row.get(f"{m}_stddev", 0) or 0),
                    )
                except (TypeError, ValueError):
                    continue

    # Top hosts
    host_rows = run_search(_spl_top_hosts(scope), start, end)
    top_hosts = [
        {"host": r.get("host", ""), "event_count": int(float(r.get("count", 0)))}
        for r in host_rows
        if r.get("host")
    ]

    return Fingerprint(
        event_volume={"per_source": per_source, "total": total, "hourly_profile": hourly},
        log_patterns=patterns,
        error_rates=error_rates,
        key_metrics=key_metrics,
        top_hosts=top_hosts,
    )
