"""The compaction engine — domain-neutral orchestration over as-code sections.

One knob (``MAX_CONTEXT_TOKENS = W``) shapes a run. Per batch the window splits into a
summary side (``W*SUMMARY_SHARE``: system prompt + the assembled section views) and a
workflow side (the rest: incoming data points + tool-call rounds). Sections declare
everything specific; the engine only:

  1. assembles the system prompt + the budget-cropped views,
  2. builds the ATOMS (reply chains as Sets) and packs them into ⅓cnt batches,
  3. per batch, ingests the batch's fresh (section × atom) work — each section runs its own
     bounded passes at its own granularity (`Section.ingest_groups`), tracked idempotently
     per (section × atom) in `done_atoms` (new / partial / finished) — then fires the other
     TaskRules (time_pass / file_overflow / finish) into a persistent priority queue and
     drains it in priority order until the workflow budget is spent,
  4. checkpoints the store + done_atoms + queue + fire-times after every batch (resumable).

There is no Big Prune: the views are cropped non-destructively to fit context, and every
oversized file is bounded by the engine's default prune (flavoured by `Section.prune_hint`)
unless a section declares its own `OnFileOverflow` rule.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic_ai import Agent, ModelRetry

from . import ui
from .config import Config
from .identity import build_identity, write_vault
from .llm import (
    ContextOverflowError,
    _build_model,
    measure_tool_schema_tokens,
    run_with_retries,
)
from .atom import build_atoms, pack_atom_batches, render_atom_content
from .render import estimate_tokens, group_by_day
from .runbudget import compute_run_budget, summary_share
from .sections.assemble import assemble
from .sections.base import (
    AGE_BONUS,
    BASE_PRIORITY,
    Kind,
    OnEntry,
    OnFileOverflow,
    OnFinish,
    OnInit,
    OnLaunch,
    OnTimePass,
    RuleContext,
    Section,
    Task,
    ViewContext,
)
from .sections.registry import build_sections, load_spec
from .sections.store import StoreError, VStore
from .storage import ChatStore

_ROUND_TOKENS = 1500  # est tokens one tool-call round adds (bounds request_limit)
# Atoms fed to a section's ingestion run start at this count and grow ×2 while the model
# copes; a truncated run (cap/overflow) halves it. Starting well under the ~40 tool-call
# cap avoids wasteful big runs that cap before settling, while growth keeps cheap sections
# (most atoms need no write) efficient.
_INGEST_CHUNK0 = 24
# Persist (checkpoint + report) after this many newly-finished atoms WITHIN a batch. 1 =
# after every ingest group: a big batch must never lose ingested atoms to a crash/restart.
_PERSIST_EVERY = 1


# --------------------------------------------------------------------------- prompt
_PREAMBLE = """\
You maintain a durable, structured memory of a long conversation by EDITING a small set
of files through tool calls. You are given the CURRENT REPORT (an assembled, recency-
aware view of that memory) plus new data points (messages, posts, or comments). Treat the
report as long-range memory: stay consistent, update what changed, resolve what it leaves
open. Work for ANY kind of conversation with no assumptions about the domain.

TOOLS: read, list_files, query, write, append, edit, touch, patch. Each tool's own description
states exactly what it does and what it REJECTS (its guards) — read those and follow them, so
you spend calls on real edits instead of rejected ones. The store enforces the same rules and
bounces a bad call back to you with the reason; a bounce is recoverable, but avoidable.

The one cross-cutting rule behind several of those guards: to change a file that ALREADY
EXISTS (edit / append / overwrite) you must first have SEEN its current content — it is in
the CURRENT REPORT above, or you call read(path). A brand-new file needs no prior read.

Make as many calls as needed, then end with a one-line note of what changed. Omitting a
file leaves it untouched — only touch what changed; never rewrite a file to restate it.

HEADINGS: the file's PATH is its heading. In the report you SEE, each file's title is
GENERATED FOR YOU from its path/name — so you usually do NOT need a header in a body, and
must never restate the path as one. If a long note genuinely needs internal structure you
may use markdown headers (`##`, `###`, …); their TEXT is auto-normalized to ALL CAPS (a
deliberate convention — headers stand out and structure the compacted view), so write
`## key findings` or `## KEY FINDINGS`, either is fine. This normalization is applied on
write and does NOT touch fenced ``` code blocks — your `#` comments and shebangs survive
verbatim. Target a file PATH, never a heading line, and NEVER pass an empty path.

IDENTITY: people appear ONLY as pseudonymous "@hash" tokens (e.g. @u1a2b3c4d) — these ARE
their identities; you are given no real names and must never invent one. "@me" is already
replaced by the speaker's @hash; "@all"/"@everyone" by the full participant list. NEVER
merge or split people: one @hash is one person, and two different @hashes are NEVER the
same person/alias. Every @hash is a real person — whether they SPOKE or were merely TAGGED
in the data — so a tagged person may get a profile too; if an @hash is not routable for a
profile, the store will tell you and you simply skip it.

