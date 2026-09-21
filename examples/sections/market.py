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
MARKET_LIMIT = 3000

MARKET_GUIDANCE = """\
market.md — a SINGLE compact digest of the market BY DIRECTION (specialization), under 3000
chars. One short paragraph or 2–4 bullet lines per direction that has offers: how many offers,
the grades seen, the comp range (NET YEARLY USD, as a range, not a mean), remote vs onsite, the
employers. Then at most 3 lines of overall trend. No sub-headings, no tables, no essays, no
directions with zero offers. Ground it in query('job_offers/', …) (filter/sort/limit) rather than
guessing; MERGE into the existing file (read it, refine it), never append a fresh retelling."""


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
            "MARKET: refine market.md — a compact by-direction digest (see its standard: per "
            "specialization with offers: count, grades, comp RANGE in net yearly USD, remote/onsite, "
            "employers; then ≤3 trend lines; under 3000 chars, no sub-headings). Ground every line "
            "in query('job_offers/', …). Read the file, then overwrite a refined version; never "
            "append a fresh retelling."
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
