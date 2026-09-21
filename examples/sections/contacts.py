"""The CONTACTS section — a typed index of apply routes (a mini hiring CRM).

One record per application contact: a Telegram handle, an email, or an apply URL, with the
role it plays (recruiter / founder / hiring manager / agency) and the companies and offers it
is attached to. Catches in-house vs external-agency recruiters and dedups one recruiter across
many postings.

PRIVACY: the writer agent only ever SEES pseudonymized ``@u<hash>`` tokens for Telegram
handles (``identity.pseudonymize_mentions`` rewrites them before any text reaches the model),
so it can only RECORD the pseudonym — the de-identified report invariant holds for free. The
real handle is recovered for the human in the ``.named.md`` view via ``identity.humanize``.
Emails/URLs are public apply routes and pass through verbatim.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from compact_agent.storage import slugify
from compact_agent.sections.base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnFinish, BASE_PRIORITY,
)
from compact_agent.sections.store import StoreError, VStore

_CHAR_LIMIT = 1500


class Contact(BaseModel):
    """One apply route. ``handle`` keeps the value verbatim (an @u… token, email, or URL)."""

    handle: str = ""
    kind: Literal["tg", "email", "url"]
    role: Literal["recruiter", "founder", "hiring_manager", "agency", "unknown"] = "unknown"
    companies: list[str] = Field(default_factory=list)
    offers: list[str] = Field(default_factory=list)


CONTACTS_GUIDANCE = """\
contacts/<key> — a TYPED record (set with patch) per apply route, one per distinct contact.
Build it while recording an offer: every "send CV to …" route becomes a contact.
  fields: handle (the value verbatim — an @u… token for a Telegram handle, else the email/URL),
  kind = tg|email|url, role = recruiter|founder|hiring_manager|agency|unknown,
  companies[] (employers this contact hires for), offers[] (offer ids it appears on).
  A Telegram handle reaches you ALREADY pseudonymized as an @u… token — record it AS-IS; do
  NOT invent a real name. Reuse one record across postings (append to companies/offers) rather
  than forking duplicates; an external-agency recruiter (role=agency) is distinct from in-house."""


class ContactsSection(Section):
    name = "contacts"
    default_features: dict[str, bool] = {}

    # ---- paths -------------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return bool(raw_parts) and raw_parts[0] == "contacts"

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        segs = list(raw_parts[1:])
        if len(segs) != 1:
            raise StoreError(
                "A contact path is contacts/<key> (one record per apply route), "
                f"not '{'/'.join(raw_parts)}'."
            )
        key = slugify(segs[0][:-5] if segs[0].endswith(".json") else segs[0])
        if not key:
            raise StoreError(f"Empty contact key in '{'/'.join(raw_parts)}'.")
        return f"contacts/{key}.json"

    def char_limit(self, key: str) -> int:
        return _CHAR_LIMIT

    def model_for(self, key: str):
        return Contact if key.startswith("contacts/") and key.endswith(".json") else None

    def paths_doc(self) -> str:
        return CONTACTS_GUIDANCE

    # ---- view --------------------------------------------------------------
    def view_title(self) -> str:
        return "Contacts (apply routes)"

    def view_blocks(self, store: VStore, ctx: ViewContext) -> list[ViewBlock]:
        blocks: list[ViewBlock] = []
        for key in store.list_paths("contacts/"):
            if not key.endswith(".json"):
                continue
            c = store.parse_typed(key) or {}
            if not c:
                continue
            head = c.get("handle") or key
            bits = [f"**{head}** ({c.get('kind', '?')}/{c.get('role', 'unknown')})"]
            if c.get("companies"):
                bits.append(", ".join(c["companies"]))
            n = len(c.get("offers", []))
            if n:
                bits.append(f"{n} offer(s)")
            blocks.append(ViewBlock("- " + " — ".join(bits), order=(store.last_updated(key), key)))
        return blocks

    def prune_hint(self) -> str:
        return "the contact value, its kind/role, and which companies/offers it serves"

    # ---- tasks -------------------------------------------------------------
    def task_rules(self) -> list[TaskRule]:
        return [TaskRule(trigger=OnFinish(), name="consolidate", build=self._consolidate)]

    def _consolidate(self, ctx: RuleContext) -> list[Task] | None:
        prompt = (
            "CONTACTS: final pass. Merge any duplicate contact records (the same handle/email/"
            "url under different keys), keep each one's kind/role right, and make sure its "
            "companies[] and offers[] cover everywhere it appears. Never de-pseudonymize an "
            "@u… handle or invent a name."
        )
        return [Task(
            priority=BASE_PRIORITY[OnFinish],
            created_batch=ctx.batch_index,
            section=self.name,
            name="consolidate",
            target="",
            prompt=prompt,
            dedup_key="contacts:consolidate",
        )]


def section(features: dict | None = None) -> Section:
    return ContactsSection(features)