TYPED vs FREEFORM: most files are freeform markdown — you write/append/edit their prose. SOME
paths are typed RECORDS: a fixed set of schema-validated fields (THE FILES below says which
paths, and lists their fields and allowed values). Set a typed record with patch(path, {field:
value, ...}) — it is created if absent, and ONLY the fields you pass change (the rest are
preserved). A wrong type or a value outside an allowed set is REJECTED with the offending
field — fix it and patch again. Use query(prefix, where, sort, limit) to find and compare
typed records (e.g. the freshest records of one kind) before you write. Never write/
append/edit a typed path, and never patch a freeform one — the store will redirect you.

THE FILES YOU MAY EDIT
"""

_GROUNDING = """\
GROUNDING: distinguish what was SAID from what you INFER. State observed facts plainly;
mark genuine inferences ("seems", "apparently", "(inferred)"). "unknown" is always
acceptable — never fill a gap with a plausible guess, and a guess must not harden into a
stated fact on a later pass unless new messages support it. Be faithful: never invent,
never duplicate; capture every data point's substance.
"""


def build_system_prompt(sections: list[Section]) -> str:
    files_block = "\n\n".join(s.paths_doc() for s in sections)
    return f"{_PREAMBLE}\n{files_block}\n\n{_GROUNDING}"


# --------------------------------------------------------------------------- agent
def build_writer_agent(cfg: Config, store: VStore, *, system_prompt: str, timeout=None):
    """A pydantic-ai agent that edits ``store`` through write/read tools.

    Bounces a ``StoreError`` (bad path/edit) back as a ``ModelRetry`` so the weak model
    self-corrects instead of failing. Mirrors the old writer agent but over ``VStore``."""
    agent = Agent(_build_model(cfg, timeout), output_type=str, system_prompt=system_prompt, retries=12)

    def _bounce(tool: str, args: str, e: StoreError):
        # Escape the interpolated args/error: a path or message containing "[...]" (e.g. a
        # path-grammar hint like "[/<topic>]", or OCR text) must NOT be parsed as Rich markup.
        from rich.markup import escape
        ui.console.print(
            f"[yellow]↩ {tool}({escape(args)}) rejected:[/yellow] {escape(str(e))}"
        )
        raise ModelRetry(str(e))

    def _require(path: str, content: str, tool: str):
        if not path:
            _bounce(tool, "path=''", StoreError(
                "path is required and must be one of the active sections' documented forms."))
        if not content:
            _bounce(tool, f"path={path!r}", StoreError(
                f"content is required and must be non-empty when writing {path!r}."))

    @agent.tool_plain
    def write(path: str = "", content: str = "", overwrite: bool = False) -> str:
        """Create a NEW file, or replace an existing one with overwrite=true. `content` must
        be non-empty. REJECTED if: the path ALREADY EXISTS and overwrite is false (read it,
        then append/edit, or pass overwrite=true); or you overwrite=true a file you have not
        read/seen first. The PATH carries all structure (see THE FILES); bodies contain NO
        markdown headings."""
        _require(path, content, "write")
        try:
            return f"wrote {store.write(path, content, overwrite=overwrite)}"
        except StoreError as e:
            _bounce("write", f"path={path!r}, overwrite={overwrite}", e)

    @agent.tool_plain
    def touch(path: str = "") -> str:
        """Claim a path with a new EMPTY file. REJECTED if the path ALREADY EXISTS — read and
        edit it instead of re-creating. Rarely needed: prefer write() with real content."""
        if not path:
            _bounce("touch", "path=''", StoreError("touch requires a non-empty `path`."))
        try:
            return f"created {store.touch(path)}"
        except StoreError as e:
            _bounce("touch", f"path={path!r}", e)

    @agent.tool_plain
    def append(path: str = "", content: str = "") -> str:
        """Add a block to the END of a file (creates it if absent). `content` must be
        non-empty. REJECTED if the file ALREADY EXISTS and you have not read/seen it first."""
        _require(path, content, "append")
        try:
            return f"appended to {store.append(path, content)}"
        except StoreError as e:
            _bounce("append", f"path={path!r}", e)

    @agent.tool_plain
    def edit(path: str = "", old: str = "", new: str = "") -> str:
        """Replace the FIRST verbatim occurrence of `old` with `new` in an EXISTING file.
        REJECTED if: `path` or `old` is empty (NEVER call edit with an empty `old`); the file
        does not exist (use write() to create one); or `old` is not found EXACTLY as written.
        `old` must match the file's text character-for-character (spacing/punctuation too) —
        read() the file first and copy the snippet to replace. To ADD new content use append/
        write, not edit."""
        if not path or not old:
            _bounce("edit", f"path={path!r}", StoreError(
                "edit requires a non-empty `path` and `old` (the verbatim text to replace)."))
        try:
            return f"edited {store.edit(path, old, new)}"
        except StoreError as e:
            _bounce("edit", f"path={path!r}", e)

    @agent.tool_plain
    def read(path: str = "") -> str:
        """Return a file's current body ('(empty)' if it does not exist yet). Call this BEFORE
        you edit/append/overwrite a file that already exists — it satisfies the read-before-
        change guard AND gives you the exact text that edit()'s `old` must match."""
        if not path:
            _bounce("read", "path=''", StoreError("read requires a non-empty `path`."))
        try:
            return store.read(path) or "(empty)"
        except StoreError as e:
            _bounce("read", f"path={path!r}", e)

    @agent.tool_plain
    def list_files(prefix: str = "") -> str:
        """List existing file paths, optionally filtered by a prefix (e.g. 'people/'). Call it
        to discover what ALREADY EXISTS before creating, so you reuse the right path (and
        read it) instead of touch/write-ing a duplicate that gets rejected."""
        paths = store.list_paths(prefix)
        return "\n".join(paths) if paths else "(no files yet)"

    @agent.tool_plain
    def patch(path: str = "", fields: dict | None = None) -> str:
        """Set fields on a TYPED JSON record (created if absent), validated against its schema
        (see THE FILES). Non-destructive: only the fields you pass change; the rest are kept. A
        wrong type or a value outside an allowed set is REJECTED with the offending field — fix
        and call again. REJECTED too if `path` is a freeform path (use write/append/edit there)
        or `fields` is empty."""
        if not path:
            _bounce("patch", "path=''", StoreError("patch requires a non-empty `path`."))
        if not fields:
            _bounce("patch", f"path={path!r}", StoreError(
                'patch requires a non-empty `fields` object, e.g. {"status": "open"}.'))
        try:
            return f"patched {store.patch(path, fields)}"
        except StoreError as e:
            _bounce("patch", f"path={path!r}", e)

    @agent.tool_plain
    def query(collection: str = "", where: dict | None = None, sort: str = "",
              limit: int = 20) -> str:
        """Search TYPED records under a path prefix (e.g. 'offers/'). `where` filters by
        field: a plain string matches case-insensitively as a substring; a string led by
        >=,<=,>,< compares numerically; a list matches by membership; dotted keys reach nested
        fields (e.g. 'reward.mean'). `sort` is a field name, optional leading '-'
        for descending (e.g. '-posted_at'). `limit` caps rows. Returns compact JSON; read-only —
        use it to find/compare before writing."""
        if not collection:
            _bounce("query", "collection=''", StoreError(
                "query requires a non-empty `collection` prefix, e.g. 'offers/'."))
        try:
            rows = store.query(collection, where or {}, sort or None, limit)
        except StoreError as e:
            _bounce("query", f"collection={collection!r}", e)
        if not rows:
            return "(no matching records)"
        return json.dumps(rows, ensure_ascii=False, indent=2)

    return agent


