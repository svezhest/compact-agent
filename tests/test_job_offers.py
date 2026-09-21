"""The job_offers section: path grammar, typed/freeform split, LRU view, ingest granularity."""

from __future__ import annotations

import pytest

import job_offers  # example plugin (see conftest)
from compact_agent.sections.base import ViewContext
from compact_agent.sections.store import StoreError, VStore


def sec():
    return job_offers.section()


def store():
    return VStore(sections=[sec()], known_keys=set())


def vctx(day="2026-05-01"):
    return ViewContext(relative_date=day, kind="channel", platform="telegram")


def test_owns_and_resolves_card_and_notes():
    s = sec()
    assert s.owns(["job_offers", "x", "y"])
    assert not s.owns(["people", "x"])
    assert s.resolve(["job_offers", "Alfa-Bank", "12345-Analyst"], set()) == \
        "job_offers/alfa-bank/12345-analyst.json"
    assert s.resolve(["job_offers", "Alfa-Bank", "12345-analyst", "notes"], set()) == \
        "job_offers/alfa-bank/12345-analyst/notes.md"


def test_resolve_rejects_bad_shape():
    with pytest.raises(StoreError):
        sec().resolve(["job_offers", "onlycompany"], set())


def test_model_for_typed_card_only():
    s = sec()
    assert s.model_for("job_offers/a/b.json") is job_offers.JobOfferCard
    assert s.model_for("job_offers/a/b/notes.md") is None
    assert s.is_typed("job_offers/a/b.json")
    assert not s.is_typed("job_offers/a/b/notes.md")


def test_ingest_groups_one_per_atom():
    class A:
        def __init__(self, r):
            self.root_id = r

    groups = sec().ingest_groups([A(1), A(2), A(3)])
    assert [g[0] for g in groups] == ["1", "2", "3"]
    assert all(len(g[1]) == 1 for g in groups)


def test_position_is_grade_cross_specialization():
    st = store()
    st.patch("job_offers/b/2-y", {"id": "2-y", "company": "b", "specialization": "ds_ml",
                                  "grade": "senior", "posted_at": "2026-05-01", "status": "open"})
    view = sec().render_view(st, vctx(), budget_tokens=2000)
    assert "senior ds_ml" in view  # de-bullshitted position = grade × specialization


def test_view_keeps_newest_offer_and_marks_trim():
    st = store()
    st.patch("job_offers/a/1-x", {"id": "1-x", "company": "a", "specialization": "backend",
                                  "posted_at": "2026-01-01", "status": "open"})
    st.patch("job_offers/b/2-y", {"id": "2-y", "company": "b", "specialization": "ds_ml",
                                  "posted_at": "2026-05-01", "status": "open"})
    view = sec().render_view(st, vctx(), budget_tokens=5)  # only the newest survives
    assert "ds_ml" in view
    assert "backend" not in view
    assert "trimmed" in view


def test_view_formats_reward_and_company():
    st = store()
    st.patch("job_offers/a/1-x", {"id": "1-x", "company": "a", "company_raw": "Acme",
                                  "specialization": "backend", "grade": "senior",
                                  "posted_at": "2026-01-01",
                                  "reward": {"net_yearly_usd_mean": 100000, "variance": 1e8}})
    view = sec().render_view(st, vctx("2026-01-01"), budget_tokens=2000)
    assert "Acme" in view
    assert "$100k" in view


def test_invalid_specialization_rejected():
    st = store()
    with pytest.raises(StoreError):
        st.patch("job_offers/a/1-x", {"specialization": "rocket_surgery"})
