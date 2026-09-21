"""The Section contract — the core of the flexible, as-code redesign.

A run is a SET of self-contained ``Section`` objects. Each section owns exactly four
things and nothing else knows its specifics:

  1. FEATURES   — on/off sub-behaviours (e.g. people + ``timeline``).
  2. PATHS      — the slice of the virtual-store namespace it owns: a path grammar it
                  validates, a per-file-type CHAR LIMIT, and the guidance/"standard"
                  text injected into the system prompt each run.
  3. VIEW       — how it appears in the assembled summary, as ordered ``ViewBlock``s
                  (oldest first); the base class crops to a token budget by KEEPING THE
                  NEWEST END, so cropping is uniform while ordering is per-section.
  4. TASKS      — ``TaskRule``s bound to triggers (on_entry / on_init / on_launch /
                  on_finish / on_time_pass / on_file_overflow) that emit ``Task``s into
                  a persistent priority queue the engine drains within the workflow budget.

The engine and store are entirely domain-neutral; all specifics live in section objects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable

from ..render import estimate_tokens

if TYPE_CHECKING:
    from .store import VStore


# --------------------------------------------------------------------------- kinds
class Kind(str, Enum):
    """A data point's shape — platform-neutral (chat message, post, comment)."""

    MESSAGE = "message"   # a line in a chat
    POST = "post"         # a broadcast post (channel)
    COMMENT = "comment"   # a comment under a post


# --------------------------------------------------------------------------- triggers
@dataclass(frozen=True)
class OnInit:
    """Fires once, the first time a store is created (skeleton files)."""


@dataclass(frozen=True)
class OnLaunch:
    """Fires every time the engine starts (incl. resume)."""


@dataclass(frozen=True)
class OnEntry:
    """Fires when new data points are dispatched this batch.

    ``kinds`` filters which shapes wake this rule (None = any). This is the spine:
    ingestion itself is an on_entry rule with the top base priority."""

    kinds: frozenset[Kind] | None = None


@dataclass(frozen=True)
class OnFinish:
    """Fires once after the whole batch loop (final consolidation / relevance sweep)."""


@dataclass(frozen=True)
class OnTimePass:
    """Fires as simulated CHAT-TIME advances.

    Two independent forms (either or both):
      * ``every_days`` — N days of chat-time since this rule last fired.
      * ``deadlines``  — recurring MM-DD dates (e.g. ("12-01","05-01")); fires when the
                         processed day crosses one since the rule last fired.
    """

    every_days: int | None = None
    deadlines: tuple[str, ...] = ()


@dataclass(frozen=True)
class OnFileOverflow:
    """Fires for each file in this section's namespace over its char limit.

    The controlled, section-local replacement for Big Prune — there is no global
    summary-overflow trigger anymore."""


Trigger = OnInit | OnLaunch | OnEntry | OnFinish | OnTimePass | OnFileOverflow


# --------------------------------------------------------------------------- tasks
@dataclass(order=True)
class Task:
    """A unit of model work waiting in the persistent priority queue.

    Ordering is by ``-priority`` then ``created_batch`` (older first) so a plain heap /
    ``sorted`` pops the most important, oldest-waiting task first. ``dedup_key`` collapses
    identical pending work (never enqueue "record day X" twice)."""

    sort_key: tuple = field(init=False, repr=False)
    priority: float
    created_batch: int
    section: str
    name: str
    target: str          # primary file path this task is about ("" if none)
    prompt: str          # the model-facing nudge
    dedup_key: str

    def __post_init__(self) -> None:
        self.sort_key = (-self.priority, self.created_batch)

    def to_dict(self) -> dict:
        return {
            "priority": self.priority, "created_batch": self.created_batch,
            "section": self.section, "name": self.name, "target": self.target,
            "prompt": self.prompt, "dedup_key": self.dedup_key,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            priority=d["priority"], created_batch=d["created_batch"],
            section=d["section"], name=d["name"], target=d.get("target", ""),
            prompt=d["prompt"], dedup_key=d["dedup_key"],
        )


