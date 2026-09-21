"""Section registry + run-spec loading (built-in default, sections.toml, CLI overrides).

Built-in sections are the four domain-neutral ones: people, world, history, key_facts.
Anything domain-specific (a study log, a job board, a codebase reconstruction) is a
PLUGIN: a Python module exporting ``section(features: dict | None) -> Section`` that is
loaded from a directory (``plugin_dirs`` in sections.toml, or ``--plugins`` on the CLI)
or registered in code with :func:`register`. See ``examples/sections/`` for worked ones.

A run's section set is resolved with this precedence:
  1. ``DEFAULT_SPEC`` — the built-in default (the four core sections).
  2. a ``sections.toml`` file (chat dir or cwd), if present.
  3. CLI overrides (add/drop a section, toggle a feature).

The spec is an ORDERED dict ``{name: {feature: bool}}``; order is the view order.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Callable

from .base import Section
from . import history, key_facts, people, world

Factory = Callable[..., Section]

# name -> factory(features) -> Section
SECTION_FACTORIES: dict[str, Factory] = {
    "people": people.section,
    "world": world.section,
    "history": history.section,
    "key_facts": key_facts.section,
}

# The built-in default: the four core sections, in view order.
DEFAULT_SPEC: dict[str, dict] = {
    "people": {},
    "world": {},
    "history": {},
    "key_facts": {},
}


def available() -> list[str]:
    return list(SECTION_FACTORIES)


def register(name: str, factory: Factory) -> None:
    """Make a section available under ``name`` (library use: register your own)."""
    SECTION_FACTORIES[name] = factory


def load_plugin_dir(path: Path | str) -> list[str]:
    """Import every ``*.py`` in ``path`` as a section plugin and register it under the
    file's stem. A module without a ``section`` callable is skipped. Returns the names."""
    path = Path(path)
    if not path.is_dir():
        raise ValueError(f"Plugin directory not found: {path}")
    names: list[str] = []
    for file in sorted(path.glob("*.py")):
        if file.name.startswith("_"):
            continue
        modname = f"compact_agent_plugins.{file.stem}"
        spec = importlib.util.spec_from_file_location(modname, file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        spec.loader.exec_module(module)
        factory = getattr(module, "section", None)
        if callable(factory):
            register(file.stem, factory)
            names.append(file.stem)
    return names


def load_spec(
    toml_path: Path | None = None,
    cli_add: dict[str, dict] | None = None,
    cli_drop: list[str] | None = None,
    plugin_dirs: list[Path | str] | None = None,
) -> dict[str, dict]:
    """Resolve the section spec: DEFAULT_SPEC <- sections.toml <- CLI overrides.

    sections.toml shape::

        plugin_dirs = ["examples/sections"]   # optional, relative to the toml file

        [sections.people]
        timeline = true
        [sections.world]
        [sections.history]
        [sections.study]

    ``cli_add`` adds/updates sections+features; ``cli_drop`` removes sections;
    ``plugin_dirs`` are loaded before names are validated (CLI ones after the toml's).
    """
    for d in plugin_dirs or []:
        load_plugin_dir(d)

    spec: dict[str, dict] = {k: dict(v) for k, v in DEFAULT_SPEC.items()}

    if toml_path and toml_path.exists():
        import tomllib

        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        for d in data.get("plugin_dirs", []) or []:
            p = Path(d)
            load_plugin_dir(p if p.is_absolute() else toml_path.parent / p)
        file_sections = data.get("sections", {})
        if file_sections:
            # A file with a [sections.*] table REPLACES the default set (explicit wins),
            # preserving its declared order.
            spec = {}
            for name, feats in file_sections.items():
                if name not in SECTION_FACTORIES:
                    raise ValueError(
                        f"Unknown section '{name}' in {toml_path}. Available: "
                        f"{', '.join(available())}."
                    )
                spec[name] = {k: bool(v) for k, v in (feats or {}).items()}

    for name in cli_drop or []:
        spec.pop(name, None)

    for name, feats in (cli_add or {}).items():
        if name not in SECTION_FACTORIES:
            raise ValueError(f"Unknown section '{name}'. Available: {', '.join(available())}.")
        spec.setdefault(name, {})
        spec[name].update(feats)

    if not spec:
        raise ValueError("No sections selected — a run needs at least one section.")
    return spec


def build_sections(spec: dict[str, dict]) -> list[Section]:
    """Instantiate the ordered Section objects for a resolved spec."""
    out: list[Section] = []
    for name, feats in spec.items():
        if name not in SECTION_FACTORIES:
            raise ValueError(f"Unknown section '{name}'. Available: {', '.join(available())}.")
        out.append(SECTION_FACTORIES[name](features=feats or None))
    return out
