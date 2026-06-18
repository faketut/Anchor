# 4 · The narrator: putting the LLM only at the edge

*Part 4 of the [Building Anchor](README.md) series.*

---

By the time Qwen sees a request, the hard work is already done. The
diff engine ([post 3](03-diff-and-weights.md)) has ranked the top
diffs. The recall system has fetched the top-3 most similar past
incidents. The LLM's job is *narration*: turn structured rows into a
2-4 sentence summary, a hypothesis, and one drill-in SPL query.

This post walks through [`narrator.py`](../../src/anchor/narrator.py)
and the design choices that keep it cheap, reproducible, and easy to
audit.

## The full prompt, verbatim

```python
SYSTEM_PROMPT = """You are Anchor, an observability assistant for Splunk.
You are given a set of statistical diffs between a HEALTHY baseline window
(the "anchor") and a CURRENT window being investigated. You may also be
given PAST_INCIDENTS — previously-investigated drifts with confirmed
outcomes whose signals overlap with the current one.

Your job:
1. Write a 2-4 sentence SUMMARY in plain English describing what changed.
   Lead with the highest-severity diffs. Quantify deltas.
2. Propose a single best HYPOTHESIS for the likely cause class
   (e.g. "downstream service degradation", "new error class", "traffic shift",
    "deploy regression"). If a PAST_INCIDENT with outcome=resolved has high
   signal overlap, you SHOULD reference it (by its short id) and lean on its
   confirmed_reason. If the past incident was a false_positive, downweight
   your concern accordingly.
3. Suggest one DRILL_IN SPL query the engineer should run next to confirm.

Be concise. Do NOT invent diffs not in the input. Do NOT claim root cause
with certainty — use words like "likely", "suggests", "consistent with".

Respond as a JSON object with exactly these keys:
  summary (string), hypothesis (string or null), drill_in_spl (string or null).
"""
```

A few things deliberately *not* in this prompt:

- No examples / few-shot. The output schema is strict JSON; examples
  bloat the prompt without changing quality.
- No "think step by step". The deterministic core already did the
  thinking. We want narration, not chain-of-thought.
- No persona ("You are an expert SRE…"). The role is `system`; that's
  the persona. Verbose personas pull the model toward filler.
- No claim of certainty. The "use words like 'likely', 'suggests'"
  instruction is the cheapest hallucination-mitigation we have.

## What the model sees as input

The user message is JSON, not prose
([`_payload()`](../../src/anchor/narrator.py)):

```json
{
  "prompt_version": 2,
  "anchor_name": "Healthy Week",
  "diffs": [
    {
      "signal": "template:appeared:PaymentGatewayTimeout #4a",
      "kind": "template", "severity": "HIGH",
      "anchor_val": 0.0, "current_val": 148,
      "delta_pct": null,
      "note": "new pattern (_json): timeout calling stripe.payment.charge"
    },
    {
      "signal": "metric:latency_ms:p95",
      "kind": "metric", "severity": "HIGH",
      "anchor_val": 312.4, "current_val": 1247.8,
      "delta_pct": 299.4, "note": ""
    }
    // up to 15 diffs
  ],
  "past_incidents": [
    {
      "id": "7db2d8aa",
      "when": "2026-04-12T19:03Z",
      "outcome": "resolved",
      "confirmed_reason": "payment-svc 2.4.1 timeout regression, rolled back",
      "signal_overlap": 0.71,
      "signals": ["template:appeared:PaymentGatewayTimeout #4a", "metric:latency_ms:p95"]
    }
    // up to 3 past incidents
  ],
  "focus": "checkout slowness"
}
```

Three small choices worth flagging:

1. **`prompt_version: 2`** in the payload. When the prompt or schema
   changes, the version bumps. Drift records store this implicitly via
   the response shape, so audits can reproduce *"which prompt produced
   this hypothesis?"*.
2. **`anchor_val` / `current_val` are raw numbers**, not formatted
   strings. Lets the model quantify deltas without us pre-baking
   "3.0×" prose.
3. **`past_incidents` is bounded at 3.** Not 10, not "all relevant".
   The Track-1 requirement is *recalling critical memories within
   limited context*. Three is enough for grounding without crowding
   out the diffs.

## What the model has to return

