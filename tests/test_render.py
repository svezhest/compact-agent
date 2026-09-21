"""Tests for rendering — notably channel posts as the compaction atom."""

from __future__ import annotations

from compact_agent.identity import build_identity
from compact_agent.render import group_by_day, render_day
from compact_agent.storage import Message


def _msgs():
    # A channel: post 10 (Mar 12) with a same-day comment and one written two days
    # later; a second post 20 on Mar 14.
    return [
        Message(id=10, date="2026-03-12T09:00:00", sender_id=1, sender_name="Ch",
                text="New release", post_id=10),
        Message(id=11, date="2026-03-12T10:00:00", sender_id=2, sender_name="Ann",
                text="nice", post_id=10, reply_to=10),
        Message(id=12, date="2026-03-14T08:00:00", sender_id=3, sender_name="Bob",
                text="late", post_id=10, reply_to=10),
        Message(id=20, date="2026-03-14T12:00:00", sender_id=1, sender_name="Ch",
                text="Second", post_id=20),
    ]


def test_comments_fold_under_their_post_day():
    # The late comment (Mar 14) belongs to the Mar 12 post, so it groups under Mar 12 —
    # the post, not the calendar day, is the atom.
    g = dict(group_by_day(_msgs()))
    assert set(g) == {"2026-03-12", "2026-03-14"}
    assert {m.id for m in g["2026-03-12"]} == {10, 11, 12}
    assert {m.id for m in g["2026-03-14"]} == {20}


def test_render_marks_posts_and_indents_comments():
    msgs = _msgs()
    idmap = build_identity(msgs)
    out = render_day("2026-03-12", dict(group_by_day(msgs))["2026-03-12"], idmap)
    assert "▸ POST — " in out
    assert out.count("    ↳ ") == 2  # both comments indented under the post


def test_ordinary_chat_is_unchanged():
    # No post_id -> flat day rendering, no POST markers.
    msgs = [
        Message(id=1, date="2026-03-12T09:00:00", sender_id=1, sender_name="A", text="hi"),
        Message(id=2, date="2026-03-12T09:01:00", sender_id=2, sender_name="B", text="yo"),
    ]
    idmap = build_identity(msgs)
    out = render_day("2026-03-12", dict(group_by_day(msgs))["2026-03-12"], idmap)
    assert "▸ POST" not in out and "↳" not in out