# Base priorities by trigger class — the "smart" ordering backbone. Concrete rules add
# urgency (overflow ratio / deadline proximity) on top; the queue adds anti-starvation
# aging. Ingestion (on_entry) must win so incoming data is recorded before it scrolls.
BASE_PRIORITY = {
    "ingest": 100.0,        # on_entry data recording — the spine
    OnFileOverflow: 70.0,   # keep files bounded (replaces pruning)
    OnTimePass: 50.0,       # rollups / deadline consolidation
    OnInit: 40.0,
    OnLaunch: 30.0,
    OnFinish: 20.0,         # end-of-run revisits
}
AGE_BONUS = 3.0  # priority gained per batch a task waits (anti-starvation)


# --------------------------------------------------------------------------- view
@dataclass
class ViewBlock:
    """One renderable unit of a section's view, with a recency ``order`` key.

    Blocks are produced OLDEST-FIRST. When the view must crop to a budget the base class
    keeps blocks from the END (newest), so the freshest material always survives."""

    text: str
    order: tuple | str | int = ()  # ascending: smaller = older


def crop_to_budget(blocks: list["ViewBlock"], budget_tokens: int, header: str) -> str:
    """Render ``blocks`` under ``budget_tokens``, keeping the highest-``order`` blocks.

    Standalone, store-free version of the keep-newest crop (also used by
    ``Section.render_view``). Blocks are sorted ascending by ``order`` and kept from the
    END until the budget is spent — at least the single last block always survives, even
    if it alone exceeds the budget (truncating it would corrupt content). A
    ``[…older trimmed…]`` marker is shown when blocks were dropped. Emits ``header`` first.

    Reusable as a library primitive: set each block's ``order`` so the items you most want
    to keep sort LAST (newest, or — for spaced repetition — most "due")."""
    kept = [b for b in blocks if b.text.strip()]
    if not kept:
        return f"{header}\n\n(none yet)"
    ordered = sorted(kept, key=lambda b: _ord(b.order))
    chosen: list[str] = []
    used = estimate_tokens(header)
    for b in reversed(ordered):
        cost = estimate_tokens(b.text)
        if chosen and used + cost > budget_tokens:
            break
        chosen.append(b.text)
        used += cost
    chosen.reverse()
    dropped = len(ordered) - len(chosen)
    body = "\n\n".join(chosen)
    if dropped:
        body = f"_[… {dropped} older item(s) trimmed to fit context …]_\n\n{body}"
    return f"{header}\n\n{body}"


@dataclass
class ViewContext:
    """Everything a view/task needs about the run, with no store coupling."""

    relative_date: str | None      # latest processed day, YYYY-MM-DD
    kind: str                      # chat | channel
    platform: str                  # meta.extra['platform'] — wording only (e.g. 'telegram')


