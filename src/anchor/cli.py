"""Click-based CLI: capture, list, show, compare, feedback, history, blind-spots."""
from __future__ import annotations

from datetime import datetime

import click
from rich.console import Console
from rich.json import JSON
from rich.table import Table

from . import agent
from .memory import get_drift
from .models import Scope

console = Console()

LLM_PROVIDERS = ["qwen", "gemini"]


def _parse_dt(value: str) -> datetime:
    # Accept ISO 8601; fall back to common formats.
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.fromisoformat(value) if fmt is None else datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise click.BadParameter(f"Could not parse datetime: {value}")


def _parse_window(start: str, end: str) -> tuple[datetime, datetime]:
    """Parse a --from / --to pair and validate that start < end.

    Splunk rejects equal or inverted time windows with a 400 from
    ``jobs/create``. Catch the mistake here so the user sees a friendly
    message instead of a stack trace.
    """
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    if end_dt <= start_dt:
        raise click.BadParameter(
            f"--to ({end}) must be strictly after --from ({start}). "
            "Splunk does not accept empty or inverted time windows."
        )
    return start_dt, end_dt


def _resolve_anchor_id(prefix: str) -> str:
    """Accept full UUID or unique short prefix; return full id or raise."""
    matches = [a for a in agent.all_anchors() if a.id.startswith(prefix)]
    if not matches:
        raise click.BadParameter(f"No anchor matching id '{prefix}'")
    if len(matches) > 1:
        ids = ", ".join(m.id[:12] for m in matches)
        raise click.BadParameter(f"Ambiguous anchor prefix '{prefix}' matches: {ids}")
    return matches[0].id


def _resolve_drift_id(prefix: str) -> str:
    matches = [d for d in agent.list_history(limit=500) if d.id.startswith(prefix)]
    if not matches:
        # Fallback for older drifts that aren't in the most-recent 500: if the
        # user gave a full UUID, look it up directly in KV.
        if len(prefix) == 36:
            direct = get_drift(prefix)
            if direct is not None:
                return direct.id
        raise click.BadParameter(f"No drift record matching id '{prefix}'")
    if len(matches) > 1:
        ids = ", ".join(m.id[:12] for m in matches)
        raise click.BadParameter(f"Ambiguous drift prefix '{prefix}' matches: {ids}")
    return matches[0].id


@click.group()
def cli() -> None:
    """Anchor — healthy-baseline drift agent for Splunk."""


# ---- ANCHOR ----------------------------------------------------------------


@cli.command()
@click.option("--name", required=True, help="Human-readable anchor name, e.g. 'May Healthy Week'.")
@click.option("--from", "start", required=True, type=str, help="Window start (ISO 8601).")
@click.option("--to", "end", required=True, type=str, help="Window end (ISO 8601).")
@click.option("--index", "indexes", multiple=True, default=("main",), help="Splunk index (repeatable).")
@click.option("--sourcetype", "sourcetypes", multiple=True, default=(), help="sourcetype filter (repeatable).")
@click.option("--metric", "metrics", multiple=True, default=(), help="Numeric field to baseline (repeatable).")
def capture(name: str, start: str, end: str, indexes: tuple, sourcetypes: tuple, metrics: tuple) -> None:
    """Capture a healthy-window fingerprint and persist it as an anchor."""
    start_dt, end_dt = _parse_window(start, end)
    scope = Scope(indexes=list(indexes), sourcetypes=list(sourcetypes))
    anchor = agent.capture_anchor(name, start_dt, end_dt, scope, list(metrics))
    fp = anchor.fingerprint
    console.print(f"[bold green]Anchored[/bold green] '{anchor.name}' [dim]({anchor.id})[/dim]")
    console.print(
        f"  events: {fp.event_volume.get('total', 0)}  "
        f"templates: {len(fp.log_patterns)}  "
        f"metrics: {len(fp.key_metrics)}  "
        f"hosts: {len(fp.top_hosts)}"
    )


@cli.command("list")
def list_anchors_cmd() -> None:
    """List all saved anchors."""
    anchors = agent.all_anchors()
    if not anchors:
        console.print("[yellow]No anchors yet. Run `anchor capture` first.[/yellow]")
        return
    t = Table(title="Anchors")
    t.add_column("id", style="dim")
    t.add_column("name")
    t.add_column("window")
    t.add_column("created_at")
    for a in sorted(anchors, key=lambda x: x.created_at, reverse=True):
        t.add_row(
            a.id[:8],
            a.name,
            f"{a.time_range.start:%Y-%m-%d %H:%M} → {a.time_range.end:%Y-%m-%d %H:%M}",
            f"{a.created_at:%Y-%m-%d %H:%M}",
        )
    console.print(t)


