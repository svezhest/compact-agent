"""The STUDY section — a per-course, per-term academic record.

A domain-neutral TEMPLATE: it bakes in NO course names and NO subject knowledge of its
own. It only gives the model a place — ``study/<course>/<YYYY>-fall|spring.md`` — and asks
it to pull whatever academic substance a conversation carries into the right file: the
topics and concepts taught, the books/papers/resources cited, assignments, exam info,
worked problems, and open questions. What counts as a "course" and how the note is
organised is inferred per chat and left to the model; the body is FREE-FORM prose.

The bucket is an ACADEMIC TERM named by its SEASON, not by an ordinal semester number.
Season labelling is deliberate: "semester 2" means different things to a master's, a
bachelor's, or a 5-year programme student in the very same chat, but ``2024-fall`` is
unambiguous for everyone — it bins by WHEN the material happened, independent of any
programme's numbering. It also keeps the Dec 1 / May 1 exam-prep deadlines on the right
term. One file per ``study/<course>/<YYYY>-fall|spring.md``:
  * ``<course>`` — a short lowercase slug the model infers for the subject/course/reading
    group/self-study track. One slug per distinct learning thread, reused across terms.
  * ``fall`` = autumn/winter term (months Aug–Jan), winter exams ~December–January.
  * ``spring`` = spring/summer term (months Feb–Jul), summer exams ~May–June.

Keys read in chronological order (``2024-fall`` < ``2025-spring`` < ``2025-fall``).
Everything here stays generic across any field — the section knows about terms and exams,
never about a particular subject.
"""

from __future__ import annotations

import re

from compact_agent.sections.base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnEntry, OnTimePass, Kind,
    BASE_PRIORITY,
)
from compact_agent.sections.store import StoreError

_COURSE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PERIOD_RE = re.compile(r"^\d{4}-(fall|spring)$")
_CHAR_LIMIT = 4000

#: within a calendar year, spring (Feb–Jul) precedes fall (Aug–Jan+1) chronologically.
_SEASON_ORDER = {"spring": 0, "fall": 1}


def _period_for(date: str) -> str:
    """Map a chat-time ``YYYY-MM-DD`` to its academic-term period key by SEASON.

    Months 8–12 -> that year's ``fall``; January belongs to the PREVIOUS year's fall
    term (so 2025-01 -> ``2024-fall``). Months 2–7 -> that year's ``spring``.
    """
    year, month = int(date[:4]), int(date[5:7])
    if month == 1:
        return f"{year - 1}-fall"
    if 8 <= month <= 12:
        return f"{year}-fall"
    return f"{year}-spring"  # months 2–7


def _order_of(key: str) -> tuple[int, int, str]:
    """(year, season, course) sort key — oldest term first, chronological."""
    course = key.split("/")[1]
    period = key.rsplit("/", 1)[-1][:-3]  # "YYYY-fall" / "YYYY-spring"
    year, season = period.split("-")
    return (int(year), _SEASON_ORDER.get(season, 0), course)


