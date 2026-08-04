"""Contract tests for the dependency evaluator (issue #11, §3)."""
from __future__ import annotations

import datetime as _dt

import pytest

from nousergon_groomer.dependency_evaluator import (
    ObservedWorld,
    evaluate_dependency,
    evaluate_item_dependencies,
    is_item_blocked,
)
from nousergon_groomer.models import Dependency, DependencyKind, Item, ItemStage

_LABEL_ABSENT_VALUE = "label_absent"


def _dep(kind: DependencyKind, target: str) -> Dependency:
    return Dependency(kind=kind, target=target)


def _item(deps=None) -> Item:
    return Item(
        id="i1", stage=ItemStage.PROPOSED,
        declared_dependencies=deps or [],
    )


# ---------------------------------------------------------------------------
# S3_OBJECT
# ---------------------------------------------------------------------------

def test_s3_object_satisfied_when_present():
    dep = _dep(DependencyKind.S3_OBJECT, "s3://b/k")
    world = ObservedWorld(s3_objects={"s3://b/k"})
    ev = evaluate_dependency(dep, world)
    assert ev.satisfied is True
    assert not ev.undecidable


def test_s3_object_unsatisfied_when_absent():
    dep = _dep(DependencyKind.S3_OBJECT, "s3://b/k")
    world = ObservedWorld(s3_objects=set())
    ev = evaluate_dependency(dep, world)
    assert ev.satisfied is False
    assert not ev.undecidable


def test_s3_object_undecidable_when_not_reported():
    dep = _dep(DependencyKind.S3_OBJECT, "s3://b/k")
    world = ObservedWorld(s3_objects=None)
    ev = evaluate_dependency(dep, world)
    assert ev.undecidable is True
    assert "not reported" in ev.reason


# ---------------------------------------------------------------------------
# S3_PREFIX
# ---------------------------------------------------------------------------

def test_s3_prefix_satisfied():
    dep = _dep(DependencyKind.S3_PREFIX, "s3://b/prefix/")
    world = ObservedWorld(s3_prefixes={"s3://b/prefix/"})
    assert evaluate_dependency(dep, world).satisfied is True


def test_s3_prefix_undecidable_when_not_reported():
    dep = _dep(DependencyKind.S3_PREFIX, "s3://b/prefix/")
    world = ObservedWorld(s3_prefixes=None)
    assert evaluate_dependency(dep, world).undecidable is True


# ---------------------------------------------------------------------------
# ISSUE_TERMINAL / PR_TERMINAL
# ---------------------------------------------------------------------------

def test_issue_terminal_satisfied():
    dep = _dep(DependencyKind.ISSUE_TERMINAL, "i2")
    world = ObservedWorld(terminal_items={"i2"})
    assert evaluate_dependency(dep, world).satisfied is True


def test_pr_terminal_unsatisfied():
    dep = _dep(DependencyKind.PR_TERMINAL, "p2")
    world = ObservedWorld(terminal_items=set())
    assert evaluate_dependency(dep, world).satisfied is False


def test_terminal_undecidable_when_not_reported():
    dep = _dep(DependencyKind.ISSUE_TERMINAL, "i2")
    world = ObservedWorld(terminal_items=None)
    assert evaluate_dependency(dep, world).undecidable is True


# ---------------------------------------------------------------------------
# PIPELINE_RUN
# ---------------------------------------------------------------------------

def test_pipeline_run_satisfied():
    dep = _dep(DependencyKind.PIPELINE_RUN, "run-123")
    world = ObservedWorld(pipeline_runs={"run-123"})
    assert evaluate_dependency(dep, world).satisfied is True


def test_pipeline_run_undecidable():
    dep = _dep(DependencyKind.PIPELINE_RUN, "run-123")
    world = ObservedWorld(pipeline_runs=None)
    assert evaluate_dependency(dep, world).undecidable is True


# ---------------------------------------------------------------------------
# DATE
# ---------------------------------------------------------------------------

def test_date_satisfied_when_arrived():
    dep = _dep(DependencyKind.DATE, "2026-07-01")
    world = ObservedWorld(today=_dt.date(2026, 7, 1))
    assert evaluate_dependency(dep, world).satisfied is True


