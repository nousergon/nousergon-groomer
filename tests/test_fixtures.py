"""Fixture-driven tests — runs the core over every recorded scenario (§8.1).

This is the §8.1 proof: the entire suite runs over JSON fixtures with no
network, no credentials, no model. Each fixture records an item population,
an observed world, an optional config, and the expected dispositions. This
test asserts the reconciler produces the expected disposition for every item
in every fixture.
"""
from __future__ import annotations

from nousergon_groomer.models import DispositionKind
from nousergon_groomer.observed_gen import GenerationStore
from nousergon_groomer.reconciler import Reconciler


def test_fixture_dispositions_match_expected(
    fixture_scenario, fixture_items, fixture_world, fixture_config, fixture_expected
):
    """Every fixture's expected dispositions match the reconciler's output."""
    store = GenerationStore()
    reconciler = Reconciler(fixture_config)
    result = reconciler.reconcile(fixture_items, fixture_world, store)

    for item_result in result.items:
        item_id = item_result.item_id
        assert item_id in fixture_expected, (
            f"item {item_id} has no expected disposition in fixture "
            f"{fixture_scenario.get('description', '?')}"
        )
        expected = fixture_expected[item_id]
        actual = item_result.disposition
        assert actual.kind is DispositionKind(expected["kind"]), (
            f"item {item_id}: expected {expected['kind']}, got {actual.kind.value} "
            f"(reason: {actual.reason})"
        )
        if "action" in expected:
            assert actual.action == expected["action"], (
                f"item {item_id}: expected action {expected['action']}, "
                f"got {actual.action}"
            )


def test_all_fixtures_have_at_least_one_item(fixture_scenario, fixture_items):
    """Every fixture must carry at least one item (no empty scenarios)."""
    assert len(fixture_items) >= 1, (
        f"fixture {fixture_scenario.get('description', '?')} has no items"
    )


def test_all_fixtures_have_a_world(fixture_scenario, fixture_world):
    """Every fixture must declare an observed world (even if all-None)."""
    assert fixture_world is not None


def test_fixtures_cover_core_scenarios(fixture_names):
    """The fixture suite must cover the documented scenarios (issue #10)."""
    required = {
        "clean_green_lane_pr",
        "red_ci_pr",
        "blocked_issue",
        "transitive_blocked",
        "at_wip_ceiling",
        "gate_labeled_pr",
        "do_not_groom",
        "undecidable",
    }
    missing = required - set(fixture_names)
    assert not missing, f"missing required fixtures: {missing}"
