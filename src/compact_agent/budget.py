"""Library surface for the token-budget / flex-size primitives.

These are the dependency-light (pure-stdlib) building blocks of compact-agent's
"sections + flex-size" pattern, re-exported here so other projects can import and
call them directly without pulling in the LLM/store plumbing:

    from compact_agent.budget import (
        estimate_tokens, allocate_views, compute_run_budget, RunBudget,
        summary_share, crop_to_budget, ViewBlock,
    )

- estimate_tokens(text)              -> cheap len//4 token heuristic
- allocate_views(budget, costs)      -> max-min fair (water-filling) split of a budget
- compute_run_budget(window, ...)    -> split a context window into summary/workflow sides
- crop_to_budget(blocks, budget, hdr)-> keep highest-order blocks under a token budget
- ViewBlock(text, order)             -> one renderable unit with a recency/priority key

Nothing here imports pydantic, pydantic_ai, openai, httpx, or rich.
"""
from __future__ import annotations

from .render import estimate_tokens
from .runbudget import RunBudget, allocate_views, compute_run_budget, summary_share
from .sections.base import ViewBlock, crop_to_budget

__all__ = [
    "estimate_tokens",
    "allocate_views",
    "compute_run_budget",
    "RunBudget",
    "summary_share",
    "crop_to_budget",
    "ViewBlock",
]
