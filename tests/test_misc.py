"""Misc fast unit tests covering review fixes (P1.1, P1.2, P2.4, P3.6)."""
from __future__ import annotations

import pytest


# ---- P1.1 Config reads env at instantiation, not at class-body import ------


def test_config_reads_env_at_instantiation(monkeypatch) -> None:
    monkeypatch.setenv("ANCHOR_LLM", "gemini")
    monkeypatch.setenv("QWEN_API_KEY", "from-env")
    from anchor.config import Config

    cfg = Config()
    assert cfg.llm_provider == "gemini"
    assert cfg.qwen_api_key == "from-env"


# ---- P1.2 connect() is process-cached --------------------------------------


def test_connect_is_cached(monkeypatch) -> None:
    from anchor import splunk_client

    calls = {"n": 0}

    def _fake_connect(**_kwargs):
        calls["n"] += 1
        return object()  # opaque handle; we never call methods on it

    monkeypatch.setattr(splunk_client.splunk_client, "connect", _fake_connect)
    splunk_client.reset_connection()

    a = splunk_client.connect()
    b = splunk_client.connect()
    assert a is b
    assert calls["n"] == 1

    splunk_client.reset_connection()
    c = splunk_client.connect()
    assert c is not a
    assert calls["n"] == 2


# ---- P2.4 distinct templates sharing a 32-char prefix yield distinct signals


def test_template_signals_distinguish_long_prefix_collisions() -> None:
    from anchor.diff import template_diff
    from anchor.models import Fingerprint, LogPattern

    shared = "X" * 40  # 40 chars; default _short keeps first 32
    a = shared + "AAA"
    b = shared + "BBB"

    def _fp(patterns):
        return Fingerprint(
            event_volume={"per_source": {}, "total": 0, "hourly_profile": [0.0] * 24},
            log_patterns=patterns,
            error_rates={},
            key_metrics={},
            top_hosts=[],
        )

    anchor = _fp([])
    current = _fp([
        LogPattern(template=a, frequency_pct=10.0, example_raw="ex-a", sourcetype="web", count=100),
        LogPattern(template=b, frequency_pct=10.0, example_raw="ex-b", sourcetype="web", count=200),
    ])

    appeared = [
        d for d in template_diff(anchor, current) if d.signal.startswith("template:appeared:")
    ]
    sigs = {d.signal for d in appeared}
    assert len(sigs) == 2, f"expected 2 distinct signals, got {sigs}"


# ---- P3.6 SPL injection guard ---------------------------------------------


def test_sanitize_token_rejects_pipe() -> None:
    from anchor.fingerprint import _safe_token

    assert _safe_token("main", "index") == "main"
    assert _safe_token("web*", "sourcetype") == "web*"
    with pytest.raises(ValueError):
        _safe_token("main; | malicious", "index")
    with pytest.raises(ValueError):
        _safe_token("'or 1=1", "sourcetype")