# --------------------------------------------------------------------------- section
class Section(ABC):
    """Base class for a fully self-contained report section."""

    #: stable section name + namespace head (e.g. "people", "world", "history")
    name: str = ""
    #: feature flags this section understands, with their defaults
    default_features: dict[str, bool] = {}
    #: whether this section's view goes into the DELIVERABLE (compaction.md). The model
    #: always sees every active section in its working view; a section that is only
    #: ground truth for others (e.g. raw typed records the digests query) sets this False
    #: so the report stays the few buckets the user asked for.
    in_report: bool = True

    def __init__(self, features: dict[str, bool] | None = None):
        self.features = {**self.default_features, **(features or {})}

    def feature(self, key: str) -> bool:
        return bool(self.features.get(key, False))

    # ---- 2) paths ----------------------------------------------------------
    @abstractmethod
    def owns(self, raw_parts: list[str]) -> bool:
        """True if a (slash-split, stripped) path belongs to this section."""

    @abstractmethod
    def resolve(self, raw_parts: list[str], known_keys: set[str]) -> str:
        """Validate + canonicalize an owned path to a stored ``.md`` key, or raise
        ``StoreError``. Calendar-correctness, allowed sub-sections, person-hash checks,
        etc. all live here."""

    @abstractmethod
    def char_limit(self, key: str) -> int:
        """Soft per-file char limit for an owned key — drives on_file_overflow."""

    @abstractmethod
    def paths_doc(self) -> str:
        """The allowed-paths + standard block injected into the system prompt."""

    def model_for(self, key: str):
        """The Pydantic model class validating a TYPED (JSON-record) key, or None when the
        key is freeform markdown. Default: every key is freeform. A section that stores typed
        records overrides this to return its model for its record keys (and None for its prose
        keys); ``is_typed`` is derived from it so the two never drift."""
        return None

    def is_typed(self, key: str) -> bool:
        """True if an owned key stores a typed JSON record (set via ``patch`` and validated
        by ``model_for``) rather than freeform markdown the model writes/edits directly."""
        return self.model_for(key) is not None

    # ---- 3) view -----------------------------------------------------------
    @abstractmethod
    def view_blocks(self, store: "VStore", ctx: ViewContext) -> list[ViewBlock]:
        """Ordered (oldest-first) renderable blocks for this section's view."""

    def view_title(self) -> str:
        return self.name.replace("_", " ").title()

    def natural_cost(self, store: "VStore", ctx: ViewContext) -> int:
        """Token cost of the FULL, uncropped view — what the allocator divides over."""
        blocks = self.view_blocks(store, ctx)
        if not blocks:
            return 0
        body = "\n\n".join(b.text for b in blocks if b.text.strip())
        return estimate_tokens(f"# {self.view_title()}\n\n{body}") if body else 0

    def render_view(self, store: "VStore", ctx: ViewContext, budget_tokens: int) -> str:
        """Render to <= ``budget_tokens``, keeping the NEWEST blocks when cropping.

        Always emits the title; includes whole blocks from the end until the budget is
        spent (at least the single newest block, even if it alone exceeds the budget —
        truncating it would corrupt content). A ``[…older trimmed…]`` marker is shown
        when blocks were dropped so a reader (and the model) knows the view is partial."""
        blocks = self.view_blocks(store, ctx)
        return crop_to_budget(blocks, budget_tokens, f"# {self.view_title()}")

    # ---- 4) tasks ----------------------------------------------------------
    @abstractmethod
    def task_rules(self) -> list["TaskRule"]:
        """The triggered rules that emit Tasks for this section."""

    def prune_hint(self) -> str:
        """One-line "what to preserve" for the default OnFileOverflow prune (engine-side).

        Lets a section flavour the generic tighten without repeating the whole builder. A
        section needing genuinely different overflow behaviour can still declare its own
        OnFileOverflow TaskRule instead. Default: no hint (generic prune)."""
        return ""

    def ingest_groups(self, atoms: list) -> list[tuple[str, list]]:
        """Partition a batch's atoms into this section's natural INGEST UNITS — one bounded
        model run per group. This is where a section sets its own task granularity.

        Default: ONE group for the whole batch (a per-batch digest — world, key_facts,
        study). `people` overrides to one group PER AUTHOR, so its task count tracks the
        number of distinct people in the batch, not the atom count. The engine still tracks
        completion per (section × atom) for correct resume; this only shapes how atoms are
        grouped into runs."""
        return [("", list(atoms))]


# --------------------------------------------------------------------------- task rule
@dataclass
class TaskRule:
    """Binds a trigger to a builder that emits concrete Tasks.

    ``build`` receives a ``RuleContext`` and returns the tasks to enqueue (or None). The
    engine handles trigger firing, base priority, dedup, and aging; a rule only decides
    WHAT work the trigger implies right now."""

    trigger: Trigger
    name: str
    build: Callable[["RuleContext"], "list[Task] | None"]
    base_priority: float | None = None  # overrides BASE_PRIORITY when set


@dataclass
class RuleContext:
    """Context handed to a TaskRule's ``build`` when its trigger fires."""

    store: "VStore"
    view: ViewContext
    batch_index: int
    section: Section
    # data points dispatched this batch (for on_entry); empty otherwise
    days: tuple[str, ...] = ()
    kinds_present: frozenset[Kind] = frozenset()
    # for on_file_overflow: the offending (path, chars, limit)
    overflow: tuple[str, int, int] | None = None
    # for on_time_pass: which deadline/elapsed boundary fired
    boundary: str | None = None


def _ord(o) -> tuple:
    """Normalize a ViewBlock.order into a sortable tuple."""
    if isinstance(o, tuple):
        return o
    return (o,)
