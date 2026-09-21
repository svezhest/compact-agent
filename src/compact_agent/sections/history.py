"""The HISTORY section — a running, recency-tapered chronicle at four granularities.

A time-ordered record the model both READS and EDITS, at day / month / quarter / year
resolution (``history/<period>.md``). The assembled view is recency-tapered: whole prior
years, then the current year's earlier quarters, then the current quarter's earlier
months, then the current month's days up to the latest — each tier falling back to finer
files when its rollup is missing, so no period's detail is silently dropped.

The recency-taper, the rollup-gap detector, and the bottom-up rebuild plan are PORTED
from the legacy ``report_store.py`` (operating on ``store.files`` instead of ``self``).
Everything stays domain-neutral.
"""

from __future__ import annotations

import calendar
import re

from .base import (
    Section, ViewBlock, ViewContext, TaskRule, Task, RuleContext,
    OnEntry, OnFinish, OnTimePass, OnInit, OnLaunch, Kind,
    BASE_PRIORITY,
)
from .store import StoreError

# The section's paths_doc, injected into the system prompt. Lives here (the section is the
# source of truth for its own slice), not in a shared prompts module.
_HISTORY_GUIDANCE = """\
history/<period>.md — the running historical record, at four granularities you may
both READ and EDIT:
    history/<YYYY-MM-DD>.md  a single day
    history/<YYYY-MM>.md     a month
    history/<YYYY-qN>.md     a quarter
    history/<YYYY>.md        a whole year
  Write each NEW day's substance to its history/<YYYY-MM-DD>.md — decisions AND the
  debate behind them: disagreements, who pushed back and why, alternatives, blockers,
  incidents, open questions. Do not flatten a contested discussion into one tidy
  conclusion. As time moves on, roll detail upward yourself: summarise a finished
  month's days into its history/<YYYY-MM>.md, a finished quarter's months into its
  history/<YYYY-qN>.md, and so on — broader strokes at higher levels. Read the
  lower-level files when you need the detail to roll up. The assembled report already
  shows the right granularity per period; your job is to keep each level faithful and
  current."""

