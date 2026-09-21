"""The PEOPLE section — pseudonymous @hash speakers, the foundation of the report.

People are pseudonymous ``@hash`` speakers (plus inferred ``aka`` aliases). Each person
is a folder ``people/<@hash>/`` holding a fixed vocabulary of standing-portrait sections,
plus — when the optional ``timeline`` feature is on — one per-CALENDAR-YEAR summary file
(``people/<@hash>/YYYY.md``).

Ported from the old ``report_store._render_people`` / ``PERSON_SECTIONS`` / ``_FOLD_MARKERS``
logic, with two deliberate changes:
  1. the ``known_quirks`` person section is REMOVED entirely;
  2. the per-year file is no longer always-on — it is the optional ``timeline`` feature.
"""

from __future__ import annotations

import re

from .base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnEntry, OnFinish, OnTimePass, OnInit, OnLaunch,
    Kind, BASE_PRIORITY,
)
from .store import StoreError

# Standing-portrait sections (NO known_quirks). The path is the heading.
PERSON_SECTIONS: tuple[str, ...] = (
    "status",
    "aka",
    "personality",
    "positive_to",
    "negative_to",
)
_SECTION_DISPLAY = {
    "status": "Status",
    "aka": "Also known as",
    "personality": "Personality",
    "positive_to": "Positive toward",
    "negative_to": "Negative toward",
}

# status-prefix markers that fold a person out of the full roster into a compact line:
# "minor" = too little data ever, "faded" = mattered once, no longer active/relevant.
_FOLD_MARKERS = ("minor", "faded")

_YEAR_RE = re.compile(r"^\d{4}$")

_PERSON_LIMIT = 1800   # per standing-portrait section file
_YEAR_LIMIT = 1500     # per per-year timeline file


