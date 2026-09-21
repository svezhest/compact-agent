"""The virtual store the model edits — now section-driven and domain-neutral.

Unlike the old ``ReportStore`` (which hard-coded one fixed vocabulary), ``VStore`` owns
no schema of its own: it delegates every path decision to the active ``Section`` objects.
A path is validated by whichever section ``owns`` it; the section also supplies each
file's soft char limit (driving ``on_file_overflow``). The store adds two things sections
need but shouldn't track themselves: the write/edit ops and per-file ``last_updated``
chat-time metadata (for people LRU cropping and ``on_time_pass``).

The path is each file's heading (the view generates a file's title from its path), so a
body rarely needs its own header. Any markdown header the model writes is normalized to
ALL-CAPS on write by `normalize_headers` (the single, DRY header handler) — outside fenced
code, where `#` is left intact for codebase notes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from .base import Section

_HEADER_LINE = re.compile(r"^([ \t]*#{1,6}[ \t]*)(\S.*)$")
_FENCE = re.compile(r"^[ \t]*```")


class StoreError(ValueError):
    """A path or edit the model got wrong; the message is fed back as a ModelRetry."""


def normalize_headers(text: str) -> str:
    """THE single header handler (DRY) applied to every body the model writes.

    The file PATH is the heading — the compacted view generates each file's title from its
    path, so a body rarely needs its own header. Any markdown header the model does write
    (for internal structure in a long note) is normalized to an ALL-CAPS header: the ``#``
    markers are KEPT and the header TEXT is upper-cased, so headers stand out and play their
    structural role in the compact view. Content inside fenced ``` code blocks is left
    untouched — there ``#`` is a comment/shebang and must survive verbatim (codebase notes).
    """
    out: list[str] = []
    in_fence = False
    for line in (text or "").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
        elif not in_fence and (m := _HEADER_LINE.match(line)):
            out.append(m.group(1) + m.group(2).upper())
        else:
            out.append(line)
    return "\n".join(out).strip()


def _split(raw: str) -> list[str]:
    p = (raw or "").strip().strip("/").replace("\\", "/")
    parts = [seg for seg in p.split("/") if seg]
    # The store is in-memory (no traversal to exploit), but "." / ".." segments break the
    # tree render (nested sections nest by path) and can never be a valid section path — reject
    # loudly so the model corrects instead of silently creating a malformed key.
    if any(seg in (".", "..") for seg in parts):
        raise StoreError(f"Path '{raw}' contains a '.'/'..' segment — use a plain "
                         "section path (no relative navigation).")
    return parts


@dataclass
class FileMeta:
    last_updated: str = ""   # chat-time YYYY-MM-DD of the most recent touch

    def to_dict(self) -> dict:
        return {"last_updated": self.last_updated}

    @classmethod
    def from_dict(cls, d: dict) -> "FileMeta":
        return cls(last_updated=(d or {}).get("last_updated", ""))


@dataclass
class VStore:
    """Section-addressed virtual filesystem with write/read ops + recency metadata."""

    sections: list[Section]
    known_keys: set[str]
    files: dict[str, str] = field(default_factory=dict)
    meta: dict[str, FileMeta] = field(default_factory=dict)
    #: chat-time of the data point currently being processed, stamped onto every touch
    now: str = ""
    #: keys the model has SEEN this run (shown in the summary view, read, or just
    #: created) — a file it has not seen may not be edited/overwritten blindly. Per-run,
    #: in-memory only (never persisted): each model run starts from `begin_run`.
    seen: set[str] = field(default_factory=set)
    #: bumped on every mutation — lets callers memoize whole-store renders (assembling
    #: the view re-tokenizes everything; without this it is re-done per ingest chunk).
    version: int = 0

    # ---- resolution --------------------------------------------------------
    def _owner(self, parts: list[str]) -> Section:
        for s in self.sections:
            if s.owns(parts):
                return s
        allowed = ", ".join(s.name for s in self.sections)
        raise StoreError(
            f"Path '{'/'.join(parts)}' is not in any active section. Active sections: "
            f"{allowed}. Use one of their documented path forms."
        )

    def resolve(self, raw: str) -> str:
        parts = _split(raw)
        if not parts:
            raise StoreError("Empty path.")
        return self._owner(parts).resolve(parts, self.known_keys)

    def char_limit(self, key: str) -> int:
        return self._owner(_split(key)).char_limit(key)

    def is_typed(self, key: str) -> bool:
        """True if a resolved key stores a typed JSON record (set via ``patch``), not freeform
        markdown. Delegated to the owning section."""
        return self._owner(_split(key)).is_typed(key)

    # ---- the read-before-modify gate ---------------------------------------
    def begin_run(self, shown: str = "") -> None:
        """Reset per-run knowledge and seed it from what the model is about to SEE.

        Each model run is a fresh conversation: the model only knows the files whose
        bodies appear in the prompt (the assembled summary view) plus whatever it reads.
        We seed `seen` by substring-matching the prompt against current bodies, so a file
        already shown in the view needn't be re-read, while one cropped out of the view
        (or never shown) must be read before it can be edited or overwritten."""
        self.seen = set()
        if shown:
            for key, body in self.files.items():
                if body and body in shown:
                    self.seen.add(key)

    def _require_seen(self, key: str, op: str) -> None:
        if key in self.files and key not in self.seen:
            raise StoreError(
                f"You must read '{key}' before you {op} it — it already has content you "
                f"have not seen and would change blindly. Call read('{key}') first."
            )

    def _reject_if_typed(self, key: str, op: str) -> None:
        if self.is_typed(key):
            raise StoreError(
                f"'{key}' is a TYPED record — {op} is only for freeform markdown. Set its "
                f"fields with patch('{key}', {{field: value}}) instead."
            )

    # ---- write/read ops ----------------------------------------------------
    def _touch(self, key: str) -> None:
        m = self.meta.setdefault(key, FileMeta())
        if self.now and self.now > m.last_updated:
            m.last_updated = self.now
        self.seen.add(key)  # touching a file means you now know its current content
        self.version += 1

    def touch(self, path: str) -> str:
        """Create an empty file to claim a path; errors if it already exists."""
        key = self.resolve(path)
        if key in self.files:
            raise StoreError(
                f"'{key}' already exists. Use read then edit/append, or write with "
                "overwrite=true, to change it."
            )
        self.files[key] = ""
        self._touch(key)
        return key

    def write(self, path: str, content: str, overwrite: bool = False) -> str:
        key = self.resolve(path)
        self._reject_if_typed(key, "write")
        if key in self.files and not overwrite:
            raise StoreError(
                f"'{key}' already exists. Pass overwrite=true to replace it, or use "
                "edit/append to change part of it."
            )
        self._require_seen(key, "overwrite")
        self.files[key] = normalize_headers(content)
        self._touch(key)
        return key

    def overwrite(self, path: str, content: str) -> str:
        return self.write(path, content, overwrite=True)

    def append(self, path: str, content: str) -> str:
        key = self.resolve(path)
        self._reject_if_typed(key, "append to")
        self._require_seen(key, "append to")
        body = normalize_headers(content)
        cur = self.files.get(key, "")
        self.files[key] = f"{cur}\n\n{body}".strip() if cur else body
        self._touch(key)
        return key

    def edit(self, path: str, old: str, new: str) -> str:
        key = self.resolve(path)
        self._reject_if_typed(key, "edit")
        if key not in self.files:
            raise StoreError(f"Cannot edit '{key}': it does not exist yet. Create it with "
                             "write or touch first.")
        self._require_seen(key, "edit")
        if not old:
            raise StoreError("edit requires a non-empty 'old' substring to replace.")
        cur = self.files[key]
        if old not in cur:
            raise StoreError(f"Cannot edit '{key}': the 'old' text was not found verbatim.")
        self.files[key] = cur.replace(old, normalize_headers(new), 1).strip()
        self._touch(key)
        return key

    def patch(self, path: str, fields: dict) -> str:
        """Set fields on a TYPED record (creating it if absent), validated against the owning
        section's schema. Non-destructive: only the given fields change, the rest are kept.
        Idempotent — the same fields yield byte-identical canonical JSON (stable field order)."""
        key = self.resolve(path)
        owner = self._owner(_split(key))
        model = owner.model_for(key)
        if model is None:
            raise StoreError(
                f"'{key}' is a freeform markdown path — use write/append/edit, not patch."
            )
        if not isinstance(fields, dict) or not fields:
            raise StoreError("patch needs a non-empty object mapping field -> value to set.")
        cur: dict = {}
        raw = self.files.get(key, "")
        if raw:
            try:
                cur = json.loads(raw)
            except json.JSONDecodeError:
                cur = {}
        try:
            validated = model.model_validate({**cur, **fields})
        except ValidationError as e:
            raise StoreError(_validation_msg(key, e))
        self.files[key] = _dump_card(validated)
        self._touch(key)
        return key

    def parse_typed(self, key: str) -> dict | None:
        """Parsed dict of a typed record's current value (None if absent or not typed)."""
        if not self.is_typed(key):
            return None
        raw = self.files.get(key, "")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def read(self, path: str) -> str:
        key = self.resolve(path)
        self.seen.add(key)  # now seen — may be edited/overwritten this run
        return self.files.get(key, "")

    def delete(self, key: str) -> None:
        self.files.pop(key, None)
        self.meta.pop(key, None)
        self.version += 1

    def list_paths(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self.files if k.startswith(prefix))

    def query(self, prefix: str, where: dict | None = None,
              sort: str | None = None, limit: int | None = None) -> list[dict]:
        """Filter/sort/limit the TYPED records under ``prefix`` (read-only).

        ``where`` maps a (dotted) field to a condition: a plain string matches
        case-insensitively as a substring; a string led by >=,<=,>,< compares numerically; a
        list matches by membership. ``sort`` is a field name, optional leading '-' = descending.
        Each returned row carries its own ``_key``."""
        where = where or {}
        rows: list[dict] = []
        for key in self.list_paths(prefix):
            try:
                owner = self._owner(_split(key))
            except StoreError:
                continue
            if not owner.is_typed(key):
                continue
            row = self.parse_typed(key)
            if not row:
                continue
            row = {**row, "_key": key}
            if _matches(row, where):
                rows.append(row)
        if sort:
            desc = sort.startswith("-")
            field_name = sort[1:] if desc else sort
            rows.sort(
                key=lambda r: ((v := _dotget(r, field_name)) is None, v if v is not None else 0),
                reverse=desc,
            )
        if limit is not None and limit >= 0:
            rows = rows[:limit]
        return rows

    def last_updated(self, key: str) -> str:
        return self.meta.get(key, FileMeta()).last_updated

    # ---- overflow ----------------------------------------------------------
    def oversized(self) -> list[tuple[str, int, int]]:
        """(key, chars, limit) for every file over its section's char limit, worst first."""
        out: list[tuple[str, int, int]] = []
        for key, body in self.files.items():
            try:
                limit = self.char_limit(key)
            except StoreError:
                continue  # orphan file from a no-longer-active section; ignore
            n = len(body or "")
            if limit and n > limit:
                out.append((key, n, limit))
        out.sort(key=lambda t: t[1] - t[2], reverse=True)
        return out

    # ---- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "files": dict(self.files),
            "meta": {k: m.to_dict() for k, m in self.meta.items()},
        }

    @classmethod
    def from_dict(cls, sections, known_keys, data: dict | None) -> "VStore":
        data = data or {}
        files = dict(data.get("files", {}))
        meta = {k: FileMeta.from_dict(v) for k, v in data.get("meta", {}).items()}
        return cls(sections=list(sections), known_keys=set(known_keys), files=files, meta=meta)


# --------------------------------------------------------------------------- typed records
def _dump_card(model) -> str:
    """Canonical JSON for a validated record — stable field order ⇒ idempotent writes."""
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _validation_msg(key: str, e: ValidationError) -> str:
    """A compact, model-facing message listing the rejected fields (a 400 to retry against)."""
    errs = e.errors()
    lines = []
    for er in errs[:8]:
        loc = ".".join(str(p) for p in er.get("loc", ()))
        lines.append(f"  - {loc or '(root)'}: {er.get('msg', 'invalid')}")
    return (f"Cannot patch '{key}': {len(errs)} field error(s) — fix and patch again:\n"
            + "\n".join(lines))


def _dotget(row: dict, path: str):
    """Reach into a nested record by a dotted field path (None if any segment is missing)."""
    cur = row
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _matches(row: dict, where: dict) -> bool:
    return all(_match_one(_dotget(row, f), cond) for f, cond in where.items())


def _match_one(v, cond) -> bool:
    if isinstance(cond, bool):
        return v == cond
    if isinstance(cond, (int, float)):
        return v == cond
    if isinstance(cond, list):
        vals = v if isinstance(v, list) else [v]
        return any(x in cond for x in vals)
    if isinstance(cond, str):
        c = cond.strip()
        for op in (">=", "<=", ">", "<"):
            if c.startswith(op):
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    return False
                try:
                    num = float(c[len(op):].strip())
                except ValueError:
                    return False
                return {">=": v >= num, "<=": v <= num, ">": v > num, "<": v < num}[op]
        return c.lower() in ("" if v is None else str(v)).lower()
    return v == cond
