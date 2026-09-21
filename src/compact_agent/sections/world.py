"""The WORLD section — a single evergreen, vivid PORTRAIT file (``world.md``).

Free-form descriptive prose of the world behind the chat: the setting, who these
people are together, how the group works and feels. It is a portrait grown and refined
over time — durable character and dynamics only. Dated happenings belong in other
sections (events/history); this file stays evergreen.
"""

from __future__ import annotations

from .base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnEntry, OnFinish, OnTimePass, OnInit, OnLaunch, Kind, BASE_PRIORITY,
)
from .store import StoreError

WORLD_KEY = "world.md"
WORLD_LIMIT = 5000

WORLD_GUIDANCE = """\
world.md — a SINGLE file holding a VIVID, FREE-FORM NARRATIVE of the world behind the
chat: the setting, who these people are together, how the group actually works and
feels, its context and dynamics. Rich descriptive prose, not a list. This is an
EVERGREEN PORTRAIT you grow and refine over time — the one place that reads like a
story. Capture durable character and dynamics only; dated happenings (what occurred on
a given day) belong in events/history, not here. Keep it under its size limit: when it
grows long, tighten it — preserve the durable substance, cut restated history."""


class WorldSection(Section):
    name = "world"
    default_features: dict[str, bool] = {}

    # ---- paths -------------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return raw_parts == ["world"] or raw_parts == ["world.md"]

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        if raw_parts == ["world"] or raw_parts == ["world.md"]:
            return WORLD_KEY
        raise StoreError(
            f"Invalid world path '{'/'.join(raw_parts)}'. The world section has exactly "
            f"one file: 'world.md'."
        )

    def char_limit(self, key: str) -> int:
        return WORLD_LIMIT

    def paths_doc(self) -> str:
        return WORLD_GUIDANCE

    # ---- view --------------------------------------------------------------
    def view_title(self) -> str:
        return "World"

    def view_blocks(self, store: "object", ctx: ViewContext) -> list[ViewBlock]:
        body = (store.files.get(WORLD_KEY, "") or "").strip()
        if not body:
            return []
        paras = [p.strip() for p in body.split("\n\n")]
        return [
            ViewBlock(text=p, order=(i,))
            for i, p in enumerate(paras) if p
        ]

    def prune_hint(self) -> str:
        return ("the durable substance — the lasting character, setting and dynamics — "
                "staying an evergreen portrait, not a log")

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
            "WORLD: refine and extend the evergreen portrait in 'world.md' from the new "
            "messages — vivid, free-form prose capturing the setting, who these people "
            "are together, and how the group works and feels. Fold in only DURABLE "
            "character and dynamics; dated happenings belong in events/history, not here. "
            "MERGE into the existing file (read it, then edit/overwrite a refined "
            "version) rather than appending a fresh retelling; do not duplicate what is "
            "already captured."
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
    return WorldSection(features)
