# Anchor — Demo Script

Target: **≤ 3 minutes** total. Recorded in a single terminal window with a
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

Note the exact timestamps printed by `seed_data.py`. The walkthrough uses
placeholders `<HEALTHY_FROM>`, `<HEALTHY_TO>`, `<DRIFT_FROM>`, `<DRIFT_TO>` —
substitute the real values into your shell history before recording so the
commands are one-key recall.

---

## Scene-by-scene script

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

### Scene 4 · The memory loop (1:50 – 2:20)

**Narration:**
> "I confirm the cause was a payment-svc deploy. I record that feedback —
> and Anchor updates its signal weights. The next investigation will rank
> these signals higher automatically. This is what turns a one-off compare
> into institutional memory."

**Run** (copy the drift_id from the previous report's footer):
```bash
anchor feedback <drift_id_prefix> \
  --outcome resolved \
  --reason "payment-svc deploy v2.14.1 — rolled back"

anchor history
```

Point to the row now marked `resolved` in the history table.

---

### Scene 5 · The differentiator (2:20 – 2:45)

**Narration over the architecture diagram** (switch tab to
`ARCHITECTURE.md` or `architecture.png` in VS Code preview):

> "Why not just ask a chatbot 'compare last week to this week'? Three reasons.
> One: fingerprints live in KV Store, so they survive long after raw logs
> age out of retention. Two: an anchor is a named, versioned team artifact —
> not a single engineer's prompt history. Three: every compare uses the same
> SPL queries, so results are reproducible apples-to-apples, not whatever
> the model decided to check that day."

Flash the system overview Mermaid diagram for ~5 seconds.

---

### Scene 6 · Close (2:45 – 3:00)

**Narration:**
> "Anchor — healthy baselines you can return to. Repo's at
> github.com/faketut/Anchor. Thanks."

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
