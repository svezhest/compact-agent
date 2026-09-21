"""Describe downloaded photos with a vision model: a structured (OCR, description) pair.

Replaces OCR-only and caption-only passes with the minimum structure that actually
pays off downstream: per image we extract

  * ``ocr``         — verbatim on-image text, as markdown (names, overlay claims, signs);
  * ``description`` — a free, factual sentence or two of the scene.

A post may carry several images (``media_meta["images"]``), so we run the model per image
(sending 10–20 images in one call would blow the context window) and combine them into the
post's ``photo_desc`` (rendered to the summariser as ``[photo: …]``); the raw per-image pairs
are kept in ``media_meta['vision']`` for downstream use. Talks ONLY to the vision endpoint —
never a platform.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from .config import Config
from .storage import ChatStore, Message
from .ui import console


class PhotoVision(BaseModel):
    """Structured vision output — a minimal, high-leverage pair."""

    ocr: str = Field(
        description="All text visible IN the image, verbatim, as markdown (keep line "
        "breaks and ordering). Empty string if the image has no text."
    )
    description: str = Field(
        description="One or two factual sentences describing the scene: landscape, "
        "place, objects, mood. No speculation, no commentary."
    )


# Delimited sections, NOT JSON: the OCR field is multi-line markdown, and models
# routinely emit raw (unescaped) newlines inside a JSON string — which is invalid JSON
# and broke parsing on many images. Sentinel-delimited sections parse trivially and are
# robust to any content (newlines, quotes, braces).
_OCR_MARK = "===OCR==="
_DESC_MARK = "===DESCRIPTION==="
_PROMPT = f"""\
You are a vision extractor for a shared image. Return EXACTLY this format and \
nothing else:
{_OCR_MARK}
<every piece of text visible IN the image, transcribed verbatim as markdown, preserving \
line breaks and order; leave blank if the image has no text>
{_DESC_MARK}
<one or two factual sentences on what the scene shows: landscape, place, notable objects, \
mood — only what is visible, no speculation>"""



def _parse_vision(content: str) -> PhotoVision:
    """Split the model's reply on the OCR/DESCRIPTION sentinels (robust to any
    content — multiline, quotes, braces). Splitting tail-first peels each section cleanly."""
    s = content.strip()
    description = ""
    if _DESC_MARK in s:
        s, description = s.split(_DESC_MARK, 1)
    ocr = s.split(_OCR_MARK, 1)[-1] if _OCR_MARK in s else s
    return PhotoVision(ocr=ocr.strip(), description=description.strip())


async def _vision(cfg: Config, client: httpx.AsyncClient, image_bytes: bytes,
                  media_type: str) -> PhotoVision:
    """One image -> (ocr, description) via the OpenAI-compatible vision endpoint."""
    b64 = base64.b64encode(image_bytes).decode()
    prompt = _PROMPT
    payload = {
        "model": cfg.vision_model,
        "stream": False,
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
            ],
        }],
    }
    r = await client.post(f"{cfg.vision_base}/chat/completions", json=payload)
    r.raise_for_status()
    return _parse_vision(r.json()["choices"][0]["message"]["content"])


def _one(p: dict, head: str = "") -> str:
    """Render one image's (description, ocr) into a compact block."""
    block = f"{head}{p['description']}".rstrip()
    if p.get("ocr", "").strip():
        block += f"\nOCR:\n{p['ocr']}"
    return block


def _render(pairs: list[dict]) -> str:
    """Combine a carousel's per-image vision into one photo_desc block."""
    if len(pairs) == 1:
        return _one(pairs[0]).strip()
    return "\n\n".join(_one(p, head=f"[image {i:02d}] ") for i, p in enumerate(pairs))


async def describe_photos(
    cfg: Config, store: ChatStore, messages: list[Message], timeout: float | None = None,
) -> int:
    """Fill ``photo_desc`` for every photo post that lacks one, via the vision model.

    Operates in place and rewrites the store after each post so progress survives a
    crash. Re-used images (same file) are described once and cached. Returns the number
    of posts described.
    """
    # Multi-image posts (media_meta["images"]) and single photos / video covers
    # (media_path) both carry images worth describing. Stickers/etc. left alone.
    todo = [
        m for m in messages
        if m.media_type in ("photo", "video")
        and (m.media_meta.get("images") or m.media_path)
        and not m.photo_desc
    ]
    if not todo:
        return 0

    console.print(
        f"[dim]Describing {len(todo)} photo post(s) via {cfg.vision_base} ({cfg.vision_model}) ...[/dim]"
    )

    cache: dict[str, dict] = {}  # image path -> {ocr, description}, dedups re-used images
    done = 0
    to = timeout if timeout is not None else 300.0
    async with httpx.AsyncClient(timeout=to) as client:
        for m in todo:
            images = list(m.media_meta.get("images") or ([m.media_path] if m.media_path else []))
            if not images:
                continue

            pairs: list[dict] = []
            for rel in images:
                if rel in cache:
                    pairs.append(cache[rel])
                    continue
                # media paths may be CWD-relative, absolute, or relative to the chat dir
                # (media/<id>/NN.jpg). Try as-is first, then under the chat dir.
                p = Path(rel)
                if not p.exists():
                    p = store.dir / rel
                if not p.exists():
                    console.print(f"[yellow]missing file[/yellow]: {rel}")
                    continue
                media_type = mimetypes.guess_type(p.name)[0] or "image/jpeg"
                try:
                    v = await _vision(cfg, client, p.read_bytes(), media_type)
                    pair = {"image": rel, "ocr": v.ocr.strip(),
                            "description": v.description.strip()}
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    console.print(f"  [yellow]vision failed[/yellow] {rel}: {e}")
                    pair = {"image": rel, "ocr": "", "description": ""}
                cache[rel] = pair
                pairs.append(pair)

            if not pairs:
                continue
            m.media_meta["vision"] = pairs
            m.photo_desc = _render(pairs) or None
            if m.photo_desc:
                done += 1
            store.rewrite(messages)  # durable after each post
            console.print(f"  described post {m.id} ({len(pairs)} img, {done}/{len(todo)})")

    return done


async def read_photos(cfg: Config, slug: str, timeout: float | None = None) -> int:
    """CLI entry point: describe all undescribed photos in a stored chat."""
    store = ChatStore(slug)
    messages = store.load_messages()
    if not messages:
        raise SystemExit(f"No messages found for '{slug}' under data/.")

    done = await describe_photos(cfg, store, messages, timeout)
    if done == 0:
        console.print("[green]Nothing to do[/green] — no photos need describing.")
    else:
        console.print(f"[bold green]Done[/bold green] — {done} photo post(s) described.")
    return done