# --------------------------------------------------------------------------- queue
class TaskQueue:
    """Persistent priority queue with dedup + anti-starvation aging."""

    def __init__(self, tasks: list[Task] | None = None):
        self._tasks: list[Task] = list(tasks or [])
        self._keys: set[str] = {t.dedup_key for t in self._tasks}

    def add(self, task: Task) -> None:
        # Dedup by dedup_key, first-wins (the older task keeps its aging head start).
        # Atom ingest never passes through the queue — grown-chain supersession is the
        # done_atoms extent check in `_ingest`, not a queue concern.
        if task.dedup_key in self._keys:
            return
        self._keys.add(task.dedup_key)
        self._tasks.append(task)

    def pop_best(self, batch_index: int) -> Task | None:
        if not self._tasks:
            return None
        # Effective priority adds aging so a long-waiting task can't starve.
        def eff(t: Task) -> float:
            return t.priority + AGE_BONUS * max(0, batch_index - t.created_batch)
        self._tasks.sort(key=lambda t: (-eff(t), t.created_batch))
        task = self._tasks.pop(0)
        self._keys.discard(task.dedup_key)
        return task

    def __len__(self) -> int:
        return len(self._tasks)

    def to_list(self) -> list[dict]:
        return [t.to_dict() for t in self._tasks]

    @classmethod
    def from_list(cls, data) -> "TaskQueue":
        return cls([Task.from_dict(d) for d in (data or [])])


