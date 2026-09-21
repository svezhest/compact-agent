"""End-to-end engine test with a STUBBED model (no network).

Drives the real batch loop / trigger firing / task queue / view assembly / checkpoint &
resume — only the LLM call (`run_with_retries`) is replaced by a stub that simulates the
model writing a per-day history file, so we can assert the orchestration without a server.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from compact_agent import engine as eng
from compact_agent.config import Config
from compact_agent.storage import ChatMeta, ChatStore, Message, now_iso

SLUG = "_enginetest"


def _cfg():
    return Config(api_base="http://x", api_key="k", api_model="m")


def _seed(tmp_root):
    store = ChatStore(SLUG, root=tmp_root)
    store.ensure()
    msgs = [
        Message(id=1, date="2026-03-10T09:00:00Z", sender_id=11, sender_name="A", text="kickoff, weekly?"),
        Message(id=2, date="2026-03-10T09:05:00Z", sender_id=22, sender_name="B", text="weekly works"),
        Message(id=3, date="2026-03-12T10:00:00Z", sender_id=11, sender_name="A", text="agenda set", reply_to=1),
        Message(id=4, date="2026-04-02T10:00:00Z", sender_id=22, sender_name="B", text="month two"),
    ]
    for m in msgs:
        store.append_message(m)
    store.write_meta(ChatMeta(chat_id=1, title="t", kind="group", slug=SLUG,
                              fetched_at=now_iso(), message_count=len(msgs),
                              extra={"platform": "telegram"}))
    return store


@pytest.fixture
def chat(tmp_path, monkeypatch):
    # Point the engine's ChatStore at a temp data root.
    monkeypatch.setattr(eng, "ChatStore", lambda slug: ChatStore(slug, root=tmp_path))
    _seed(tmp_path)
    captured: dict = {}
    real_bwa = eng.build_writer_agent

    def patched_bwa(cfg, store, **kw):
        captured["store"] = store
        return real_bwa(cfg, store, **kw)

    monkeypatch.setattr(eng, "build_writer_agent", patched_bwa)

    async def fake_run(agent, prompt, request_limit=40):
        # Simulate the model: on an atom ingestion prompt, record a history file for the
        # batch's reference day (the "as of <YYYY-MM-DD>" anchor) + a person status.
        store = captured["store"]
        import re
        m = re.search(r"NEW DATA POINTS \(reply-chain atoms; as of (\d{4}-\d{2}-\d{2})\)", prompt)
        if m:
            from compact_agent.sections.store import StoreError
            day = m.group(1)
            store.write(f"history/{day}", f"On {day}: stuff happened.", overwrite=True)
            for key in list(store.known_keys)[:1]:
                try:  # people section may not be active this run
                    if f"people/{key}/status.md" not in store.files:
                        store.write(f"people/{key}/status", "active — participant")
                except StoreError:
                    pass
        return None

    monkeypatch.setattr(eng, "run_with_retries", fake_run)
    yield tmp_path
    shutil.rmtree(tmp_path / SLUG, ignore_errors=True)


def test_engine_runs_end_to_end(chat):
    out = asyncio.run(eng.compact_chat_v2(_cfg(), SLUG, window=20000))
    assert out.exists()
    report = out.read_text()
    assert "# History" in report and "# People and Roles" in report
    # the stub recorded each processed day -> history files exist + appear in the view
    prog = (chat / SLUG / ".compact_progress.json")
    import json
    data = json.loads(prog.read_text())
    assert data["version"] == 3
    # every (section × atom) that was ingested is recorded with its extent
    assert data["done_atoms"], "expected processed (section, atom) entries"
    assert any(k.startswith("history:") for k in data["done_atoms"])
    assert any(k.startswith("history/") for k in data["store"]["files"])


def test_engine_resumes_without_reprocessing(chat, monkeypatch):
    asyncio.run(eng.compact_chat_v2(_cfg(), SLUG, window=20000))
    # Second run: every day already done -> the ingestion stub must NOT be asked again.
    calls = {"n": 0}
    prev = eng.run_with_retries

    async def counting(agent, prompt, request_limit=40):
        if "NEW DATA POINTS" in prompt:
            calls["n"] += 1
        return await prev(agent, prompt, request_limit=request_limit)

    monkeypatch.setattr(eng, "run_with_retries", counting)
    asyncio.run(eng.compact_chat_v2(_cfg(), SLUG, window=20000))
    assert calls["n"] == 0  # nothing re-ingested on resume


def test_engine_without_people_section(chat):
    spec = {"history": {}, "key_facts": {}}
    out = asyncio.run(eng.compact_chat_v2(_cfg(), SLUG, window=20000, spec=spec))
    report = out.read_text()
    assert "# People and Roles" not in report
    assert "# History" in report


def test_truncated_ingest_never_loses_atoms(chat, monkeypatch):
    """If every ingest run is truncated (returns None, as the cap/overflow does), the
    adaptive chunk must shrink to single atoms and force progress one-by-one — terminating
    (no infinite loop) and eventually marking every (section × atom) done, never silently
    dropping a chunk it could not finish."""
    runs = {"n": 0}

    async def always_truncated(agent, prompt, request_limit=40):
        if "NEW DATA POINTS" in prompt:
            runs["n"] += 1
        return None  # simulate the per-run cap / context overflow signal

    import json
    monkeypatch.setattr(eng, "run_with_retries", always_truncated)
    asyncio.run(eng.compact_chat_v2(_cfg(), SLUG, window=20000))
    prog = json.loads((chat / SLUG / ".compact_progress.json").read_text())
    # progress is made (atoms forced through singly) and the run terminated
    assert prog["done_atoms"], "force-progress-by-one must still record (section, atom) work"
    assert runs["n"] > 0

def test_corrupt_checkpoint_degrades_to_fresh_run(chat):
    """A truncated/corrupt .compact_progress.json (crash mid-write before the atomic
    tmp→replace, disk trouble) must warn + start fresh — never a stack trace."""
    import json
    asyncio.run(eng.compact_chat_v2(_cfg(), SLUG, window=20000))
    prog = chat / SLUG / ".compact_progress.json"
    full = prog.read_text()
    prog.write_text(full[: len(full) // 2])  # simulate a torn write
    out = asyncio.run(eng.compact_chat_v2(_cfg(), SLUG, window=20000))
    assert out.exists()
    data = json.loads(prog.read_text())  # checkpoint rebuilt, valid again
    assert data["version"] == 3 and data["done_atoms"]


def test_engine_parallel_ingest_matches_sequential(chat):
    """concurrency>1 runs per-person/per-day groups concurrently; the result set of
    processed (section × atom) entries and store files must equal the sequential run's."""
    import json
    seq = asyncio.run(eng.compact_chat_v2(_cfg(), SLUG, window=20000))
    seq_data = json.loads((chat / SLUG / ".compact_progress.json").read_text())
    (chat / SLUG / ".compact_progress.json").unlink()
    par = asyncio.run(eng.compact_chat_v2(_cfg(), SLUG, window=20000, concurrency=4))
    par_data = json.loads((chat / SLUG / ".compact_progress.json").read_text())
    assert par.exists()
    assert set(par_data["done_atoms"]) == set(seq_data["done_atoms"])
    assert set(par_data["store"]["files"]) == set(seq_data["store"]["files"])