class StudySection(Section):
    name = "study"
    default_features: dict[str, bool] = {}

    # ---- 2) paths ----------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return bool(raw_parts) and raw_parts[0] == "study"

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        if len(raw_parts) != 3:
            raise StoreError(
                "Bad study path. Use study/<course>/<YYYY>-fall or -spring "
                "(course slug and term season)."
            )
        course = raw_parts[1].lower()
        if not _COURSE_RE.match(course):
            raise StoreError(
                f"Bad study course '{raw_parts[1]}'. Use a short lowercase slug of "
                "letters, digits and hyphens (no spaces or odd characters)."
            )
        period = raw_parts[2][:-3] if raw_parts[2].endswith(".md") else raw_parts[2]
        if not _PERIOD_RE.match(period):
            raise StoreError(
                f"Bad study period '{period}'. Use study/<course>/<YYYY>-fall or -spring "
                "(name the term by SEASON, never by an ordinal semester number)."
            )
        return f"study/{course}/{period}.md"

    def char_limit(self, key: str) -> int:
        return _CHAR_LIMIT

    def paths_doc(self) -> str:
        return (
            "## Study — academic record\n"
            "Paths: `study/<course>/<YYYY>-fall.md` or `study/<course>/<YYYY>-spring.md` — "
            "one file per COURSE per TERM.\n"
            "`<course>` is a short lowercase slug YOU infer for the subject/course/"
            "reading-group/self-study track the chat is about (e.g. its topic area); use "
            "one slug per distinct learning thread and reuse it across terms. The term is "
            "named by its SEASON (`fall`/`spring`), NEVER by an ordinal semester number — "
            "'semester 2' means different things to a master's vs a bachelor's vs a "
            "5-year student in the same chat, but `2024-fall` is unambiguous for all.\n\n"
            "Use this section ONLY when a conversation actually carries learning/academic "
            "substance (a course, lectures, coursework, a study or reading group, "
            "self-study). If it is just ordinary chat, leave study empty.\n\n"
            "Each file is the durable academic record for that course that term. "
            "Capture whatever the chat teaches: topics and concepts covered, definitions "
            "and results, books / papers / links / tools cited, assignments given or "
            "submitted, worked problems, exam dates and exam topics, and open questions "
            "or gaps. Capture the ACTUAL academic content — concrete topics, titles, "
            "terms — not vague summaries.\n\n"
            "Terms by season:\n"
            "- `fall` = AUTUMN/WINTER term, roughly August–January; winter exams ~Dec–Jan.\n"
            "- `spring` = SPRING/SUMMER term, roughly February–July; summer exams ~May–Jun.\n\n"
            "Pick the file from a message's date by season:\n"
            "- Months 8–12 (Aug–Dec) -> that year's `fall`. January belongs to the "
            "PREVIOUS year's fall term (so January 2027 -> `2026-fall`).\n"
            "- Months 2–7 (Feb–Jul) -> that year's `spring`.\n\n"
            "Each file is a course AS TAUGHT IN ONE TERM. Put new material in the file for "
            "the term the MATERIAL BELONGS TO — decide by WHEN IT WAS STUDIED, not merely "
            "the calendar date of the message:\n"
            "- Live, ongoing coursework -> the CURRENT term of the message date. When a "
            "continuing course moves into a new term, START that term's file "
            "(`study/<same-course>/<new-period>.md`) instead of appending the new term into "
            "the previous term's file — that is the next term, not a duplicate.\n"
            "- A later, passing reference to an EARLIER course (e.g. months on, someone "
            "mentions an update to last year's topic) belongs to that course's EXISTING "
            "file — do NOT mint a new future term for it, and if it carries no real new "
            "coursework, record nothing.\n\n"
            "The body is FREE-FORM: organise it however best fits this material — there is "
            "no required structure. The path is the heading; do not repeat the course or "
            "term as a markdown heading inside the body."
        )

    # ---- 3) view -----------------------------------------------------------
    def view_blocks(self, store, ctx: ViewContext) -> list[ViewBlock]:
        blocks: list[ViewBlock] = []
        for key in store.list_paths("study/"):
            body = store.read(key)
            if not body.strip():
                continue
            period = key.rsplit("/", 1)[-1][:-3]   # "YYYY-fall" / "YYYY-spring"
            course = key.split("/")[1]
            text = f"## {course} {period}\n\n{body}"
            blocks.append(ViewBlock(text=text, order=_order_of(key)))
        blocks.sort(key=lambda b: b.order)
        return blocks

    def view_title(self) -> str:
        return "Study"

    def prune_hint(self) -> str:
        return ("the key topics, results, exam-relevant material, assignments, resources "
                "and open gaps")

    # ---- 4) tasks ----------------------------------------------------------
    def task_rules(self) -> list[TaskRule]:
        return [
            TaskRule(
                trigger=OnEntry(kinds=frozenset({Kind.MESSAGE, Kind.COMMENT, Kind.POST})),
                name="ingest",
                build=self._build_ingest,
            ),
            TaskRule(
                trigger=OnTimePass(deadlines=("12-01", "05-01")),
                name="exam-prep-consolidation",
                build=self._build_consolidation,
            ),
        ]

    # ---- builders ----------------------------------------------------------
    def _build_ingest(self, ctx: RuleContext) -> list[Task] | None:
        prompt = (
            "STUDY: If (and only if) the new data carries academic/learning substance, "
            "record it.\n"
            "FIRST, ALWAYS: call list_files('study/') to see the courses that already "
            "exist. The course slug is free-form and you choose it, so you MUST see the "
            "existing list — otherwise you will invent a near-duplicate slug for a course "
            "that is already there and fork it. The slug is the path, so the LIST is enough "
            "to dedup; you do NOT need to read every course. Match new material to an "
            "existing course/file whenever it is the same subject; only create a new "
            "`study/<course>/...` when it is genuinely a different course. THEN read only "
            "the specific file(s) you are about to write to (you must read a file before "
            "editing it).\n"
            "THEN pull out the actual content — topics and concepts covered, definitions "
            "and results, books/papers/links/tools cited, assignments given or submitted, "
            "worked problems, exam dates and topics, and open questions. Write it into the "
            "right file, by the term the material BELONGS TO (when it was studied), not "
            "merely the message date. Reuse an existing course's SLUG. Name the term by "
            "SEASON: months 8–12 -> `<YYYY>-fall` (January -> the previous year's fall); "
            "months 2–7 -> `<YYYY>-spring`. For live, ongoing coursework use the current "
            "term of the date; when a continuing course rolls into a new term, START "
            "`study/<same-course>/<new-period>.md` rather than appending the new term into "
            "the old term's file. But a later passing reference to an EARLIER course goes "
            "into that course's EXISTING file — do NOT create a new future term for it. "
            "Within a file the body is free-form — organise it sensibly and MERGE rather "
            "than duplicating. If the messages carry no real learning content, do nothing."
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

    def _build_consolidation(self, ctx: RuleContext) -> list[Task] | None:
        """One task PER current-term course file — never a single run that reads every
        course (that scales with store size and blows the per-run tool-call cap). The
        deadline names the term ending: the current date's `_period_for` is the term
        whose exams are near, so we only touch files for that period."""
        boundary = ctx.boundary or ""
        period = _period_for(ctx.store.now) if ctx.store.now else None
        suffix = f"/{period}.md" if period else ".md"
        targets = [
            k for k in ctx.store.list_paths("study/")
            if k.endswith(suffix) and ctx.store.read(k).strip()
        ]
        when = f" (deadline {boundary})" if boundary else ""
        tasks: list[Task] = []
        for path in targets:
            prompt = (
                f"STUDY: Exams for this term are about a month away{when}. Read `{path}` "
                "and consolidate it into an exam-prep-ready summary: the key topics and "
                "results covered, the material most likely to be examined, the core "
                "resources, and any open gaps still to close. Keep it tight and durable, "
                "and overwrite the file with the consolidated form."
            )
            tasks.append(Task(
                priority=BASE_PRIORITY[OnTimePass] + 5.0,
                created_batch=ctx.batch_index,
                section=self.name,
                name="exam-prep-consolidation",
                target=path,
                prompt=prompt,
                dedup_key=f"{self.name}:deadline-consolidation:{boundary}:{path}",
            ))
        return tasks or None

def section(features: dict | None = None) -> Section:
    return StudySection(features)
