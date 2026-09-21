"""The atom model — reply-chain grouping, content render, and the task queue.

An atom is a reply chain as a Set: one atom per chain root, presented once. A grown chain
is re-ingested once via the engine's done_atoms extent check (delta-only). Content renders
forward-compatibly with explicit placeholders for unavailable photo/audio/file modalities."""

from __future__ import annotations

from compact_agent.atom import Atom, build_atoms, chain_root, render_atom_content
from compact_agent.engine import TaskQueue
from compact_agent.identity import build_identity
from compact_agent.sections.base import Task
from compact_agent.storage import Message


def _m(id, date, sender=11, text="x", reply_to=None, post_id=None, topic_id=None,
       media_type=None, media_meta=None, photo_desc=None):
    return Message(id=id, date=date, sender_id=sender, sender_name="A", text=text,
                   reply_to=reply_to, post_id=post_id, topic_id=topic_id,
                   media_type=media_type, media_meta=media_meta or {}, photo_desc=photo_desc)


def _by_id(msgs):
    return {m.id: m for m in msgs}


# ---- chain_root --------------------------------------------------------------
def test_chain_root_singleton_is_self():
    m = _m(1, "2026-01-01T10:00:00Z")
    assert chain_root(m, _by_id([m])) == 1


def test_chain_root_walks_to_top():
    a = _m(1, "2026-01-01T10:00:00Z")
    b = _m(2, "2026-01-01T10:01:00Z", reply_to=1)
    c = _m(3, "2026-01-01T10:02:00Z", reply_to=2)
    msgs = [a, b, c]
    assert chain_root(c, _by_id(msgs)) == 1
    assert chain_root(b, _by_id(msgs)) == 1


def test_chain_root_channel_post_is_self():
    post = _m(5, "2026-01-01T10:00:00Z", post_id=5)
    comment = _m(6, "2026-01-01T10:01:00Z", post_id=5, reply_to=5)
    assert chain_root(post, _by_id([post, comment])) == 5
    assert chain_root(comment, _by_id([post, comment])) == 5


def test_chain_root_missing_parent_is_that_parent_id():
    orphan = _m(2, "2026-01-01T10:00:00Z", reply_to=99)  # 99 not in store
    assert chain_root(orphan, _by_id([orphan])) == 99


def test_chain_root_cycle_is_safe():
    a = _m(1, "2026-01-01T10:00:00Z", reply_to=2)
    b = _m(2, "2026-01-01T10:01:00Z", reply_to=1)
    # must terminate (cap + seen-set), not hang
    assert chain_root(a, _by_id([a, b])) in (1, 2)


# ---- build_atoms -------------------------------------------------------------
def test_build_atoms_groups_chain_into_one_and_orders_by_leaf():
    chain = [
        _m(1, "2026-01-01T10:00:00Z"),
        _m(2, "2026-01-01T10:05:00Z", reply_to=1),
    ]
    later_singleton = _m(3, "2026-01-02T09:00:00Z")
    atoms = build_atoms([chain[0], chain[1], later_singleton])
    assert len(atoms) == 2
    chain_atom = next(a for a in atoms if a.root_id == 1)
    assert [m.id for m in chain_atom.members] == [1, 2]
    assert chain_atom.extent == (2, "2026-01-01T10:05:00Z")
    # ordered oldest leaf first
    assert [a.root_id for a in atoms] == [1, 3]


def test_build_atoms_source_from_leaf():
    post = _m(5, "2026-01-01T10:00:00Z", post_id=5)
    a = build_atoms([post])[0]
    assert a.source == "channel"
    topic_msg = _m(7, "2026-01-01T10:00:00Z", topic_id=42)
    assert build_atoms([topic_msg])[0].source == "chat/topic:42"


