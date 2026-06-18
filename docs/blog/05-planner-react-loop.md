# 5 · The planner: function-calling for SRE drill-down

*Part 5 of the [Building Anchor](README.md) series.*

---

`anchor compare` does one LLM round-trip and returns a narrative
([post 4](04-narrator-llm-at-edge.md)). That's enough for ~80% of
drift investigations. For the remaining 20% — where the engineer
wants the agent to *follow a thread* — there's `anchor compare --deep`.

`--deep` swaps the single Qwen call for a function-calling ReAct loop:

```
thought → tool_call → observation → thought → tool_call → observation → … → final JSON
```

The planner has read-only access to four tools. It's told to prefer
depth over breadth and stop early. A hard step cap prevents runaway
loops. This post walks through
[`investigator.py`](../../src/anchor/investigator.py).

## The four tools

| Tool | What it wraps | Why it exists |
|---|---|---|
| `recall_similar_drifts(signals, k, min_similarity)` | `memory.recall_similar_drifts` | The default first move when a signal feels familiar |
| `get_drift_details(drift_id)` | `memory.get_drift` | After recall, read a full past record before relying on it |
| `run_spl(spl, earliest, latest)` | `splunk_client.run_search` (capped at 50 rows) | Evidence-gathering: deploy logs, host breakdowns, audit |
| `list_recent_drifts(limit, outcome)` | `memory.list_drifts` | Situational awareness when nothing recalls |

A few principles in that list:

1. **Every tool wraps deterministic code.** The planner can't make up
   SPL that we then execute blind — `run_spl` goes through the same
   `splunk_client.run_search` the diff engine uses, with the same
   `max_count=50` cap.
2. **Read-only.** No tool mutates KV Store. The planner can't
   accidentally apply feedback or delete an anchor.
3. **No "give the LLM Python".** Sandboxed shell tools are powerful
   and dangerous. Four narrow tools beat one wide one.

## What the planner sees

Initial user payload
([`_initial_payload`](../../src/anchor/investigator.py)):

```json
{
  "planner_version": 1,
  "anchor_name": "Healthy Week",
  "compare_window": {"start": "...", "end": "..."},
  "initial_summary": "p95 latency tripled and a new PaymentGateway template appeared...",
  "initial_hypothesis": "downstream payment-svc degradation",
  "top_diffs": [
    {"signal": "...", "severity": "HIGH", "delta_pct": 299.4, "note": "..."}
    // up to 10
  ],
  "already_recalled": [
    {"id": "7db2d8aa", "outcome": "resolved", "similarity": 0.71}
  ]
}
```

The "initial" fields come from the regular `compare` that ran first.
The planner builds on that — it doesn't redo the diff. `already_recalled`
tells it which past incidents the narrator already saw, so it can
choose to dig deeper into one of them or look elsewhere.

## The system prompt

```
You are Anchor's deep-investigation planner.

You receive an initial CompareResult: anchor name + top diffs + an
initial narration. Your job is to deepen the investigation using the
tools provided, then return a tighter root-cause hypothesis with an
evidence chain.

Strategy:
1. If diffs contain a new template or a metric spike, call
   recall_similar_drifts on those signals to find precedents.
2. If a precedent has outcome=resolved with a confirmed_reason, call
   get_drift_details to read its full record before relying on it.
3. If you suspect a deploy/config change, call run_spl against relevant
   indexes (e.g. deploy_log, config_change, audit) within the compare
   window.
4. Stop and finalize as soon as you have a defensible hypothesis. You
   have up to 6 tool calls — prefer depth over breadth and stop early
   when evidence converges.

Tool observations are clipped at ~8 KB; if you need more, narrow the SPL.
```

Four things worth noting:

- **A numbered strategy, not free-form** — gives the model a default
  branching order. It deviates when warranted, but it has somewhere
  to start.
- **Hard cap of 6 tool calls.** Default `CONFIG.investigate_max_steps`.
  This is the difference between "agent" and "agent loop until your
  Qwen bill explodes".
- **Observations capped at 8 KB.** If `run_spl` returns a giant rowset,
  it's truncated and the planner is told to narrow the SPL. This
  prevents one fat tool call from blowing the whole context window.
- **"Stop early when evidence converges."** Counter-instinct for an
  LLM trained on "be helpful". Without this, the planner spends all
  6 calls even when it had the answer after 2.

## The loop

```python
for step_num in range(1, max_steps + 1):
    rsp = client.chat.completions.create(
        model=model, messages=messages,
        tools=TOOLS, tool_choice="auto", temperature=0.1,
    )
    msg = rsp.choices[0].message
    messages.append(_serialize_assistant(msg))

    tool_calls = getattr(msg, "tool_calls", None)
    if not tool_calls:
        return _parse_final(msg.content or "", steps, truncated=False)

    # dispatch each tool call, append observation as a `tool` message
    for tc in tool_calls:
        observation = _dispatch(tc.function.name, json.loads(tc.function.arguments))
        messages.append({
            "role": "tool", "tool_call_id": tc.id,
            "name": tc.function.name, "content": _truncate(observation),
        })
        steps.append(InvestigationStep(...))
        if step_callback:
            step_callback(steps[-1])
```

