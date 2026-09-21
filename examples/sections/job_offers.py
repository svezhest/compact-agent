"""The JOB OFFERS section — vacancies as TYPED records + freeform prose.

A job channel reposts vacancies; each post is a board entry that may carry one role,
several (a company hiring for many positions), or none. We split a post into one CARD per
role — a typed, schema-validated JSON record at ``job_offers/<company>/<id>`` (set with
``patch``) — plus a freeform ``…/notes`` file for the human colour (responsibilities, why
it's a strong/odd offer). The card is the machine-clean, comparable layer: salary is
collapsed to ONE axis (estimated net yearly USD, mean + variance), and the title is
un-bullshitted to a short comparable ``role`` so python/rust/analyst cluster cleanly.

The card lives on the WRITE plane (keyed upsert → idempotent); the view crops to the newest
offers by ``posted_at`` (LRU + cross-channel-by-date); the ``query`` tool browses them.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from compact_agent.storage import slugify
from compact_agent.sections.base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnEntry, OnTimePass, Kind, BASE_PRIORITY,
)
from compact_agent.sections.store import StoreError, VStore

_CARD_LIMIT = 8000   # a typed card is schema-bounded (~1 KB); high so it never "overflows"
_NOTES_LIMIT = 2500


class Reward(BaseModel):
    """Comp collapsed to ONE comparable axis: estimated NET YEARLY USD, with uncertainty."""

    net_yearly_usd_mean: float | None = None
    variance: float | None = None


class OfferContact(BaseModel):
    kind: Literal["tg", "email", "url"]
    value: str


# The de-bullshitting taxonomy: a position is a CROSS PRODUCT of one SPECIALIZATION and one
# GRADE — both controlled vocabularies (a job platform's own filters), so the zoo of titles
# collapses to clean, comparable cells. `specialization` is long, so it's a functional str-Enum.
_SPECIALIZATIONS = [
    "backend", "frontend", "fullstack", "web_dev", "ai_engineering", "game_dev",
    "game_producer", "game_design", "level_design", "erp_dev", "mobile_dev", "embedded_iot",
    "ds_ml", "business_analytics", "system_analytics", "data_analytics", "qa",
    "product_management", "project_management", "program_management", "delivery_management",
    "engineering_management", "ui_ux_design", "product_design", "graphic_design",
    "motion_design", "creative_marketing", "sales_bizdev", "customer_support", "recruiting",
    "hr", "hr_leadership", "cybersecurity", "finance", "devops", "system_engineering",
    "architecture", "content_creation", "pr_communications", "game_art", "video_production",
    "education", "ai_training", "engineering", "legal", "medical_healthcare",
    "science_research", "logistics_supply_chain", "consulting", "administration",
    "management", "other",
]
Specialization = Enum("Specialization", {s: s for s in _SPECIALIZATIONS}, type=str)
Grade = Literal["trainee", "junior", "middle", "senior", "lead", "head", "director",
                "c_level", "unknown"]


class JobOfferCard(BaseModel):
    """One vacancy as a typed record. Set field-by-field with ``patch`` (idempotent).

    The DE-BULLSHITTED POSITION is the cross product ``grade × specialization`` — both are
    controlled vocabularies, so varied titles collapse to clean, comparable cells."""

    id: str = ""
    posted_at: str = ""        # YYYY-MM-DD of the post (the sort / LRU key)
    last_seen: str = ""
    tldr: str = ""             # the channel's own one-line summary, if any
    company: str = ""          # slug
    company_raw: str = ""      # verbatim
    specialization: Specialization = Specialization.other   # the controlled spec axis
    grade: Grade = "unknown"                                 # the controlled grade axis
    title_raw: str = ""        # verbatim title — nuance the two axes can't hold
    skills: list[str] = Field(default_factory=list)
    skills_nice_to_have: list[str] = Field(default_factory=list)
    reward: Reward = Field(default_factory=Reward)
    employment: Literal["full_time", "part_time", "contract", "internship",
                        "unknown"] = "unknown"
    work_mode: Literal["onsite", "hybrid", "remote", "unknown"] = "unknown"
    location: str = ""
    relocation: bool | None = None
    contacts: list[OfferContact] = Field(default_factory=list)
    apply_urls: list[str] = Field(default_factory=list)
    status: Literal["open", "likely_closed", "expired"] = "open"
    tier: Literal["routine", "notable", "standout"] | None = None
    source: str = ""


JOB_OFFERS_GUIDANCE = """\
job_offers/<company>/<id> — a TYPED offer CARD (set with patch), and
job_offers/<company>/<id>/notes — its FREEFORM prose (write/append/edit). ONE card per
distinct role; <company> is a slug, <id> = '<post-id>-<short-role-slug>'. A single post may
yield several cards (several roles) or none (not a vacancy → do nothing — skipping is correct).
  CARD fields (patch only; a bad enum/type is rejected with the field name):
    id, posted_at (YYYY-MM-DD of the post), last_seen, tldr (the post's own one-liner),
    company (slug), company_raw, title_raw (verbatim title),
    specialization = ONE slug from the list below, grade = trainee|junior|middle|senior|lead|
      head|director|c_level|unknown,
    skills[], skills_nice_to_have[],
    reward = { net_yearly_usd_mean, variance },
    employment = full_time|part_time|contract|internship|unknown,
    work_mode = onsite|hybrid|remote|unknown, location, relocation (true/false),
    contacts = [{ kind: tg|email|url, value }], apply_urls[],
    status = open|likely_closed|expired, tier = routine|notable|standout, source.
  DE-BULLSHIT THE POSITION into a cross product — grade × specialization — so messy titles
  collapse to clean cells ('Senior ML Engineer (LLMOps)' → grade=senior, specialization=ds_ml;
  'Тимлид' → grade=lead, specialization=engineering_management; 'Backend Engineer (Java)' →
  grade=middle, specialization=backend). Pick the SINGLE best specialization; put nuance the two
  axes can't hold in title_raw; use specialization='other' only when nothing fits. Slugs:
    backend, frontend, fullstack, web_dev, ai_engineering, game_dev, game_producer, game_design,
    level_design, erp_dev (1C/SAP), mobile_dev, embedded_iot, ds_ml (Data Science & ML),
    business_analytics, system_analytics, data_analytics, qa, product_management,
    project_management, program_management, delivery_management, engineering_management,
    ui_ux_design, product_design, graphic_design, motion_design, creative_marketing,
    sales_bizdev, customer_support, recruiting, hr, hr_leadership (HRBP/HRD), cybersecurity,
    finance, devops, system_engineering, architecture, content_creation, pr_communications,
    game_art, video_production, education, ai_training, engineering, legal, medical_healthcare,
    science_research, logistics_supply_chain, consulting, administration, management, other.
  REWARD is the one money axis — always estimate NET YEARLY USD. Convert currency & period
  (monthly→×12; RUB/GBP/USDT→USD at a sensible rate), fold equity/options/bonus into a HIGHER
  mean AND a WIDER variance, and widen variance further when pay is "by agreement", a broad
  range, or taxes are unclear. Two numbers, never a prose blob; null only if genuinely unstated.
  NOTES hold everything human — responsibilities, why it's a strong/odd offer, caveats — and NO
  typed values. Keep the card machine-clean and the notes rich."""


def _reward_str(reward: dict) -> str:
    mean = reward.get("net_yearly_usd_mean")
    if not mean:
        return "comp n/a"
    var = reward.get("variance")
    if var:
        return f"~${mean / 1000:.0f}k±{math.sqrt(var) / 1000:.0f}k/yr"
    return f"~${mean / 1000:.0f}k/yr"


def _position(card: dict) -> str:
    """The de-bullshitted position: grade × specialization (falls back to the verbatim title)."""
    grade = card.get("grade") or "unknown"
    spec = card.get("specialization") or "other"
    pos = " ".join(x for x in [grade if grade != "unknown" else "",
                               spec if spec != "other" else ""] if x)
    return pos or (card.get("title_raw") or "?")


def _offer_line(card: dict) -> str:
    reward = card.get("reward") or {}
    parts = [
        f"**{_position(card)}** @ {card.get('company_raw') or card.get('company') or '?'}",
        _reward_str(reward),
    ]
    wm = card.get("work_mode")
    place = "/".join(x for x in [(wm if wm and wm != "unknown" else ""), card.get("location", "")] if x)
    if place:
        parts.append(place)
    if card.get("status", "open") != "open":
        parts.append(card["status"])
    if card.get("tier") and card["tier"] != "routine":
        parts.append(f"[{card['tier']}]")
    line = "- " + " — ".join(parts)
    extras = []
    if (card.get("tldr") or "").strip():
        extras.append(card["tldr"].strip())
    if card.get("posted_at"):
        extras.append(f"_posted {card['posted_at']}_")
    if extras:
        line += "\n  " + " · ".join(extras)
    return line


class JobOffersSection(Section):
    name = "job_offers"
    # Ground truth for the digests (companies / market / shortlist query it); the model
    # sees the cards in its working view, the reader gets the digests only.
    in_report = False
    default_features: dict[str, bool] = {}

    # ---- paths -------------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return bool(raw_parts) and raw_parts[0] == "job_offers"

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        segs = list(raw_parts[1:])
        if not segs:
            raise StoreError(
                "An offer card path is job_offers/<company>/<id>; its prose is "
                "job_offers/<company>/<id>/notes."
            )
        if segs[-1] in ("notes", "notes.md"):
            if len(segs) != 3:
                raise StoreError("Offer prose is job_offers/<company>/<id>/notes.")
            company, oid = slugify(segs[0]), slugify(segs[1])
            if not company or not oid:
                raise StoreError(f"Empty company/id in '{'/'.join(raw_parts)}'.")
            return f"job_offers/{company}/{oid}/notes.md"
        if len(segs) != 2:
            raise StoreError(
                "An offer card path is job_offers/<company>/<id> (its prose is "
                f"job_offers/<company>/<id>/notes), not '{'/'.join(raw_parts)}'."
            )
        company = slugify(segs[0])
        oid = slugify(segs[1][:-5] if segs[1].endswith(".json") else segs[1])
        if not company or not oid:
            raise StoreError(f"Empty company/id in '{'/'.join(raw_parts)}'.")
        return f"job_offers/{company}/{oid}.json"

    def char_limit(self, key: str) -> int:
        return _NOTES_LIMIT if key.endswith("notes.md") else _CARD_LIMIT

    def model_for(self, key: str):
        return JobOfferCard if key.startswith("job_offers/") and key.endswith(".json") else None

    def paths_doc(self) -> str:
        return JOB_OFFERS_GUIDANCE

    # ---- view --------------------------------------------------------------
    def view_title(self) -> str:
        return "Job Offers"

    def _card_keys(self, store: VStore) -> list[str]:
        return [k for k in store.list_paths("job_offers/") if k.endswith(".json")]

    def view_blocks(self, store: VStore, ctx: ViewContext) -> list[ViewBlock]:
        blocks: list[ViewBlock] = []
        for key in self._card_keys(store):
            card = store.parse_typed(key) or {}
            if not card:
                continue
            blocks.append(ViewBlock(text=_offer_line(card), order=(card.get("posted_at", ""), key)))
        return blocks

    def prune_hint(self) -> str:
        return ("the responsibilities, what makes the role stand out, and the apply details "
                "(contacts / links)")

    def ingest_groups(self, atoms: list) -> list[tuple[str, list]]:
        """One ingest run PER POST: a post (and its comment thread) is one board entry whose
        roles are extracted together, so each run stays focused on a single vacancy thread."""
        return [(str(a.root_id), [a]) for a in atoms]

    # ---- tasks -------------------------------------------------------------
    def task_rules(self) -> list[TaskRule]:
        return [
            TaskRule(
                trigger=OnEntry(kinds=frozenset({Kind.MESSAGE, Kind.POST, Kind.COMMENT})),
                name="ingest",
                build=self._ingest,
            ),
            TaskRule(trigger=OnTimePass(every_days=30), name="expire", build=self._expire),
        ]

    def _ingest(self, ctx: RuleContext) -> list[Task] | None:
        prompt = (
            "JOB OFFERS: treat each new POST as a job-board entry. A post may hold ONE offer, "
            "SEVERAL (multiple roles at one company), or NONE — if it is not a vacancy, do "
            "NOTHING. For EACH distinct role:\n"
            "1. Pick a stable id = '<post-id>-<short-role-slug>' (e.g. '2418-ml-eng'); the card "
            "lives at job_offers/<company>/<id>.\n"
            "2. patch the card with the typed fields (schema in THE FILES). Normalize as you go: "
            "reward → ESTIMATE net yearly USD (convert currency/period, fold equity into a higher "
            "mean + wider variance, widen variance when comp/taxes are unclear; null only if truly "
            "unstated); DE-BULLSHIT the position into grade × specialization (one specialization "
            "slug from THE FILES; grade trainee|junior|middle|senior|lead|head|director|c_level), "
            "verbatim title in title_raw; fill skills[], "
            "skills_nice_to_have[], company (slug)+company_raw, employment, work_mode, location, "
            "tldr (the post's own one-liner).\n"
            "3. contacts — every apply route as {kind:'tg'|'email'|'url', value}: a Telegram "
            "handle appears as an @u… token (record it AS-IS), emails/links verbatim; also "
            "apply_urls.\n"
            "4. write the offer's prose (responsibilities, what stands out, caveats) to "
            "job_offers/<company>/<id>/notes — keep freeform colour OUT of the card.\n"
            "5. Reflect the employer in companies/<company> (patch its facts; merge a line of "
            "notes) and each apply contact in contacts/<key> (patch kind/role; add this company "
            "+ offer id).\n"
            "A repost/update of a known offer REUSES its id (patch merges; bump last_seen). "
            "Record only what the post states or clearly implies; mark guesses, never invent comp."
        )
        return [Task(
            priority=BASE_PRIORITY["ingest"],
            created_batch=ctx.batch_index,
            section=self.name,
            name="ingest",
            target="",
            prompt=prompt,
            dedup_key="job_offers:ingest",
        )]

    def _expire(self, ctx: RuleContext) -> list[Task] | None:
        now = ctx.view.relative_date or "the latest day"
        prompt = (
            f"JOB OFFERS upkeep, as of {now}. Postings go stale. Use query('job_offers/', "
            "sort='posted_at') to scan the OLDEST offers; for any whose status is still 'open' "
            f"but was posted clearly long ago (roughly 90+ days before {now}), patch "
            "status='expired' (or 'likely_closed' if it reads as filled). Leave fresh offers alone."
        )
        return [Task(
            priority=BASE_PRIORITY[OnTimePass],
            created_batch=ctx.batch_index,
            section=self.name,
            name="expire",
            target="",
            prompt=prompt,
            dedup_key="job_offers:expire",
        )]


def section(features: dict | None = None) -> Section:
    return JobOffersSection(features)