# ---- content render (forward-compatible placeholders) ------------------------
def test_render_atom_content_modalities():
    msgs = [
        _m(1, "2026-01-01T10:00:00Z", text="hello"),
        _m(2, "2026-01-01T10:01:00Z", reply_to=1, text="", media_type="photo"),
        _m(3, "2026-01-01T10:02:00Z", reply_to=1, text="see pic", media_type="photo",
           photo_desc="a whiteboard with a proof"),
        _m(4, "2026-01-01T10:03:00Z", reply_to=1, text="", media_type="voice"),
        _m(5, "2026-01-01T10:04:00Z", reply_to=1, text="", media_type="file",
           media_meta={"filename": "slides.pdf"}),
    ]
    idmap = build_identity(msgs)
    out = render_atom_content(build_atoms(msgs)[0], idmap)
    assert "hello" in out
    assert "[photo: vision unavailable]" in out          # no desc -> placeholder
    assert "[photo: a whiteboard with a proof]" in out    # desc present
    assert "[audio: transcription unavailable]" in out    # voice placeholder
    assert "[file: slides.pdf]" in out


# ---- merged-origin source ----------------------------------------------------
def test_merged_source_is_surfaced_to_model():
    """A merged store tags each message with its origin chat (extra.source); the atom adopts
    it and render shows a [source: …] line so the model keeps separate projects apart."""
    m = _m(1, "2026-01-01T10:00:00Z", text="hi")
    m.extra = {"source": "магистратура-кт-2025"}
    atom = build_atoms([m])[0]
    assert atom.source == "магистратура-кт-2025"          # origin wins over structural source
    out = render_atom_content(atom, build_identity([m]))
    assert "[source: магистратура-кт-2025]" in out
    assert "hi" in out


def test_structural_source_is_not_shown():
    """A plain (non-merged) chat keeps the structural placeholder and adds NO source line."""
    m = _m(1, "2026-01-01T10:00:00Z", text="hi", topic_id=42)
    out = render_atom_content(build_atoms([m])[0], build_identity([m]))
    assert "[source:" not in out                          # chat/topic:42 is structural, hidden


def test_ingest_groups_default_is_one_per_batch():
    from compact_agent.sections import world
    msgs = [_m(1, "2026-01-01T10:00:00Z"), _m(2, "2026-01-01T10:01:00Z", sender=22)]
    atoms = build_atoms(msgs)
    groups = world.section().ingest_groups(atoms)
    assert len(groups) == 1                       # whole batch digested in one run
    assert len(groups[0][1]) == len(atoms)


def test_ingest_groups_people_is_one_per_author():
    from compact_agent.sections import people
    msgs = [
        _m(1, "2026-01-01T10:00:00Z", sender=11),
        _m(2, "2026-01-01T10:01:00Z", sender=22),
        _m(3, "2026-01-01T10:02:00Z", sender=11),  # same author as atom 1
    ]
    idmap = build_identity(msgs)
    atoms = build_atoms(msgs, idmap=idmap)
    groups = dict(people.section().ingest_groups(atoms))
    assert len(groups) == 2                        # two distinct authors -> two runs
    # the author of msgs 1 & 3 owns two atoms; the other owns one
    sizes = sorted(len(v) for v in groups.values())
    assert sizes == [1, 2]


def test_ingest_groups_history_is_one_per_day():
    from compact_agent.sections import history
    msgs = [
        _m(1, "2026-01-01T10:00:00Z"),
        _m(2, "2026-01-01T18:00:00Z", sender=22),
        _m(3, "2026-01-02T09:00:00Z"),
    ]
    atoms = build_atoms(msgs)
    groups = dict(history.section().ingest_groups(atoms))
    assert set(groups) == {"2026-01-01", "2026-01-02"}   # one run per day
    assert len(groups["2026-01-01"]) == 2


def test_people_resolve_recovers_dropped_at_prefix():
    """The model routinely writes 'people/u00381f78/...' (no @); resolve must recover the
    canonical '@u00381f78' key instead of bouncing 'Unknown person' on every tool call."""
    from compact_agent.sections import people
    sec = people.section()
    known = {"@u00381f78"}
    # bare key (no @) resolves to the canonical path
    assert sec.resolve(["people", "u00381f78", "status.md"], known) == "people/@u00381f78/status.md"
    # the correct @-form still works
    assert sec.resolve(["people", "@u00381f78", "status.md"], known) == "people/@u00381f78/status.md"
    # a genuinely unknown person still bounces
    import pytest
    from compact_agent.sections.store import StoreError
    with pytest.raises(StoreError):
        sec.resolve(["people", "unknownperson", "status.md"], known)


