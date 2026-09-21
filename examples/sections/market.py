"""The MARKET section — one evergreen prose digest of the job market (``market.md``).

A single free-form note: which roles/skills are in demand, the comp ranges (in net yearly
USD) by role family, remote vs onsite, notable employers, and any trend/drift over time. It is
the human-readable synthesis that sits ABOVE the typed offer cards — world-type (refined per
batch, never a per-offer log), and grounded by querying the offers rather than re-deriving them.
"""

from __future__ import annotations

from compact_agent.sections.base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnEntry, Kind, BASE_PRIORITY,
)
from compact_agent.sections.store import StoreError, VStore

MARKET_KEY = "market.md"
MARKET_LIMIT = 5000

MARKET_GUIDANCE = """\
market.md — a SINGLE evergreen, free-form PROSE digest of the job market seen so far: which
roles and skills are in demand, the comp ranges (in NET YEARLY USD) by role family, remote vs
onsite, the notable employers, and any trend/drift over time. Not a per-offer log — a synthesis.
Ground it in the offers with query('job_offers/', …) (filter/sort/limit) rather than guessing,
and MERGE into the existing file (read it, refine it) rather than appending a fresh retelling.
Keep it under its size limit: tighten as it grows, preserving the durable read of the market."""


class MarketSection(Section):
    name = "market"
    default_features: dict[str, bool] = {}

    # ---- paths -------------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return raw_parts in (["market"], ["market.md"])

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        if self.owns(raw_parts):
            return MARKET_KEY
        raise StoreError(
            f"The market section has exactly one file, 'market.md', not '{'/'.join(raw_parts)}'."
        )

    def char_limit(self, key: str) -> int:
        return MARKET_LIMIT

    def paths_doc(self) -> str:
        return MARKET_GUIDANCE

    # ---- view --------------------------------------------------------------
    def view_title(self) -> str:
        return "Market"

    def view_blocks(self, store: VStore, ctx: ViewContext) -> list[ViewBlock]:
        body = (store.files.get(MARKET_KEY, "") or "").strip()
        if not body:
            return []
        return [ViewBlock(text=p.strip(), order=(i,))
                for i, p in enumerate(body.split("\n\n")) if p.strip()]

    def prune_hint(self) -> str:
        return "the durable read of the market — demand, comp ranges, trends — not a log"

    # ---- tasks -------------------------------------------------------------
    def task_rules(self) -> list[TaskRule]:
        return [
            TaskRule(
                trigger=OnEntry(kinds=frozenset({Kind.MESSAGE, Kind.POST, Kind.COMMENT})),
                name="ingest",
                build=self._ingest,
            ),
        ]

    def _ingest(self, ctx: RuleContext) -> list[Task] | None:
        prompt = (
            "MARKET: refine the single evergreen digest in market.md from the offers recorded so "
            "far. Use query('job_offers/', …) to ground it — which roles/skills are in demand, the "
            "comp ranges (net yearly USD) by role family, remote vs onsite, the notable employers, "
            "and any trend or drift. Free-form prose, a synthesis (not a per-offer list). MERGE "
            "into the existing file (read it, then edit/overwrite a refined version); never append "
            "a fresh retelling."
        )
        return [Task(
            priority=BASE_PRIORITY["ingest"],
            created_batch=ctx.batch_index,
            section=self.name,
            name="ingest",
            target="",
            prompt=prompt,
            dedup_key="market:ingest",
        )]


def section(features: dict | None = None) -> Section:
    return MarketSection(features)
