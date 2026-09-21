"""Convert a Telegram Desktop chat export (result.json) into the unified store.

This is an OFFLINE converter — it reads a file you already exported (Telegram Desktop →
chat → Export chat history → JSON) and never contacts Telegram. It shows the input
contract every importer must meet; adapt it for other exports.

    uv run python examples/import_telegram_export.py path/to/result.json my-chat
    uv run compact-agent compact my-chat

Mentions are NOT resolved (that would need the platform); the identity layer still
pseudonymises every @handle, it just cannot fold a tagged-but-silent user onto a speaker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from compact_agent.storage import ChatMeta, ChatStore, Message, now_iso, slugify


def _from_id(v) -> int | None:
    # Telegram exports "user123" / "channel123" / "chat123"; keep the numeric tail.
    if v is None:
        return None
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    return int(digits) if digits else None


def _text(v) -> str:
    # `text` is a string or a list of strings and {"type", "text"} entity dicts.
    if isinstance(v, str):
        return v
    return "".join(part if isinstance(part, str) else part.get("text", "") for part in v or [])


def convert(export: Path, slug: str) -> int:
    data = json.loads(export.read_text(encoding="utf-8"))
    store = ChatStore(slug)
    store.ensure()
    store.reset()
    n = 0
    for raw in data.get("messages", []):
        if raw.get("type") != "message":
            continue  # skip service messages (joins, pins, …)
        media_type = None
        media_path = None
        if raw.get("photo"):
            media_type, media_path = "photo", str(export.parent / raw["photo"])
        elif raw.get("media_type"):
            media_type = raw["media_type"]  # sticker | voice_message | video_file | …
        store.append_message(Message(
            id=int(raw["id"]),
            date=raw["date"],  # export-local time; only the day matters downstream
            sender_id=_from_id(raw.get("from_id")),
            sender_name=raw.get("from"),
            text=_text(raw.get("text")),
            reply_to=raw.get("reply_to_message_id"),
            media_type=media_type,
            media_path=media_path,
        ))
        n += 1
    kind = {"personal_chat": "user", "public_channel": "channel", "private_channel": "channel"}.get(
        data.get("type", ""), "group")
    store.write_meta(ChatMeta(
        chat_id=int(data.get("id", 0)), title=data.get("name") or slug, kind=kind, slug=slug,
        fetched_at=now_iso(), message_count=n, extra={"platform": "telegram", "import": "desktop-export"},
    ))
    return n


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    slug = sys.argv[2] if len(sys.argv) > 2 else slugify(src.parent.name)
    print(f"wrote {convert(src, slug)} messages to data/{slug}/")
