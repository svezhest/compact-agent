"""The SHORTLIST section — the offers that fit ONE candidate profile (``shortlist.md``).

Pairs with ``job_offers``: after each batch the model re-reads the profile (a text file you
provide), queries the typed offer cards, and maintains a single ranked list of the ones worth
applying to — with a one-line reason and a fit score per entry. World-type: refined per
batch, never a per-post log. The profile text is injected into the system prompt, so it is
declared BEFORE the run like every other bucket.

Profile file: ``$COMPACT_PROFILE`` if set, else ``profile.md`` in the cwd or the chat dir.
"""

from __future__ import annotations

import os
from pathlib import Path

from compact_agent.sections.base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnEntry, Kind, BASE_PRIORITY,
)
from compact_agent.sections.store import StoreError, VStore

SHORTLIST_KEY = "shortlist.md"
SHORTLIST_LIMIT = 2500


def _load_profile() -> str:
    for cand in (os.getenv("COMPACT_PROFILE"), "profile.md"):
        if cand and Path(cand).exists():
            return Path(cand).read_text(encoding="utf-8").strip()
    return "(no profile.md found — treat every offer as a weak fit)"


class ShortlistSection(Section):
    name = "shortlist"
    default_features: dict[str, bool] = {}

    def __init__(self, features: dict | None = None):
        super().__init__(features)
        self.profile = _load_profile()

    # ---- paths -------------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return raw_parts in (["shortlist"], ["shortlist.md"])

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        if self.owns(raw_parts):
            return SHORTLIST_KEY
        raise StoreError(
            f"The shortlist section has exactly one file, 'shortlist.md', not '{'/'.join(raw_parts)}'."
        )

    def char_limit(self, key: str) -> int:
        return SHORTLIST_LIMIT

    def paths_doc(self) -> str:
        return (
            "shortlist.md — a SINGLE ranked list of the offers that FIT THE CANDIDATE PROFILE "
            "below, at most 10 lines, under 2500 chars. One line per offer: `- [fit N/10] "
            "<company> — <grade × specialization>, <comp if known>, <work_mode> — <why it fits / "
            "the catch> (card job_offers/<company>/<id>)`. Best first. ONLY fits of 6/10 and up — "
            "no 'partial', 'mismatch' or 'out of scope' sections, no headings, no preamble; if "
            "nothing fits, one line saying so. Drop entries that stop fitting or whose card turned "
            "likely_closed/expired. Ground it in query('job_offers/', …), never in memory. MERGE "
            "(read, then overwrite a refined list); never append a retelling.\n"
            f"CANDIDATE PROFILE:\n{self.profile}"
        )

    # ---- view --------------------------------------------------------------
    def view_title(self) -> str:
        return "Shortlist"

    def view_blocks(self, store: VStore, ctx: ViewContext) -> list[ViewBlock]:
        body = (store.files.get(SHORTLIST_KEY, "") or "").strip()
        if not body:
            return []
        return [ViewBlock(text=p.strip(), order=(i,))
                for i, p in enumerate(body.split("\n\n")) if p.strip()]

    def prune_hint(self) -> str:
        return "the best-fitting offers with their card path and reason — drop the weakest fits"

    # ---- tasks -------------------------------------------------------------
    def task_rules(self) -> list[TaskRule]:
        return [TaskRule(
            trigger=OnEntry(kinds=frozenset({Kind.MESSAGE, Kind.POST, Kind.COMMENT})),
            name="ingest",
            build=self._ingest,
        )]

    def _ingest(self, ctx: RuleContext) -> list[Task] | None:
        prompt = (
            "SHORTLIST: refresh shortlist.md against the CANDIDATE PROFILE in your instructions. "
            "query('job_offers/', where={'status': 'open'}, sort='-posted_at') and read the cards "
            "(and notes) that could fit; score each fit 0–10 on role match, stack overlap, grade, "
            "comp and work mode; keep only 6/10 and up, best first, one line each with the card "
            "path and a concrete reason — at most 10 lines, nothing below 6/10, no headings. Read "
            "shortlist.md first and overwrite a refined version."
        )
        return [Task(
            priority=BASE_PRIORITY["ingest"] - 1,  # after job_offers ingest in the same batch
            created_batch=ctx.batch_index,
            section=self.name,
            name="ingest",
            target="",
            prompt=prompt,
            dedup_key="shortlist:ingest",
        )]


def section(features: dict | None = None) -> Section:
    return ShortlistSection(features)
