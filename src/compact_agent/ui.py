"""Shared terminal presentation (rich) for the whole CLI.

ONE Console instance and a few thin helpers, so every command — compact, describe — looks alike instead of each module constructing its own Console and
its own bespoke markup. Keep all styling here (DRY): callers describe intent
(rule / phase / step / stat / warn), this module decides how it renders.
"""

from __future__ import annotations

from rich.console import Console

# The single shared Console. Import this, never build a new Console().
console = Console()


def rule(title: str, style: str = "bold cyan") -> None:
    """A section header rule — the top-level visual divider ."""
    console.rule(f"[{style}]{title}")


def phase(title: str, style: str = "bold magenta") -> None:
    """A phase divider WITHIN a run (e.g. 'Processing batches', 'Finalizing')."""
    console.rule(f"[{style}]{title}", style=style)


def done(title: str) -> None:
    """The closing rule on successful completion."""
    console.rule(f"[bold green]{title}", style="green")


def step(msg: str, style: str = "cyan") -> None:
    """A single action line within a phase."""
    console.print(f"[{style}]›[/{style}] {msg}")


def stat(label: str, value: object) -> None:
    """A label/value line, indented — used for the run's opening summary."""
    console.print(f"  [dim]{label:<18}[/dim] {value}")


def detail(msg: str, style: str = "dim") -> None:
    """A subordinate, indented detail line (per-prune-step deltas, etc.)."""
    console.print(f"  [{style}]· {msg}[/{style}]")


def info(msg: str) -> None:
    """A plain status line (already-marked-up rich text passes through)."""
    console.print(msg)


def warn(msg: str) -> None:
    console.print(f"[yellow]Warning:[/yellow] {msg}")


def banner(msg: str, style: str = "bold yellow") -> None:
    """A one-line highlighted notice."""
    console.print(f"[{style}]{msg}[/{style}]")