```python
rsp = client.chat.completions.create(
    model=model,
    response_format={"type": "json_object"},
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": _payload(diffs, focus, anchor_name, past_incidents)},
    ],
    temperature=0.2,
)
```

The combination of `response_format={"type": "json_object"}` and
`temperature=0.2` is the whole reliability story:

- **JSON mode** means Qwen returns syntactically valid JSON every
  time. No retry loop, no markdown-fence stripping, no regex
  extraction.
- **Low temperature** keeps the narration boring in a good way. The
  same diffs produce the same summary across runs. SREs are not
  looking for creative writing.

The parsing on the other side is correspondingly mundane:

```python
data = json.loads(raw)
return NarratorResponse(
    summary=data.get("summary", "").strip() or "(empty)",
    hypothesis=(data.get("hypothesis") or None),
    drill_in_spl=(data.get("drill_in_spl") or None),
)
```

No `data["summary"]` — every key uses `.get(..., default)`. If Qwen
gets weird, we degrade to a sensible empty value instead of throwing.

## Provider abstraction (the small one)

Qwen and Gemini both expose OpenAI-compatible chat completions
endpoints. So Anchor's "multi-provider" support is one function
parameterized over base URL, API key, and model name:

```python
def _openai_compat_narrate(diffs, focus, anchor_name, *,
                           api_key, base_url, model, ...):
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=LLM_TIMEOUT_S)
    ...
```

`narrate()` is a five-line switch picking which `_openai_compat_narrate`
to call. There used to be a third branch for a hypothetical Splunk-hosted
model; it was dead code and got deleted in code review. The principle:
if no path through your code is exercised, the code is wrong.

## Where the LLM is in the bigger picture

Looking at the system overview from the [README](../../README.md#system-overview),
the LLM sits at the *edge* of the data pipeline, never in the middle:

```
Splunk → fingerprint → diff (weighted) → recall (Jaccard or cosine) → Qwen → user
                       ^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^^^   ^^^^
                       deterministic     deterministic                  narration
```

That layering buys us four things, none of which a pure-LLM agent
gets:

| Property | Why we get it |
|---|---|
| Reproducibility | Same window → same top diffs → same prompt input |
| Bounded cost | One LLM call per compare, fixed-size payload |
| Auditability | `drift_history` stores both the structured diffs *and* the prose; if Qwen was wrong, the structured data is still there |
| Graceful degradation | If Qwen is down, you still see the ranked diffs in the rendered report; the narration just says "(empty)" |

The same principle applies to the optional `--deep` planner in
[post 5](05-planner-react-loop.md): the model only gets to call
*tools that wrap deterministic code*. It never gets to make up SPL
that we then execute blind.

## What we deliberately don't do

- **No streaming.** The CLI waits for the full JSON response. Streaming
  partial JSON is a parsing headache and the time saved is dwarfed by
  the SPL queries that ran before the LLM call anyway.
- **No re-ranking by the model.** We send the top-15 already ranked.
  We don't ask the model to re-rank; we ask it to *narrate* the existing
  ranking. The diff engine is the source of truth, not Qwen.
- **No tool calls in the basic narrator.** That's the planner's job
  ([post 5](05-planner-react-loop.md)). Keeping the basic narrator
  tool-free means `anchor compare` is always one LLM round-trip and
  the latency is predictable.
- **No retries on JSON parse failure.** With JSON mode + temperature
  0.2 this hasn't happened in months of testing. If it ever does, the
  fallback returns `(empty)` and the engineer sees the structured diffs.
  Better than a hidden retry loop adding latency.

## The cost shape

For a normal `anchor compare` on the demo dataset:

| Component | Approximate cost |
|---|---|
| 5 SPL queries | ~250 ms total |
| Diff engine (pure Python) | < 10 ms |
| Recall (Jaccard over ~500 rows) | < 50 ms |
| One Qwen `qwen-plus` call | ~1.5-3 s |
| KV write of new drift record | ~30 ms |

The LLM is the dominant tail. Everything else is well below human
perception. If you wanted to speed Anchor up, you'd move from
`qwen-plus` to `qwen-turbo` — *not* refactor the pipeline.

---

*Next: [The planner — function-calling for SRE drill-down
→](05-planner-react-loop.md)*

*Previous: [← The diff — ranking severity by what we've learned
matters](03-diff-and-weights.md)*
