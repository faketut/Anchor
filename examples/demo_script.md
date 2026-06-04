# Demo script (≤3 minutes)

Times are guidelines — practice once with a stopwatch before recording.

## 0:00 — 0:20 · The problem
On camera:
> "When my system is healthy, I know what its logs look like. When something
> goes wrong weeks later, I waste 30 minutes manually comparing dashboards to
> remember what 'normal' was. Anchor fixes that."

## 0:20 — 0:45 · Capture the baseline
Run:
```bash
python examples/seed_data.py                     # generates healthy + drifted logs
# (Upload to Splunk index=main as sourcetype=json_auto via Add Data UI)

anchor capture \
  --name "Healthy Week" \
  --from 2026-05-20T00:00:00 \
  --to   2026-05-27T00:00:00 \
  --index main \
  --metric latency_ms
```
Show output: `Anchored 'Healthy Week' — 168000 events, 47 templates, 1 metric.`

## 0:45 — 1:45 · Compare against a bad day
Run:
```bash
anchor compare \
  --from 2026-06-02T00:00:00 \
  --to   2026-06-03T00:00:00 \
  --focus "checkout slowness"
```
Walk through the report panel-by-panel:
- SUMMARY — point to the LLM-written paragraph naming the new template
- TOP DIFFS table — highlight HIGH rows: `template:appeared:PaymentGatewayTimeout`,
  `metric:latency_ms:p99` up 800%
- DRILL-IN SPL — note the agent suggested the next query

## 1:45 — 2:15 · Show the memory loop
Run:
```bash
anchor feedback <drift_id> --outcome resolved \
  --reason "payment-svc deploy rolled back"
```
Then:
```bash
anchor history
```
Point to the drift now marked `resolved`. Mention: "Signal weights just nudged
up. Next investigation will rank these signals higher automatically."

## 2:15 — 2:45 · The differentiator
On camera:
> "A prompt to a chatbot would not survive log retention, would not be shared
> across the team, and would not be reproducible. Anchor stores a versioned
> fingerprint in Splunk KV Store — institutional memory that gets smarter the
> more your team uses it."

## 2:45 — 3:00 · Architecture flash
Show the Mermaid diagram from ARCHITECTURE.md for ~5 seconds. End card with
repo URL.