@cli.command("show")
@click.argument("anchor_id")
@click.option("--raw", is_flag=True, help="Print the full fingerprint as JSON.")
@click.option("--top", default=10, type=int, help="How many top patterns/hosts to show.")
def show_anchor_cmd(anchor_id: str, raw: bool, top: int) -> None:
    """Show a saved anchor's fingerprint. ANCHOR_ID may be a full id or a unique prefix."""
    full_id = _resolve_anchor_id(anchor_id)
    anchor = next(a for a in agent.all_anchors() if a.id == full_id)

    if raw:
        console.print(JSON(anchor.model_dump_json(indent=2)))
        return

    fp = anchor.fingerprint
    console.rule(f"[bold]{anchor.name}[/bold] [dim]({anchor.id})[/dim]")
    console.print(
        f"  window:     {anchor.time_range.start:%Y-%m-%d %H:%M} → "
        f"{anchor.time_range.end:%Y-%m-%d %H:%M}"
    )
    console.print(f"  created:    {anchor.created_at:%Y-%m-%d %H:%M} by {anchor.created_by}")
    console.print(f"  scope:      indexes={anchor.scope.indexes} sourcetypes={anchor.scope.sourcetypes or '*'}")
    console.print(
        f"  totals:     events={fp.event_volume.get('total', 0)} "
        f"templates={len(fp.log_patterns)} metrics={len(fp.key_metrics)} "
        f"hosts={len(fp.top_hosts)}\n"
    )

    # Volume per sourcetype
    per_src = fp.event_volume.get("per_source", {})
    if per_src:
        t = Table(title="Volume per sourcetype")
        t.add_column("sourcetype")
        t.add_column("count", justify="right")
        for src, n in sorted(per_src.items(), key=lambda x: -x[1]):
            t.add_row(src, f"{n:,}")
        console.print(t)

    # Top templates
    if fp.log_patterns:
        t = Table(title=f"Top {min(top, len(fp.log_patterns))} log templates")
        t.add_column("freq %", justify="right")
        t.add_column("count", justify="right")
        t.add_column("sourcetype")
        t.add_column("example", overflow="fold")
        for p in fp.log_patterns[:top]:
            t.add_row(f"{p.frequency_pct:.2f}", f"{p.count:,}", p.sourcetype, p.example_raw[:80])
        console.print(t)

    # Error rates
    if fp.error_rates:
        t = Table(title="Error rates")
        t.add_column("sourcetype")
        t.add_column("errors", justify="right")
        t.add_column("warns", justify="right")
        t.add_column("total", justify="right")
        for src, e in fp.error_rates.items():
            t.add_row(src, str(e["error_count"]), str(e["warn_count"]), str(e["total"]))
        console.print(t)

    # Metrics
    if fp.key_metrics:
        t = Table(title="Key metrics")
        t.add_column("metric")
        t.add_column("p50", justify="right")
        t.add_column("p95", justify="right")
        t.add_column("p99", justify="right")
        t.add_column("mean", justify="right")
        t.add_column("stddev", justify="right")
        for name, m in fp.key_metrics.items():
            t.add_row(name, f"{m.p50:.2f}", f"{m.p95:.2f}", f"{m.p99:.2f}", f"{m.mean:.2f}", f"{m.stddev:.2f}")
        console.print(t)

    # Top hosts
    if fp.top_hosts:
        t = Table(title=f"Top {min(top, len(fp.top_hosts))} hosts")
        t.add_column("host")
        t.add_column("events", justify="right")
        for h in fp.top_hosts[:top]:
            t.add_row(h.get("host", ""), f"{h.get('event_count', 0):,}")
        console.print(t)


# ---- COMPARE ---------------------------------------------------------------


