"""The atom — a reply chain as the unit of work.

An ATOM is a message together with all of its reply-ancestors: the whole chain, treated
as a Set. It is the unit the engine batches and the sections ingest, so a chain enters the
model's context exactly ONCE rather than once per message. A later reply EXTENDS the chain
(grows the atom): the engine's `done_atoms` extent check re-ingests the grown chain once,
feeding only the delta (see `render_atom_content(since=...)`).

Chain identity is the explicit `reply_to` graph only — a forum `topic_id` is NOT a chain
boundary; it is recorded as the atom's `source`. A channel post is its own chain root (a
post points to itself via `post_id`).

Content is rendered forward-compatibly: text + photos + audio + files, each unavailable
modality kept as an explicit PLACEHOLDER (vision/transcription pipelines can fill the slot
later) rather than silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .identity import IdentityMap
from .render import estimate_tokens, media_slots as _media_slots
from .storage import Message

_MAX_WALK = 10_000  # cap the reply walk so a cycle / pathological chain can't hang


def chain_root(m: Message, by_id: dict[int, Message]) -> int:
    """The id at the top of `m`'s reply chain — the atom's stable identity.

    A channel post is its own root (`post_id == id`). Otherwise walk `reply_to` upward
    until a message with no in-store parent; a message with no reply is its own root.
    Cycle- and missing-parent-safe: caps the walk and falls back to the last seen id."""
    if m.post_id is not None:
        return m.post_id
    seen: set[int] = set()
    cur = m
    for _ in range(_MAX_WALK):
        if cur.reply_to is None or cur.id in seen:
            return cur.id
        seen.add(cur.id)
        parent = by_id.get(cur.reply_to)
        if parent is None:
            return cur.reply_to  # parent out of store: the chain root is its id
        cur = parent
    return cur.id


def _source_of(m: Message) -> str:
    # A merged store stamps each message with its ORIGIN chat slug so a pile of chats stays
    # distinguishable (codebase/insights must not conflate separate projects); it wins over
    # the structural channel/topic source.
    origin = (m.extra or {}).get("source")
    if origin:
        return str(origin)
    if m.post_id is not None:
        return "channel"
    if m.topic_id is not None:
        return f"chat/topic:{m.topic_id}"
    return "chat"


_GENERIC_SOURCES = {"chat", "channel"}


def _is_named_source(source: str) -> bool:
    """True for a real origin label (a merged chat slug) worth surfacing to the model — not
    the structural placeholders (`chat`, `channel`, `chat/topic:<id>`)."""
    return bool(source) and source not in _GENERIC_SOURCES and not source.startswith("chat/topic:")


@dataclass
class Atom:
    """A reply chain as a Set of messages, presented once."""

    root_id: int
    members: list[Message]               # chain, date-ordered, deduped by id
    leaf_id: int = field(init=False)
    leaf_date: str = field(init=False)
    sender: str = ""                     # leaf sender @hash (filled by build_atoms)
    source: str = "chat"                 # channel | chat | chat/topic:<id>
    mentions: list = field(default_factory=list)  # @hash of REAL people tagged in the chain
    # Cached full render (since=0) — `pack_atom_batches` tokenizes every atom, then the
    # ingest prompt renders it again; the idmap is fixed per run, so render once.
    _content_cache: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        leaf = self.members[-1]
        self.leaf_id = leaf.id
        self.leaf_date = leaf.date

    @property
    def extent(self) -> tuple[int, str]:
        """(member_count, leaf_date) — monotonic as the chain grows; the supersession key."""
        return (len(self.members), self.leaf_date)


def build_atoms(messages: list[Message], by_id: dict[int, Message] | None = None,
                idmap: "IdentityMap | None" = None) -> list[Atom]:
    """Group messages into atoms by reply-chain root, ordered oldest→newest by leaf date.

    When `idmap` is given, `Atom.sender` is the leaf message's `@hash` — sections that group
    their ingest by author (people) partition on it without touching identity themselves."""
    by_id = by_id or {m.id: m for m in messages}
    groups: dict[int, list[Message]] = {}
    for m in messages:
        groups.setdefault(chain_root(m, by_id), []).append(m)
    atoms: list[Atom] = []
    for root, members in groups.items():
        members.sort(key=lambda m: (m.date, m.id))
        leaf = members[-1]
        sender = idmap.label_for(leaf.sender_id, leaf.sender_name) if idmap else ""
        mentions: list[str] = []
        if idmap:
            seen: set[str] = set()
            for msg in members:
                for mn in getattr(msg, "mentions", None) or []:
                    if mn.get("real") and mn.get("user_id") is not None:
                        k = idmap.label_for(mn["user_id"], mn.get("name"))
                        if k != sender and k not in seen:
                            seen.add(k)
                            mentions.append(k)
        atoms.append(Atom(root_id=root, members=members, source=_source_of(leaf),
                          sender=sender, mentions=mentions))
    atoms.sort(key=lambda a: (a.leaf_date, a.leaf_id))
    return atoms


def render_atom_content(atom: Atom, idmap: IdentityMap, since: int = 0) -> str:
    """Render an atom's chain to LLM-facing markdown, forward-compatible across modalities.

    Each member is a line `[hh:mm] @hash: text` followed by structured media slots; every
    unavailable modality is an explicit placeholder, never a silent drop. Identity is
    pseudonymized in code (the model sees `@hash`, never real names).

    ``since`` drives PARTIAL re-ingest: when a finished atom grows (a new reply lands), we
    re-feed only the DELTA — the chain root (the post/first message) as context, then the
    members from index ``since`` onward — instead of the whole chain again. ``since=0``
    (default) renders the full atom (a new atom)."""
    if not since and atom._content_cache is not None:
        return atom._content_cache
    members = atom.members
    if since and 0 < since < len(members):
        root = members[0]
        delta = members[since:]
        members = [root] + delta if root not in delta else delta
    lines: list[str] = []
    if _is_named_source(atom.source):
        # Surface the origin chat so the model keeps merged projects/courses apart.
        lines.append(f"[source: {atom.source}]")
    for m in members:
        who = idmap.label_for(m.sender_id, m.sender_name)
        time = m.date[11:16] if len(m.date) >= 16 else ""
        head = f"[{time}] {who}:"
        if m.text:
            head += " " + idmap.sanitize(m.text, sender_key=who)
        lines.append(head)
        for slot in _media_slots(m):
            # Media slots carry third-party text too (vision OCR reads @handles off
            # watermarks/captions; filenames embed names) — sanitize like a body.
            lines.append("  " + idmap.sanitize(slot))
    out = "\n".join(lines)
    if not since:
        atom._content_cache = out
    return out


def atom_tokens(atom: Atom, idmap: IdentityMap) -> int:
    return estimate_tokens(render_atom_content(atom, idmap))


def pack_atom_batches(atoms: list[Atom], budget: int, idmap: IdentityMap) -> list[list[Atom]]:
    """Pack atoms (already oldest→newest) into batches up to `budget` tokens (⅓cnt).

    A single atom whose content exceeds the budget becomes its own batch — force progress
    by ONE atom rather than splitting a reply chain (`max(atom.tokens, budget)` from the
    end). The chain is the unit; it is never cut."""
    budget = max(1, budget)
    batches: list[list[Atom]] = []
    cur: list[Atom] = []
    cur_tok = 0
    for a in atoms:
        t = atom_tokens(a, idmap)
        if cur and cur_tok + t > budget:
            batches.append(cur)
            cur, cur_tok = [], 0
        cur.append(a)
        cur_tok += t
    if cur:
        batches.append(cur)
    return batches
