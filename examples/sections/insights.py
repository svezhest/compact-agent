"""The INSIGHTS section — the model's own SYNTHESIS, kept apart from the factual record.

Where every other section records what WAS (facts, code, events, people), insights holds
what the model INFERS across the whole corpus: design rationale, trade-offs, predictions,
lessons, meta-patterns, hypotheses. It is the layer where "accumulate → become genius"
crystallises. Inference is allowed here (it is the point) but must be marked — never
laundered into the factual sections.

Free-form notes, one per topic (``insights/<topic>[/<subtopic>].md``), with a raised char
limit: if this section is on, it is probably the heart of a genuinely hard piece of work.
"""

from __future__ import annotations

from compact_agent.render import estimate_tokens
from compact_agent.storage import slugify
from compact_agent.sections.base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnEntry, OnFinish, Kind, BASE_PRIORITY,
)
from compact_agent.sections.discovery import nested_discovery_doc
from compact_agent.sections.store import StoreError, VStore

_PREFIX = "insights/"
_CHAR_LIMIT = 12000  # raised — insight notes carry the heavy thinking


INSIGHTS_GUIDANCE = (
    "insights/<topic>[/<subtopic>].md — YOUR synthesis, not the record. If this section is "
    "enabled it is probably KEY to this work: you are processing something genuinely complex "
    "that needs real notes. The char limit here is RAISED. Content is FULLY FREE-FORM "
    "markdown — organise it however the thinking demands.\n"
    "  Capture INFERENCE: design rationale, trade-offs, predictions, lessons, meta-patterns, "
    "open hypotheses — the conclusions you DRAW from the corpus, beyond what was literally "
    "said. Mark every inference (\"likely\", \"(inferred from …)\", \"disputed\") and its "
    "basis; never present a guess as fact, and never duplicate the factual sections — link to "
    "the idea, don't restate the data.\n"
    "  A `[source: <chat>]` line means the corpus MERGES several chats: attribute an insight to "
    "its source when it matters, and call out cross-source meta-patterns explicitly rather than "
    "blurring distinct projects together.\n  "
    + nested_discovery_doc(_PREFIX, ["topic"])
)


class InsightsSection(Section):
    name = "insights"
    default_features: dict[str, bool] = {}

    # ---- paths -------------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return bool(raw_parts) and raw_parts[0] == "insights" and len(raw_parts) >= 2

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        segs = list(raw_parts[1:])
        if not (1 <= len(segs) <= 2):
            raise StoreError(
                "Insight paths are insights/<topic>[/<subtopic>].md (2–3 parts), "
                f"not '{'/'.join(raw_parts)}'."
            )
        if segs[-1].endswith(".md"):
            segs[-1] = segs[-1][:-3]
        slugged = [slugify(s) for s in segs]
        if any(not s for s in slugged):
            raise StoreError(f"Empty path segment in '{'/'.join(raw_parts)}'.")
        return _PREFIX + "/".join(slugged) + ".md"

    def char_limit(self, key: str) -> int:
        return _CHAR_LIMIT

    def paths_doc(self) -> str:
        return INSIGHTS_GUIDANCE

    # ---- view --------------------------------------------------------------
    def view_title(self) -> str:
        return "Insights (synthesis)"

    def view_blocks(self, store: VStore, ctx: ViewContext) -> list[ViewBlock]:
        blocks: list[ViewBlock] = []
        for key in store.list_paths(_PREFIX):
            body = (store.read(key) or "").strip()
            if not body:
                continue
            topic = key[len(_PREFIX):-3].replace("/", " › ")
            blocks.append(ViewBlock(text=f"## {topic}\n\n{body}", order=(store.last_updated(key), key)))
        return blocks

    def prune_hint(self) -> str:
        return ("the load-bearing conclusions and their basis — keep the inference markers; "
                "cut restated facts and superseded guesses")

    # ---- tasks -------------------------------------------------------------
    def task_rules(self) -> list[TaskRule]:
        return [
            TaskRule(
                trigger=OnEntry(kinds=frozenset({Kind.MESSAGE, Kind.POST, Kind.COMMENT})),
                name="ingest",
                build=self._ingest,
            ),
            TaskRule(trigger=OnFinish(), name="synthesize", build=self._synthesize),
        ]

    def _ingest(self, ctx: RuleContext) -> list[Task] | None:
        prompt = (
            "INSIGHTS: from the new data, update your SYNTHESIS — the conclusions you DRAW, "
            "not the facts themselves. File durable inference into insights/<topic>.md "
            "(discover existing topics first to reuse them): rationale, trade-offs, "
            "predictions, lessons, meta-patterns, open hypotheses. MARK every inference and "
            "its basis; never state a guess as fact, never duplicate the factual sections. "
            "If the new data carries no real analytical substance, do nothing."
        )
        return [Task(
            priority=BASE_PRIORITY["ingest"], created_batch=ctx.batch_index,
            section=self.name, name="ingest", target="", prompt=prompt,
            dedup_key=f"{self.name}:ingest",
        )]

    def _synthesize(self, ctx: RuleContext) -> list[Task] | None:
        prompt = (
            "INSIGHTS: final synthesis pass. Review insights/ as a whole (list_files): merge "
            "overlapping topics, promote recurring observations into clear conclusions, drop "
            "guesses the corpus never supported, and make each note state its conclusion and "
            "the strength of evidence. Stay inference — do not invent facts."
        )
        return [Task(
            priority=BASE_PRIORITY[OnFinish], created_batch=ctx.batch_index,
            section=self.name, name="synthesize", target="", prompt=prompt,
            dedup_key=f"{self.name}:synthesize",
        )]


def section(features: dict | None = None) -> Section:
    return InsightsSection(features)