# --------------------------------------------------------------------------- engine
def _kinds_in(messages) -> frozenset[Kind]:
    out = set()
    for m in messages:
        if m.post_id is not None:
            out.add(Kind.POST if m.id == m.post_id else Kind.COMMENT)
        else:
            out.add(Kind.MESSAGE)
    return frozenset(out)


def _entry_matches(trigger: OnEntry, present: frozenset[Kind]) -> bool:
    return trigger.kinds is None or bool(trigger.kinds & present)


def _atom_status(done_atoms: dict, section_name: str, atom) -> str:
    """A (section × atom) is NEW (never ingested), FINISHED (ingested at its full current
    extent), or PARTIAL (ingested earlier, then grew — a new reply landed on a finished
    chain). Progress is tracked exactly by these three states; PARTIAL re-ingests only the
    delta (see `render_atom_content(since=...)`)."""
    prev = done_atoms.get(f"{section_name}:{atom.root_id}")
    if prev is None:
        return "new"
    return "finished" if prev[0] >= len(atom.members) else "partial"


def _partial_since(prev: list | None, members: list) -> int:
    """Delta start index for a PARTIAL atom — cut by DATE, not by stored count.

    A re-import can BACKFILL older comments mid-list (e.g. paginated comment threads), which would shift
    an index-based slice to re-feed seen tails and silently drop the genuinely new members.
    ``idx`` counts members at or before the stored leaf date; when that agrees with the
    stored member count, the growth is a clean tail and the delta starts there. Any
    disagreement means a backfill landed before the cut — return 0 (re-feed the whole atom
    rather than lose it). 0 also for new/finished atoms (full render)."""
    if not prev or prev[0] >= len(members):
        return 0
    idx = next((i for i, m in enumerate(members) if m.date > prev[1]), len(members))
    return idx if (idx == prev[0] and idx < len(members)) else 0


def _crossed_deadlines(last: str, now: str, deadlines: tuple[str, ...]) -> list[str]:
    """MM-DD deadlines whose date falls in (last, now] — fired since we last checked."""
    if not deadlines or not now:
        return []
    out = []
    for md in deadlines:
        # Walk each year in the span; a deadline fires once its YYYY-MM-DD passes `last`.
        for year in range(int((last or now)[:4]), int(now[:4]) + 1):
            d = f"{year}-{md}"
            if (not last or d > last) and d <= now:
                out.append(d)
    return out


