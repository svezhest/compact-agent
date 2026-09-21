"""Render stored messages into LLM-facing text, chunked by day to fit context."""

from __future__ import annotations

from collections import defaultdict

from .identity import IdentityMap
from .storage import Message


def estimate_tokens(text: str) -> int:
    # Cheap heuristic: ~4 chars/token. Good enough for budgeting.
    return max(1, len(text) // 4)


def _render_poll(meta: dict) -> str:
    head = f"  [poll{' (quiz)' if meta.get('quiz') else ''}: {meta.get('question', '')}]"
    lines = [head]
    for o in meta.get("options", []):
        v = o.get("voters")
        lines.append(f"    - {o.get('text', '')}" + (f" — {v} votes" if v is not None else ""))
    total = meta.get("total_voters")
    if total is not None:
        lines.append(f"    ({total} total voters{', closed' if meta.get('closed') else ''})")
    return "\n".join(lines)


def _render_media(m: Message) -> str | None:
    mt = m.media_type
    meta = m.media_meta or {}
    if mt == "photo":
        if m.photo_desc:
            return f"  [photo: {m.photo_desc}]"
        if meta.get("resend"):
            return "  [photo: re-sent image]"
        return "  [photo: download failed]" if meta.get("download_failed") else "  [photo]"
    if mt == "poll":
        return _render_poll(meta)
    if mt == "sticker":
        emoji = meta.get("emoji") or ""
        pack = meta.get("pack")
        return f'  [sticker: {emoji}' + (f', pack: "{pack}"]' if pack else "]")
    if mt in ("audio", "voice"):
        name = meta.get("filename") or meta.get("title") or mt
        return f"  [{mt}: {name}]"
    if mt == "video":
        return f"  [video: {meta.get('filename') or 'video'}]"
    if mt == "file":
        return f"  [file: {meta.get('filename') or 'file'}]"
    if mt == "contact":
        # A shared contact card names a person who is usually NOT a chat participant, so
        # the scrub table can't catch them — pseudonymize here with a stable digest (keyed
        # by phone when present, else name). Real name+phone stay only in messages.jsonl.
        from .identity import _digest
        seed = (meta.get("phone") or meta.get("name") or "").strip().lower()
        if not seed:
            return "  [contact]"
        return f"  [contact: @u{_digest(f'contact:{seed}')}]"
    if mt == "geo":
        return f"  [location: {meta.get('lat')}, {meta.get('long')}]"
    if mt:
        return f"  [{mt}]"
    return None


def media_slots(m: Message) -> list[str]:
    """The atom content model's media slots for one message: photos / audio / files.

    Lives HERE, next to `_render_media`, so there is exactly ONE media module (the two
    renderers diverged once — resend/download-failed detail in one, video-thumbnail in
    the other). Slot semantics: every unavailable modality is an explicit PLACEHOLDER
    (vision/transcription pipelines can fill the slot later), never a silent drop."""
    mt = m.media_type
    if not mt:
        return []
    meta = m.media_meta or {}
    if mt == "photo":
        if m.photo_desc:
            return [f"[photo: {m.photo_desc}]"]
        if meta.get("resend"):
            return ["[photo: re-sent image — vision unavailable]"]
        if meta.get("download_failed"):
            return ["[photo: download failed — vision unavailable]"]
        return ["[photo: vision unavailable]"]
    if mt in ("audio", "voice"):
        return ["[audio: transcription unavailable]"]
    if mt == "video":
        # A video is a file; we recognise it via its THUMBNAIL through vision. Surface that
        # as a vision slot (audio transcription is a separate, future ASR slot).
        if m.photo_desc:
            return [f"[video thumbnail: {m.photo_desc}]"]
        return [f"[file: {meta.get('filename') or 'video'} — thumbnail not described]"]
    if mt == "file":
        return [f"[file: {meta.get('filename') or 'file'}]"]
    # polls/stickers/contacts/geo etc. keep their compact descriptive form
    rendered = _render_media(m)
    return [rendered.strip()] if rendered else [f"[{mt}]"]


def render_message(m: Message, idmap: IdentityMap) -> str:
    # Identity is pseudonymized in code: the model sees the sender's @hash, never
    # the real display name. Known names are also scrubbed out of the body text.
    who = idmap.label_for(m.sender_id, m.sender_name)
    time = m.date[11:16] if len(m.date) >= 16 else ""
    parts = [f"[{time}] {who}:"]
    if m.text:
        parts.append(idmap.sanitize(m.text, sender_key=who))
    media = _render_media(m)
    if media:
        # Same treatment as the body: vision OCR / filenames can carry raw @handles.
        parts.append("\n" + idmap.sanitize(media))
    return " ".join(parts)


def group_by_day(messages: list[Message]) -> list[tuple[str, list[Message]]]:
    """Bucket messages by day.

    For channel data (messages carry ``post_id``) the ATOM is the post, not the day:
    every message is filed under its POST's day, so a post and all its comments — even
    ones written days later — stay together as one unit.
    """
    post_day = {
        m.post_id: m.day for m in messages if m.post_id is not None and m.id == m.post_id
    }
    days: dict[str, list[Message]] = defaultdict(list)
    for m in messages:
        day = post_day.get(m.post_id, m.day) if m.post_id is not None else m.day
        days[day].append(m)
    return sorted(days.items())


def _render_posts(messages: list[Message], idmap: IdentityMap) -> list[str]:
    """Channel layout: each post is an atom — the post line, then its comments."""
    by_post: dict[int, list[Message]] = defaultdict(list)
    for m in messages:
        by_post[m.post_id].append(m)
    lines: list[str] = []
    for pid in sorted(by_post):
        grp = sorted(by_post[pid], key=lambda m: m.id)
        post = next((m for m in grp if m.id == pid), None)
        comments = [m for m in grp if m.id != pid]
        if post is not None:
            lines.append("▸ POST — " + render_message(post, idmap))
        else:
            lines.append(f"▸ POST {pid} — (post text unavailable)")
        lines.extend("    ↳ " + render_message(c, idmap) for c in comments)
    return lines


def render_day(day: str, messages: list[Message], idmap: IdentityMap) -> str:
    lines = [f"## {day}"]
    if any(m.post_id is not None for m in messages):
        lines.extend(_render_posts(messages, idmap))  # channel: posts are the atoms
    else:
        lines.extend(render_message(m, idmap) for m in messages)
    return "\n".join(lines)