That's the entire loop. The `step_callback` is what enables the live
trace in the CLI — each step prints as it lands, so the engineer sees
the planner thinking in real time. Exceptions raised by the callback
are deliberately *not* caught; they indicate a consumer bug, not a
planner failure.

## A real (abbreviated) transcript

Here's what `anchor compare --deep` looks like on the demo dataset
when the engineer is investigating checkout slowness:

```
step 1
  thought:   "p95 latency tripled and a new PaymentGatewayTimeout
              template appeared. Both look related to the payment
              path. Let me recall past drifts with overlapping signals."
  tool:      recall_similar_drifts(
               signals=["template:appeared:PaymentGatewayTimeout #4a",
                        "metric:latency_ms:p95"], k=5)
  obs:       [{"id":"7db2d8aa","outcome":"resolved","similarity":0.71,
               "confirmed_reason":"payment-svc 2.4.1 timeout regression"}]

step 2
  thought:   "High overlap with a resolved deploy regression. Reading
              the full record to see what evidence they used."
  tool:      get_drift_details(drift_id="7db2d8aa")
  obs:       {"top_diffs":[...], "suggested_spl":
              "index=deploy_log service=payment-svc"}

step 3
  thought:   "If this is the same class of incident I should see a
              deploy event for payment-svc in the compare window."
  tool:      run_spl(spl="index=deploy_log service=payment-svc",
                     earliest="2026-06-02T00:00:00",
                     latest="2026-06-03T00:00:00")
  obs:       {"row_count": 1, "rows":[{"_time":"2026-06-02T17:42Z",
              "service":"payment-svc","version":"2.4.7","actor":"ci"}]}

step 4  (final, no tool_calls)
  {
    "summary": "p95 latency tripled at 17:42, coincident with payment-svc
                deploy 2.4.7. New PaymentGatewayTimeout template matches
                resolved incident 7db2d8aa (payment-svc 2.4.1 timeout
                regression).",
    "hypothesis": "deploy regression in payment-svc 2.4.7",
    "evidence": [
      "recall: incident 7db2d8aa had Jaccard 0.71 on payment timeout signals",
      "deploy_log: payment-svc 2.4.7 deployed at 2026-06-02T17:42Z within
       compare window"
    ],
    "confidence": 0.8
  }
```

The whole loop took 4 calls, well under the 6 cap. Two of those were
memory recall, one was SPL evidence-gathering, one was the final
synthesis. That distribution is typical — when the diff has obvious
precedents, the planner spends most of its budget on grounding rather
than exploration.

## Why `qwen-max-latest` specifically

The narrator runs on `qwen-plus`. The planner defaults to
`qwen-max-latest` (or whatever the `QWEN_PLANNER_MODEL` env var is set
to). The difference matters:

- **`qwen-plus`** is fine for one-shot JSON narration. Cheap, fast.
- **`qwen-max-latest`** has noticeably better function-calling
  discipline — it stops earlier, picks tools more accurately, and
  fabricates fewer SPL arguments.

The tier-up is justifiable because `--deep` is an opt-in command,
typically run on a single drift the engineer cares about. If you ran
it on every compare you'd burn through your Qwen budget — exactly the
reason it's `--deep` and not the default.

## Safety / robustness details

- **Argument access uses `.get()` everywhere.** A model that hallucinates
  a missing required argument returns `{"error": "..."}` from `_dispatch`
  instead of crashing the loop.
- **`signal_embedding` is stripped from observations.** The recalled
  drift records have an optional 1024-dim embedding. The planner can't
  reason about raw float vectors and it would eat the 8 KB observation
  budget. Excluded explicitly.
- **Truncation is loud, not silent.** `_truncate` appends
  `…[truncated, N more chars]` so the planner knows it didn't see the
  whole thing and can choose to narrow the SPL.
- **The hard cap is honored** — if we hit `max_steps` without a final
  answer, the result is returned with `truncated=True` and whatever
  observations we gathered. The CLI shows that flag in the rendered
  report.

## Tests for an LLM loop

Testing a function-calling agent is hard. We don't try to test that
"Qwen picks the right tool"; that's not a property of our code.
Instead, the tests
([`tests/test_investigator.py`](../../tests/test_investigator.py))
fake the OpenAI client and verify the *plumbing*:

- Tool dispatch routes each name to the right wrapper
- `_truncate` preserves the original length in its breadcrumb
- `step_callback` fires once per step
- The hard cap is honored
- `_parse_final` survives malformed JSON

That's the layer worth testing. The LLM's *judgment* is best tested by
running it on real fixtures and reading the output — which is exactly
what the demo script in [`examples/demo_script.md`](../../examples/demo_script.md)
does.

---

*Next: [The deployment — Splunk + Qwen on Alibaba Cloud in three
commands →](06-deploy-alibaba-cloud.md)*

*Previous: [← The narrator — putting the LLM only at the edge](04-narrator-llm-at-edge.md)*
