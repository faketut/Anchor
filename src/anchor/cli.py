"""Click-based CLI: capture, list, show, compare, feedback, history, blind-spots."""
from __future__ import annotations

from datetime import datetime

import click
from rich.console import Console
from rich.table import Table

from . import agent
from .models import Scope

console = Console()


def _parse_dt(value: str) -> datetime:
    # Accept ISO 8601; fall back to common formats.
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.fromisoformat(value) if fmt is None else datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise click.BadParameter(f"Could not parse datetime: {value}")


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
    scope = Scope(indexes=list(indexes), sourcetypes=list(sourcetypes))
    anchor = agent.capture_anchor(name, _parse_dt(start), _parse_dt(end), scope, list(metrics))
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


# ---- COMPARE ---------------------------------------------------------------


@cli.command()
@click.option("--anchor", "anchor_id", default=None, help="Anchor id (default: latest).")
@click.option("--from", "start", required=True, type=str, help="Compare window start (ISO 8601).")
@click.option("--to", "end", required=True, type=str, help="Compare window end (ISO 8601).")
@click.option("--focus", default=None, help="Optional natural-language focus hint.")
@click.option("--metric", "metrics", multiple=True, default=(), help="Override metrics (default: anchor's).")
def compare(anchor_id: str | None, start: str, end: str, focus: str | None, metrics: tuple) -> None:
    """Compare a time window against an anchor and narrate the drift."""
    result = agent.compare(
        anchor_id, _parse_dt(start), _parse_dt(end), focus=focus, metric_fields=list(metrics) or None
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
            t.add_row(
                f"[{sev_color[d.severity]}]{d.severity}[/]",
                d.signal,
                str(d.anchor_val),
                str(d.current_val),
                "" if d.delta_pct is None else f"{d.delta_pct:+.1f}",
                d.note,
            )
        console.print(t)

    if result.drill_in_spl:
        console.print(f"\n[bold]DRILL-IN SPL[/bold]\n[cyan]{result.drill_in_spl}[/cyan]\n")

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
    updated = agent.submit_feedback(drift_id, outcome, reason)  # type: ignore[arg-type]
    console.print(
        f"[green]Recorded[/green] outcome=[bold]{updated.outcome}[/bold] for drift {drift_id[:8]}."
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


if __name__ == "__main__":
    cli()
