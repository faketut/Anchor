# 3 · The diff: ranking severity by what we've learned matters

*Part 3 of the [Building Anchor](README.md) series.*

---

The diff engine ([`diff.py`](../../src/anchor/diff.py)) is the most
boring file in the repo on purpose. It's ~250 lines of pure functions
with zero LLM calls, zero network I/O, and zero hidden state. Given an
anchor fingerprint and a current one, it returns a ranked list of
`DiffEntry` rows. That's it.

The interesting part is what gets multiplied on top of those rows
right before ranking.

## Three diffs in, ranked list out

```python
def diff_all(anchor, current, weights=None, *, limit=20):
    weights = weights or {}
    entries = (
        volume_diff(anchor, current)
        + template_diff(anchor, current)
        + metric_diff(anchor, current)
    )
    def rank(e):
        base = SEV_ORDER[e.severity]
        w = weights.get(e.signal, SignalWeight(signal_name=e.signal)).weight
        mag = abs(e.delta_pct or 0.0) / 100.0
        return base * w + mag * 0.01
    entries.sort(key=rank, reverse=True)
    return entries[:limit]
```

That `base * w + mag * 0.01` is the entire learned-ranking story:

- `base` is a 1 / 2 / 3 score from `LOW` / `MEDIUM` / `HIGH`.
- `w` is the learned weight for this signal (default 1.0, floor 0.1,
  cap 3.0).
- `mag` is a tiny tiebreaker so a 500% change ranks above a 51% change
  at the same severity tier.

The result: if `template:payment_4xx_upstream` has been *confirmed*
five times in the last quarter, its `w` is around 1.5. When it shows
up again at MEDIUM severity, it ranks ahead of a HIGH-severity
`volume:foo` change with `w = 1.0`. That's the system telling you:
*"You've cared about this before. Look here first."*

## The three classes of diff

### Volume diff

Per-sourcetype event counts. Two interesting edge cases:

```python
if a == 0 and c == 0:
    continue                        # both zero — not interesting
delta = _pct_change(a, c)
if delta is None:                   # anchor was 0, current > 0
    out.append(DiffEntry(..., delta_pct=None, severity="HIGH",
                         note="new sourcetype"))
```

The "anchor was 0, current is positive" case used to return some
magic percent. That's been wrong since the first review — there's no
honest percent change from zero. The fix:

> **Return `None` for delta_pct and have the renderer print `new`
> instead of a fabricated number.**

The diff engine's only job is to surface signal; lying about
divisions-by-zero adds noise.

### Template diff

Three sub-cases against the `log_patterns` list from
[post 2](02-fingerprint.md):

| Set operation | Signal name | Severity |
|---|---|---|
| in current, not in anchor | `template:appeared:<short>` | HIGH if `count > 10`, else MEDIUM |
| in anchor, not in current | `template:disappeared:<short>` | MEDIUM |
| in both, frequency shifted ≥ 50% | `template:shifted:<short>` | derived from delta |

The `<short>` is a stable id. It's the first 32 chars of the template
plus a 6-char MD5 suffix:

```python
def _short(template: str, n: int = 32) -> str:
    suffix = hashlib.md5(
        template.encode("utf-8", errors="replace"),
        usedforsecurity=False,
    ).hexdigest()[:6]
    head = template[:n] if len(template) <= n else template[:n] + "..."
    return f"{head}#{suffix}"
```

Two distinct templates that share a 32-char prefix used to collapse
into the same signal name (and therefore the same learned weight).
The MD5 suffix fixes that without losing the human-readable head.
(`usedforsecurity=False` placates Bandit; MD5 here is a hash, not a
crypto primitive.)

### Metric diff

For each metric named in `--metric latency_ms`, we already captured
p50/p95/p99 in the fingerprint. The diff compares each percentile
individually:

```python
for pct in ("p50", "p95", "p99"):
    a_val = getattr(a_stats, pct)
    c_val = getattr(c_stats, pct)
    delta = _pct_change(a_val, c_val)
    if delta is None or abs(delta) < LOW_DELTA:
        continue
    out.append(DiffEntry(signal=f"metric:{name}:{pct}", ...))
```

That `< LOW_DELTA` (50%) filter is intentional. A p95 that moved 12%
is statistical noise on a one-day window; we don't want to fill the
top diffs with it.

## The weights: how Anchor learns

Three constants govern the entire feedback loop
([`memory.py`](../../src/anchor/memory.py)):

```python
WEIGHT_DELTA = 0.1     # +0.1 on confirmed, -0.2 on false_positive
WEIGHT_MIN   = 0.1     # never zero — a signal can always recover
WEIGHT_MAX   = 3.0     # never dominant — diversity matters
```

