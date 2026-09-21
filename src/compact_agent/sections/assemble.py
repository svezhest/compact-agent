"""Assemble the section views into the summary the model reads.

The summary budget (``RunBudget.summary_budget``) is divided across the active sections
by max-min fair share, then each section renders itself cropped to its allocation
(keeping its newest end). This is the read-side of the budget: non-destructive — the
store keeps everything, only the VIEW is bounded.
"""

from __future__ import annotations

from ..runbudget import allocate_views
from .base import Section, ViewContext
from .store import VStore


def section_costs(store: VStore, sections: list[Section], ctx: ViewContext) -> dict[str, int]:
    return {s.name: s.natural_cost(store, ctx) for s in sections}


def assemble(
    store: VStore,
    sections: list[Section],
    ctx: ViewContext,
    summary_budget: int,
) -> str:
    """Render the assembled, budget-cropped report from the active sections."""
    costs = section_costs(store, sections, ctx)
    alloc = allocate_views(summary_budget, costs)
    parts = [s.render_view(store, ctx, alloc.get(s.name, 0)) for s in sections]
    return "\n\n".join(p for p in parts if p.strip()) + "\n"
