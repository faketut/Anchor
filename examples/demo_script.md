# Anchor — Demo Script

Target: **≤ 4 minutes** total. Recorded in a single terminal window with a
visible Splunk Web tab in the background. Practice once with a stopwatch
before recording.

## Pre-flight checklist (do these before hitting record)

- [ ] Docker Desktop running
- [ ] `docker compose up -d` succeeded; `http://localhost:8000` loads (login `admin` / `Anchor!Demo2026`)
- [ ] `.env` filled in (defaults match the docker-compose container; add `QWEN_API_KEY` *or* `GEMINI_API_KEY`)
- [ ] `pip install -e .` succeeded in active venv
- [ ] `python examples/seed_data.py` run; both `healthy.log` and `drifted.log` exist
- [ ] Both logs ingested into `index=main` via `docker exec -u splunk anchor-splunk splunk add oneshot ...` (verify: `index=main | stats count` in Splunk Web shows a non-zero result). The `-u splunk` flag is required — without it, root cannot write to splunk-owned paths in the container.
- [ ] Terminal font ≥ 16pt, dark background, window sized for screen recording (1280×720 min)
- [ ] Clipboard cleared, notes app closed, notifications muted

### Pick your windows (copy from `seed_data.py` output)

The script prints two lines like:

```
Healthy window: 2026-06-01T00:00:00+00:00 → 2026-06-08T00:00:00+00:00
Drifted window: 2026-06-14T00:00:00+00:00 → 2026-06-15T00:00:00+00:00
```

Fill in the four placeholders below using a **half-and-half split** of the
drifted window — that guarantees both compare runs see the same injected
anomalies (`PaymentGatewayTimeout` template + checkout-svc p99 spike), so
`recall_similar_drifts` finds the first incident with Jaccard overlap well
above its 0.15 floor.

| placeholder         | value (example for the dates above)            |
| ------------------- | ---------------------------------------------- |
| `<HEALTHY_FROM>`    | `2026-06-01T00:00`                             |
| `<HEALTHY_TO>`      | `2026-06-08T00:00`                             |
| `<DRIFT_FROM>`      | `2026-06-14T00:00`  *(first 12 h of drifted)*  |
| `<DRIFT_TO>`        | `2026-06-14T12:00`                             |
| `<DRIFT2_FROM>`     | `2026-06-14T12:00`  *(last 12 h of drifted)*   |
| `<DRIFT2_TO>`       | `2026-06-15T00:00`                             |

Substitute the real values into your shell history before recording so the
commands are one-key recall.

---

## Scene-by-scene script

> Total budget ≈ 3:30. Use the spare 30 s as buffer for narration pacing,
> not extra panels.

### Scene 1 · The hook (0:00 – 0:20)

**On-camera narration:**
> "When my system is healthy, I know what its logs look like. Three weeks later,
> when an incident hits, I waste 20 minutes re-discovering what 'normal' was.
> Anchor turns 'healthy' into a saved, shared artifact I can compare against."

**Action:** Show `anchor --help` to establish the tool exists.

```bash
anchor --help
```

---

### Scene 2 · Capture a baseline (0:20 – 0:50)

**Narration:**
> "First, I anchor a window I know was healthy. Anchor runs SPL against
> Splunk, extracts a fingerprint — volume per source, log templates, error
> rates, metric percentiles — and stores it in Splunk's KV Store."

**Run:**
```bash
anchor capture \
  --name "Healthy Week" \
  --from <HEALTHY_FROM> \
  --to   <HEALTHY_TO> \
  --index main \
  --metric latency_ms
```

**Expected output:**
```
Anchored 'Healthy Week' (a1b2c3d4-...)
  events: 168000  templates: 47  metrics: 1  hosts: 5
```

**Optional 3-second pivot:** show `anchor show a1b2c3` (8-char prefix works)
to flash the fingerprint table — *only* if you have time.

---

### Scene 3 · Diagnose a bad day (0:50 – 1:50)

**Narration:**
> "Now suppose I'm investigating a suspect window. I run compare against my
> anchor. The agent re-extracts the same fingerprint over the new window,
> diffs every signal, ranks by severity, and asks Qwen to narrate."

**Run:**
```bash
anchor compare \
  --from <DRIFT_FROM> \
  --to   <DRIFT_TO> \
  --focus "checkout slowness"
```

**Walk through the output for ~40 seconds**, pointing to each panel in turn:

1. **SUMMARY** — read the LLM paragraph aloud; emphasize it named the new
   error template (`PaymentGatewayTimeout`) and the affected service.
2. **HYPOTHESIS** — note that it categorized the cause class
   (e.g. "downstream service degradation"), not a hallucinated root cause.
3. **Top diffs table** — point to the HIGH severity rows:
   - `template:appeared:PaymentGatewayTimeout` — 0 → 400+/hr
   - `metric:latency_ms:p99` — ~150ms → ~1500ms (+900%)
4. **DRILL-IN SPL** — highlight that the agent suggested the next query to
   run. Optionally Cmd+Click into Splunk Web and paste it.

---

### Scene 4 · The memory loop (1:50 – 2:50)

**Narration:**
> "I confirm the cause was a payment-svc deploy. I record that feedback —
> and Anchor updates its signal weights. Now watch what happens when I
> re-run compare on a *new* drift window with overlapping signals."

