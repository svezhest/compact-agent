"""Tests for deterministic pseudonymous identity and the dedup invariants (B/C/L)."""

from __future__ import annotations

from compact_agent.identity import build_identity
from compact_agent.render import render_day
from compact_agent.storage import Message


def msg(mid, sid, name, text="hi", date="2026-01-01T10:00:00Z"):
    return Message(id=mid, date=date, sender_id=sid, sender_name=name, text=text)


def test_one_key_per_sender_id_even_with_name_variants():
    # Same sender_id under two display names -> ONE identity (no over-split).
    msgs = [msg(1, 555, "Alex"), msg(2, 555, "Alex Petrov")]
    idmap = build_identity(msgs)
    assert len(idmap.keys()) == 1
    only = next(iter(idmap.by_id.values()))
    assert set(only.names) == {"Alex", "Alex Petrov"}


def test_distinct_same_named_senders_stay_separate():
    # Two different sender_ids with the SAME name -> TWO identities (no over-merge).
    msgs = [msg(1, 1, "Alex"), msg(2, 2, "Alex")]
    idmap = build_identity(msgs)
    assert len(idmap.keys()) == 2


def test_hash_is_deterministic_across_builds():
    a = build_identity([msg(1, 555, "Alex")]).label_for(555, "Alex")
    b = build_identity([msg(9, 555, "Whoever")]).label_for(555, "Whoever")
    assert a == b  # keyed on id, independent of name


def test_numeric_id_is_scrubbed_but_name_flows_through():
    # ONLY the literal numeric sender_id is scrubbed; the display name in CONTENT is by
    # design left intact (short names would otherwise shred ordinary words).
    idmap = build_identity([msg(1, 555, "Alex Petrov", "ask Alex Petrov or 555")])
    rendered = render_day("2026-01-01", [msg(1, 555, "Alex Petrov", "ask Alex Petrov or 555")], idmap)
    assert "555" not in rendered                       # the raw id is pseudonymized
    assert "Alex Petrov" in rendered                   # the name in content flows through
    assert idmap.label_for(555, "Alex Petrov") in rendered  # speaker label is still @hash


def test_short_name_does_not_corrupt_words():
    # A two-char display name must NOT match inside ordinary words (the bug that shredded
    # 'harsh'/'summers' into @hash confetti on real data).
    idmap = build_identity([msg(1, 7, "ar", "harsh winters and warm summers")])
    out = idmap.scrub("harsh winters and warm summers")
    assert out == "harsh winters and warm summers"     # untouched — no substring scrub


def _msg_mentions(mid, sid, name, text, mentions):
    return Message(id=mid, date="2026-01-01T10:00:00Z", sender_id=sid,
                   sender_name=name, text=text, mentions=mentions)


def test_real_mention_folds_to_uniform_person_key():
    """A @handle resolved at fetch time to a real user (user_id) becomes a roster member
    with a uniform @u key — folded with that user's speaker identity, no @m class."""
    prof = {"handle": "prof", "user_id": 777, "name": "Prof X", "real": True}
    msgs = [
        _msg_mentions(1, 11, "A", "@prof will you grade this?", [prof]),
        msg(2, 777, "Prof X", "answered in zoom"),  # the prof later speaks
    ]
    idmap = build_identity(msgs)
    # the tagged prof is in the routable roster
    prof_key = idmap.label_for(777, "Prof X")
    assert prof_key in idmap.keys()
    assert prof_key.startswith("@u")
    # a mention of @prof pseudonymizes to the SAME key as the speaker (one person)
    assert idmap.pseudonymize_mentions("ping @prof") == f"ping {prof_key}"


def test_unresolved_mention_stays_off_roster_uniform_prefix():
    """A handle that did NOT resolve to a real user is still pseudonymized (privacy) with
    the uniform @u prefix, but is NOT a routable roster key."""
    ghost = {"handle": "ghosthandle", "user_id": None, "name": None, "real": False}
    msgs = [_msg_mentions(1, 11, "A", "@ghosthandle ?", [ghost])]
    idmap = build_identity(msgs)
    tok = idmap.pseudonymize_mentions("@ghosthandle")
    assert tok.startswith("@u") and tok not in idmap.keys()


def test_tagged_person_gets_its_own_people_group():
    from compact_agent.atom import build_atoms
    from compact_agent.sections import people
    prof = {"handle": "prof", "user_id": 777, "name": "Prof X", "real": True}
    msgs = [_msg_mentions(1, 11, "A", "@prof question", [prof])]
    idmap = build_identity(msgs)
    atoms = build_atoms(msgs, idmap=idmap)
    groups = dict(people.section().ingest_groups(atoms))
    # one group for the author, one for the tagged-but-silent prof
    assert len(groups) == 2
    assert idmap.label_for(777, "Prof X") in groups


def test_mention_expansion():
    msgs = [
        msg(1, 1, "A", "@all standup, @me leads"),
        msg(2, 2, "B", "ok"),
        msg(3, 3, "C", "[photo]"),
    ]
    idmap = build_identity(msgs)
    out = render_day("2026-01-01", msgs, idmap)
    ka = idmap.label_for(1, "A")
    # @me -> speaker's own hash; @all -> every participant incl. the silent one
    assert f"{ka} leads" in out
    for sid in (1, 2, 3):
        assert idmap.label_for(sid, None) in out


def test_humanize_maps_hash_to_handle():
    idmap = build_identity([msg(1, 555, "Alex Petrov")])
    key = idmap.label_for(555, "Alex Petrov")
    assert idmap.humanize(f"### {key}\nbody") == "### @alexpetrov\nbody"


def test_ungrounded_sender_flagged():
    idmap = build_identity([Message(id=1, date="2026-01-01T10:00:00Z",
                                    sender_id=None, sender_name="Ghost", text="boo")])
    assert idmap.ungrounded == 1


def test_bare_username_in_body_flows_through():
    # A @username written WITHOUT the @ is no longer scrubbed from body text — names in
    # content are by design allowed through (only @handle mentions are pseudonymized).
    msgs = [Message(id=1, date="2026-01-01T10:00:00Z", sender_id=7,
                    sender_name="Вася", sender_username="vasya_real",
                    text="пишите vasya_real в личку")]
    idmap = build_identity(msgs)
    out = idmap.scrub(msgs[0].text)
    assert out == "пишите vasya_real в личку"          # untouched
