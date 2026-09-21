"""The one-number budget engine: split + max-min fair view allocation."""

from __future__ import annotations

import pytest

from compact_agent.runbudget import allocate_views, compute_run_budget


def test_split_matches_share():
    b = compute_run_budget(10000, system_tokens=500, share=1 / 3)
    assert b.summary_budget == int(10000 / 3) - 500   # = 2833
    assert b.workflow_budget == 10000 - int(10000 / 3)


def test_window_too_small_for_system_raises():
    with pytest.raises(ValueError):
        compute_run_budget(1000, system_tokens=2000, share=1 / 3)


def test_allocator_crops_when_all_over_share():
    # Worked example A: people=4k, key_facts=2k, budget 2833 -> each capped ~1416.
    b = compute_run_budget(10000, system_tokens=500, share=1 / 3)
    a = allocate_views(b.summary_budget, {"people": 4000, "key_facts": 2000})
    assert a["people"] == a["key_facts"] == b.summary_budget // 2
    assert sum(a.values()) <= b.summary_budget


def test_allocator_donates_slack_to_heavier_section():
    # Worked example B: key_facts=1k fits, donating slack -> people gets the rest.
    b = compute_run_budget(10000, system_tokens=500, share=1 / 3)
    a = allocate_views(b.summary_budget, {"people": 3000, "key_facts": 1000})
    assert a["key_facts"] == 1000
    assert a["people"] == b.summary_budget - 1000
    assert sum(a.values()) <= b.summary_budget


def test_allocator_everything_fits_uncropped():
    b = compute_run_budget(40000, system_tokens=600, share=1 / 3)
    costs = {f"s{i}": 100 for i in range(10)}
    a = allocate_views(b.summary_budget, costs)
    assert all(a[k] == 100 for k in costs)  # all fit, none cropped


def test_allocator_never_overspends_across_sweep():
    for window in (8000, 16000, 32000, 64000, 128000):
        b = compute_run_budget(window, system_tokens=1000, share=1 / 3)
        for costs in ({"a": 99999}, {"a": 50, "b": 99999}, {"a": 10, "b": 10, "c": 10}):
            a = allocate_views(b.summary_budget, costs)
            assert sum(a.values()) <= b.summary_budget
            assert all(v >= 0 for v in a.values())
