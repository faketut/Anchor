"""Anchor MCP server (stdio).

Exposes Anchor's core memory-agent capabilities as Model Context Protocol
tools so any MCP-aware client (Claude Desktop, Cursor, Qwen Chat with MCP,
custom agents) can drive a Splunk drift investigation conversationally.

Tools exposed:
  * anchor.list_anchors          — list captured baselines
  * anchor.capture_anchor        — capture a new baseline
  * anchor.compare               — run a drift compare (single-shot narrator)
  * anchor.deep_compare          — run compare + function-calling planner
  * anchor.recall                — Jaccard / semantic recall over drift history
  * anchor.feedback              — record outcome on a drift (drives learning)
  * anchor.list_history          — recent drifts
  * anchor.learned_signals       — view learned weights

Run via the console script after `pip install -e '.[mcp]'`:

    anchor-mcp

Or wire into Claude Desktop / Cursor as:

    {
      "mcpServers": {
        "anchor": { "command": "anchor-mcp", "args": [] }
      }
    }
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

# `mcp` is an optional dependency — import lazily so `import anchor` in the
# rest of the test suite never pays the cost (and never crashes if mcp isn't
# installed in dev environments).
try:  # pragma: no cover  — exercised at runtime, not unit test time
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ModuleNotFoundError as exc:  # pragma: no cover
    raise ModuleNotFoundError(
        "MCP server requires the 'mcp' extra. Install with:\n"
        "    pip install -e '.[mcp]'"
    ) from exc

from . import agent
from ._time import ensure_aware
from .memory import recall_similar_drifts
from .models import Scope


# ---- tool schemas ----------------------------------------------------------

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "anchor.list_anchors",
        "description": "List all captured healthy baselines (anchors).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "anchor.capture_anchor",
        "description": (
            "Capture a new healthy baseline from a time window. Stores a "
            "fingerprint (volume, templates, error rates, metric percentiles) "
            "in Splunk KV Store under the given name."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["name", "start", "end"],
            "properties": {
                "name": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 start."},
                "end": {"type": "string", "description": "ISO 8601 end."},
                "indexes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["main"],
                },
                "sourcetypes": {"type": "array", "items": {"type": "string"}},
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Numeric field names for percentile extraction.",
                },
            },
        },
    },
    {
        "name": "anchor.compare",
        "description": (
            "Compare a window against an anchor. Returns top diffs + narrator "
            "summary + recalled past incidents. Single-shot LLM call."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["start", "end"],
            "properties": {
                "anchor_id": {"type": "string", "description": "Anchor id or prefix; omit for latest."},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "focus": {"type": "string"},
                "metrics": {"type": "array", "items": {"type": "string"}},
                "provider": {"type": "string", "enum": ["qwen", "gemini"]},
            },
        },
    },
    {
        "name": "anchor.deep_compare",
        "description": (
            "compare() + a function-calling planner that lets the LLM drill in "
            "via recall / get_drift / run_spl tools. Returns the original diff "
            "plus a reasoning trace and refined hypothesis with confidence."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["start", "end"],
            "properties": {
                "anchor_id": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "focus": {"type": "string"},
                "metrics": {"type": "array", "items": {"type": "string"}},
                "provider": {"type": "string", "enum": ["qwen", "gemini"]},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 12},
            },
        },
    },
    {
        "name": "anchor.recall",
        "description": (
            "Recall past drifts whose signal set overlaps the given signals. "
            "By default only returns drifts with confirmed outcomes "
            "(resolved or false_positive) so the caller can rely on the "
            "engineer_confirmed_reason field as ground truth."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["signals"],
            "properties": {
                "signals": {"type": "array", "items": {"type": "string"}},
                "k": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10},
                "min_similarity": {"type": "number", "default": 0.15, "minimum": 0.0, "maximum": 1.0},
            },
        },
    },
    {
        "name": "anchor.feedback",
        "description": (
            "Record an outcome on a past drift. Drives Anchor's signal weight "
            "learning: resolved bumps the signal weights up, false_positive "
            "bumps them down."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["drift_id", "outcome"],
            "properties": {
                "drift_id": {"type": "string"},
                "outcome": {
                    "type": "string",
                    "enum": ["resolved", "ongoing", "false_positive", "unknown"],
                },
                "reason": {"type": "string"},
            },
        },
    },
    {
        "name": "anchor.list_history",
        "description": "List recent drift records, optionally unresolved-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                "unresolved_only": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "anchor.learned_signals",
        "description": "View Anchor's learned per-signal weights (memory introspection).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---- dispatch --------------------------------------------------------------


def _iso(s: str) -> datetime:
    return ensure_aware(datetime.fromisoformat(s.replace("Z", "+00:00")))


def _call(name: str, args: dict[str, Any]) -> Any:
    """Synchronous dispatcher mapping tool name -> agent call -> JSON-safe value.

    Kept sync because every agent.* call is sync (Splunk SDK + OpenAI SDK
    both block); the MCP layer wraps it in an executor.
    """
    if name == "anchor.list_anchors":
        return [a.model_dump(mode="json") for a in agent.all_anchors()]

    if name == "anchor.capture_anchor":
        scope = Scope(
            indexes=args.get("indexes") or ["main"],
            sourcetypes=args.get("sourcetypes") or [],
        )
        anchor = agent.capture_anchor(
            name=args["name"],
            start=_iso(args["start"]),
            end=_iso(args["end"]),
            scope=scope,
            metric_fields=args.get("metrics") or None,
        )
        return anchor.model_dump(mode="json")

    if name == "anchor.compare":
        cr = agent.compare(
            args.get("anchor_id"),
            _iso(args["start"]),
            _iso(args["end"]),
            focus=args.get("focus"),
            metric_fields=args.get("metrics") or None,
            provider=args.get("provider"),
        )
        return cr.to_dict()

    if name == "anchor.deep_compare":
        base, invest = agent.deep_compare(
            args.get("anchor_id"),
            _iso(args["start"]),
            _iso(args["end"]),
            focus=args.get("focus"),
            metric_fields=args.get("metrics") or None,
            provider=args.get("provider"),
            max_steps=args.get("max_steps"),
        )
        out = base.to_dict()
        out["investigation"] = invest.model_dump(mode="json")
        return out

    if name == "anchor.recall":
        rows = recall_similar_drifts(
            args["signals"],
            k=int(args.get("k", 3)),
            min_similarity=float(args.get("min_similarity", 0.15)),
        )
        return [
            {
                "drift": d.model_dump(mode="json", exclude={"signal_embedding"}),
                "similarity": round(sim, 3),
            }
            for d, sim in rows
        ]

    if name == "anchor.feedback":
        updated = agent.submit_feedback(
            args["drift_id"],
            args["outcome"],  # type: ignore[arg-type]
            args.get("reason"),
        )
        return updated.model_dump(mode="json")

    if name == "anchor.list_history":
        rows = agent.list_history(
            unresolved_only=bool(args.get("unresolved_only", False)),
            limit=int(args.get("limit", 20)),
        )
        return [r.model_dump(mode="json", exclude={"signal_embedding"}) for r in rows]

    if name == "anchor.learned_signals":
        return [w.model_dump(mode="json") for w in agent.learned_signals()]

    raise ValueError(f"Unknown tool '{name}'")


# ---- server entrypoint -----------------------------------------------------


def _build_server() -> "Server":
    server: Server = Server("anchor")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [Tool(**t) for t in _TOOLS]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        args = arguments or {}
        try:
            # Offload blocking work (Splunk SDK + OpenAI SDK are sync).
            result = await asyncio.to_thread(_call, name, args)
            payload = json.dumps(result, default=str, indent=2)
        except Exception as exc:  # noqa: BLE001  — return as text, not crash
            payload = json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"}, default=str, indent=2
            )
        return [TextContent(type="text", text=payload)]

    return server


def main() -> None:
    """`anchor-mcp` console-script entrypoint."""

    async def _run() -> None:
        server = _build_server()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
