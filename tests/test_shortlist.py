"""shortlist example plugin: one file, profile injected into the prompt, ingest task."""

from __future__ import annotations

import pytest

import shortlist  # example plugin (see conftest)
from compact_agent.sections.base import ViewContext
from compact_agent.sections.store import StoreError, VStore


def test_profile_is_injected(monkeypatch, tmp_path):
    prof = tmp_path / "profile.md"
    prof.write_text("Rust developer, remote only.")
    monkeypatch.setenv("COMPACT_PROFILE", str(prof))
    s = shortlist.section()
    assert "Rust developer, remote only." in s.paths_doc()


def test_single_path_and_view(monkeypatch):
    monkeypatch.delenv("COMPACT_PROFILE", raising=False)
    s = shortlist.section()
    st = VStore(sections=[s], known_keys=set())
    assert st.write("shortlist", "- [fit 8/10] acme — senior × ai_engineering") .startswith("shortlist")
    with pytest.raises(StoreError):
        s.resolve(["shortlist", "extra"], set())
    ctx = ViewContext(relative_date="2026-03-01", kind="channel", platform="telegram")
    assert "acme" in s.view_blocks(st, ctx)[0].text


def test_ingest_task_dedups():
    s = shortlist.section()
    rules = s.task_rules()
    assert len(rules) == 1 and rules[0].name == "ingest"
