"""insights + codebase sections and the nested discovery abstraction."""

from __future__ import annotations

from compact_agent.sections.discovery import nested_discovery_doc
from insights import InsightsSection  # example plugins (see conftest)
from codebase import CodebaseSection


def test_nested_discovery_lists_each_tier():
    doc = nested_discovery_doc("places/", ["country", "region", "place"])
    assert "list_files('places/')" in doc and "COUNTRYS" in doc.upper()
    assert "list_files('places/<country>/')" in doc
    assert "list_files('places/<country>/<region>/')" in doc


def test_insights_resolve_and_raised_limit():
    s = InsightsSection()
    assert s.resolve(["insights", "CB Rate Outlook"], set()) == "insights/cb-rate-outlook.md"
    assert s.resolve(["insights", "macro", "Inflation"], set()) == "insights/macro/inflation.md"
    assert s.char_limit("insights/x.md") >= 10000  # raised vs other sections


def test_codebase_resolve_keeps_extension():
    s = CodebaseSection()
    assert s.resolve(["code", "gigacode-rag", "src", "Engine.py"], set()) == "code/gigacode-rag/src/engine.py"
    assert s.resolve(["code", "proj", "README"], set()) == "code/proj/readme.md"  # no ext -> .md


def test_owns_boundaries():
    assert InsightsSection().owns(["insights", "x"]) and not InsightsSection().owns(["insights"])
    assert CodebaseSection().owns(["code", "p", "f"]) and not CodebaseSection().owns(["world"])


def test_store_version_bumps_on_every_mutation():
    # The version counter is what makes the engine's assembled-view memoization safe:
    # any mutation must invalidate it; reads must not.
    from compact_agent.sections.store import VStore
    store = VStore(sections=[InsightsSection()], known_keys=set())
    v0 = store.version
    key = store.write("insights/topic.md", "note")
    assert store.version > v0
    v1 = store.version
    store.read(key)
    assert store.version == v1          # reads don't invalidate
    store.append(key, "more")
    assert store.version > v1
    v2 = store.version
    store.delete(key)
    assert store.version > v2

def test_relative_path_segments_are_rejected():
    import pytest
    from compact_agent.sections.store import StoreError, _split
    for bad in ("history/../people", "./history/2026-01-01", "a/.."):
        with pytest.raises(StoreError):
            _split(bad)
    assert _split("history/2026-01-01") == ["history", "2026-01-01"]