When you run `anchor feedback <id> --outcome resolved`, every signal
in that drift's `top_diffs` gets `weight += 0.1`. On
`--outcome false_positive`, every signal gets `weight -= 0.2` (the
asymmetry is deliberate — false positives are more painful than missed
catches, so the penalty bites harder).

That alone would be enough to *learn*. The harder problem is
**forgetting**.

## Timely forgetting: weights decay halfway every 30 days

The Track-1 hackathon requirement says *"timely forgetting of
outdated information"*. Anchor implements that as exponential decay
toward the neutral value 1.0:

```python
DECAY_HALF_LIFE_DAYS = 30.0

def decay_weights(now, half_life_days=DECAY_HALF_LIFE_DAYS):
    skip_cutoff = now - timedelta(hours=DECAY_SKIP_RECENT_HOURS)
    for d in kv_all("signal_weights"):
        w = SignalWeight.model_validate(d)
        if w.last_updated and w.last_updated > skip_cutoff:
            continue                                # too fresh, don't decay
        age_days = (now - w.last_updated).total_seconds() / 86400.0
        factor = 0.5 ** (age_days / half_life_days)
        new_weight = 1.0 + (w.weight - 1.0) * factor
        ...
```

Read that line by line:

- **`factor = 0.5 ** (age_days / half_life_days)`** — classic
  half-life. After 30 idle days, factor is 0.5. After 60 days, 0.25.
  After 90 days, 0.125.
- **`new_weight = 1.0 + (w.weight - 1.0) * factor`** — pulls the
  weight *toward* 1.0, never past it. A weight at 1.5 decays to
  1.25 at 30 days, 1.125 at 60 days, etc.
- **`w.last_updated > skip_cutoff`** — the 24-hour grace window
  prevents a freshly-confirmed signal from being immediately washed
  out by decay on the next compare.

There's a subtle invariant in the caller
([`agent.compare`](../../src/anchor/agent.py)) worth flagging:

> *Always call `get_weights()` BEFORE `bump_appearance()`.*

`get_weights()` triggers decay-and-write. `bump_appearance()` then
writes appearance counters. If you reverse them, you'd overwrite the
decayed `weight` value with a stale snapshot. The docstring on
`bump_appearance` calls this out explicitly because it's the kind of
bug a future refactor would silently reintroduce.

## A small operational detail

A previous schema didn't have `last_updated`. Rows written under that
schema can't decay (we don't know when they were last touched). Rather
than fabricate a date, `decay_weights` counts them and emits a one-shot
breadcrumb to stderr:

```
anchor: 4 signal_weights row(s) have no `last_updated` and will not
decay; run `anchor feedback` on the corresponding signal once to backfill.
```

The first `feedback` call on each backfills `last_updated`. After that
they participate in decay like everyone else. No migration script
needed — the system heals itself in normal use.

## What this looks like to the engineer

Run `anchor learned` to see the current weight table sorted by
deviation from 1.0:

```
SIGNAL                                       WEIGHT  CONFIRMED  FALSE_POS  LAST_UPDATED
template:appeared:PaymentGatewayTimeout #4a  2.10    9          0          2026-06-12 14:30Z
metric:latency_ms:p95                        1.45    4          0          2026-06-14 09:11Z
template:shifted:GC_pause_long #d2           0.62    0          3          2026-06-08 22:04Z
template:appeared:DebugLogEntry #91          0.10    0          7          2026-05-29 17:55Z
```

That table is the system's memory in human-readable form. The first
two are *learned signal — pay attention*. The last two are *learned
noise — please stop alerting on this*. Without decay, the noise rows
would stay at 0.1 forever even after the underlying issue is fixed.
With decay, they'll drift back toward 1.0 over a few months — and the
next time the same template legitimately appears, the engineer's
feedback re-bias-ifies it from scratch.

## Why this matters for the LLM

The narrator in [post 4](04-narrator-llm-at-edge.md) only sees the
top 15 diffs (`diff_all(..., limit=15)`). So the *ranking* is the most
consequential piece of state in the whole pipeline. Get the ranking
right and the LLM has a fighting chance. Get it wrong — by leaving
weights flat at 1.0 forever, say — and Qwen ends up narrating noise.

The weight table *is* the system getting smarter across sessions.

---

*Next: [The narrator — putting the LLM only at the edge
→](04-narrator-llm-at-edge.md)*

*Previous: [← The fingerprint — turning a healthy week into a row in
KV Store](02-fingerprint.md)*
