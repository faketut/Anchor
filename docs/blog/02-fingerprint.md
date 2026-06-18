# 2 · The fingerprint: turning a healthy week into a row in KV Store

*Part 2 of the [Building Anchor](README.md) series.*

---

When you run

```bash
anchor capture --name "Healthy Week" \
  --from 2026-05-20T00:00:00 --to 2026-05-27T00:00:00 \
  --index main --metric latency_ms
```

…the CLI does two things: it runs **five SPL queries** against Splunk
to characterize the window, and it writes **one document** into the
`anchors` KV Store collection. This post unpacks both halves.

## What's in a fingerprint

The `Fingerprint` model
([`models.py`](../../src/anchor/models.py)) carries five fields:

| Field | What it captures | SPL flavor |
|---|---|---|
| `event_volume` | per-sourcetype counts, total, hourly profile | `stats count by sourcetype` + `bin _time span=1h` |
| `log_patterns` | top-N "shape" buckets via Splunk's built-in `punct` field | `stats count, values(_raw) by punct \| sort -count \| head 50` |
| `error_rates` | error / warn / info ratio per sourcetype | `eval _lvl=case(...) \| stats sum(eval(...))` |
| `key_metrics` | p50/p95/p99/mean/stddev for named numeric fields | `stats perc50(x) as x_p50, perc95(x) as x_p95, ...` |
| `top_hosts` | top-20 hosts by event count | `top limit=20 host` |

That's deliberately a *small* feature set. Anchor isn't trying to be an
ML platform; it's trying to capture the cheapest possible summary that
still discriminates *"yesterday looked like the baseline"* from
*"yesterday is different and here's how"*.

## Why `punct` instead of clustering

The cheapest log-template proxy in Splunk is the built-in `punct`
field — it's the punctuation skeleton of the event, computed at index
time. `[ERROR] payment 4xx upstream svc=stripe id=...` and the same
line with a different request id collapse to the same `punct`. No
clustering library, no Levenshtein, no LLM call.

That decision shows up directly in the SPL builder
([`fingerprint.py`](../../src/anchor/fingerprint.py)):

```python
def _spl_patterns(scope: Scope) -> str:
    base = _index_filter(scope)
    return (
        f"{base} | eval _punct=if(isnull(punct),\"<none>\",punct) "
        f"| stats count, values(sourcetype) as sourcetype, "
        f"       values(_raw) as examples by _punct "
        f"| sort -count | head 50 "
        f"| eval example=mvindex(examples,0), "
        f"       sourcetype=mvindex(sourcetype,0)"
    )
```

The `head 50` cap is intentional: we want the top-N representative
patterns, not every long-tail one-off. If a new pattern enters the
top-50 in a future window, that's a `template:appeared:...` signal in
[post 3](03-diff-and-weights.md)'s diff engine. If a known pattern
*falls out* of the top-50, that's `template:disappeared:...`.

## The trust boundary: SPL injection

The CLI accepts `--index foo --sourcetype bar --metric x`. Those
tokens get spliced into SPL strings. That's exactly the place a
malicious value like `'foo;|delete'` could try to escape the search
context.

Defense in depth: a whitelist of safe identifier characters, applied
to every token before it touches SPL:

```python
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_*\-]+$")

def _safe_token(s: str, kind: str) -> str:
    if not _TOKEN_RE.match(s):
        raise ValueError(f"unsafe {kind} token: {s!r}")
    return s
```

The CLI is the trust boundary, but a defence-in-depth whitelist costs
two lines and closes a footgun.

## From `Fingerprint` to KV row

The persistence layer
([`memory.py`](../../src/anchor/memory.py)) wraps the fingerprint in
an `Anchor` envelope, assigns a UUID, and writes one document:

```python
def save_anchor(name, start, end, scope, fp) -> Anchor:
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
```

Two small things worth noting:

1. **`ensure_collections()` is idempotent.** The first `anchor capture`
   on a fresh Splunk creates the three collections; subsequent calls
   are a no-op. This is what makes the `setup_ecs.sh` install in
   [post 6](06-deploy-alibaba-cloud.md) survive re-runs.
2. **`_key = anchor.id`.** KV Store auto-assigns a key if you don't,
   but we want the UUID to *be* the key so `kv_get("anchors", id)` is
   a direct lookup rather than a query.

## What an anchor looks like in JSON

Roughly:

```json
{
  "_key": "8d3a...",
  "id":   "8d3a...",
  "name": "Healthy Week",
  "created_at": "2026-05-27T18:42:11Z",
  "created_by": "fenjian",
  "time_range": {"start": "2026-05-20T00:00:00Z", "end": "2026-05-27T00:00:00Z"},
  "scope": {"indexes": ["main"], "sourcetypes": []},
  "fingerprint": {
    "event_volume":  {"per_source": {"_json": 412380}, "total": 412380, "hourly_profile": [...]},
    "log_patterns":  [{"template": "...", "frequency_pct": 18.4, "count": 75880, ...}, ...],
    "error_rates":   {"_json": {"error_count": 142, "warn_count": 503, "total": 412380}},
    "key_metrics":   {"latency_ms": {"p50": 78.1, "p95": 312.4, ...}},
    "top_hosts":     [{"host": "checkout-7d4b...", "event_count": 41280}, ...]
  }
}
```

You can inspect this in the Splunk Web UI under *Settings → Lookups →
KV store collections → `anchors`*. Useful sanity check on first
install.

## What this buys us

Two superpowers, one each for the next two posts:

- **Post 3** — every later `compare` re-runs the same five SPL queries
  on a *different* window, produces a second `Fingerprint`, and the
  diff engine subtracts the two. Pure functions, no LLM.
- **Post 4** — when the LLM eventually does see the data, it sees the
  *ranked diff*, not raw logs. That keeps the prompt small and the
  cost bounded.

## What we didn't include (and why)

- **No raw events.** Anchor stores statistics, not log payloads. PII
  stays in your indexers; the fingerprint is safe to ship anywhere.
- **No embeddings on the anchor itself.** We embed *signals* (post 3),
  not raw text. One embedding per drift, not per event.
- **No "trends".** A baseline is a single window. If you want to
  capture weekly seasonality, capture multiple baselines and pick the
  one whose `scope` matches the compare window. Simpler than
  generalizing.

---

*Next: [The diff — ranking severity by what we've learned matters
→](03-diff-and-weights.md)*

*Previous: [← Why a MemoryAgent for on-call](01-why-memoryagent.md)*
