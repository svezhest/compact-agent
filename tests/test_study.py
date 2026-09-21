"""The study section's season-named term bucketing.

Terms are named by SEASON (`fall`/`spring`), never by an ordinal semester number, so a
key is unambiguous regardless of programme length. January folds into the previous year's
fall term; keys sort chronologically."""

from __future__ import annotations

import pytest

from compact_agent.sections.store import StoreError, VStore
from study import _order_of, _period_for, section  # example plugin (see conftest)


@pytest.mark.parametrize("date,period", [
    ("2024-08-31", "2024-fall"),    # August -> that year's fall
    ("2024-12-15", "2024-fall"),
    ("2025-01-15", "2024-fall"),    # January -> PREVIOUS year's fall
    ("2025-02-01", "2025-spring"),  # February -> that year's spring
    ("2025-07-31", "2025-spring"),
    ("2025-09-01", "2025-fall"),
])
def test_period_for_seasons(date, period):
    assert _period_for(date) == period


def test_keys_sort_chronologically():
    keys = [
        "study/algorithms/2025-fall.md",
        "study/algorithms/2024-fall.md",
        "study/algorithms/2025-spring.md",
    ]
    assert sorted(keys, key=_order_of) == [
        "study/algorithms/2024-fall.md",   # Aug24–Jan25
        "study/algorithms/2025-spring.md",  # Feb–Jul25
        "study/algorithms/2025-fall.md",    # Sep25–Jan26
    ]


def test_resolve_accepts_season_rejects_ordinal():
    store = VStore(sections=[section()], known_keys=set())
    assert store.resolve("study/cpp/2024-fall") == "study/cpp/2024-fall.md"
    assert store.resolve("study/cpp/2025-spring.md") == "study/cpp/2025-spring.md"
    with pytest.raises(StoreError):
        store.resolve("study/cpp/2024-s1")        # ordinal no longer valid
    with pytest.raises(StoreError):
        store.resolve("study/cpp/2024-summer")    # unknown season
