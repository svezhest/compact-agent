"""The CODEBASE section — a synthetic repo skeleton reconstructed from a work chat.

For a chat about building software, the model rebuilds the project's KEY structure from
what was shown or described: the directory layout, the important modules and their
responsibilities, key function/class signatures, and representative code snippets. Paths
mirror a real repo — ``code/<project>/<repo-relative-path>`` — and code lives in fenced
``` blocks (the header normalizer leaves fenced content verbatim, so `#` comments survive).
Reconstruction only: never invent code that was not shown or described; mark VERBATIM vs
RECONSTRUCTED-from-description.
"""

from __future__ import annotations

import re

from compact_agent.sections.base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnEntry, Kind, BASE_PRIORITY,
)
from compact_agent.sections.discovery import nested_discovery_doc
from compact_agent.sections.store import StoreError, VStore

_PREFIX = "code/"
_CHAR_LIMIT = 8000  # code files run long
_BAD = re.compile(r"[^a-z0-9._-]+")


def _seg(s: str) -> str:
    """Normalize one path segment but KEEP dots — `Engine.py` -> `engine.py` (not mangled)."""
    s = s.strip().lower().replace(" ", "-")
    return _BAD.sub("", s).strip("-")


CODEBASE_GUIDANCE = (
    "code/<project>/<repo-relative-path> — a reconstructed SKELETON of a software project the "
    "chat is about. Mirror the real repo: e.g. `code/gigacode-rag/_map.md` (architecture + "
    "directory tree), `code/gigacode-rag/src/engine.py`. Capture the KEY structure — modules "
    "and their responsibilities, important function/class signatures, and representative code "
    "actually shown or described. Put code in fenced ``` blocks (kept verbatim — your `#` "
    "comments are safe). RECONSTRUCTION ONLY: never invent code that was not shown/described; "
    "mark VERBATIM (shown) vs RECONSTRUCTED (from description). If the chat carries no software "
    "substance, do nothing. When an atom opens with a `[source: <chat>]` line the corpus is a "
    "MERGE of several chats — treat each distinct source as a SEPARATE project (derive "
    "`<project>` from it) so unrelated repos never collapse into one tree.\n  "
    + nested_discovery_doc(_PREFIX, ["project", "path"])
)


class CodebaseSection(Section):
    name = "codebase"
    default_features: dict[str, bool] = {}

    # ---- paths -------------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return bool(raw_parts) and raw_parts[0] == "code" and len(raw_parts) >= 2

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        segs = list(raw_parts[1:])
        if not segs:
            raise StoreError("A codebase path is code/<project>/<repo-relative-path>.")
        if segs[-1].endswith(".md"):
            segs[-1] = segs[-1][:-3]
        normed = [_seg(s) for s in segs]
        if any(not s for s in normed):
            raise StoreError(f"Empty path segment in '{'/'.join(raw_parts)}'.")
        key = _PREFIX + "/".join(normed)
        return key if key.endswith(".md") or "." in normed[-1] else key + ".md"

    def char_limit(self, key: str) -> int:
        return _CHAR_LIMIT

    def paths_doc(self) -> str:
        return CODEBASE_GUIDANCE

    # ---- view --------------------------------------------------------------
    def view_title(self) -> str:
        return "Codebase (reconstructed)"

    def view_blocks(self, store: VStore, ctx: ViewContext) -> list[ViewBlock]:
        blocks: list[ViewBlock] = []
        for key in store.list_paths(_PREFIX):
            body = (store.read(key) or "").strip()
            if not body:
                continue
            path = key[len(_PREFIX):]
            blocks.append(ViewBlock(text=f"## {path}\n\n{body}", order=(store.last_updated(key), key)))
        return blocks

    def prune_hint(self) -> str:
        return ("the architecture map, key signatures and the most load-bearing snippets; "
                "keep VERBATIM/RECONSTRUCTED markers; drop boilerplate")

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
            "CODEBASE: if the new data discusses a software project, reconstruct its key "
            "structure into code/<project>/<repo-relative-path> (discover existing projects/"
            "paths first to reuse them): the directory map (_map.md), modules and roles, key "
            "signatures, and representative code shown/described — code in ``` fences. Mark "
            "VERBATIM vs RECONSTRUCTED; never invent code that was not shown. If there is no "
            "software substance, do nothing."
        )
        return [Task(
            priority=BASE_PRIORITY["ingest"], created_batch=ctx.batch_index,
            section=self.name, name="ingest", target="", prompt=prompt,
            dedup_key=f"{self.name}:ingest",
        )]


def section(features: dict | None = None) -> Section:
    return CodebaseSection(features)
