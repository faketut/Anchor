"""Smoke tests for the Click CLI. No Splunk; all data layer is monkeypatched."""
from __future__ import annotations

from click.testing import CliRunner

from anchor import cli as cli_module


def test_list_command_renders_with_no_anchors(monkeypatch) -> None:
    monkeypatch.setattr(cli_module.agent, "all_anchors", lambda: [])
    result = CliRunner().invoke(cli_module.cli, ["list"])
    assert result.exit_code == 0
    assert "No anchors yet" in result.output


def test_learned_command_handles_empty_memory(monkeypatch) -> None:
    monkeypatch.setattr(cli_module.agent, "learned_signals", lambda: [])
    result = CliRunner().invoke(cli_module.cli, ["learned"])
    assert result.exit_code == 0
    assert "No memory yet" in result.output


def test_compare_command_threads_provider_to_agent(monkeypatch) -> None:
    """`--llm gemini` must reach `agent.compare(provider=...)` (P3.4)."""
    seen_kwargs: dict = {}

    def _fake_compare(anchor_id, start, end, focus=None, metric_fields=None, provider=None):
        seen_kwargs["provider"] = provider
        raise SystemExit(0)  # short-circuit before any rendering

    monkeypatch.setattr(cli_module.agent, "compare", _fake_compare)
    CliRunner().invoke(
        cli_module.cli,
        ["compare", "--from", "2026-06-14", "--to", "2026-06-14T01:00", "--llm", "gemini"],
        catch_exceptions=True,
    )
    assert seen_kwargs.get("provider") == "gemini"


def test_compare_rejects_equal_window(monkeypatch) -> None:
    """`--from` == `--to` must fail at the CLI with a friendly error,
    not bubble up as a Splunk HTTP 400."""

    def _should_not_be_called(*_a, **_kw):
        raise AssertionError("agent.compare must not be invoked on an empty window")

    monkeypatch.setattr(cli_module.agent, "compare", _should_not_be_called)
    result = CliRunner().invoke(
        cli_module.cli,
        [
            "compare",
            "--from", "2026-06-12T23:00:00+00:00",
            "--to", "2026-06-12T23:00:00+00:00",
            "--focus", "checkout slowness",
        ],
    )
    assert result.exit_code != 0
    assert "must be strictly after" in result.output


def test_compare_rejects_inverted_window(monkeypatch) -> None:
    monkeypatch.setattr(cli_module.agent, "compare", lambda *a, **k: None)
    result = CliRunner().invoke(
        cli_module.cli,
        ["compare", "--from", "2026-06-14T02:00", "--to", "2026-06-14T01:00"],
    )
    assert result.exit_code != 0
    assert "must be strictly after" in result.output
