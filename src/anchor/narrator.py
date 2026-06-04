"""LLM narrator: turns structured diffs into SUMMARY + HYPOTHESIS + DRILL_SPL."""
from __future__ import annotations

import json
from typing import Any

from .config import CONFIG
from .models import DiffEntry, NarratorResponse

SYSTEM_PROMPT = """You are Anchor, an observability assistant for Splunk.
You are given a set of statistical diffs between a HEALTHY baseline window
(the "anchor") and a CURRENT window being investigated.

Your job:
1. Write a 2-4 sentence SUMMARY in plain English describing what changed.
   Lead with the highest-severity diffs. Quantify deltas.
2. Propose a single best HYPOTHESIS for the likely cause class
   (e.g. "downstream service degradation", "new error class", "traffic shift",
    "deploy regression"). If evidence is insufficient, say so.
3. Suggest one DRILL_IN SPL query the engineer should run next to confirm.

Be concise. Do NOT invent diffs not in the input. Do NOT claim root cause
with certainty — use words like "likely", "suggests", "consistent with".

Respond as a JSON object with exactly these keys:
  summary (string), hypothesis (string or null), drill_in_spl (string or null).
"""


def _payload(diffs: list[DiffEntry], focus: str | None, anchor_name: str) -> str:
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
    return json.dumps(
        {"anchor_name": anchor_name, "focus": focus or "", "diffs": items},
        default=str,
        indent=2,
    )


def narrate(diffs: list[DiffEntry], focus: str | None, anchor_name: str) -> NarratorResponse:
    if not diffs:
        return NarratorResponse(
            summary="No material drift detected vs. anchor. System behavior is within healthy baseline.",
            hypothesis=None,
            drill_in_spl=None,
        )

    if CONFIG.llm_provider == "qwen":
        return _openai_compat_narrate(
            diffs, focus, anchor_name,
            api_key=CONFIG.qwen_api_key,
            base_url=CONFIG.qwen_base_url,
            model=CONFIG.qwen_model,
        )
    if CONFIG.llm_provider == "gemini":
        return _openai_compat_narrate(
            diffs, focus, anchor_name,
            api_key=CONFIG.gemini_api_key,
            base_url=CONFIG.gemini_base_url,
            model=CONFIG.gemini_model,
        )
    if CONFIG.llm_provider == "splunk":
        return _splunk_narrate(diffs, focus, anchor_name)
    raise ValueError(f"Unknown LLM provider: {CONFIG.llm_provider}")


def _openai_compat_narrate(
    diffs: list[DiffEntry],
    focus: str | None,
    anchor_name: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
) -> NarratorResponse:
    """Call any OpenAI-compatible chat completions endpoint (Qwen, Gemini, etc.)."""
    from openai import OpenAI

    if not api_key:
        raise RuntimeError(f"No API key set for provider '{CONFIG.llm_provider}'")
    client = OpenAI(api_key=api_key, base_url=base_url)
    rsp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _payload(diffs, focus, anchor_name)},
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


def _splunk_narrate(diffs: list[DiffEntry], focus: str | None, anchor_name: str) -> NarratorResponse:
    """Placeholder for Splunk-hosted model. Wire to actual endpoint when available."""
    import httpx

    if not CONFIG.splunk_llm_endpoint:
        raise RuntimeError("SPLUNK_LLM_ENDPOINT not set")
    rsp = httpx.post(
        CONFIG.splunk_llm_endpoint,
        json={
            "model": CONFIG.splunk_llm_model,
            "system": SYSTEM_PROMPT,
            "input": _payload(diffs, focus, anchor_name),
            "format": "json",
        },
        timeout=60,
    )
    rsp.raise_for_status()
    data = rsp.json()
    return NarratorResponse(
        summary=data.get("summary", "(empty)"),
        hypothesis=data.get("hypothesis"),
        drill_in_spl=data.get("drill_in_spl"),
    )