@cli.command()
@click.option("--anchor", "anchor_id", default=None, help="Anchor id or unique prefix (default: latest).")
@click.option("--from", "start", required=True, type=str, help="Compare window start (ISO 8601).")
@click.option("--to", "end", required=True, type=str, help="Compare window end (ISO 8601).")
@click.option("--focus", default=None, help="Optional natural-language focus hint.")
@click.option("--metric", "metrics", multiple=True, default=(), help="Override metrics (default: anchor's).")
@click.option("--llm", type=click.Choice(LLM_PROVIDERS), default=None, help="Override ANCHOR_LLM provider for this call.")
def compare(anchor_id: str | None, start: str, end: str, focus: str | None, metrics: tuple, llm: str | None) -> None:
    """Compare a time window against an anchor and narrate the drift."""
    start_dt, end_dt = _parse_window(start, end)
    resolved_id = _resolve_anchor_id(anchor_id) if anchor_id else None
    result = agent.compare(
        resolved_id,
        start_dt,
        end_dt,
        focus=focus,
        metric_fields=list(metrics) or None,
        provider=llm,
    )

    console.rule(f"[bold]Drift report[/bold] — anchor '{result.anchor.name}' [dim]({result.drift.id})[/dim]")
    console.print(f"\n[bold]SUMMARY[/bold]\n{result.summary}\n")
    if result.hypothesis:
        console.print(f"[bold]HYPOTHESIS[/bold]\n{result.hypothesis}\n")

    if result.top_diffs:
        t = Table(title="Top diffs")
        t.add_column("severity", style="bold")
        t.add_column("signal")
        t.add_column("anchor")
        t.add_column("current")
        t.add_column("Δ%", justify="right")
        t.add_column("note", style="dim")
        sev_color = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "white"}
        for d in result.top_diffs:
            if d.delta_pct is None:
                # "appeared" signals — anchor=0, current>0; show "new" rather than
                # a fabricated magic percent.
                delta_str = "new"
            else:
                delta_str = f"{d.delta_pct:+.1f}"
            t.add_row(
                f"[{sev_color[d.severity]}]{d.severity}[/]",
                d.signal,
                str(d.anchor_val),
                str(d.current_val),
                delta_str,
                d.note,
            )
        console.print(t)

    if result.drill_in_spl:
        console.print(f"\n[bold]DRILL-IN SPL[/bold]\n[cyan]{result.drill_in_spl}[/cyan]\n")

    if result.recalled:
        t = Table(title="Recalled past incidents (memory)")
        t.add_column("id", style="dim")
        t.add_column("when")
        t.add_column("outcome")
        t.add_column("overlap", justify="right")
        t.add_column("confirmed reason", overflow="fold")
        for past, sim in result.recalled:
            t.add_row(
                past.id[:8],
                f"{past.timestamp:%Y-%m-%d %H:%M}",
                past.outcome,
                f"{sim:.0%}",
                past.engineer_confirmed_reason or "—",
            )
        console.print(t)

    console.print(
        f"[dim]Mark outcome with:[/dim] "
        f"anchor feedback {result.drift.id} --outcome resolved|false_positive|ongoing --reason '...'"
    )


# ---- FEEDBACK --------------------------------------------------------------


@cli.command()
@click.argument("drift_id")
@click.option(
    "--outcome",
    required=True,
    type=click.Choice(["resolved", "ongoing", "false_positive", "unknown"]),
)
@click.option("--reason", default=None, help="Free-text explanation (optional).")
def feedback(drift_id: str, outcome: str, reason: str | None) -> None:
    """Record outcome for a drift report; updates signal weights."""
    full_id = _resolve_drift_id(drift_id)
    updated = agent.submit_feedback(full_id, outcome, reason)  # type: ignore[arg-type]
    console.print(
        f"[green]Recorded[/green] outcome=[bold]{updated.outcome}[/bold] for drift {full_id[:8]}."
        + (f" reason: {updated.engineer_confirmed_reason}" if updated.engineer_confirmed_reason else "")
    )


# ---- HISTORY ---------------------------------------------------------------


@cli.command()
@click.option("--unresolved", is_flag=True, help="Show only unresolved drifts.")
@click.option("--limit", default=20, type=int)
def history(unresolved: bool, limit: int) -> None:
    """List past drift reports."""
    drifts = agent.list_history(unresolved_only=unresolved, limit=limit)
    if not drifts:
        console.print("[yellow]No drift records yet.[/yellow]")
        return
    t = Table(title="Drift history")
    t.add_column("id", style="dim")
    t.add_column("when")
    t.add_column("anchor", style="dim")
    t.add_column("top diff")
    t.add_column("outcome")
    for d in drifts:
        top = d.top_diffs[0].signal if d.top_diffs else "—"
        t.add_row(d.id[:8], f"{d.timestamp:%Y-%m-%d %H:%M}", d.anchor_id[:8], top, d.outcome)
    console.print(t)


