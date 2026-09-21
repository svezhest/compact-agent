"""Run budget: the one-number context model that replaces Big Prune.

A run is shaped by a SINGLE knob, ``MAX_CONTEXT_TOKENS`` (the model's real window
``W``). Each batch splits ``W`` in two:

    summary side  = W * SUMMARY_SHARE          # system prompt + the assembled views
    workflow side = W * (1 - SUMMARY_SHARE)     # incoming data points + tool-call rounds

``SUMMARY_SHARE`` defaults to 1/3 (env ``SUMMARY_SHARE``). The system prompt is fixed
for the life of a run (the sections are fixed), so the room left for the section VIEWS
is::

    summary_budget = W * SUMMARY_SHARE - system_tokens

That budget is divided across the active sections by MAX-MIN FAIR SHARE (water-filling):
sections are equal-weighted; a section that needs less than its share releases the slack
down the chain to heavier sections, so with enough room everything fits uncropped. A
section still over its final allocation CROPS its own view (keeping the newest end) — the
on-disk store is never destructively pruned to fit context. ``n`` (sections) is tiny, so
the O(n^2) loop is irrelevant.

The workflow side is spent by the engine: dispatch thread atoms + run task tool calls,
metering tokens until ``workflow_budget`` is used, then re-render the views and advance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .render import estimate_tokens

# The read-vs-work split. Env-overridable; defaults to the 1/3 you specified.
DEFAULT_SUMMARY_SHARE = 0.333


def summary_share() -> float:
    """Fraction of the window given to system + summary views (rest is workflow)."""
    raw = os.getenv("SUMMARY_SHARE")
    if raw is None:
        return DEFAULT_SUMMARY_SHARE
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_SUMMARY_SHARE
    # Keep it sane: always leave room for BOTH sides.
    return min(0.9, max(0.1, v))


@dataclass(frozen=True)
class RunBudget:
    """Per-run token accounting, derived once from the window and the section count."""

    window: int            # W = MAX_CONTEXT_TOKENS
    share: float           # SUMMARY_SHARE
    system_tokens: int     # measured, fixed for the run (system prompt + tool schemas)
    summary_budget: int    # tokens for ALL section views together (W*share - system)
    workflow_budget: int   # tokens for incoming data + tool-call accumulation

    def equal_share(self, n_sections: int) -> int:
        """The naive 1/n slice, before water-filling redistributes slack."""
        return self.summary_budget // max(1, n_sections)


def compute_run_budget(
    window: int,
    *,
    system_tokens: int,
    share: float | None = None,
) -> RunBudget:
    """Solve the split for a given window. ``system_tokens`` is measured by the engine
    (system prompt assembled from the active sections + the writer tool schemas)."""
    if window <= 0:
        raise ValueError("MAX_CONTEXT_TOKENS must be positive.")
    share = summary_share() if share is None else share
    summary_side = int(window * share)
    summary_budget = summary_side - system_tokens
    if summary_budget <= 0:
        raise ValueError(
            f"Window {window} tok at share {share:g} leaves {summary_side} tok for the "
            f"summary side, but the system prompt alone is {system_tokens} tok. Use a "
            "larger MAX_CONTEXT_TOKENS (or fewer sections / a smaller SUMMARY_SHARE budget)."
        )
    workflow_budget = window - summary_side
    return RunBudget(
        window=window,
        share=share,
        system_tokens=system_tokens,
        summary_budget=summary_budget,
        workflow_budget=workflow_budget,
    )


def allocate_views(summary_budget: int, costs: dict[str, int]) -> dict[str, int]:
    """Max-min fair division of ``summary_budget`` across sections by natural cost.

    Equal-weighted water-filling: start with an equal share; any section whose natural
    view is cheaper than its share is "satisfied" — it takes only what it needs and
    donates the rest to the still-hungry sections, which split the freed room again.
    Repeat until either everyone is satisfied (everything fits, possibly with budget to
    spare) or no one is — at which point the remaining hungry sections cap at an equal
    split of what's left.

    Returns {section: allocated_tokens}. A section may be allocated MORE than it needs
    (when others donate); that's fine — its view simply renders in full. Allocation
    never exceeds the natural cost for a satisfied section, and the total handed out is
    <= summary_budget. A section's own ``render_view`` crops to its allocation.
    """
    if not costs:
        return {}
    remaining = max(0, summary_budget)
    hungry = dict(costs)  # sections not yet satisfied -> their natural cost
    alloc: dict[str, int] = {}

    while hungry:
        share = remaining // len(hungry)
        # Satisfied = natural cost fits within this share; they take only what they need.
        satisfied = {name: cost for name, cost in hungry.items() if cost <= share}
        if not satisfied:
            # No one fits — everybody left caps at an equal split of what remains.
            for name in hungry:
                alloc[name] = share
            break
        for name, cost in satisfied.items():
            alloc[name] = cost
            remaining -= cost
            del hungry[name]
    return alloc