def test_default_prune_covers_every_section():
    """No section declares its own OnFileOverflow anymore; the engine's default prune
    bounds EVERY oversized file, flavoured by the section's one-line prune_hint."""
    from compact_agent.engine import _default_prune_task
    import study
    from compact_agent.sections import world
    t = _default_prune_task(world.section(), "world.md", 9000, 5000, 0)
    assert t.name == "tighten" and t.target == "world.md"
    assert t.dedup_key == "world:tighten:world.md"
    assert "evergreen portrait" in t.prompt          # world's prune_hint flavour
    assert t.priority > 0
    # a section with no special hint still gets a generic prune
    t2 = _default_prune_task(study.section(), "study/algos/2025-fall.md", 6000, 4000, 1)
    assert "exam-relevant" in t2.prompt               # study's hint
    assert "compacted version" in t2.prompt


def test_ingest_persists_mid_batch():
    """A big batch must checkpoint WITHIN ingest (per atom), not only at the batch boundary,
    so a crash/restart never loses ingested atoms — and as a background (non-blocking) write."""
    import inspect
    from compact_agent import engine
    assert engine._PERSIST_EVERY == 1                       # after every ingest group
    run_src = inspect.getsource(engine.Engine.run)
    assert "self._ingest(agent, batch_atoms, bi, rel, budget, done_atoms, persist)" in run_src
    ing_src = inspect.getsource(engine.Engine._ingest)
    assert "persist(rel, background=True)" in ing_src       # mid-batch write is offloaded


def test_engine_overflow_fallback_passes_a_section():
    """Regression: the engine's overflow fallback must hand `_default_prune_task` the
    Section OBJECT (it calls .prune_hint()/.name), not the section NAME string."""
    import inspect
    from compact_agent import engine
    src = inspect.getsource(engine.Engine._fire)
    assert "_default_prune_task(owner," in src      # owner (Section), never owner.name
    assert "_default_prune_task(owner.name" not in src


def test_no_section_declares_onfileoverflow():
    """DRY: the per-section overflow builders are gone — prune lives once in the engine."""
    from compact_agent.sections.base import OnFileOverflow
    import study
    from compact_agent.sections import world, key_facts, people, history
    for mod in (world, key_facts, people, history, study):
        rules = mod.section().task_rules()
        assert not any(isinstance(r.trigger, OnFileOverflow) for r in rules)


def test_queue_non_atom_tasks_keep_first_wins():
    q = TaskQueue()
    first = Task(priority=50.0, created_batch=0, section="world", name="x", target="",
                 prompt="first", dedup_key="world:x")
    second = Task(priority=50.0, created_batch=1, section="world", name="x", target="",
                  prompt="second", dedup_key="world:x")
    q.add(first)
    q.add(second)
    assert len(q) == 1
    assert q.pop_best(0).prompt == "first"


# ---- atom status (new / partial / finished) + partial delta render ----------
from compact_agent.engine import _atom_status


def test_atom_status_new_partial_finished():
    post = _m(100, "2026-06-01T10:00:00Z", post_id=100, text="post")
    c1 = _m(101, "2026-06-01T11:00:00Z", post_id=100, text="c1")
    atom = build_atoms([post, c1])[0]
    done: dict = {}
    assert _atom_status(done, "places", atom) == "new"
    done["places:100"] = [2, atom.leaf_date]
    assert _atom_status(done, "places", atom) == "finished"
    # a new reply grows the chain -> partial (an update to a finished atom)
    c2 = _m(102, "2026-06-02T09:00:00Z", post_id=100, text="c2")
    grown = build_atoms([post, c1, c2])[0]
    assert _atom_status(done, "places", grown) == "partial"