@cli.command("blind-spots")
@click.option("--min-count", default=3, type=int)
def blind_spots(min_count: int) -> None:
    """Surface signals that recur in unresolved drifts."""
    spots = agent.blind_spots(min_count=min_count)
    if not spots:
        console.print("[green]No recurring blind spots.[/green]")
        return
    t = Table(title=f"Recurring blind spots (≥{min_count} unresolved)")
    t.add_column("signal")
    t.add_column("unresolved count", justify="right")
    for s, c in spots:
        t.add_row(s, str(c))
    console.print(t)


# ---- DESTRUCTIVE (remove history) ------------------------------------------

_OUTCOMES = ["resolved", "ongoing", "false_positive", "unknown"]


@cli.command("delete-drift")
@click.argument("drift_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def delete_drift_cmd(drift_id: str, yes: bool) -> None:
    """Delete a single drift record. DRIFT_ID may be a full id or a unique prefix."""
    full_id = _resolve_drift_id(drift_id)
    if not yes:
        click.confirm(f"Delete drift {full_id[:8]}? This cannot be undone.", abort=True)
    if agent.remove_drift(full_id):
        console.print(f"[green]Deleted[/green] drift {full_id[:8]}.")
    else:
        console.print(f"[yellow]No drift {full_id[:8]} found.[/yellow]")


@cli.command("purge-drifts")
@click.option(
    "--outcome",
    type=click.Choice(_OUTCOMES),
    default=None,
    help="Only purge drifts with this outcome. Omit to purge ALL drifts.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def purge_drifts_cmd(outcome: str | None, yes: bool) -> None:
    """Bulk-delete drift history entries.

    Examples:
        anchor purge-drifts --outcome unknown      # clear noisy unconfirmed runs
        anchor purge-drifts --outcome false_positive --yes
        anchor purge-drifts --yes                   # nuke entire drift history
    """
    label = f"outcome={outcome}" if outcome else "ALL"
    if not yes:
        click.confirm(
            f"Permanently delete every drift record matching {label}? "
            "This will also erase the agent's memory of those past incidents.",
            abort=True,
        )
    removed = agent.remove_drifts(outcome=outcome)  # type: ignore[arg-type]
    console.print(f"[green]Removed[/green] {removed} drift record(s) ({label}).")


# ---- LEARNED (memory introspection) ----------------------------------------


@cli.command("learned")
@click.option("--limit", default=20, type=int, help="How many signals to show in each table.")
@click.option("--eps", default=0.05, type=float, help="Within this distance from 1.0 = 'forgotten'.")
def learned_cmd(limit: int, eps: float) -> None:
    """Show what Anchor has learned: re-weighted signals + signals decaying back to neutral.

    This is the introspection window into Anchor's persistent memory:
      - 'Learned' = weight ≠ 1.0; agent has opinions about these signals.
      - 'Forgotten' = weight ≈ 1.0 but seen before; institutional memory faded
        (via time-based decay) or never had feedback.
    """
    signals = agent.learned_signals()
    if not signals:
        console.print("[yellow]No memory yet — run `anchor compare` and then `anchor feedback`.[/yellow]")
        return

    learned = [w for w in signals if abs(w.weight - 1.0) >= eps]
    forgotten = [w for w in signals if abs(w.weight - 1.0) < eps and w.total_appearances > 0]

    if learned:
        t = Table(title=f"Top {min(limit, len(learned))} learned signals")
        t.add_column("signal", overflow="fold")
        t.add_column("weight", justify="right")
        t.add_column("✓ confirmed", justify="right")
        t.add_column("✗ false pos", justify="right")
        t.add_column("seen", justify="right")
        t.add_column("last used")
        for w in learned[:limit]:
            arrow = "[green]▲[/green]" if w.weight > 1.0 else "[red]▼[/red]"
            last = f"{w.last_used_at:%Y-%m-%d %H:%M}" if w.last_used_at else "—"
            t.add_row(
                w.signal_name,
                f"{arrow} {w.weight:.2f}",
                str(w.confirmed_count),
                str(w.false_positive_count),
                str(w.total_appearances),
                last,
            )
        console.print(t)

    if forgotten:
        t = Table(title=f"Forgotten signals (weight ≈ 1.0 after decay)")
        t.add_column("signal", overflow="fold")
        t.add_column("seen", justify="right")
        t.add_column("last used")
        for w in forgotten[:limit]:
            last = f"{w.last_used_at:%Y-%m-%d %H:%M}" if w.last_used_at else "—"
            t.add_row(w.signal_name, str(w.total_appearances), last)
        console.print(t)

    if not learned and not forgotten:
        console.print("[yellow]No learned or forgotten signals to show.[/yellow]")


if __name__ == "__main__":
    cli()
