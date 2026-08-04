"""Fixture-driven tests — runs the core over every recorded scenario (§8.1).

This is the §8.1 proof: the entire suite runs over JSON fixtures with no
network, no credentials, no model. Each fixture records an item population,
an observed world, an optional config, and the expected dispositions **and
stages**. This test asserts the reconciler produces both for every item in
every fixture.

Asserting the stage is not decoration. Two of the six forward stages are
derived rather than stored, so nothing outside this suite can catch a
derivation that silently changes — and F7 and F6 are computed from stage-entry
timestamps, which means a wrong stage is a wrong number on the surface rather
than a visible failure.
"""
from __future__ import annotations

from nousergon_groomer.models import DispositionKind, ItemStage
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
        if "stage" in expected:
            assert item_result.stage is ItemStage(expected["stage"]), (
                f"item {item_id}: expected effective stage {expected['stage']}, "
                f"got {item_result.stage.value}"
            )


def test_every_fixture_item_declares_its_expected_stage(fixture_scenario, fixture_expected):
    """Every fixture asserts the stage, not only the disposition (§8.1).

    The binding constraint on the stage model is that *every stage transition
    is assertable against recorded fixtures*. A fixture that pins the verdict
    and leaves the stage free would pass while the derivation that produced it
    drifted.
    """
    missing = sorted(k for k, v in fixture_expected.items() if "stage" not in v)
    assert not missing, (
        f"fixture {fixture_scenario.get('description', '?')!r}: items {missing} "
        "declare an expected disposition with no expected stage"
    )


def test_fixture_stage_entry_timestamps_are_recorded(
    fixture_items, fixture_world, fixture_config, fixture_expected
):
    """Deliverable 4: F6 and F7 are computed from the store's stamps alone.

    Every item the reconciler dispositions gets its effective stage stamped in
    the §1.2 store. Without this the two outcome numbers are not merely
    approximate — they are uncomputable, because no other surface records when
    an item entered a *derived* stage.
    """
    store = GenerationStore()
    config = fixture_config.model_copy(update={"observed_at": "2026-08-04T12:00:00+00:00"})
    result = Reconciler(config).reconcile(fixture_items, fixture_world, store)

    for item_result in result.items:
        record = store.get(item_result.item_id)
        assert record is not None, f"no store record for {item_result.item_id}"
        assert record.stage == item_result.stage.value
        assert record.stage_entered_at.get(item_result.stage.value) == (
            "2026-08-04T12:00:00+00:00"
        ), (
            f"item {item_result.item_id} at stage {item_result.stage.value} has no "
            f"entry timestamp: {record.stage_entered_at}"
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
        "gate_derived_dependency",
        "do_not_groom",
        "undecidable",
        # alpha-engine-config#6307 — the stage model's two closes-when cases
        # plus the one-unit-two-records shape the substrate forces.
        "conflicted_change_is_work",
        "merged_is_not_terminal",
        "one_item_two_records",
    }
    missing = required - set(fixture_names)
    assert not missing, f"missing required fixtures: {missing}"