**Run** (copy the drift_id from the previous report's footer):
```bash
anchor feedback <drift_id_prefix> \
  --outcome resolved \
  --reason "payment-svc deploy v2.14.1 — rolled back"

# Show that the agent now has opinions
anchor learned
```

Point out in the `learned` table: `template:appeared:PaymentGatewayTimeout`
and the `metric:latency_ms:p99` row now have **weight > 1.0** with
`✓ confirmed = 1`. These signals will rank higher in *every future compare*.

**Then re-run compare on the *second half* of the drifted window** — this
is the key shot of the demo, so don't rush it:

```bash
anchor compare \
  --from <DRIFT2_FROM> --to <DRIFT2_TO> \
  --focus "checkout slowness round 2"
```

> **Why this window works.** The seed script injects the same anomalies
> uniformly across all 24 drifted hours, so the first-half and second-half
> compares both surface `PaymentGatewayTimeout` and the checkout p99
> spike. Their signal sets overlap ≈ 100%, well above the 0.15 Jaccard
> floor in `memory.recall_similar_drifts`, so the recall fires reliably on
> camera while still looking like a *different* incident (different
> timestamps, fresh drift_id, freshly written narration).

Point to the new **"Recalled past incidents"** table beneath the report.
It cites the drift you just resolved, with its `confirmed reason` shown.
Then point to the SUMMARY — the LLM now *references the prior incident
by ID* and proposes a tighter hypothesis because it has prior ground truth.

> "This is what turns a one-off compare into institutional memory. The agent
> remembers what mattered, forgets what didn't (weights decay over 30 days),
> and recalls the right past incident the next time a similar pattern shows
> up — even three weeks later when the original raw logs have aged out."

---

### Scene 4b · Sophisticated Qwen integration *(optional, 20–30 s)*

*Skip this scene if you're already at 2:50 — the basic compare is the
story. Include it only if you want to showcase the function-calling
planner.*

**Narration:**
> "And when I want depth instead of a one-shot narration, I add `--deep`.
> Now Qwen's function-calling planner takes the wheel — it can recall
> past drifts, fetch their full records, run its own SPL — until it
> converges on a hypothesis. Every step prints live."

**Run:**
```bash
anchor compare --deep --max-steps 4 \
  --from <DRIFT2_FROM> --to <DRIFT2_TO> \
  --focus "checkout slowness — get to root cause"
```

Point to:
1. **Live step trace** — cyan tool names (`recall_similar_drifts`,
   `get_drift_details`, `run_spl`) as Qwen issues them.
2. **Investigation footer** — the final structured hypothesis with
   `evidence[]` citing each step.

Optional one-liner: *"Same backend is also exposed as an MCP server
(`anchor-mcp`) and as a Qwen Application Center Custom Skill — see the
deploy/ folder."*

---

### Scene 5 · The differentiator (2:50 – 3:15)

**Narration over the architecture diagram** (switch tab to the System
overview Mermaid diagram in README.md, VS Code preview):

> "Why not just ask a chatbot 'compare last week to this week'? Four reasons.
> One: fingerprints, drift history, and signal weights live in KV Store on
> Alibaba Cloud — they survive long after raw logs age out of retention,
> and a nightly job backs them up to OSS. Two: anchors are named, versioned
> team artifacts, not a single engineer's prompt history. Three: every
> compare runs the same deterministic SPL queries and ranks diffs by a
> *learned* weight — results are reproducible apples-to-apples, and the
> agent gets sharper across sessions. Four: when a similar pattern recurs,
> the agent recalls the past incident from KV and feeds it to Qwen as
> evidence — so the model isn't re-discovering the same conclusion every time."

Flash the system overview Mermaid diagram for ~5 seconds.

---

### Scene 6 · Close (3:15 – 3:30)

**Narration:**
> "Anchor — a MemoryAgent for SRE incident response. Splunk + Qwen Cloud +
> Alibaba Cloud, all open source. Repo's at github.com/faketut/Anchor. Thanks."

End card with:
- Repo URL
- License (MIT)
- Track: Observability

---

## Recovery moves (if something fails on camera)

| Failure | Recovery |
|---|---|
| `compare` returns empty | The seed data window doesn't match. Check `index=main earliest=<DRIFT_FROM> latest=<DRIFT_TO>` in Splunk Web first. |
| LLM call times out | Re-run with `--llm gemini` (or `--llm qwen`) to switch providers mid-demo. |
| KV Store not initialized | Run `anchor list` first — it triggers `ensure_collections()` and returns "no anchors" cleanly. |
| Drift report mentions wrong signal | Don't apologize on camera; pivot to the **Top diffs table** which is deterministic, and let viewers see the LLM is one component of a larger system. |

---

## Recording tooling

- macOS built-in: `Cmd+Shift+5` → Record Selected Portion
- Or [OBS Studio](https://obsproject.com/) for higher quality + scene cuts
- Trim with QuickTime Player or [LosslessCut](https://github.com/mifi/lossless-cut)
- Export: 1080p, h264, no audio normalization beyond the obvious

**Upload to YouTube as Unlisted**, paste the link into the submission form.