def test_date_unsatisfied_when_future():
    dep = _dep(DependencyKind.DATE, "2026-08-01")
    world = ObservedWorld(today=_dt.date(2026, 7, 1))
    assert evaluate_dependency(dep, world).satisfied is False


def test_date_undecidable_when_today_not_reported():
    dep = _dep(DependencyKind.DATE, "2026-07-01")
    world = ObservedWorld(today=None)
    assert evaluate_dependency(dep, world).undecidable is True


def test_date_rejects_malformed_target():
    dep = _dep(DependencyKind.DATE, "not-a-date")
    world = ObservedWorld(today=_dt.date(2026, 7, 1))
    with pytest.raises(ValueError, match="not an ISO date"):
        evaluate_dependency(dep, world)


# ---------------------------------------------------------------------------
# MILESTONE_REACHED (issue #33)
# ---------------------------------------------------------------------------

def test_milestone_reached_satisfied_when_present():
    dep = _dep(DependencyKind.MILESTONE_REACHED, "m0-contracts:reached")
    world = ObservedWorld(reached_milestones={"m0-contracts:reached"})
    assert evaluate_dependency(dep, world).satisfied is True


def test_milestone_reached_unsatisfied_when_absent():
    dep = _dep(DependencyKind.MILESTONE_REACHED, "m0-contracts:reached")
    world = ObservedWorld(reached_milestones={"some-other-milestone"})
    ev = evaluate_dependency(dep, world)
    assert ev.satisfied is False
    assert ev.undecidable is False


def test_milestone_reached_undecidable_when_not_reported():
    dep = _dep(DependencyKind.MILESTONE_REACHED, "m0-contracts:reached")
    world = ObservedWorld(reached_milestones=None)
    assert evaluate_dependency(dep, world).undecidable is True


# ---------------------------------------------------------------------------
# LABEL_ABSENT — removed (§3.5): a label is inside the loop's own write
# authority, so a label-presence condition can never be a valid external
# dependency. Kept as a negative test rather than deleted silently, so a
# reintroduction of the kind (under this name or a narrower label-based one)
# fails loud at construction rather than at review time.
# ---------------------------------------------------------------------------

def test_label_absent_kind_no_longer_constructible():
    assert not hasattr(DependencyKind, "LABEL_ABSENT")
    with pytest.raises(ValueError):
        DependencyKind(_LABEL_ABSENT_VALUE)
    with pytest.raises(ValueError):
        Dependency(kind=_LABEL_ABSENT_VALUE, target="gate:weekly-sf")


# ---------------------------------------------------------------------------
# Item-level evaluation
# ---------------------------------------------------------------------------

def test_evaluate_item_dependencies_returns_one_per_dep():
    deps = [
        _dep(DependencyKind.S3_OBJECT, "s3://b/k1"),
        _dep(DependencyKind.S3_OBJECT, "s3://b/k2"),
    ]
    item = _item(deps)
    world = ObservedWorld(s3_objects={"s3://b/k1"})
    evaluations = evaluate_item_dependencies(item, world)
    assert len(evaluations) == 2
    assert evaluations[0].satisfied is True
    assert evaluations[1].satisfied is False


def test_is_item_blocked_returns_true_for_unsatisfied():
    dep = _dep(DependencyKind.S3_OBJECT, "s3://b/k")
    item = _item([dep])
    world = ObservedWorld(s3_objects=set())
    blocked, evaluations = is_item_blocked(item, world)
    assert blocked is True


def test_is_item_blocked_returns_false_for_satisfied():
    dep = _dep(DependencyKind.S3_OBJECT, "s3://b/k")
    item = _item([dep])
    world = ObservedWorld(s3_objects={"s3://b/k"})
    blocked, _ = is_item_blocked(item, world)
    assert blocked is False


def test_is_item_blocked_returns_false_for_undecidable():
    """An undecidable dep is NOT a definite blocker — it is surfaced separately."""
    dep = _dep(DependencyKind.S3_OBJECT, "s3://b/k")
    item = _item([dep])
    world = ObservedWorld(s3_objects=None)
    blocked, evaluations = is_item_blocked(item, world)
    assert blocked is False
    assert evaluations[0].undecidable is True


def test_is_item_blocked_false_for_no_deps():
    item = _item()
    blocked, evaluations = is_item_blocked(item, ObservedWorld())
    assert blocked is False
    assert evaluations == []
