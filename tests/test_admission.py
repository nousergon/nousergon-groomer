"""Contract tests for the admission controller (issue #11, §4)."""
from __future__ import annotations

import pytest

from nousergon_groomer.admission import AdmissionController
from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.models import (
    Dependency,
    DependencyKind,
    Item,
    ItemKind,
    ItemState,
)


def _issue(state=ItemState.OPEN_ISSUE_ACTIONABLE, **kw):
    defaults = {"id": "i1", "kind": ItemKind.ISSUE, "state": state}
    defaults.update(kw)
    return Item(**defaults)


def _pr(state=ItemState.OPEN_CLEAN_GREEN, **kw):
    defaults = {"id": "p1", "kind": ItemKind.PR, "state": state}
    defaults.update(kw)
    return Item(**defaults)


def _world():
    return ObservedWorld(s3_objects=set(), terminal_items=set())


# ---------------------------------------------------------------------------
# Ceiling validation
# ---------------------------------------------------------------------------

def test_ceiling_below_one_rejected():
    with pytest.raises(ValueError, match="wip_ceiling must be >= 1"):
        AdmissionController(0)


def test_ceiling_one_ok():
    AdmissionController(1)


# ---------------------------------------------------------------------------
# WIP accounting (§4.2)
# ---------------------------------------------------------------------------

def test_current_wip_counts_open_prs():
    items = [
        _pr(ItemState.OPEN_CLEAN_GREEN, id="p1"),
        _pr(ItemState.OPEN_RED_CI, id="p2"),
        _pr(ItemState.OPEN_DRAFT, id="p3", is_draft=True),
    ]
    assert AdmissionController.current_wip(items) == 3


def test_current_wip_excludes_terminal_prs():
    items = [
        _pr(ItemState.OPEN_CLEAN_GREEN, id="p1"),
        _pr(ItemState.MERGED, id="p2"),
        _pr(ItemState.CLOSED, id="p3"),
    ]
    assert AdmissionController.current_wip(items) == 1


def test_current_wip_excludes_issues():
    items = [
        _pr(ItemState.OPEN_CLEAN_GREEN, id="p1"),
        _issue(id="i1"),
    ]
    assert AdmissionController.current_wip(items) == 1


def test_at_ceiling_true_when_saturated():
    items = [_pr(ItemState.OPEN_CLEAN_GREEN, id=f"p{n}") for n in range(3)]
    ctrl = AdmissionController(3)
    assert ctrl.at_ceiling(items) is True


def test_at_ceiling_false_when_below():
    items = [_pr(ItemState.OPEN_CLEAN_GREEN, id="p1")]
    ctrl = AdmissionController(3)
    assert ctrl.at_ceiling(items) is False


# ---------------------------------------------------------------------------
# Admission gate (§4.3)
# ---------------------------------------------------------------------------

def test_admits_unblocked_issue_below_ceiling():
    issue = _issue()
    ctrl = AdmissionController(5)
    decision = ctrl.can_admit(issue, [issue], _world())
    assert decision.admitted is True
    assert decision.reason == "admitted"


def test_rejects_at_ceiling():
    issue = _issue()
    prs = [_pr(ItemState.OPEN_CLEAN_GREEN, id=f"p{n}") for n in range(3)]
    ctrl = AdmissionController(3)
    decision = ctrl.can_admit(issue, prs, _world())
    assert decision.admitted is False
    assert "WIP" in decision.reason
    assert "ceiling" in decision.reason


def test_rejects_blocked_issue():
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    issue = _issue(declared_dependencies=[dep])
    ctrl = AdmissionController(5)
    decision = ctrl.can_admit(issue, [issue], _world())
    assert decision.admitted is False
    assert "blocked" in decision.reason


def test_rejects_non_issue():
    pr = _pr()
    ctrl = AdmissionController(5)
    decision = ctrl.can_admit(pr, [pr], _world())
    assert decision.admitted is False
    assert "not an issue" in decision.reason


def test_rejects_terminal_issue():
    issue = _issue(ItemState.MERGED)
    ctrl = AdmissionController(5)
    decision = ctrl.can_admit(issue, [issue], _world())
    assert decision.admitted is False
    assert "not actionable" in decision.reason


def test_rejects_waiting_issue():
    issue = _issue(ItemState.OPEN_ISSUE_WAITING)
    ctrl = AdmissionController(5)
    decision = ctrl.can_admit(issue, [issue], _world())
    assert decision.admitted is False


def test_undecidable_dep_does_not_deny():
    """An undecidable dep is not a definite blocker — admission is not denied."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    issue = _issue(declared_dependencies=[dep])
    ctrl = AdmissionController(5)
    # s3_objects is None → undecidable → not blocked → admitted
    world = ObservedWorld(s3_objects=None)
    decision = ctrl.can_admit(issue, [issue], world)
    assert decision.admitted is True
