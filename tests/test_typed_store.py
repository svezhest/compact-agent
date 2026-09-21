"""Typed-record support in VStore: patch validation, typed/freeform guards, query."""

from __future__ import annotations

import pytest

import companies, contacts, job_offers, market  # example plugins (see conftest)
from compact_agent.sections.store import StoreError, VStore


def store() -> VStore:
    secs = [job_offers.section(), companies.section(), contacts.section(), market.section()]
    return VStore(sections=secs, known_keys=set())


CARD = dict(id="1-analyst", company="alfa-bank", specialization="data_analytics", grade="junior",
            posted_at="2026-02-20", reward={"net_yearly_usd_mean": 36000, "variance": 4e7},
            skills=["sql", "python"], status="open")


def test_patch_creates_and_parses_typed_card():
    st = store()
    k = st.patch("job_offers/alfa-bank/1-analyst", CARD)
    assert k == "job_offers/alfa-bank/1-analyst.json"
    assert st.is_typed(k)
    card = st.parse_typed(k)
    assert card["specialization"] == "data_analytics"
    assert card["grade"] == "junior"
    assert card["reward"]["net_yearly_usd_mean"] == 36000


def test_patch_rejects_bad_grade():
    st = store()
    st.patch("job_offers/alfa-bank/1-analyst", CARD)
    with pytest.raises(StoreError):
        st.patch("job_offers/alfa-bank/1-analyst", {"grade": "grandmaster"})


def test_patch_rejects_bad_specialization():
    st = store()
    with pytest.raises(StoreError):
        st.patch("job_offers/alfa-bank/1-analyst", {"specialization": "rocket_surgery"})


def test_patch_rejects_bad_nested_contact_kind():
    st = store()
    with pytest.raises(StoreError):
        st.patch("job_offers/alfa-bank/1-analyst",
                 {**CARD, "contacts": [{"kind": "telegram", "value": "x"}]})


def test_patch_is_non_destructive_merge():
    st = store()
    k = st.patch("job_offers/alfa-bank/1-analyst", CARD)
    st.patch("job_offers/alfa-bank/1-analyst", {"tier": "standout"})
    card = st.parse_typed(k)
    assert card["tier"] == "standout"
    assert card["specialization"] == "data_analytics"  # untouched fields preserved


def test_patch_is_idempotent():
    st = store()
    k = st.patch("job_offers/alfa-bank/1-analyst", CARD)
    first = st.files[k]
    st.patch("job_offers/alfa-bank/1-analyst", CARD)
    assert st.files[k] == first  # byte-identical canonical JSON


def test_write_append_edit_rejected_on_typed_path():
    st = store()
    k = st.patch("job_offers/alfa-bank/1-analyst", CARD)
    with pytest.raises(StoreError):
        st.write(k, "x", overwrite=True)
    with pytest.raises(StoreError):
        st.append(k, "x")
    with pytest.raises(StoreError):
        st.edit(k, "a", "b")


def test_patch_rejected_on_freeform_path():
    st = store()
    st.write("job_offers/alfa-bank/1-analyst/notes", "prose")
    with pytest.raises(StoreError):
        st.patch("job_offers/alfa-bank/1-analyst/notes", {"x": 1})


def test_notes_facet_is_freeform():
    st = store()
    k = st.write("job_offers/alfa-bank/1-analyst/notes", "Builds dashboards.")
    assert k.endswith("notes.md")
    assert not st.is_typed(k)


def test_query_filters_sorts_limits():
    st = store()
    st.patch("job_offers/alfa-bank/1-analyst", CARD)
    st.patch("job_offers/recraft/2-ml",
             {"id": "2-ml", "company": "recraft", "specialization": "ds_ml", "grade": "middle",
              "posted_at": "2026-03-01", "reward": {"net_yearly_usd_mean": 150000},
              "skills": ["python", "pytorch"], "status": "open"})

    newest = st.query("job_offers/", sort="-posted_at")
    assert [r["specialization"] for r in newest] == ["ds_ml", "data_analytics"]
    assert all("_key" in r for r in newest)

    rich = st.query("job_offers/", where={"reward.net_yearly_usd_mean": ">=100000"})
    assert [r["specialization"] for r in rich] == ["ds_ml"]

    py = st.query("job_offers/", where={"skills": ["python"]})
    assert len(py) == 2

    one = st.query("job_offers/", sort="-posted_at", limit=1)
    assert len(one) == 1 and one[0]["specialization"] == "ds_ml"

    substr = st.query("job_offers/", where={"specialization": "analyt"})
    assert [r["specialization"] for r in substr] == ["data_analytics"]


def test_query_ignores_freeform_files():
    st = store()
    st.patch("job_offers/alfa-bank/1-analyst", CARD)
    st.write("job_offers/alfa-bank/1-analyst/notes", "prose here")
    assert len(st.query("job_offers/")) == 1  # the notes.md is not a typed record
