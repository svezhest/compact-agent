"""Nested discovery — the reusable "list each tier before you file" guidance.

Sections with a NESTED namespace (a codebase's project/path, insights' topic, a
geography's country/region/place) all face the same hazard: without seeing what already exists at each
level, the model forks near-duplicate names (Russia vs Russian Federation). This is the
one place that describes the fix, parameterised by the namespace prefix and its ordered
tier names — so it works for any nesting depth and every section asks the same way.
"""

from __future__ import annotations


def nested_discovery_doc(prefix: str, tiers: list[str]) -> str:
    """Guidance telling the model to `list_files` at each tier before writing.

    ``prefix`` is the section's namespace head (e.g. ``"places/"``); ``tiers`` are the
    ordered level names (e.g. ``["country", "region", "place"]``). Emits one list_files
    line per level, each scoped under the chosen ancestors, then a MERGE-in-place close."""
    lines = [
        "DISCOVER BEFORE YOU FILE — list each level you have not already seen, to REUSE an "
        "existing name instead of forking a near-duplicate:"
    ]
    path = prefix
    for tier in tiers:
        lines.append(f"  - list_files('{path}') → existing {tier.upper()}S; reuse one if it fits.")
        path += f"<{tier}>/"
    lines.append(
        "Go as deep as the content justifies and stop where it gets uncertain. THEN read the "
        "target note (if it exists) and MERGE in place; create it only if genuinely new."
    )
    return "\n".join(lines)
