"""Contract tests for the transitive dependency graph (issue #11, §3.4)."""
from __future__ import annotations

import pytest

from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.dependency_graph import DependencyCycleError, DependencyGraph
from nousergon_groomer.models import Dependency, DependencyKind, Item, ItemStage


def _dep(kind, target):
    return Dependency(kind=kind, target=target)


def _issue(id, deps=None, stage=ItemStage.PROPOSED):
    return Item(id=id, stage=stage, declared_dependencies=deps or [])


def _world():
    return ObservedWorld(
        s3_objects=set(), s3_prefixes=set(), terminal_items=set(),
        pipeline_runs=set(),
    )


# ---------------------------------------------------------------------------
# Direct blocked
# ---------------------------------------------------------------------------

def test_direct_blocked_on_external_leaf():
    i1 = _issue("i1", [_dep(DependencyKind.S3_OBJECT, "s3://b/k")])
    graph = DependencyGraph([i1], _world())
    chain = graph.get_blocked_chain("i1")
    assert len(chain) == 1
    assert chain[0].dependency.target == "s3://b/k"


def test_not_blocked_when_satisfied():
    i1 = _issue("i1", [_dep(DependencyKind.S3_OBJECT, "s3://b/k")])
    world = ObservedWorld(s3_objects={"s3://b/k"})
    graph = DependencyGraph([i1], world)
    assert graph.get_blocked_chain("i1") == []
    assert graph.is_transitively_blocked("i1") is False


def test_not_blocked_when_no_deps():
    i1 = _issue("i1")
    graph = DependencyGraph([i1], _world())
    assert graph.get_blocked_chain("i1") == []


# ---------------------------------------------------------------------------
# Transitive blocked
# ---------------------------------------------------------------------------

def test_transitive_blocked_two_levels():
    i2 = _issue("i2", [_dep(DependencyKind.S3_OBJECT, "s3://b/k")])
    i1 = _issue("i1", [_dep(DependencyKind.ISSUE_TERMINAL, "i2")])
    graph = DependencyGraph([i1, i2], _world())
    chain = graph.get_blocked_chain("i1")
    assert len(chain) == 2
    assert chain[0].dependency.kind is DependencyKind.ISSUE_TERMINAL
    assert chain[1].dependency.kind is DependencyKind.S3_OBJECT
    assert "s3://b/k" in chain[1].dependency.target


def test_transitive_blocked_three_levels():
    i3 = _issue("i3", [_dep(DependencyKind.S3_OBJECT, "s3://b/k")])
    i2 = _issue("i2", [_dep(DependencyKind.ISSUE_TERMINAL, "i3")])
    i1 = _issue("i1", [_dep(DependencyKind.ISSUE_TERMINAL, "i2")])
    graph = DependencyGraph([i1, i2, i3], _world())
    chain = graph.get_blocked_chain("i1")
    assert len(chain) == 3
    assert graph.is_transitively_blocked("i1") is True


def test_transitive_not_blocked_when_target_terminal():
    """i1 depends on i2 being terminal; i2 is terminal → i1 not blocked."""
    i2 = _issue("i2", stage=ItemStage.DONE)
    i1 = _issue("i1", [_dep(DependencyKind.ISSUE_TERMINAL, "i2")])
    world = ObservedWorld(terminal_items={"i2"})
    graph = DependencyGraph([i1, i2], world)
    assert graph.get_blocked_chain("i1") == []


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

def test_cycle_raises():
    i1 = _issue("i1", [_dep(DependencyKind.ISSUE_TERMINAL, "i2")])
    i2 = _issue("i2", [_dep(DependencyKind.ISSUE_TERMINAL, "i1")])
    graph = DependencyGraph([i1, i2], _world())
    with pytest.raises(DependencyCycleError) as exc_info:
        graph.get_blocked_chain("i1")
    assert "i1" in exc_info.value.cycle_path
    assert "i2" in exc_info.value.cycle_path


def test_self_cycle_raises():
    i1 = _issue("i1", [_dep(DependencyKind.ISSUE_TERMINAL, "i1")])
    graph = DependencyGraph([i1], _world())
    with pytest.raises(DependencyCycleError):
        graph.get_blocked_chain("i1")


# ---------------------------------------------------------------------------
# Missing target
# ---------------------------------------------------------------------------

def test_missing_target_treated_as_external_leaf():
    """A dep pointing at an item not carried in the graph is an external leaf."""
    i1 = _issue("i1", [_dep(DependencyKind.ISSUE_TERMINAL, "i-missing")])
    graph = DependencyGraph([i1], _world())
    chain = graph.get_blocked_chain("i1")
    assert len(chain) == 1
    assert chain[0].dependency.target == "i-missing"


def test_unknown_item_id_raises():
    i1 = _issue("i1")
    graph = DependencyGraph([i1], _world())
    with pytest.raises(KeyError):
        graph.get_blocked_chain("unknown")


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def test_duplicate_item_ids_rejected():
    i1 = _issue("i1")
    i1_dup = _issue("i1")
    with pytest.raises(ValueError, match="duplicate item ids"):
        DependencyGraph([i1, i1_dup], _world())


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------

def test_blocked_items_lists_all_blocked():
    i1 = _issue("i1", [_dep(DependencyKind.S3_OBJECT, "s3://b/k")])
    i2 = _issue("i2")
    i3 = _issue("i3", [_dep(DependencyKind.S3_OBJECT, "s3://b/k2")])
    graph = DependencyGraph([i1, i2, i3], _world())
    blocked = set(graph.blocked_items())
    assert blocked == {"i1", "i3"}


def test_directly_blocked_items():
    i2 = _issue("i2", [_dep(DependencyKind.S3_OBJECT, "s3://b/k")])
    i1 = _issue("i1", [_dep(DependencyKind.ISSUE_TERMINAL, "i2")])
    graph = DependencyGraph([i1, i2], _world())
    # Both are directly blocked: i1's issue_terminal dep is unsatisfied
    # (i2 is not in terminal_items), and i2's s3_object dep is unsatisfied.
    # "directly blocked" = blocked by one of your own declared deps, whether
    # external leaf or item-pointing.
    direct = set(graph.directly_blocked_items())
    assert "i1" in direct
    assert "i2" in direct
