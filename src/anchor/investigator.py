"""Deep-investigation planner: a function-calling loop on top of `compare()`.

Given an initial `CompareResult`, this drives Qwen (or any OpenAI-compatible
endpoint that supports `tools=`) through a ReAct-style investigation:

  thought -> tool_call -> observation -> ... -> final JSON

The planner has read-only access to four tools that wrap the existing
memory + splunk modules. It is explicitly told to prefer depth over breadth
and to stop once a defensible hypothesis is in hand. A hard step cap
(`CONFIG.investigate_max_steps`) prevents runaway loops; if hit, the planner
is given one last turn to finalize using only the observations gathered.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .agent import CompareResult
from .config import CONFIG
from .models import InvestigationResult, InvestigationStep

StepCallback = Callable[[InvestigationStep], None]

# Bump when SYSTEM_PROMPT, TOOLS, or _initial_payload schema changes.
PLANNER_VERSION = 1
PLANNER_TIMEOUT_S = 120.0

PLANNER_SYSTEM_PROMPT = """You are Anchor's deep-investigation planner.

You receive an initial CompareResult: anchor name + top diffs + an initial
narration. Your job is to deepen the investigation using the tools provided,
then return a tighter root-cause hypothesis with an evidence chain.

Strategy:
1. If diffs contain a new template or a metric spike, call
   `recall_similar_drifts` on those signals to find precedents.
2. If a precedent has outcome=resolved with a confirmed_reason, call
   `get_drift_details` to read its full record before relying on it.
3. If you suspect a deploy/config change, call `run_spl` against relevant
   indexes (e.g. deploy_log, config_change, audit) within the compare window.
4. Stop and finalize as soon as you have a defensible hypothesis. Do NOT
   exceed 4-5 tool calls — prefer depth over breadth.

When you are done, respond with NO tool_calls and a single JSON object:
  {
    "summary":    "2-3 sentences referencing concrete observations",
    "hypothesis": "single best root-cause class",
    "evidence":   ["bullet citing tool+observation", ...],
    "confidence": 0.0-1.0
  }

