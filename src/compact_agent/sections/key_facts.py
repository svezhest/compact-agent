"""The KEY FACTS section — a single durable, deduplicated reference catalog.

``key_facts.md`` is the things someone would look up later: stable identifiers,
links, locations, conventions, and settled decisions WITH the why behind them —
organised by CATEGORY (a **bold** lead-in per group), kept terse and updated in
place. Not opinions, not one-off events.
"""

from __future__ import annotations

from .base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnEntry, OnFinish, OnTimePass, OnInit, OnLaunch,
    Kind, BASE_PRIORITY,
)
from .store import StoreError, VStore


_FILE = "key_facts.md"
_CHAR_LIMIT = 5000


FACTS_GUIDANCE = """\
key_facts.md — a single durable, deduplicated REFERENCE CATALOG: the things
someone would look up later. Terse and structured — organise by CATEGORY with a
**bold** lead-in per category and a bullet list under it; derive the categories
from THIS chat's own content (group like with like — links together, places
together, decisions together) and name them plainly. Update entries IN PLACE;
never keep a dated copy beside an updated one.
    - A fact is STABLE reference: an identifier, link, location, handle,
      credential, fixed convention, or a settled decision WITH the why behind it.
    - A recurring real-world PATTERN is a fact (e.g. "the group meets at <place>
      every Friday"). A one-off event is NOT — that belongs in events/.
    - An OPINION or feeling is NOT a fact. Sentiment and character belong in
      the narrative or a person's profile, not here.
    - If the chat has hard "traps" or decision rules that bite (a hidden
      deadline, a rule whose violation is costly, an easy-to-miss gotcha), give
      them their own category and put the most severe first — these are the
      highest-value lookups."""


class KeyFactsSection(Section):
    name = "key_facts"
    default_features: dict[str, bool] = {}

    # ---- paths -------------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return raw_parts in (["key_facts"], ["key_facts.md"])

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        if not self.owns(raw_parts):
            raise StoreError(
                f"Key facts owns only '{_FILE}', not '{'/'.join(raw_parts)}'."
            )
        return _FILE

    def char_limit(self, key: str) -> int:
        return _CHAR_LIMIT

    def paths_doc(self) -> str:
        return FACTS_GUIDANCE

    # ---- view --------------------------------------------------------------
    def view_title(self) -> str:
        return "Key Facts"

    def view_blocks(self, store: VStore, ctx: ViewContext) -> list[ViewBlock]:
        body = (store.read(_FILE) or "").strip()
        if not body:
            return []
        # A category is a paragraph/group led by a **bold** lead-in; splitting on
        # blank lines is a robust approximation. Keep appearance order so cropping
        # (which keeps the newest/last blocks) drops the earliest categories.
        chunks = [c.strip() for c in body.split("\n\n")]
        return [
            ViewBlock(text=chunk, order=(i,))
            for i, chunk in enumerate(chunks)
            if chunk
        ]

    def prune_hint(self) -> str:
        return ("every identifier and every contested/settled decision (with its why), "
                "and the **bold** category structure")

    # ---- tasks -------------------------------------------------------------
    def task_rules(self) -> list[TaskRule]:
        return [
            TaskRule(
                trigger=OnEntry(kinds=frozenset(
                    {Kind.MESSAGE, Kind.POST, Kind.COMMENT}
                )),
                name="ingest",
                build=self._ingest,
            ),
        ]

    def _ingest(self, ctx: RuleContext) -> list[Task] | None:
        prompt = (
            "KEY FACTS: from the new messages, extract any durable, look-up-later "
            "facts into key_facts.md — stable identifiers, links, locations, "
            "handles, fixed conventions, and settled decisions WITH the why. "
            "Group them by plainly-named CATEGORY (a **bold** lead-in per group); "
            "update existing entries IN PLACE — never keep a dated duplicate beside "
            "an updated fact. Skip opinions and one-off events. "
            "Read key_facts.md first if it exists, then edit/append in place — merge new "
            "facts into existing categories rather than appending a duplicate block."
        )
        return [Task(
            priority=BASE_PRIORITY["ingest"],
            created_batch=ctx.batch_index,
            section=self.name,
            name="ingest",
            target="",
            prompt=prompt,
            dedup_key=f"{self.name}:ingest",
        )]



def section(features: dict | None = None) -> Section:
    return KeyFactsSection(features)
