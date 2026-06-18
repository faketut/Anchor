# 1 · Why a MemoryAgent for on-call

*Part 1 of the [Building Anchor](README.md) series.*

---

## The 2 a.m. problem

Every on-call engineer has had this moment: pager goes off, you open
Splunk, you stare at a wall of graphs, and the first 15–20 minutes
evaporate into the same question:

> *"Wait — what does normal even look like for this service?"*

You'd think tools would solve this by now. They don't, because they
solve adjacent problems:

- **Anomaly detection** trains on a sliding window of recent history.
  If your service has been quietly degrading for a week, "recent" is
  already drifted; the model thinks today's badness is normal.
- **LLM chatbots** answer *"is this weird?"* once, then forget. The
  next compare starts from zero.
- **Static dashboards** show you the numbers but don't say what's
  *different*. You're still the one doing the diff in your head.

Anchor's bet is that what an SRE actually wants is closer to
`git diff` than to `kibana --auto-detect`. Pick a reference state,
compare a window against it, and get a *narrative* about the delta —
not just the delta itself.

## The three memories

For that narrative to get better over time, the agent has to remember
three things. Each lives in a separate
[Splunk KV Store](https://docs.splunk.com/Documentation/Splunk/latest/Admin/AboutKVstore)
collection:

| Memory | Collection | What it does |
|---|---|---|
| What "healthy" looked like | `anchors` | A human-curated baseline. Survives raw-log retention. Diff against this, not against yesterday. |
| Which signals actually matter | `signal_weights` | Re-ranks diffs by accumulated feedback. Confirmed signals weigh more; false positives weigh less. |
| What we did about it last time | `drift_history` | Every past compare, with engineer-confirmed reasons attached. Recall the most similar one on every new compare. |

This is the MemoryAgent loop in one sentence:

> *Each `compare` reads `signal_weights` (learned ranking) and
> `drift_history` (recalled past incidents) before calling the LLM,
> then writes a new drift record. Each `feedback` updates
> `signal_weights`.*

## Where the LLM fits

The LLM is *not* the decision layer. Look at the compare lifecycle
(rendered as Mermaid in the [README](../../README.md#compare-lifecycle-with-memory)):

```
CLI → KV: load anchor + weights (apply decay)
CLI → Splunk: SPL (fingerprint queries)
CLI → CLI: diff + rank (severity × weight)         ← deterministic
CLI → KV: recall top-3 similar past drifts          ← deterministic
CLI → Qwen: ranked diffs + past incidents
Qwen → CLI: summary + hypothesis + SPL             ← LLM only here
CLI → KV: save new drift record
```

By the time Qwen sees the request, the data is already structured,
ranked, and accompanied by precedent. The LLM's job is *narration*,
not detection. That keeps the system:

- **Reproducible.** The same window always produces the same top diffs.
- **Cheap.** One Qwen call per investigation, not per data point.
- **Debuggable.** When a hypothesis is wrong, you can inspect the
  ranked diffs and decide whether the diff engine or the LLM was the
  weak link.

We'll come back to this in [post 4](04-narrator-llm-at-edge.md).

## Why on top of Splunk?

Three pragmatic reasons:

1. **Most SREs already have it.** Anchor doesn't ship a new database;
   it uses KV Store, which ships with Splunk. No Lambda, no VPC, no
   extra monthly bill.
2. **SPL is already the lingua franca** for "show me events with these
   shapes in this window". The fingerprint extractor in
   [`fingerprint.py`](../../src/anchor/fingerprint.py) is, fundamentally,
   five SPL queries.
3. **KV Store survives log retention.** Your raw logs roll off in
   90 days; your healthy anchor doesn't.

We'll look at how a single `anchor capture` call becomes one row in
KV Store in [post 2](02-fingerprint.md).

## The hackathon framing (briefly)

Anchor was built for the *Qwen Cloud × Splunk* hackathon's MemoryAgent
track. The track asks for four specific properties:

| Track-1 requirement | Anchor implementation |
|---|---|
| Persistent memory | three KV Store collections, nightly OSS backups |
| Accumulates experience | `apply_feedback()` mutates `signal_weights` |
| Better decisions across sessions | `diff_all()` ranks by `severity × weight` |
| Timely forgetting | `decay_weights()` pulls weights halfway to 1.0 every 30 days |
| Bounded recall under context limit | `recall_similar_drifts()` returns top-3 |

Posts 3 and 5 dig into the math behind two of those — *timely
forgetting* and *bounded recall*.

## What you'll get from the rest of the series

- **[Post 2](02-fingerprint.md)** — how five SPL queries become a
  `Fingerprint` object and one KV row.
- **[Post 3](03-diff-and-weights.md)** — the diff engine and the
  decay-toward-1.0 trick that lets the agent forget on a schedule.
- **[Post 4](04-narrator-llm-at-edge.md)** — what we send to Qwen,
  what we get back, and why JSON-mode + low temperature beat free-text.
- **[Post 5](05-planner-react-loop.md)** — the optional `--deep`
  function-calling planner, with a real transcript.
- **[Post 6](06-deploy-alibaba-cloud.md)** — three commands to bring
  the backend up on Alibaba Cloud ECS, with OSS backups.

---

*Next: [The fingerprint — turning a healthy week into a row in
KV Store →](02-fingerprint.md)*