class Engine:
    def __init__(self, cfg: Config, slug: str, window: int, *, timeout=None,
                 spec: dict | None = None, concurrency: int = 1):
        self.cfg = cfg
        self.slug = slug
        self.window = window
        self.timeout = timeout
        # How many INDEPENDENT ingest groups (different files: one per person / post / day)
        # may run as concurrent model calls. Sections that digest the whole batch into one
        # shared file (world-type) always run alone, after the parallel ones.
        self.concurrency = max(1, concurrency)
        self.chat = ChatStore(slug)
        self.spec = spec or load_spec(self.chat.dir / "sections.toml")
        self.sections = build_sections(self.spec)
        self._view_cache: tuple[tuple, str] | None = None

    def _assemble(self, vctx: ViewContext, budget_tokens: int, *, report: bool = False) -> str:
        """`assemble` memoized on the store version.

        Assembling re-renders and re-tokenizes the WHOLE store; the ingest loop needs the
        view per chunk and `persist` per batch, but the store only changes when a model run
        actually writes — so most consecutive calls are identical. Without this the cost is
        O(batches × chunks × store size): quadratic in the corpus."""
        # id(store) guards against a replaced store (from_dict resets version to 0).
        key = (id(self.store), self.store.version,
               vctx.relative_date, vctx.kind, vctx.platform, budget_tokens, report)
        if self._view_cache is not None and self._view_cache[0] == key:
            return self._view_cache[1]
        text = assemble(self.store, self.sections, vctx, budget_tokens, report=report)
        self._view_cache = (key, text)
        return text

    def _rules(self, trigger_type) -> list[tuple[Section, "object"]]:
        out = []
        for s in self.sections:
            for r in s.task_rules():
                if isinstance(r.trigger, trigger_type):
                    out.append((s, r))
        return out

    def _fire(self, rule_ctx_base: dict, batch_index: int,
              *, oversized=None, time_state=None,
              do_init=False, do_launch=False, do_finish=False) -> list[Task]:
        """Fire the non-entry triggers and return the tasks to queue.

        OnEntry never goes through here — atom ingest is the dedicated `_ingest` path
        (per-section grouping, adaptive chunking, done_atoms tracking)."""
        queued: list[Task] = []

        def ctx(**extra) -> RuleContext:
            return RuleContext(batch_index=batch_index, **rule_ctx_base, **extra)

        if do_init:
            for s, r in self._rules(OnInit):
                queued += r.build(ctx(section=s)) or []
        if do_launch:
            for s, r in self._rules(OnLaunch):
                queued += r.build(ctx(section=s)) or []
        if oversized:
            for path, n, lim in oversized:
                try:
                    owner = self.store._owner(path.split("/"))
                except StoreError:
                    continue
                fired = False
                for r in owner.task_rules():
                    if isinstance(r.trigger, OnFileOverflow):
                        queued += r.build(ctx(section=owner, overflow=(path, n, lim))) or []
                        fired = True
                if not fired:
                    # Safety net: EVERY oversized file gets pruned, even if its section
                    # declared no tailored OnFileOverflow rule — bounding size is structural,
                    # not something a section can forget. Unlikely in practice (all current
                    # sections specialise it), but guarantees the invariant for any file.
                    queued.append(_default_prune_task(owner, path, n, lim, batch_index))
        if time_state is not None:
            for s, r in self._rules(OnTimePass):
                key = f"{s.name}:{r.name}"
                last = time_state.get(key, "")
                now = rule_ctx_base["view"].relative_date or ""
                fired = False
                tr: OnTimePass = r.trigger
                if tr.every_days and last and now:
                    fired = _days_between(last, now) >= tr.every_days
                elif tr.every_days and not last:
                    fired = True  # first time
                bounds = _crossed_deadlines(last, now, tr.deadlines)
                if fired or bounds:
                    boundary = bounds[-1] if bounds else None
                    queued += r.build(ctx(section=s, boundary=boundary)) or []
                    time_state[key] = now
        if do_finish:
            for s, r in self._rules(OnFinish):
                queued += r.build(ctx(section=s)) or []
        return queued

    # ---- the run -----------------------------------------------------------
    async def run(self) -> Path:
        self.cfg.require_llm()
        messages = self.chat.load_messages()
        if not messages:
            raise SystemExit(f"No messages found for '{self.slug}' under data/. See README for the input format.")
        meta = self.chat.read_meta()
        platform = meta.extra.get("platform", "chat")
        kind = "channel" if any(m.post_id is not None for m in messages) else "chat"

        out_path = self.chat.dir / f"chat-{self.slug}-compaction.md"
        named_path = self.chat.dir / f"chat-{self.slug}-compaction.named.md"
        progress_path = self.chat.dir / ".compact_progress.json"
        self._persist_task: asyncio.Task | None = None  # in-flight background checkpoint

        idmap = build_identity(messages)
        write_vault(self.chat.dir / "identity.json", idmap)

        # System prompt is FIXED for the run (sections are fixed) -> measure once.
        system_prompt = build_system_prompt(self.sections)
        system_tokens = estimate_tokens(system_prompt) + measure_tool_schema_tokens()
        budget = compute_run_budget(self.window, system_tokens=system_tokens)

        # ---- resume / fresh ----
        # done_atoms maps "<section>:<root_id>" -> [member_count, leaf_date] already
        # ingested. On resume an atom whose stored extent >= its current extent is skipped;
        # a grown chain (larger extent) is re-ingested once. Resume yields a strictly
        # shorter queue — nothing unchanged is reprocessed.
        done_atoms: dict[str, list] = {}
        store_data = None
        queue = TaskQueue()
        time_state: dict[str, str] = {}
        did_init = False
        fresh = True
        if progress_path.exists():
            try:
                prog = json.loads(progress_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                # A truncated/corrupt checkpoint (crash mid-write predating the atomic
                # tmp→replace, disk trouble) must degrade to a fresh run, not a stack trace.
                ui.warn(f"Progress file unreadable ({e}) — starting a fresh v3 run.")
                prog = {}
            if prog.get("version") == 3:
                done_atoms = prog.get("done_atoms", {})
                store_data = prog.get("store")
                queue = TaskQueue.from_list(prog.get("tasks"))
                time_state = prog.get("time_state", {})
                did_init = prog.get("did_init", False)
                fresh = False
            else:
                ui.warn("Old-format progress found — starting a fresh v3 run.")
        # Preserve an existing compaction as a solid result before overwriting it.
        if fresh and out_path.exists():
            out_path.replace(out_path.with_suffix(".prev.md"))

        self.store = VStore.from_dict(self.sections, idmap.keys(), store_data)
        self.idmap = idmap
        agent = build_writer_agent(self.cfg, self.store, system_prompt=system_prompt,
                                   timeout=self.timeout)
        self.agent = agent

        # ---- banner ----
        ui.rule(f"Compacting {self.slug}")
        ui.stat("Messages", len(messages))
        ui.stat("Platform / kind", f"{platform} / {kind}")
        ui.stat("Sections", ", ".join(s.name for s in self.sections))
        ui.banner(f"Budget — window W = {self.window} tok  (share {summary_share():.3f})")
        ui.stat("System (fixed)", f"{system_tokens} tok")
        ui.stat("Summary side", f"{budget.summary_budget} tok for {len(self.sections)} section view(s)")
        ui.stat("Workflow side", f"{budget.workflow_budget} tok (data + tool-call rounds)")

        all_days = [d for d, _ in group_by_day(messages)]
        latest_day = all_days[-1] if all_days else None
        by_id = {m.id: m for m in messages}

        # The unit of work is the ATOM (a reply chain as a Set), batched up to ⅓cnt (the
        # summary share). Each (section × atom) is tracked in `done_atoms` and ingested at
        # most once per extent; a grown chain (larger extent) is re-ingested exactly once.
        atoms = build_atoms(messages, by_id, idmap)
        atom_batches = pack_atom_batches(atoms, budget.summary_budget, idmap)

        def vctx(day: str | None) -> ViewContext:
            return ViewContext(relative_date=day, kind=kind, platform=platform)

        def is_done(section_name: str, atom) -> bool:
            return _atom_status(done_atoms, section_name, atom) == "finished"

        def batch_pending(batch_atoms) -> bool:
            return any(not is_done(s.name, a)
                       for a in batch_atoms for s, r in self._rules(OnEntry)
                       if _entry_matches(r.trigger, _kinds_in(a.members)))

        def _write_snapshot(report: str, named: str, payload: str) -> None:
            out_path.write_text(report, encoding="utf-8")
            named_path.write_text(named, encoding="utf-8")
            write_vault(self.chat.dir / "identity.json", idmap)
            # The progress file is the ONLY resume point of a multi-hour run — a crash
            # mid-dump must not truncate it. tmp → replace, like ChatStore.rewrite.
            tmp = progress_path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(progress_path)

        def persist(rel: str | None, *, background: bool = False) -> None:
            # Snapshot SYNCHRONOUSLY (the loop keeps mutating done_atoms/store), then push the
            # serialize + disk writes off the event loop so the per-atom mid-batch checkpoint
            # doesn't stall the next LLM call on I/O.
            report = self._assemble(vctx(rel), budget.summary_budget, report=True)
            named = idmap.humanize(report)
            payload = json.dumps({
                "version": 3, "done_atoms": dict(done_atoms), "store": self.store.to_dict(),
                "tasks": queue.to_list(), "time_state": time_state, "did_init": did_init,
            })
            if not background:
                _write_snapshot(report, named, payload)
                return
            # Chain background writes so they stay ORDERED and never clobber the same .tmp.
            prev = self._persist_task

            async def _bg() -> None:
                if prev is not None:
                    try:
                        await prev
                    except Exception:
                        pass
                await asyncio.to_thread(_write_snapshot, report, named, payload)

            self._persist_task = asyncio.create_task(_bg())

        async def drain(rel: str | None, batch_index: int, token_cap: int) -> None:
            """Run queued tasks in priority order until the token cap is spent.

            A task whose run dies on context overflow is RE-QUEUED once (it was already
            popped — dropping it would silently lose e.g. a finish-time consolidate, or an
            OnTimePass rollup whose time_state has already advanced). A second failure is
            dropped with a warning: retrying the same oversized prompt forever can't win."""
            spent = 0
            failed_once: set[str] = set()
            while spent < token_cap:
                task = queue.pop_best(batch_index)
                if task is None:
                    break
                spent += estimate_tokens(task.prompt) + _ROUND_TOKENS
                ui.detail(f"task {task.section}:{task.name} (p={task.priority:.0f})", style="magenta")
                res = await self._run(agent, task.prompt, rel)
                if res is None:
                    if task.dedup_key in failed_once:
                        ui.warn(f"Task {task.dedup_key} failed twice on overflow — dropped.")
                    else:
                        failed_once.add(task.dedup_key)
                        queue.add(task)
            if self._persist_task is not None:
                await self._persist_task   # let any in-flight mid-batch write land first
            persist(rel)

        # ---- launch-time tasks ----
        base_ctx = {"store": self.store, "view": vctx(latest_day)}
        if not done_atoms:
            for t in self._fire({**base_ctx, "view": vctx(all_days[0] if all_days else None)},
                                0, do_init=not did_init):
                queue.add(t)
            did_init = True
        for t in self._fire(base_ctx, 0, do_launch=True):
            queue.add(t)

        # batch_pending re-renders every atom's kinds × sections — compute it once.
        pending_flags = [batch_pending(b) for b in atom_batches]
        ui.phase(f"Processing {sum(pending_flags)} batch(es)")
        for bi, batch_atoms in enumerate(atom_batches):
            if not pending_flags[bi]:
                continue
            rel = max(a.leaf_date for a in batch_atoms)[:10]
            self.store.now = rel
            ctx_base = {"store": self.store, "view": vctx(rel)}

            # non-entry triggers fire per batch (oversized / time-pass); rest queued
            for t in self._fire(ctx_base, bi,
                                oversized=self.store.oversized(), time_state=time_state):
                queue.add(t)

            # ingest the batch's fresh (section × atom) work, then drain the queue
            ui.step(f"Ingest batch {bi + 1} ({len(batch_atoms)} atom(s), ~{rel}) ...")
            await self._ingest(agent, batch_atoms, bi, rel, budget, done_atoms, persist)
            await drain(rel, bi, budget.workflow_budget)

        # ---- finish-time tasks ----
        ui.phase("Finalizing")
        for t in self._fire({"store": self.store, "view": vctx(latest_day)},
                            len(atom_batches), do_finish=True):
            queue.add(t)
        await drain(latest_day, len(atom_batches), budget.workflow_budget)
        if self._persist_task is not None:
            await self._persist_task
        persist(latest_day)

        if len(queue):
            # The final drain is token-capped: leftovers persist in the progress file but
            # nothing would tell the user the artifact is missing e.g. its consolidation.
            ui.warn(f"{len(queue)} task(s) left undrained by the final token cap — "
                    "re-run the same compact command to finish them.")
        ui.stat("Report", out_path)
        ui.done(f"Compaction complete · {self.slug}")
        return out_path

    # ---- model runs --------------------------------------------------------
    async def _run(self, agent, prompt: str, rel: str | None):
        # Each model run is a fresh conversation: reset the read-before-modify knowledge
        # and seed it from whatever this prompt shows (the assembled view embeds bodies).
        self.store.begin_run(prompt)
        try:
            return await run_with_retries(agent, prompt, request_limit=40)
        except ContextOverflowError as e:
            ui.warn(f"Context exceeded on a task — skipping it ({e}).")
            return None

    async def _ingest(self, agent, batch_atoms, bi, rel, budget, done_atoms, persist=None):
        """Ingest a batch's fresh (section × atom) work — minimum tasks per section.

        Each section sets its own task granularity via `ingest_groups`: world / key_facts /
        study digest the whole batch in ONE run; people splits into ONE run PER AUTHOR (so
        its task count tracks the number of distinct people, not atoms). Within a group,
        completion is still per (section × atom) in `done_atoms` for correct resume; a grown
        chain (larger extent) is re-ingested exactly once.

        Completion is recorded ONLY for a run that actually finished: `_run` returns None
        when the per-run cap or the context window cut it short — we then can't know which
        atoms landed, so we mark NOTHING and shrink. A group is fed in an ADAPTIVE chunk
        starting at `_INGEST_CHUNK0`, doubling while the model copes and halving on every
        truncation; a single atom that still won't fit is marked done anyway (force progress
        by one). No atom is ever marked done without a completed pass that saw it — truncated
        work stays pending for the next batch/resume, never silently lost."""
        # Checkpoint + report mid-batch every _PERSIST_EVERY newly-finished atoms, not just
        # at the batch boundary: a big batch (hundreds of atoms) was otherwise hours of work
        # with NO checkpoint, so a crash/restart lost all of it and the report never updated.
        base = len(done_atoms)

        def _maybe_persist() -> None:
            nonlocal base
            if persist and len(done_atoms) - base >= _PERSIST_EVERY:
                persist(rel, background=True)
                base = len(done_atoms)

        # Two phases per batch: PARALLEL — every section that splits the batch into several
        # groups (per person / post / day: disjoint files) runs its groups concurrently under
        # a semaphore; then SEQUENTIAL — whole-batch digests (world-type, one shared file each)
        # run one at a time, after, so they see the parallel phase's writes.
        parallel: list[tuple] = []
        sequential: list[tuple] = []
        for s, r in self._rules(OnEntry):
            fresh = [
                a for a in batch_atoms
                if _entry_matches(r.trigger, _kinds_in(a.members))
                and _atom_status(done_atoms, s.name, a) != "finished"  # new or partial
            ]
            if not fresh:
                continue
            groups = s.ingest_groups(fresh)
            if self.concurrency > 1 and len(groups) > 1:
                parallel += [(s, r, gkey, gatoms) for gkey, gatoms in groups]
            else:
                sequential += [(s, r, gkey, gatoms) for gkey, gatoms in groups]

        if parallel:
            sem = asyncio.Semaphore(self.concurrency)

            async def _one(item):
                s, r, gkey, gatoms = item
                async with sem:
                    await self._ingest_group(agent, s, r, gkey, gatoms, bi, rel, budget, done_atoms)
                _maybe_persist()

            ui.detail(f"parallel ingest: {len(parallel)} group(s), {self.concurrency} at a time",
                      style="cyan")
            await asyncio.gather(*(asyncio.create_task(_one(it)) for it in parallel))

        for s, r, gkey, gatoms in sequential:
            await self._ingest_group(agent, s, r, gkey, gatoms, bi, rel, budget, done_atoms)
            _maybe_persist()

    async def _ingest_group(self, agent, s, r, gkey, gatoms, bi, rel, budget, done_atoms):
        """Run one section ingest group as adaptive, truncation-safe bounded passes."""
        vctx = ViewContext(relative_date=rel, kind="", platform="")
        remaining = list(gatoms)
        chunk_n = min(len(remaining), _INGEST_CHUNK0)
        label = f"{s.name}:{gkey}" if gkey else s.name
        while remaining:
            chunk = remaining[:chunk_n]
            present = _kinds_in([m for a in chunk for m in a.members])
            built = r.build(RuleContext(batch_index=bi, store=self.store, view=vctx,
                                        section=s, kinds_present=present)) or []
            frags = "\n".join(f"- {t.prompt}" for t in built) or "- Record the new data points faithfully."
            view = self._assemble(vctx, budget.summary_budget)
            parts = []
            for a in chunk:
                prev = done_atoms.get(f"{s.name}:{a.root_id}")
                # PARTIAL atom (grew since finished): re-feed only the delta (root as
                # context + new members), not the already-ingested comments — see
                # `_partial_since` for the date-cut / backfill semantics.
                since = _partial_since(prev, a.members)
                grew = bool(prev) and prev[0] < len(a.members)
                tag = (f"{a.source} · update ({len(a.members) - since} new)" if since
                       else f"{a.source} · update (backfill — full re-feed)" if grew
                       else a.source)
                parts.append(f"[atom @{a.root_id} · {tag}]\n"
                             + render_atom_content(a, self.idmap, since=since))
            content = "\n\n".join(parts)
            prompt = (
                "=== CURRENT REPORT (assembled memory so far) ===\n" + view + "\n\n"
                "=== WHAT TO UPDATE (this section only) ===\n" + frags + "\n\n"
                f"=== NEW DATA POINTS (reply-chain atoms; as of {rel}) ===\n" + content
            )
            ui.detail(f"ingest {label} ({len(chunk)}/{len(remaining)} atom(s))", style="cyan")
            res = await self._run(agent, prompt, rel)
            if res is None and chunk_n > 1:
                chunk_n = max(1, chunk_n // 2)   # truncated: shrink, retry remainder
                continue
            if res is None:
                # chunk_n == 1: a single atom that STILL doesn't fit is marked done anyway
                # (force progress by one) — but say so, or the gap is invisible until
                # someone notices the report is missing a chain.
                ui.warn(f"Atom @{chunk[0].root_id} ({label}) exceeded context even alone — "
                        "marked done WITHOUT a completed pass (content skipped).")
            for a in chunk:
                done_atoms[f"{s.name}:{a.root_id}"] = list(a.extent)
            remaining = remaining[len(chunk):]
            chunk_n = min(len(remaining), max(chunk_n * 2, 1))  # grow back up while it copes


def _default_prune_task(section: "Section", path: str, chars: int, limit: int,
                        batch_index: int) -> Task:
    """The default OnFileOverflow prune — one per oversized file, for ANY section.

    Every section's files are bounded by this same generic tightening unless the section
    declares its own OnFileOverflow rule for special handling. A one-line `prune_hint()`
    lets a section say WHAT to preserve without repeating the whole builder (DRY)."""
    urgency = min(30.0, 30.0 * (chars - limit) / max(1, limit))
    hint = section.prune_hint()
    keep = f" Keep {hint};" if hint else " Keep the essential, load-bearing content;"
    prompt = (
        f"The file `{path}` is over its size limit ({chars} > {limit} chars). Tighten it:"
        f"{keep} cut redundancy and low-value detail. Read it first, then overwrite it with "
        "the compacted version."
    )
    return Task(
        priority=BASE_PRIORITY[OnFileOverflow] + urgency,
        created_batch=batch_index,
        section=section.name,
        name="tighten",
        target=path,
        prompt=prompt,
        dedup_key=f"{section.name}:tighten:{path}",
    )


def _days_between(a: str, b: str) -> int:
    from datetime import date
    ya, ma, da = (int(x) for x in a.split("-"))
    yb, mb, db = (int(x) for x in b.split("-"))
    return (date(yb, mb, db) - date(ya, ma, da)).days


async def compact_chat_v2(cfg: Config, slug: str, window: int, timeout=None,
                          spec: dict | None = None, concurrency: int = 1) -> Path:
    return await Engine(cfg, slug, window, timeout=timeout, spec=spec,
                        concurrency=concurrency).run()
