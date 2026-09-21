"""On-disk layout and message schema — the INPUT CONTRACT of the compactor.

This tool never talks to a platform. Whatever produces the data (an export converter, your
own harvester, a synthetic generator) writes one directory per conversation::

    data/<chat-slug>/
        meta.json          # ChatMeta: chat identity + import metadata
        messages.jsonl      # one Message per line (append-friendly)
        media/              # optional images referenced by Message.media_path

JSONL is the storage format: append-friendly, greppable, and streams without
rewriting the whole file. See ``examples/`` for a converter and a synthetic sample.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

DATA_ROOT = Path("data")


def slugify(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name.lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s or "chat"


@dataclass
class Message:
    id: int
    date: str  # ISO 8601 UTC
    sender_id: int | None
    sender_name: str | None
    text: str
    sender_username: str | None = None  # platform @handle, to link self-mentions
    reply_to: int | None = None
    topic_id: int | None = None  # forum topic, if any
    post_id: int | None = None  # channel post this belongs to (a post points to itself,
    #                             a comment to its post); None for ordinary chats
    media_type: str | None = None  # "photo" | "poll" | "sticker" | "file" | ...
    media_path: str | None = None  # relative to chat dir (photos only)
    photo_desc: str | None = None  # LLM image description, filled by `read_photos`
    media_meta: dict = field(default_factory=dict)  # filename, poll, sticker pack, etc.
    # @handle mentions resolved against the platform by the IMPORTER (the only place that
    # may). Each: {"handle", "user_id" (None if not a real user), "name", "real": bool}.
    # Lets the identity layer fold a mention onto the real person's key — a tagged-but-
    # silent user (answers off-channel) still gets one uniform @hash, never a string-only
    # pseudonym. Empty when the importer doesn't resolve mentions.
    mentions: list = field(default_factory=list)
    # Free-form per-message metadata. A merged store stamps ``extra["source"]`` with the
    # origin chat slug so a pile of chats stays separable (codebase/insights must not
    # conflate distinct projects). Empty for ordinary single-chat stores.
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @property
    def day(self) -> str:
        return self.date[:10]  # YYYY-MM-DD


@dataclass
class ChatMeta:
    chat_id: int
    title: str
    kind: str  # "user" | "group" | "channel" | "forum"
    slug: str
    fetched_at: str  # ISO timestamp of the import
    message_count: int = 0
    members: int | None = None
    extra: dict = field(default_factory=dict)


class ChatStore:
    def __init__(self, slug: str, root: Path = DATA_ROOT):
        self.dir = root / slug
        self.media_dir = self.dir / "media"
        self.messages_path = self.dir / "messages.jsonl"
        self.meta_path = self.dir / "meta.json"

    def ensure(self) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def reset(self) -> None:
        """Start a fresh import: drop old messages + media so re-runs don't duplicate."""
        import shutil

        if self.messages_path.exists():
            self.messages_path.unlink()
        if self.media_dir.exists():
            shutil.rmtree(self.media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def write_meta(self, meta: ChatMeta) -> None:
        self.meta_path.write_text(
            json.dumps(asdict(meta), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def read_meta(self) -> ChatMeta:
        return ChatMeta(**json.loads(self.meta_path.read_text(encoding="utf-8")))

    def append_message(self, msg: Message) -> None:
        with self.messages_path.open("a", encoding="utf-8") as f:
            f.write(msg.to_json() + "\n")

    def rewrite(self, messages: list["Message"]) -> None:
        """Overwrite the whole log (used to backfill fields in place)."""
        tmp = self.messages_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for m in messages:
                f.write(m.to_json() + "\n")
        tmp.replace(self.messages_path)

    def iter_messages(self):
        if not self.messages_path.exists():
            return
        with self.messages_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    known = {f.name for f in fields(Message)}
                    yield Message(**{k: v for k, v in data.items() if k in known})

    def load_messages(self) -> list[Message]:
        return list(self.iter_messages())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