Never invent observations. If a tool returns no rows, say so.
"""


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "recall_similar_drifts",
            "description": (
                "Find past drift records whose signals overlap the given set. "
                "Returns up to k matches with Jaccard similarity scores. "
                "Use this first whenever a signal feels familiar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "signals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Signal names to match (e.g. 'template:PaymentGatewayTimeout').",
                    },
                    "k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
                    "min_similarity": {
                        "type": "number",
                        "default": 0.1,
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
                "required": ["signals"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_drift_details",
            "description": (
                "Fetch the full record for a past drift (top diffs, hypothesis, "
                "confirmed reason, suggested SPL). Use after recall_similar_drifts "
                "to read evidence from a specific past incident."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "drift_id": {
                        "type": "string",
                        "description": "Full id or 8-char prefix of a drift.",
                    },
                },
                "required": ["drift_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_spl",
            "description": (
                "Execute a Splunk SPL search and return rows (capped at 50). "
                "Use for evidence-gathering (deploy logs, related errors, host-level "
                "breakdowns). Prefer stats/count/timechart over raw events."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spl": {
                        "type": "string",
                        "description": "SPL body. Will be prefixed with 'search' if not a generating command.",
                    },
                    "earliest": {
                        "type": "string",
                        "description": "ISO timestamp or Splunk relative time (e.g. '-2h@h').",
                    },
                    "latest": {
                        "type": "string",
                        "description": "ISO timestamp or Splunk relative time (e.g. 'now').",
                    },
                },
                "required": ["spl", "earliest", "latest"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_drifts",
            "description": (
                "List the most recent drift records, optionally filtered by outcome. "
                "Use for situational awareness when the current diff feels familiar "
                "but you don't know which signals to recall on."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 20},
                    "outcome": {
                        "type": "string",
                        "enum": ["resolved", "ongoing", "false_positive", "unknown"],
                        "description": "Restrict to this outcome (omit for all).",
                    },
                },
            },
        },
    },
]


# ---- tool dispatch ---------------------------------------------------------


_OBS_CHAR_LIMIT = 2000  # cap tool messages going back into context


def _truncate(text: str, limit: int = _OBS_CHAR_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated, {len(text) - limit} more chars]"


def _dispatch(name: str, args: dict[str, Any]) -> str:
    """Execute a tool call, return a JSON string ready to feed back as a tool message."""
    if name == "recall_similar_drifts":
        from .memory import recall_similar_drifts

        results = recall_similar_drifts(
            args.get("signals", []) or [],
            k=int(args.get("k", 5)),
            min_similarity=float(args.get("min_similarity", 0.1)),
        )
        return json.dumps(
            [
                {
                    "id": drift.id[:8],
                    "when": drift.timestamp.isoformat(),
                    "outcome": drift.outcome,
                    "confirmed_reason": drift.engineer_confirmed_reason or "",
                    "similarity": round(sim, 3),
                    "top_signals": [d.signal for d in drift.top_diffs[:6]],
                }
                for drift, sim in results
            ],
            default=str,
        )

    if name == "get_drift_details":
        from .memory import get_drift

        drift = get_drift(args["drift_id"])
        if drift is None:
            return json.dumps({"error": f"drift '{args['drift_id'][:12]}' not found"})
        return json.dumps(drift.model_dump(mode="json"), default=str)

    if name == "run_spl":
        from .splunk_client import run_search

        rows = run_search(
            args["spl"],
            args["earliest"],
            args["latest"],
            max_count=50,
        )
        return json.dumps({"row_count": len(rows), "rows": rows[:50]}, default=str)

    if name == "list_recent_drifts":
        from .memory import list_drifts

        outcome = args.get("outcome") or None
        recent = list_drifts(outcome=outcome, limit=int(args.get("limit", 10)))
        return json.dumps(
            [
                {
                    "id": d.id[:8],
                    "when": d.timestamp.isoformat(),
                    "outcome": d.outcome,
                    "top_signals": [x.signal for x in d.top_diffs[:4]],
                }
                for d in recent
            ],
            default=str,
        )

    return json.dumps({"error": f"unknown tool '{name}'"})


# ---- planner loop ----------------------------------------------------------


def _make_client(provider: str | None) -> tuple[Any, str]:
    """Return (OpenAI client, model name) for the planner."""
    from openai import OpenAI

    chosen = provider or CONFIG.llm_provider
    if chosen == "qwen":
        if not CONFIG.qwen_api_key:
            raise RuntimeError("QWEN_API_KEY not set; cannot run deep investigation")
        model = CONFIG.qwen_planner_model or CONFIG.qwen_model
        return (
            OpenAI(
                api_key=CONFIG.qwen_api_key,
                base_url=CONFIG.qwen_base_url,
                timeout=PLANNER_TIMEOUT_S,
            ),
            model,
        )
    if chosen == "gemini":
        if not CONFIG.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY not set; cannot run deep investigation")
        # Gemini's OpenAI-compat surface supports tools as of v1beta.
        return (
            OpenAI(
                api_key=CONFIG.gemini_api_key,
                base_url=CONFIG.gemini_base_url,
                timeout=PLANNER_TIMEOUT_S,
            ),
            CONFIG.gemini_model,
        )
    raise ValueError(f"Provider '{chosen}' does not support deep investigation")


def _initial_payload(cr: CompareResult) -> str:
    return json.dumps(
        {
            "planner_version": PLANNER_VERSION,
            "anchor_name": cr.anchor.name,
            "compare_window": {
                "start": cr.drift.compare_window.start.isoformat(),
                "end": cr.drift.compare_window.end.isoformat(),
            },
            "initial_summary": cr.summary,
            "initial_hypothesis": cr.hypothesis,
            "top_diffs": [
                {
                    "signal": d.signal,
                    "severity": d.severity,
                    "delta_pct": d.delta_pct,
                    "note": d.note,
                }
                for d in cr.top_diffs[:10]
            ],
            "already_recalled": [
                {"id": past.id[:8], "outcome": past.outcome, "similarity": round(sim, 3)}
                for past, sim in cr.recalled[:3]
            ],
        },
        default=str,
        indent=2,
    )


def _serialize_assistant(msg: Any) -> dict[str, Any]:
    """Convert an OpenAI ChatCompletion message into a dict suitable for resending."""
    out: dict[str, Any] = {"role": "assistant", "content": msg.content}
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]
    return out


def _parse_final(
    content: str, steps: list[InvestigationStep], *, truncated: bool
) -> InvestigationResult:
    try:
        data = json.loads(content or "{}")
    except json.JSONDecodeError:
        data = {"summary": (content or "").strip()[:500] or "(no final answer)"}

    confidence = data.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    return InvestigationResult(
        steps=steps,
        summary=(data.get("summary") or "").strip() or "(empty)",
        hypothesis=data.get("hypothesis") or None,
        evidence=list(data.get("evidence") or []),
        confidence=confidence,
        truncated=truncated,
    )


def investigate(
    compare_result: CompareResult,
    *,
    provider: str | None = None,
    max_steps: int | None = None,
    step_callback: StepCallback | None = None,
) -> InvestigationResult:
    """Drive a function-calling investigation on top of an initial compare.

    If `step_callback` is given it's invoked synchronously after each
    InvestigationStep is appended, so callers can stream the reasoning trace
    to a terminal (or any other sink) instead of waiting for the final answer.
    Exceptions raised by the callback are deliberately not caught — they
    indicate a programmer error in the consumer, not a planner failure.
    """
    max_steps = max_steps if max_steps is not None else CONFIG.investigate_max_steps
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")

    client, model = _make_client(provider)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": _initial_payload(compare_result)},
    ]
    steps: list[InvestigationStep] = []

    for step_num in range(1, max_steps + 1):
        rsp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.1,
        )
        msg = rsp.choices[0].message
        messages.append(_serialize_assistant(msg))

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            # Planner is done.
            return _parse_final(msg.content or "", steps, truncated=False)

        thought = (msg.content or "").strip() or None
        for call in tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                observation = _dispatch(name, args)
            except Exception as exc:  # noqa: BLE001  — surface failure to the planner
                observation = json.dumps(
                    {"error": f"{type(exc).__name__}: {exc}"}, default=str
                )
            observation = _truncate(observation)
            step = InvestigationStep(
                n=step_num,
                thought=thought,
                tool=name,
                args=args,
                observation=observation,
            )
            steps.append(step)
            if step_callback is not None:
                step_callback(step)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": observation,
                }
            )

    # Step budget exhausted — force a final JSON answer.
    messages.append(
        {
            "role": "user",
            "content": (
                "Step budget exhausted. Produce your final JSON answer now "
                "(summary, hypothesis, evidence[], confidence) using ONLY the "
                "observations collected so far. Do not request more tool calls."
            ),
        }
    )
    rsp = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return _parse_final(rsp.choices[0].message.content or "{}", steps, truncated=True)
