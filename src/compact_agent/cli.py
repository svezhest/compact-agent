"""compact-agent CLI — topic-bucketed compaction of a stored conversation via an LLM.

    compact-agent list                              # chats under ./data/
    compact-agent describe <slug> --photos          # optional: caption/OCR images via a vision model
    compact-agent compact <slug> [-s study ...]     # build / resume the report

Input is a `data/<slug>/` folder in the unified store format (see README). This tool never
talks to a messaging platform — bring the data yourself (see examples/).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer

from .config import Config
from .engine import compact_chat_v2
from .describe import read_photos
from .sections.registry import available, load_spec
from .storage import DATA_ROOT, ChatStore
from .ui import console

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def describe(
    slug: str = typer.Argument(..., help="Chat slug under ./data/ (see `list`)."),
    photos: bool = typer.Option(False, "--photos", help="Describe photos with the vision model."),
    timeout: Optional[float] = typer.Option(
        None, "--timeout", help="Per-request timeout in seconds (default: 300)."
    ),
):
    """Describe media with the LLM, filling `[photo]` placeholders in the messages.

    Choose at least one media type to describe (currently only --photos).
    """
    cfg = Config.load()
    if not photos:
        raise typer.BadParameter("Select a media type to describe, e.g. --photos.")
    asyncio.run(read_photos(cfg, slug, timeout))


@app.command()
def compact(
    slug: str = typer.Argument(..., help="Chat slug under ./data/ (see `list`)."),
    window: Optional[int] = typer.Option(
        None, "--window", "-w",
        help="The model's context window in TOKENS (MAX_CONTEXT_TOKENS). The ONE knob "
        "that shapes the run: each batch splits it into a summary side (SUMMARY_SHARE, "
        "default 1/3) and a workflow side. Defaults to env MAX_CONTEXT_TOKENS.",
    ),
    section: list[str] = typer.Option(
        [], "--section", "-s",
        help="Add/configure a section, e.g. -s study or -s people:timeline=off. "
        f"Repeatable. Built in: {', '.join(available())}. Overrides sections.toml.",
    ),
    drop: list[str] = typer.Option(
        [], "--drop", help="Drop a section from this run (repeatable), e.g. --drop people."
    ),
    plugins: list[Path] = typer.Option(
        [], "--plugins", "-p",
        help="Directory of section plugins (*.py exporting `section(features)`). Repeatable. "
        "Also settable as `plugin_dirs` in sections.toml.",
    ),
    timeout: Optional[float] = typer.Option(
        None, "--timeout", help="Per-request timeout in seconds (default: 1200)."
    ),
    concurrency: int = typer.Option(
        1, "--concurrency", "-j",
        help="Concurrent model calls for independent ingest groups (per person / post / "
        "day). Whole-batch digests still run one at a time. Env: COMPACT_CONCURRENCY.",
    ),
):
    """Compact a stored chat into a structured markdown report (resumable).

    The active sections come from sections.toml (chat dir or cwd) or the built-in
    default (people, world, history, key_facts), with -s/--drop overriding per run.
    Photos are left as [photo] placeholders unless `describe --photos` ran first.
    """
    cfg = Config.load()
    w = window or cfg.max_context_tokens

    cli_add: dict[str, dict] = {}
    for spec_str in section:
        name, _, feat_str = spec_str.partition(":")
        feats: dict[str, bool] = {}
        for kv in (f for f in feat_str.split(",") if f):
            k, _, v = kv.partition("=")
            feats[k.strip()] = (v.strip().lower() not in ("off", "false", "0", "no")) if v else True
        cli_add[name.strip()] = feats

    toml = ChatStore(slug).dir / "sections.toml"
    if not toml.exists():
        toml = Path("sections.toml")
    try:
        spec = load_spec(toml, cli_add or None, list(drop) or None, list(plugins) or None)
    except ValueError as e:
        raise typer.BadParameter(str(e))

    import os
    j = concurrency if concurrency != 1 else int(os.getenv("COMPACT_CONCURRENCY", "1"))
    asyncio.run(compact_chat_v2(cfg, slug, w, timeout, spec, concurrency=j))


@app.command(name="list")
def list_chats():
    """List chats stored under ./data/."""
    if not DATA_ROOT.exists():
        console.print("No chats under ./data/ yet.")
        raise typer.Exit()
    for d in sorted(DATA_ROOT.iterdir()):
        if (d / "meta.json").exists():
            console.print(f"  [bold]{d.name}[/bold]")


if __name__ == "__main__":
    app()