# ---- period grammar (ported) -------------------------------------------------
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")
_QUARTER_RE = re.compile(r"^(\d{4})-q([1-4])$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# Soft per-file char limits — history widens as it rolls up (day < month < quarter < year).
_LIMIT_DAY = 4000
_LIMIT_MONTH = 6000
_LIMIT_QUARTER = 6000
_LIMIT_YEAR = 10000


def _valid_ymd(date: str) -> bool:
    """Calendar-valid YYYY-MM-DD (rejects month 13, day 32, Feb 30, ...)."""
    if not _DATE_RE.match(date):
        return False
    y, m, d = (int(x) for x in date.split("-"))
    if not (1 <= m <= 12):
        return False
    return 1 <= d <= calendar.monthrange(y, m)[1]


def _valid_history_period(period: str) -> bool:
    if _YEAR_RE.match(period):
        return True
    if _QUARTER_RE.match(period):
        return True
    if _MONTH_RE.match(period):
        return 1 <= int(period.split("-")[1]) <= 12
    return _valid_ymd(period)


def _quarter(month: int) -> int:
    return (month - 1) // 3 + 1


def _has_content(body: str | None) -> bool:
    """True if a file holds real substance, not a rolled-up breadcrumb pointer.

    When detail is rolled into a coarser period the fine file is often left as a short
    wholly-parenthesised pointer like "(rolled into 2025-q3.md)". The tapered view must
    treat such a file as ABSENT and fall through to the level that actually holds the
    content, rather than displaying the breadcrumb."""
    b = (body or "").strip()
    if not b:
        return False
    if len(re.findall(r"\w", b, flags=re.UNICODE)) < 2:
        return False
    return not (b.startswith("(") and b.endswith(")") and len(b) < 160)


def _history_display(period: str) -> str:
    if _YEAR_RE.match(period):
        return period
    if mo := _QUARTER_RE.match(period):
        return f"{mo.group(1)} Q{mo.group(2)}"
    if _MONTH_RE.match(period):
        y, m = (int(x) for x in period.split("-"))
        return f"{y} {calendar.month_name[m]}"
    return period  # YYYY-MM-DD


def _order_key(period: str) -> str:
    """A sortable start-date string so the NEWEST periods survive cropping."""
    if _YEAR_RE.match(period):
        return f"{period}-00-00"
    if mo := _QUARTER_RE.match(period):
        return f"{mo.group(1)}-{(int(mo.group(2)) - 1) * 3 + 1:02d}-00"
    if _MONTH_RE.match(period):
        return f"{period}-00"
    return period  # YYYY-MM-DD


class HistorySection(Section):
    name = "history"
    default_features: dict[str, bool] = {}

    # ---- 2) paths ----------------------------------------------------------
    def owns(self, raw_parts: list[str]) -> bool:
        return bool(raw_parts) and raw_parts[0] == "history"

    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        if len(raw_parts) != 2:
            raise StoreError("A history path is 'history/<period>'.")
        period = raw_parts[1][:-3] if raw_parts[1].endswith(".md") else raw_parts[1]
        period = period.lower()
        if not _valid_history_period(period):
            raise StoreError(
                f"Bad history period '{period}'. Use YYYY, YYYY-qN, YYYY-MM, or "
                "YYYY-MM-DD."
            )
        return f"history/{period}.md"

    def char_limit(self, key: str) -> int:
        period = key[len("history/"):-3] if key.startswith("history/") else key
        if _YEAR_RE.match(period):
            return _LIMIT_YEAR
        if _QUARTER_RE.match(period):
            return _LIMIT_QUARTER
        if _MONTH_RE.match(period):
            return _LIMIT_MONTH
        return _LIMIT_DAY

    def paths_doc(self) -> str:
        return _HISTORY_GUIDANCE

    # ---- ported store helpers (operate on store.files) ---------------------
    def _history_index(self, store) -> dict[str, set]:
        """Group existing history files by tier for fallback descent."""
        idx: dict[str, set] = {"year": set(), "quarter": set(), "month": set(), "day": set()}
        for path in store.files:
            if not (path.startswith("history/") and path.endswith(".md")):
                continue
            period = path[len("history/"):-3]
            if _YEAR_RE.match(period):
                idx["year"].add(int(period))
            elif mo := _QUARTER_RE.match(period):
                idx["quarter"].add((int(mo.group(1)), int(mo.group(2))))
            elif _MONTH_RE.match(period):
                y, m = (int(x) for x in period.split("-"))
                idx["month"].add((y, m))
            elif _DATE_RE.match(period):
                idx["day"].add(period)
        return idx

    def _live(self, store, period: str) -> bool:
        return _has_content(store.files.get(f"history/{period}.md", ""))

    def _periods_for_month(self, store, idx, y: int, m: int, max_day: str | None) -> list[str]:
        if (y, m) in idx["month"] and self._live(store, f"{y}-{m:02d}"):
            return [f"{y}-{m:02d}"]
        return sorted(
            d for d in idx["day"]
            if d.startswith(f"{y}-{m:02d}-") and (max_day is None or d <= max_day)
            and self._live(store, d)
        )

    def _periods_for_quarter(self, store, idx, y: int, q: int) -> list[str]:
        if (y, q) in idx["quarter"] and self._live(store, f"{y}-q{q}"):
            return [f"{y}-q{q}"]
        out: list[str] = []
        for m in range((q - 1) * 3 + 1, (q - 1) * 3 + 4):
            out.extend(self._periods_for_month(store, idx, y, m, None))
        return out

    def _periods_for_year(self, store, idx, y: int) -> list[str]:
        if y in idx["year"] and self._live(store, f"{y}"):
            return [f"{y}"]
        out: list[str] = []
        for q in (1, 2, 3, 4):
            out.extend(self._periods_for_quarter(store, idx, y, q))
        return out

    def _tapered_history_periods(self, store, relative_date: str) -> list[tuple[str, str]]:
        """Ordered (period, file_key) for the recency-tapered view."""
        ly, lm, _ = (int(x) for x in relative_date.split("-"))
        lq = _quarter(lm)
        idx = self._history_index(store)
        periods: list[str] = []

        prior_years = {y for y in idx["year"] if y < ly}
        prior_years |= {y for (y, _q) in idx["quarter"] if y < ly}
        prior_years |= {y for (y, _m) in idx["month"] if y < ly}
        prior_years |= {int(d[:4]) for d in idx["day"] if int(d[:4]) < ly}
        for y in sorted(prior_years):
            periods.extend(self._periods_for_year(store, idx, y))

        for q in range(1, lq):  # current year's earlier, completed quarters
            periods.extend(self._periods_for_quarter(store, idx, ly, q))

        cq: list[str] = []
        for m in range((lq - 1) * 3 + 1, lm):  # current quarter's earlier months
            cq.extend(self._periods_for_month(store, idx, ly, m, None))
        cur_days = [
            d for d in sorted(idx["day"])
            if d.startswith(f"{ly}-{lm:02d}-") and d <= relative_date and self._live(store, d)
        ]
        if cur_days:
            cq.extend(cur_days)
        elif self._live(store, f"{ly}-{lm:02d}"):
            cq.append(f"{ly}-{lm:02d}")
        if cq:
            periods.extend(cq)
        elif self._live(store, f"{ly}-q{lq}"):
            periods.append(f"{ly}-q{lq}")
        elif self._live(store, f"{ly}"):
            periods.append(f"{ly}")

        out: list[tuple[str, str]] = []
        for p in periods:
            key = f"history/{p}.md"
            if _has_content(store.files.get(key, "")):
                out.append((p, key))
        return out

    def _rollup_gaps(self, store, relative_date: str) -> list[tuple[str, list[str]]]:
        """Completed periods that SHOULD be rolled up but aren't yet."""
        ly, lm, _ = (int(x) for x in relative_date.split("-"))
        lq = _quarter(lm)
        idx = self._history_index(store)

        def year_sources(y: int) -> list[str]:
            return sorted(
                [f"history/{p}.md" for p in
                 [f"{yy}-q{q}" for (yy, q) in idx["quarter"] if yy == y]
                 + [f"{yy}-{m:02d}" for (yy, m) in idx["month"] if yy == y]
                 + [d for d in idx["day"] if int(d[:4]) == y]]
            )

        def quarter_sources(y: int, q: int) -> list[str]:
            months = range((q - 1) * 3 + 1, (q - 1) * 3 + 4)
            return sorted(
                [f"history/{y}-{m:02d}.md" for (yy, m) in idx["month"] if yy == y and m in months]
                + [f"history/{d}.md" for d in idx["day"]
                   if int(d[:4]) == y and _quarter(int(d[5:7])) == q]
            )

        def month_sources(y: int, m: int) -> list[str]:
            return sorted(f"history/{d}.md" for d in idx["day"] if d.startswith(f"{y}-{m:02d}-"))

        gaps: list[tuple[str, list[str]]] = []
        prior_years = {y for y in idx["year"] if y < ly}
        prior_years |= {y for (y, _q) in idx["quarter"] if y < ly}
        prior_years |= {y for (y, _m) in idx["month"] if y < ly}
        prior_years |= {int(d[:4]) for d in idx["day"] if int(d[:4]) < ly}
        for y in sorted(prior_years):
            if y not in idx["year"]:
                src = year_sources(y)
                if src:
                    gaps.append((f"history/{y}.md", src))
        for q in range(1, lq):
            if (ly, q) not in idx["quarter"]:
                src = quarter_sources(ly, q)
                if src:
                    gaps.append((f"history/{ly}-q{q}.md", src))
        for m in range((lq - 1) * 3 + 1, lm):
            if (ly, m) not in idx["month"]:
                src = month_sources(ly, m)
                if src:
                    gaps.append((f"history/{ly}-{m:02d}.md", src))
        return gaps

    def _rebuild_plan(self, store) -> list[tuple[str, list[str]]]:
        """Deterministic BOTTOM-UP rebuild order for the final consolidation pass."""
        idx = self._history_index(store)
        plan: list[tuple[str, list[str]]] = []

        months = set(idx["month"]) | {(int(d[:4]), int(d[5:7])) for d in idx["day"]}
        for (y, m) in sorted(months):
            days = sorted(f"history/{d}.md" for d in idx["day"]
                          if d.startswith(f"{y}-{m:02d}-"))
            if days:
                plan.append((f"history/{y}-{m:02d}.md", days))

        quarters = set(idx["quarter"]) | {(y, _quarter(m)) for (y, m) in months}
        for (y, q) in sorted(quarters):
            msrc = sorted(f"history/{yy}-{m:02d}.md" for (yy, m) in months
                          if yy == y and _quarter(m) == q)
            if msrc:
                plan.append((f"history/{y}-q{q}.md", msrc))

        years = set(idx["year"]) | {y for (y, _q) in quarters}
        for y in sorted(years):
            qsrc = sorted(f"history/{yy}-q{q}.md" for (yy, q) in quarters if yy == y)
            if qsrc:
                plan.append((f"history/{y}.md", qsrc))
        return plan

    def _latest_day(self, store) -> str | None:
        days = [
            p[len("history/"):-3]
            for p in store.files
            if p.startswith("history/") and _DATE_RE.match(p[len("history/"):-3])
        ]
        return max(days) if days else None

    # ---- 3) view -----------------------------------------------------------
    def view_blocks(self, store, ctx: ViewContext) -> list[ViewBlock]:
        relative_date = ctx.relative_date or self._latest_day(store)
        if relative_date is None:
            return []
        blocks: list[ViewBlock] = []
        for period, key in self._tapered_history_periods(store, relative_date):
            body = store.files.get(key, "").strip()
            if not _has_content(body):
                continue
            text = f"## {_history_display(period)}\n{body}"
            blocks.append(ViewBlock(text=text, order=_order_key(period)))
        blocks.sort(key=lambda b: b.order)
        return blocks

    def view_title(self) -> str:
        return "History"

    def prune_hint(self) -> str:
        return "the decisions, the contested points and the shape of what happened"

    def ingest_groups(self, atoms: list) -> list[tuple[str, list]]:
        """One ingest run PER DAY: group the batch's atoms by their leaf calendar day, so
        each run writes exactly one `history/<YYYY-MM-DD>.md`. History's ingest task count
        tracks the number of distinct days in the batch — its natural unit."""
        by_day: dict[str, list] = {}
        for a in atoms:
            by_day.setdefault(a.leaf_date[:10], []).append(a)
        return sorted(by_day.items())

    # ---- 4) tasks ----------------------------------------------------------
    def task_rules(self) -> list[TaskRule]:
        return [
            TaskRule(
                trigger=OnEntry(kinds=frozenset({Kind.MESSAGE, Kind.POST, Kind.COMMENT})),
                name="ingest",
                build=self._build_ingest,
            ),
            TaskRule(
                trigger=OnTimePass(every_days=90),
                name="rollup",
                build=self._build_rollup,
            ),
            TaskRule(
                trigger=OnFinish(),
                name="consolidate",
                build=self._build_consolidate,
            ),
        ]

    # ---- builders ----------------------------------------------------------
    def _build_ingest(self, ctx: RuleContext) -> list[Task] | None:
        prompt = (
            "HISTORY: Record each NEW day's substance to its history/<YYYY-MM-DD>.md "
            "file — decisions AND the debate behind them: disagreements, who pushed back "
            "and why, alternatives considered, blockers, incidents, and open questions. "
            "Every processed day needs its own day file. Do NOT flatten a contested "
            "discussion into one tidy conclusion; preserve the friction. Merge into the "
            "existing day file rather than duplicating."
        )
        return [Task(
            priority=BASE_PRIORITY["ingest"],
            created_batch=ctx.batch_index,
            section=self.name,
            name="ingest",
            target="",
            prompt=prompt,
            dedup_key="history:ingest",
        )]

    def _build_rollup(self, ctx: RuleContext) -> list[Task] | None:
        relative_date = ctx.view.relative_date or self._latest_day(ctx.store)
        if relative_date is None:
            return None
        gaps = self._rollup_gaps(ctx.store, relative_date)
        if not gaps:
            return None
        # ONE task per completed period (a finished month / quarter / year), not one mega
        # task — each rolls a single parent from its children, dedup_keyed by its target so
        # a still-open gap collapses across batches and re-fires only if unmet.
        tasks: list[Task] = []
        for target, src in gaps:
            prompt = (
                f"HISTORY: A period has completed — fold it upward. READ {', '.join(src)} "
                f"and WRITE {target} in broader strokes (a coarser period summarises its "
                "children): keep the shape of what happened and any still-contested points, "
                "drop fine day-by-day detail. Merge into the existing file if present."
            )
            tasks.append(Task(
                priority=BASE_PRIORITY[OnTimePass],
                created_batch=ctx.batch_index,
                section=self.name,
                name="rollup",
                target=target,
                prompt=prompt,
                dedup_key=f"history:rollup:{target}",
            ))
        return tasks

    def _build_consolidate(self, ctx: RuleContext) -> list[Task] | None:
        plan = self._rebuild_plan(ctx.store)
        if not plan:
            return None
        lines = "\n".join(
            f"  - {target} from: {', '.join(src)}" for target, src in plan
        )
        prompt = (
            "HISTORY: Final pass — REVISIT each rolled-up period bottom-up (months from "
            "their days, quarters from their months, years from their quarters) and make "
            "sure no detail was lost or contradicted along the way. This is a faithful "
            "correction pass, NOT a rewrite: only fix what is missing or wrong, and do "
            "not bloat the higher-level files.\n"
            f"{lines}"
        )
        return [Task(
            priority=BASE_PRIORITY[OnFinish],
            created_batch=ctx.batch_index,
            section=self.name,
            name="consolidate",
            target="",
            prompt=prompt,
            dedup_key="history:consolidate",
        )]

def section(features: dict | None = None) -> Section:
    return HistorySection(features)