def test_partial_since_clean_tail_growth():
    from compact_agent.engine import _partial_since
    post = _m(100, "2026-06-01T10:00:00Z", post_id=100)
    c1 = _m(101, "2026-06-01T11:00:00Z", post_id=100)
    c2 = _m(102, "2026-06-02T09:00:00Z", post_id=100)
    grown = build_atoms([post, c1, c2])[0]
    # ingested at (2, c1.date); growth is a clean tail -> delta starts at index 2
    assert _partial_since([2, c1.date], grown.members) == 2
    # finished at full extent / never ingested -> full render
    assert _partial_since([3, c2.date], grown.members) == 0
    assert _partial_since(None, grown.members) == 0


def test_partial_since_backfill_forces_full_refeed():
    # A re-fetch backfills an OLDER comment mid-list (paginated comment threads): an index-based
    # slice would re-feed the seen tail and silently DROP the new older member. The
    # date-cut detects the disagreement and falls back to a full re-feed.
    from compact_agent.engine import _partial_since
    post = _m(100, "2026-06-01T10:00:00Z", post_id=100)
    c_late = _m(102, "2026-06-03T09:00:00Z", post_id=100)
    ingested = build_atoms([post, c_late])[0]          # ingested as (2, c_late.date)
    c_backfill = _m(101, "2026-06-02T12:00:00Z", post_id=100)  # older, lands MID-list
    grown = build_atoms([post, c_late, c_backfill])[0]
    assert [m.id for m in grown.members] == [100, 101, 102]    # inserted before the leaf
    assert _partial_since([2, ingested.leaf_date], grown.members) == 0  # full re-feed


def test_render_atom_delta_keeps_root_and_new_only():
    post = _m(100, "2026-06-01T10:00:00Z", post_id=100, text="POST")
    c1 = _m(101, "2026-06-01T11:00:00Z", post_id=100, text="OLD-COMMENT")
    c2 = _m(102, "2026-06-02T09:00:00Z", post_id=100, text="NEW-COMMENT")
    msgs = [post, c1, c2]
    idmap = build_identity(msgs)
    atom = build_atoms(msgs, idmap=idmap)[0]
    out = render_atom_content(atom, idmap, since=2)
    assert "POST" in out             # root kept as context
    assert "NEW-COMMENT" in out      # the delta is fed
    assert "OLD-COMMENT" not in out  # already-ingested comment is NOT re-fed


def test_render_video_thumbnail_micro_note():
    post = _m(200, "2026-06-01T10:00:00Z", post_id=200, text="vid",
              media_type="video", photo_desc="a map of the Urals")
    idmap = build_identity([post])
    atom = build_atoms([post], idmap=idmap)[0]
    out = render_atom_content(atom, idmap)
    assert "[video thumbnail: a map of the Urals]" in out


# ---- privacy: media slots are pseudonymized like a body -----------------------
def test_ocr_photo_desc_handles_are_pseudonymized():
    # Vision OCR reads @handles off watermarks; they must never reach the model raw.
    msgs = [_m(1, "2026-01-01T10:00:00Z", media_type="photo",
               photo_desc="watermark @vasya_real, sign 'ВХОД'")]
    idmap = build_identity(msgs)
    out = render_atom_content(build_atoms(msgs, _by_id(msgs), idmap)[0], idmap)
    assert "@vasya_real" not in out
    assert "ВХОД" in out  # content text itself is intentionally untouched


def test_contact_card_name_never_reaches_the_model():
    msgs = [_m(1, "2026-01-01T10:00:00Z", media_type="contact",
               media_meta={"name": "Ivan Petrov", "phone": "+79991234567"})]
    idmap = build_identity(msgs)
    out = render_atom_content(build_atoms(msgs, _by_id(msgs), idmap)[0], idmap)
    assert "Ivan Petrov" not in out
    assert "79991234567" not in out
    assert "[contact: @u" in out  # stable pseudonym, not a silent drop
