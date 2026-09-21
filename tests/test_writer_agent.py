"""Wiring test for the engine's tool-calling writer agent (over VStore).

Uses pydantic-ai's FunctionModel to script exact tool calls (no network): the tools
mutate the store, a bad path bounces back as a ModelRetry the model recovers from, and
hitting the per-run request_limit stops gracefully (returns None) rather than crashing."""

from __future__ import annotations

import asyncio

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from compact_agent.config import Config
from compact_agent.engine import build_writer_agent
from compact_agent.llm import run_with_retries
import companies, contacts, job_offers, market  # example plugins (see conftest)
from compact_agent.sections import people, world
from compact_agent.sections.store import VStore


def _cfg():
    return Config(api_base="http://x", api_key="k", api_model="m")


def _store():
    return VStore(sections=[people.section(), world.section()], known_keys={"@uaaa"})


def _run_scripted(store, script):
    steps = list(script)

    def fn(messages: list[ModelRequest], info: AgentInfo) -> ModelResponse:
        rounds = sum(
            1 for m in messages
            for p in m.parts if p.part_kind in ("tool-return", "retry-prompt")
        )
        if rounds < len(steps):
            name, kwargs = steps[rounds]
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=kwargs)])
        return ModelResponse(parts=[TextPart("done")])

    a = build_writer_agent(_cfg(), store, system_prompt="sys")
    a.model = FunctionModel(fn)
    return a.run_sync("go")


def test_write_tool_mutates_store():
    store = _store()
    _run_scripted(store, [
        ("write", {"path": "world.md", "content": "# vivid\nthe group"}),
        ("write", {"path": "people/@uaaa/status", "content": "active"}),
    ])
    assert store.read("world.md") == "# VIVID\nthe group"   # heading kept, text ALL-CAPS
    assert store.read("people/@uaaa/status") == "active"


def test_edit_unseen_file_blocked_then_recovers_after_read():
    """A file that exists but was NOT shown/read this run can't be edited blindly: the
    edit bounces, the model reads it, then the edit lands."""
    store = _store()
    store.files["world.md"] = "the old group"  # pre-existing, unseen this run
    _run_scripted(store, [
        ("edit", {"path": "world.md", "old": "old", "new": "new"}),  # unseen -> bounced
        ("read", {"path": "world.md"}),                               # now seen
        ("edit", {"path": "world.md", "old": "old", "new": "new"}),  # lands
    ])
    assert store.read("world.md") == "the new group"


def test_overwrite_unseen_file_blocked():
    """Overwriting an existing-but-unseen file is rejected (no blind clobber)."""
    store = _store()
    store.files["world.md"] = "precious prior content"
    _run_scripted(store, [
        ("write", {"path": "world.md", "content": "junk", "overwrite": True}),  # bounced
        ("read", {"path": "world.md"}),
        ("write", {"path": "world.md", "content": "deliberate", "overwrite": True}),
    ])
    assert store.read("world.md") == "deliberate"


def test_seeing_file_in_report_satisfies_the_gate():
    """If the file's body is in the prompt (the assembled view), no explicit read needed."""
    store = _store()
    store.files["world.md"] = "the shown group"
    store.begin_run("=== CURRENT REPORT ===\nthe shown group\n")  # seeds `seen`
    # build an agent and edit directly (begin_run already marked world.md seen)
    assert store.edit("world.md", "shown", "seen") == "world.md"
    assert store.read("world.md") == "the seen group"


def test_touch_creates_empty_then_appendable():
    store = _store()
    _run_scripted(store, [
        ("touch", {"path": "world.md"}),
        ("append", {"path": "world.md", "content": "first block"}),
    ])
    assert store.read("world.md") == "first block"


def test_bad_path_recovers_via_model_retry():
    store = _store()
    _run_scripted(store, [
        ("write", {"path": "people/@uZZZ/status", "content": "x"}),  # unknown hash -> bounced
        ("write", {"path": "people/@uaaa/status", "content": "active"}),
    ])
    assert store.read("people/@uaaa/status") == "active"
    assert "people/@uZZZ/status.md" not in store.files


def test_hitting_request_limit_stops_gracefully():
    """pydantic-ai RAISES UsageLimitExceeded at the cap; the writes before it already
    landed, so run_with_retries returns None (graceful stop), never crashes."""
    store = _store()

    def fn(messages: list[ModelRequest], info: AgentInfo) -> ModelResponse:
        rounds = sum(
            1 for m in messages
            for p in m.parts if p.part_kind in ("tool-return", "retry-prompt")
        )
        return ModelResponse(parts=[ToolCallPart(
            tool_name="write",
            args={"path": "world.md", "content": f"round {rounds}", "overwrite": True},
        )])

    agent = build_writer_agent(_cfg(), store, system_prompt="sys")
    agent.model = FunctionModel(fn)
    result = asyncio.run(run_with_retries(agent, "go", request_limit=2))
    assert result is None
    assert store.files  # writes before the cap persisted


def test_normalize_headers_uppercases_outside_fences_keeps_code():
    from compact_agent.sections.store import normalize_headers
    body = "# key findings\nlots of detail\n```python\n# a code comment\nx = 1\n```\n## next"
    out = normalize_headers(body)
    assert "# KEY FINDINGS" in out          # header text upper-cased, marker kept
    assert "## NEXT" in out
    assert "# a code comment" in out         # inside ``` fence: untouched


# --- typed records: patch / query through the writer agent --------------------------------
def _jobs_store():
    return VStore(sections=[job_offers.section(), companies.section(),
                            contacts.section(), market.section()], known_keys=set())


def test_patch_tool_sets_typed_card_and_freeform_notes():
    store = _jobs_store()
    _run_scripted(store, [
        ("patch", {"path": "job_offers/acme/1-dev", "fields": {
            "id": "1-dev", "company": "acme", "specialization": "backend", "grade": "senior",
            "posted_at": "2026-02-20", "reward": {"net_yearly_usd_mean": 120000}, "status": "open"}}),
        ("write", {"path": "job_offers/acme/1-dev/notes", "content": "Builds the payments core."}),
    ])
    card = store.parse_typed("job_offers/acme/1-dev.json")
    assert card and card["specialization"] == "backend"
    assert card["reward"]["net_yearly_usd_mean"] == 120000
    assert store.read("job_offers/acme/1-dev/notes.md") == "Builds the payments core."


def test_patch_bad_enum_bounces_then_recovers():
    store = _jobs_store()
    _run_scripted(store, [
        ("patch", {"path": "job_offers/acme/1-dev", "fields": {"grade": "wizard"}}),  # bounced
        ("patch", {"path": "job_offers/acme/1-dev", "fields": {
            "id": "1-dev", "company": "acme", "specialization": "backend", "grade": "senior"}}),
    ])
    card = store.parse_typed("job_offers/acme/1-dev.json")
    assert card["grade"] == "senior"


def test_query_tool_runs_through_agent():
    store = _jobs_store()
    store.patch("job_offers/acme/1-dev", {"id": "1-dev", "company": "acme",
                                          "specialization": "backend", "posted_at": "2026-02-20",
                                          "status": "open"})
    _run_scripted(store, [
        ("query", {"collection": "job_offers/", "sort": "-posted_at", "limit": 5}),
    ])  # must not raise — the tool is wired and read-only
    assert store.parse_typed("job_offers/acme/1-dev.json")["specialization"] == "backend"
