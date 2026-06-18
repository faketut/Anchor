# Building Anchor — a blog series

A six-post walkthrough of how Anchor — a MemoryAgent for SRE incident
response — was designed and built on Splunk, Qwen, and Alibaba Cloud.

Each post stands alone but they read best in order. They mirror the
codebase one-to-one: every claim is backed by a permalink into `src/anchor/`.

| # | Title | Module(s) covered |
|---|---|---|
| 1 | [Why a MemoryAgent for on-call](01-why-memoryagent.md) | the problem, the three memories |
| 2 | [The fingerprint: turning a healthy week into a row in KV Store](02-fingerprint.md) | [`fingerprint.py`](../../src/anchor/fingerprint.py), [`memory.py::save_anchor`](../../src/anchor/memory.py) |
| 3 | [The diff: ranking severity by what we've learned matters](03-diff-and-weights.md) | [`diff.py`](../../src/anchor/diff.py), [`memory.py`](../../src/anchor/memory.py) weights + decay |
| 4 | [The narrator: putting the LLM only at the edge](04-narrator-llm-at-edge.md) | [`narrator.py`](../../src/anchor/narrator.py), [`agent.py`](../../src/anchor/agent.py) |
| 5 | [The planner: function-calling for SRE drill-down](05-planner-react-loop.md) | [`investigator.py`](../../src/anchor/investigator.py) |
| 6 | [The deployment: Splunk + Qwen on Alibaba Cloud in three commands](06-deploy-alibaba-cloud.md) | [`deploy/`](../../deploy/) |

## Reading order shortcuts

- **You're an SRE skim-reading**: post 1, then post 4. (Why, then what
  the engineer actually sees.)
- **You're an agent/LLM builder**: post 3, post 4, post 5. (The
  deterministic-core / LLM-edge / planner triad.)
- **You're a Splunk admin**: post 2, post 6. (KV Store as an app
  database, plus the ECS bootstrap.)

## Repo at a glance

- Main README: [`/README.md`](../../README.md)
- Architecture diagram: [`docs/architecture.png`](../architecture.png)
- Live demo script (timed): [`examples/demo_script.md`](../../examples/demo_script.md)
- Alibaba Cloud deploy walkthrough: [`deploy/alibaba-cloud.md`](../../deploy/alibaba-cloud.md)

> ⭐ If this series saves you a 2 a.m. dashboard scroll,
> [star Anchor on GitHub](https://github.com/faketut/Anchor).
