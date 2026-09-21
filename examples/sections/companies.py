"""The COMPANIES section — one accumulating profile per employer.

Like ``people`` but for hiring entities: each company is ``companies/<company>/`` holding a
small TYPED ``facts.json`` (domain, stage, hq, remote policy, size, aliases) plus freeform
``notes.md`` (reputation, founder pedigree, products, culture). Companies are written as a
side effect of offer ingest (no own OnEntry), so the profile thickens as their postings pile
up; an OnFinish pass merges duplicates. The view is LRU by last activity, like people.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from compact_agent.storage import slugify
from compact_agent.sections.base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnFinish, BASE_PRIORITY,
)
from compact_agent.sections.store import StoreError, VStore

_FACTS_LIMIT = 1500
_NOTES_LIMIT = 2500


class CompanyFacts(BaseModel):
    """The structured, comparable facts about an employer (kept deliberately light)."""

    aliases: list[str] = Field(default_factory=list)
    domain: str = ""          # fintech, healthtech, ai, gamedev, …
    stage: str = ""           # bank, startup-seed, startup-growth, bigtech, public, agency, …
    hq: str = ""
    remote_policy: str = ""   # onsite | hybrid | remote | mixed
    size_est: str = ""        # "~15", "10+ eng", …


COMPANIES_GUIDANCE = """\
companies/<company> — a per-employer profile (write its prose here), and
companies/<company>/facts — a small TYPED record (set with patch). Build it as you record
offers: the company a vacancy is for gets its profile updated.
  facts fields (patch): aliases[], domain, stage (bank|startup-seed|startup-growth|bigtech|
  public|agency|…), hq, remote_policy (onsite|hybrid|remote|mixed), size_est.
  The PROSE (companies/<company>) holds reputation, founder pedigree, products, team/culture
  — merge in place, never append a fresh retelling. One folder per real employer; fold
  alternate names into facts.aliases rather than forking a duplicate folder."""


class CompaniesSection(Section):
    name = "companies"
    default_features: dict[str, bool] = {}

    # ---- paths -------------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return bool(raw_parts) and raw_parts[0] == "companies"

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        segs = list(raw_parts[1:])
        if len(segs) == 1:
            company = slugify(segs[0])
            if not company:
                raise StoreError(f"Empty company in '{'/'.join(raw_parts)}'.")
            return f"companies/{company}/notes.md"
        if len(segs) == 2:
            company = slugify(segs[0])
            facet = segs[1]
            if not company:
                raise StoreError(f"Empty company in '{'/'.join(raw_parts)}'.")
            if facet in ("facts", "facts.json"):
                return f"companies/{company}/facts.json"
            if facet in ("notes", "notes.md"):
                return f"companies/{company}/notes.md"
        raise StoreError(
            "A company path is companies/<company> (profile prose) or "
            f"companies/<company>/facts (typed facts), not '{'/'.join(raw_parts)}'."
        )

    def char_limit(self, key: str) -> int:
        return _FACTS_LIMIT if key.endswith("facts.json") else _NOTES_LIMIT

    def model_for(self, key: str):
        return CompanyFacts if key.startswith("companies/") and key.endswith("facts.json") else None

    def paths_doc(self) -> str:
        return COMPANIES_GUIDANCE

    # ---- view --------------------------------------------------------------
    def view_title(self) -> str:
        return "Companies"

    def _companies(self, store: VStore) -> list[str]:
        names = set()
        for p in store.list_paths("companies/"):
            parts = p.split("/")
            if len(parts) >= 2 and parts[1]:
                names.add(parts[1])
        return sorted(names)

    def view_blocks(self, store: VStore, ctx: ViewContext) -> list[ViewBlock]:
        blocks: list[ViewBlock] = []
        for c in self._companies(store):
            facts = store.parse_typed(f"companies/{c}/facts.json") or {}
            notes = (store.files.get(f"companies/{c}/notes.md", "") or "").strip()
            lines = [f"## {c}"]
            fbits = [f"{k.replace('_', ' ')}: {facts[k]}"
                     for k in ("domain", "stage", "hq", "remote_policy", "size_est") if facts.get(k)]
            if facts.get("aliases"):
                fbits.append("aka: " + ", ".join(facts["aliases"]))
            if fbits:
                lines.append(" · ".join(fbits))
            if notes:
                lines.append(notes)
            if len(lines) > 1:
                lu = max(store.last_updated(f"companies/{c}/facts.json"),
                         store.last_updated(f"companies/{c}/notes.md"))
                blocks.append(ViewBlock("\n".join(lines), order=(lu, c)))
        return blocks

    def prune_hint(self) -> str:
        return "the durable employer facts and reputation — a tight standing profile, not a log"

    # ---- tasks -------------------------------------------------------------
    def task_rules(self) -> list[TaskRule]:
        return [TaskRule(trigger=OnFinish(), name="consolidate", build=self._consolidate)]

    def _consolidate(self, ctx: RuleContext) -> list[Task] | None:
        prompt = (
            "COMPANIES: final pass. Merge duplicate company folders (the same employer under "
            "different slugs/aliases), fold alternate names into facts.aliases, and keep each "
            "profile tight — facts.json for the structured facts, notes for reputation/pedigree/"
            "products. Don't invent anything the offers didn't show."
        )
        return [Task(
            priority=BASE_PRIORITY[OnFinish],
            created_batch=ctx.batch_index,
            section=self.name,
            name="consolidate",
            target="",
            prompt=prompt,
            dedup_key="companies:consolidate",
        )]


def section(features: dict | None = None) -> Section:
    return CompaniesSection(features)
