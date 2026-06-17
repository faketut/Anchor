"""Qwen text-embedding integration for semantic drift recall.

Qwen DashScope exposes `text-embedding-v3` (1024 dims) via an OpenAI-compatible
endpoint, so we reuse the same client we use for narration.

Two surface functions:

* `embed_signals(signals)` — embed a single drift's signal set as text.
  Returns None if the embedding API key is missing or the call fails, so
  callers can degrade to Jaccard recall without raising.

* `cosine(a, b)` — pure-Python cosine similarity, no numpy.

The decision *whether* to embed lives in `memory.save_drift`, gated by
`CONFIG.semantic_recall`. Recall ranking lives in `memory.recall_similar_drifts`.
"""
from __future__ import annotations

import math
import sys

from .config import CONFIG


def _signals_to_text(signals: list[str]) -> str:
    """Render a signal set as a single embeddable string.

    Sorting keeps embeddings stable across runs even if upstream ordering
    shifts; joining with newlines is what DashScope examples use for
    multi-line keyword inputs.
    """
    return "\n".join(sorted(set(signals)))


def embed_signals(signals: list[str]) -> list[float] | None:
    """Embed a drift's signal set. Returns None on any failure or missing key."""
    if not signals:
        return None
    if not CONFIG.qwen_api_key or not CONFIG.qwen_embed_model:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=CONFIG.qwen_api_key,
            base_url=CONFIG.qwen_base_url,
            timeout=30.0,
        )
        rsp = client.embeddings.create(
            model=CONFIG.qwen_embed_model,
            input=_signals_to_text(signals),
        )
        return list(rsp.data[0].embedding)
    except Exception as exc:  # noqa: BLE001  — degrade silently
        print(
            f"[anchor.embedding] warn: embed_signals failed ({type(exc).__name__}: {exc}); "
            "falling back to Jaccard recall.",
            file=sys.stderr,
        )
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0.0 for empty/mismatched/zero-norm vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