class PeopleSection(Section):
    name = "people"
    default_features = {"timeline": True}

    # ---- paths -------------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return bool(raw_parts) and raw_parts[0] == "people"

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        if len(raw_parts) != 3:
            raise StoreError("A person path is 'people/<@hash>/<section>'.")
        key = raw_parts[1]
        if key not in known_keys:
            # The model routinely drops the leading '@' from the path segment
            # ('people/u00381f78/...' instead of 'people/@u00381f78/...'). Recover the
            # canonical key instead of bouncing — the @hash is otherwise valid.
            alt = key if key.startswith("@") else "@" + key
            if alt in known_keys:
                key = alt
            else:
                raise StoreError(
                    f"Unknown person '{key}'. Use an @hash that appears in the data as a speaker "
                    "or a tagged real person; a non-routable mention handle has no profile. "
                    "Never invent one."
                )
        section = raw_parts[2]
        if section.endswith(".md"):
            section = section[:-3]
        timeline = self.feature("timeline")
        valid = section in PERSON_SECTIONS or (timeline and bool(_YEAR_RE.match(section)))
        if not valid:
            allowed = ", ".join(PERSON_SECTIONS)
            extra = ", or a per-year summary 'YYYY'" if timeline else ""
            raise StoreError(f"Unknown section '{section}'. Allowed: {allowed}{extra}.")
        return f"people/{key}/{section}.md"

    def char_limit(self, key: str) -> int:
        parts = key.split("/")
        section = parts[2][:-3] if len(parts) == 3 and parts[2].endswith(".md") else (
            parts[2] if len(parts) == 3 else ""
        )
        if _YEAR_RE.match(section):
            return _YEAR_LIMIT
        return _PERSON_LIMIT

    def paths_doc(self) -> str:
        timeline_para = ""
        if self.feature("timeline"):
            timeline_para = (
                "\n  people/<@hash>/YYYY.md — optional \"timeline\": a PER-YEAR summary for "
                "one person, a\n"
                "  few lines on what that year meant for them (arc, big moves, how they "
                "changed).\n"
                "  One file per CALENDAR YEAR only — there is NO per-quarter/month/day person\n"
                "  file. Use it to keep the standing sections evergreen: when year-specific "
                "detail\n"
                "  piles up in personality or status, roll it down into that year's summary "
                "and keep\n"
                "  the sections timeless."
            )
        return (
            "people/<@hash>/<section>.md — one person, split across exactly these sections, "
            "each its\n"
            "own file (omit a section you have nothing for). People are pseudonymous @hash\n"
            "speakers: NEVER invent or merge @hashes — use only an @hash that actually appears "
            "as a\n"
            "speaker. Only @u speakers get profiles, never @m mention tokens.\n"
            "    status        - current standing: active / left on <date> / part-time / role.\n"
            "                    If you have seen TOO LITTLE of someone to profile them (a "
            "walk-on\n"
            "                    who appeared once or twice, no durable traits yet), begin "
            "status\n"
            "                    with the word \"minor\" (optionally \"minor — <one short "
            "phrase>\")\n"
            "                    and leave their other sections empty. The report folds every "
            "such\n"
            "                    person into a single compact line instead of a full empty "
            "block;\n"
            "                    promote them out of \"minor\" the moment real substance "
            "accrues.\n"
            "                    RELEVANCE (lifecycle): a profile earns its place by mattering "
            "to the\n"
            "                    group NOW — judge by STANDING importance, not recent chatter. "
            "Someone\n"
            "                    who shaped the group keeps their place even while quiet (a "
            "lead, a\n"
            "                    founder, the owner of an open thread or commitment). Someone "
            "who\n"
            "                    passed through and left no lasting mark fades: if a person is "
            "long\n"
            "                    inactive AND holds no standing role, no open commitment, and "
            "no\n"
            "                    unresolved thread, begin their status with \"faded\" "
            "(optionally\n"
            "                    \"faded — <why>\") and the report folds them into a compact "
            "reference\n"
            "                    line. Drop \"faded\" the instant they matter again. Newcomers "
            "start\n"
            "                    ordinary and earn a fuller profile as a role and contributions\n"
            "                    accrue. \"minor\" (too little data ever) and \"faded\" "
            "(mattered once,\n"
            "                    not now) are DIFFERENT — leaving (even \"left on <date>\") is "
            "NOT by\n"
            "                    itself fading; a departed lead can still be highly relevant.\n"
            "    aka           - names this person is ADDRESSED or REFERRED TO by IN THE CHAT:\n"
            "                    nicknames, short forms, role-labels others call them. You only "
            "ever\n"
            "                    see the @hash, so put every alias you infer HERE, one per "
            "line, each\n"
            "                    with how sure you are — \"L**a** (often, confident)\" vs "
            "\"maybe the\n"
            "                    'PM' they mention (unsure)\". This is the ONLY place to record "
            "a guessed\n"
            "                    name: never weave a guessed real name into any other section "
            "or treat\n"
            "                    a guess as fact. Keep it to names actually used in the "
            "conversation.\n"
            "    personality   - durable character: how they work, recurring behaviour, "
            "strengths,\n"
            "                    habits. This grows as you see patterns; promote anything "
            "repeated\n"
            "                    into a standing trait. Distil — prune yesterday's one-off "
            "detail.\n"
            "    positive_to   - whom they get along with / support / align with, and how.\n"
            "    negative_to   - whom they clash with / push back on / disagree with, and how.\n"
            "  A profile is a STANDING portrait, not a diary; never write \"<date>: did X\" "
            "lines."
            + timeline_para
        )

    # ---- view --------------------------------------------------------------
    def _person_keys(self, store) -> list[str]:
        keys = set()
        for path in store.list_paths("people/"):
            parts = path.split("/")
            if len(parts) == 3 and parts[0] == "people":
                keys.add(parts[1])
        return sorted(keys)

    def _person_files(self, store, key: str) -> list[str]:
        return [
            p for p in store.list_paths(f"people/{key}/")
            if p.split("/")[:2] == ["people", key] and len(p.split("/")) == 3
        ]

    def _person_years(self, store, key: str) -> list[str]:
        years = []
        for p in self._person_files(store, key):
            seg = p.split("/")[2]
            seg = seg[:-3] if seg.endswith(".md") else seg
            if _YEAR_RE.match(seg):
                years.append(seg)
        return sorted(years)

    def _last_updated(self, store, key: str) -> str:
        return max((store.last_updated(p) for p in self._person_files(store, key)), default="")

    def view_blocks(self, store, ctx: ViewContext) -> list[ViewBlock]:
        blocks: list[ViewBlock] = []
        folded: dict[str, list[str]] = {m: [] for m in _FOLD_MARKERS}
        timeline = self.feature("timeline")
        for key in self._person_keys(store):
            status = store.files.get(f"people/{key}/status.md", "").strip()
            marker = next((m for m in _FOLD_MARKERS if status.lower().startswith(m)), None)
            if marker:
                tail = status[len(marker):].lstrip(" —-:").strip()
                folded[marker].append(f"{key} ({tail})" if tail else key)
                continue
            lines = [f"## {key}"]
            for section in PERSON_SECTIONS:
                body = store.files.get(f"people/{key}/{section}.md", "").strip()
                if body:
                    lines.append(f"### {_SECTION_DISPLAY[section]}\n{body}")
            if timeline:
                for year in self._person_years(store, key):
                    body = store.files.get(f"people/{key}/{year}.md", "").strip()
                    if body:
                        lines.append(f"### {year}\n{body}")
            if len(lines) > 1:
                blocks.append(ViewBlock(
                    "\n\n".join(lines),
                    order=(self._last_updated(store, key), key),
                ))
        # Folded blocks go FIRST (oldest) so they are cropped before full profiles.
        if folded["minor"]:
            blocks.append(ViewBlock(
                "**Minor participants** (too little data to profile)\n"
                + ", ".join(folded["minor"]),
                order=("",),
            ))
        if folded["faded"]:
            blocks.append(ViewBlock(
                "**Faded** (mattered once, no longer active or relevant)\n"
                + ", ".join(folded["faded"]),
                order=("",),
            ))
        return blocks

    def view_title(self) -> str:
        return "People and Roles"

    # ---- tasks -------------------------------------------------------------
    def _people_view_text(self, store) -> str:
        ctx = ViewContext(relative_date=None, kind="chat", platform="chat")
        blocks = [b.text for b in self.view_blocks(store, ctx) if b.text.strip()]
        if not blocks:
            return "(no people recorded yet)"
        return f"# {self.view_title()}\n\n" + "\n\n".join(blocks)

    def prune_hint(self) -> str:
        return ("identifiers, every aka/alias and any contested points — restate as a tight "
                "STANDING portrait; never invent or merge @hashes")

    def ingest_groups(self, atoms: list) -> list[tuple[str, list]]:
        """One ingest run PER PERSON: group atoms by their leaf author AND by every REAL
        person tagged in them. A tagged-but-silent person (e.g. a lecturer who answers off-
        channel) gets their own run from the atoms that address them, so we can record what
        is said TO/ABOUT them — not just what they type. An atom is fed to its author's group
        and to each mentioned person's group; people's task count tracks the number of
        distinct people in the batch, not the atom count."""
        groups: dict[str, list] = {}
        for a in atoms:
            keys = set(getattr(a, "mentions", None) or [])
            keys.add(a.sender or "")
            for k in keys:
                groups.setdefault(k, []).append(a)
        return list(groups.items())

    def task_rules(self) -> list[TaskRule]:
        timeline = self.feature("timeline")
        timeline_clause = (
            ", and the per-year timeline file people/<@hash>/YYYY.md when year-specific "
            "detail accrues"
            if timeline else ""
        )

        def build_ingest(ctx: RuleContext) -> list[Task] | None:
            prompt = (
                "PEOPLE: for each person @hash active in these new messages — whether they "
                "SPOKE or were clearly TAGGED/ADDRESSED in them — update their people/<@hash>/ "
                "files: status, aka (aliases used in chat), personality, positive_to, "
                "negative_to" + timeline_clause + ". You may record what is said TO or ABOUT a "
                "tagged person even if they never type here (e.g. someone answers off-channel). "
                "A profile is a STANDING portrait, not a diary. NEVER invent or merge @hashes; "
                "use only an @hash that actually appears in the data (as a speaker or a "
                "mention). If a tagged @hash is not a routable person, the store will tell you "
                "— just skip it."
            )
            return [Task(
                priority=BASE_PRIORITY["ingest"],
                created_batch=ctx.batch_index,
                section=self.name,
                name="ingest",
                target="",
                prompt=prompt,
                dedup_key="people:ingest",
            )]

        def build_relevance(ctx: RuleContext) -> list[Task] | None:
            rel = ctx.view.relative_date or "the latest day"
            view_text = self._people_view_text(ctx.store)
            prompt = (
                f"PEOPLE relevance refresh, as of {rel}. Re-judge each person's STANDING "
                "relevance — abstract and domain-neutral. Update ONLY people/<@hash>/status:\n"
                "  - KEEP relevant: anyone still active, OR who holds a standing role, an open "
                "commitment, or an unresolved thread — even if quiet. A departed lead/founder "
                "who still shapes how the group works stays relevant; leaving is not fading.\n"
                "  - FADE: someone long inactive who left no lasting mark — a passer-through "
                "with a couple of contributions, no standing role, no open commitment, no live "
                "thread. Begin their status with \"faded — <one short why>\".\n"
                "  - PROMOTE: a newcomer who has gained a real role/contributions earns a fuller "
                "profile; drop \"faded\"/\"minor\" from anyone who matters again.\n"
                "Relevance is about importance NOW, not recent chatter volume. When unsure, "
                "KEEP. Never invent or merge @hashes.\n\n"
                "Current people view:\n\n" + view_text
            )
            return [Task(
                priority=BASE_PRIORITY[OnFinish],
                created_batch=ctx.batch_index,
                section=self.name,
                name="relevance",
                target="",
                prompt=prompt,
                dedup_key="people:relevance",
            )]

        return [
            TaskRule(
                trigger=OnEntry(kinds=frozenset({Kind.MESSAGE, Kind.POST, Kind.COMMENT})),
                name="ingest",
                build=build_ingest,
            ),
            TaskRule(trigger=OnFinish(), name="relevance", build=build_relevance),
        ]


def section(features: dict | None = None) -> Section:
    return PeopleSection(features)
