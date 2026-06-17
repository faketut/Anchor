"""LLM narrator: turns structured diffs into SUMMARY + HYPOTHESIS + DRILL_SPL."""
from __future__ import annotations

import json
from typing import Any

from .config import CONFIG
from .models import DiffEntry, DriftRecord, NarratorResponse

# Bump this when SYSTEM_PROMPT or payload schema changes so audits of stored
# drift records can disambiguate which prompt version produced each summary.
PROMPT_VERSION = 2

# How long to wait for the LLM endpoint to respond. The CLI shouldn't hang.
LLM_TIMEOUT_S = 60.0

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


def _payload(
    diffs: list[DiffEntry],
    focus: str | None,
    anchor_name: str,
    past_incidents: list[tuple[DriftRecord, float]] | None = None,
) -> str:
    items: list[dict[str, Any]] = []
    for d in diffs[:15]:
        items.append(
            {
                "signal": d.signal,
                "kind": d.kind,
                "severity": d.severity,
                "anchor_val": d.anchor_val,
                "current_val": d.current_val,
                "delta_pct": d.delta_pct,
                "note": d.note,
            }
        )
    past: list[dict[str, Any]] = []
    for drift, sim in (past_incidents or [])[:3]:
        past.append(
            {
                "id": drift.id[:8],
                "when": drift.timestamp.isoformat(),
                "outcome": drift.outcome,
                "confirmed_reason": drift.engineer_confirmed_reason or "",
                "signal_overlap": round(sim, 3),
                "signals": [d.signal for d in drift.top_diffs[:8]],
            }
        )
    payload: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "anchor_name": anchor_name,
        "diffs": items,
        "past_incidents": past,
    }
    if focus:
        # Omit empty focus rather than sending an empty-string key.
        payload["focus"] = focus
    return json.dumps(payload, default=str, indent=2)


def narrate(
    diffs: list[DiffEntry],
    focus: str | None,
    anchor_name: str,
    past_incidents: list[tuple[DriftRecord, float]] | None = None,
    provider: str | None = None,
) -> NarratorResponse:
    if not diffs:
        return NarratorResponse(
            summary="No material drift detected vs. anchor. System behavior is within healthy baseline.",
            hypothesis=None,
            drill_in_spl=None,
        )

    chosen = provider or CONFIG.llm_provider
    if chosen == "qwen":
        return _openai_compat_narrate(
            diffs, focus, anchor_name,
            api_key=CONFIG.qwen_api_key,
            base_url=CONFIG.qwen_base_url,
            model=CONFIG.qwen_model,
            past_incidents=past_incidents,
            provider_label="qwen",
        )
    if chosen == "gemini":
        return _openai_compat_narrate(
            diffs, focus, anchor_name,
            api_key=CONFIG.gemini_api_key,
            base_url=CONFIG.gemini_base_url,
            model=CONFIG.gemini_model,
            past_incidents=past_incidents,
            provider_label="gemini",
        )
    raise ValueError(f"Unknown LLM provider: {chosen}")


def _openai_compat_narrate(
    diffs: list[DiffEntry],
    focus: str | None,
    anchor_name: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    past_incidents: list[tuple[DriftRecord, float]] | None = None,
    provider_label: str = "openai-compat",
) -> NarratorResponse:
    """Call any OpenAI-compatible chat completions endpoint (Qwen, Gemini, etc.)."""
    from openai import OpenAI

    if not api_key:
        raise RuntimeError(f"No API key set for provider '{provider_label}'")
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=LLM_TIMEOUT_S)
    rsp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _payload(diffs, focus, anchor_name, past_incidents)},
        ],
        temperature=0.2,
    )
    raw = rsp.choices[0].message.content or "{}"
    data = json.loads(raw)
    return NarratorResponse(
        summary=data.get("summary", "").strip() or "(empty)",
        hypothesis=(data.get("hypothesis") or None),
        drill_in_spl=(data.get("drill_in_spl") or None),
    )
