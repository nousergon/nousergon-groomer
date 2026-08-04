"""Contract tests for the admission controller (issue #11, §4)."""
from __future__ import annotations

import pytest

from nousergon_groomer.admission import AdmissionController
from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.models import (
    Change,
    ChangeCondition,
    Dependency,
    DependencyKind,
    Item,
    ItemStage,
)


def _issue(stage=ItemStage.PROPOSED, **kw):
    defaults = {"id": "i1", "stage": stage}
    defaults.update(kw)
    return Item(**defaults)


def _pr(condition=ChangeCondition.CLEAN, **kw):
    """An item in flight, carrying a change in ``condition``."""
    item_id = kw.pop("id", "p1")
    defaults = {
        "id": item_id,
        "stage": kw.pop("stage", ItemStage.IN_FLIGHT),
        "change": Change(ref=item_id, condition=condition),
    }
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

def test_current_wip_charges_every_in_flight_condition():
    """§4.2: no exemption class. Clean, red and draft each occupy one slot.

    The pre-0.9.0 model needed an enumeration of five PR states to get this
    right, and an enumeration is where an exemption hides. One stage, one
    predicate: a condition cannot fall out of the count by being added.
    """
    items = [
        _pr(ChangeCondition.CLEAN, id="p1"),
        _pr(ChangeCondition.CI_RED, id="p2"),
        _pr(ChangeCondition.DRAFT, id="p3"),
        _pr(ChangeCondition.CONFLICTED, id="p4"),
        _pr(ChangeCondition.CI_PENDING, id="p5"),
    ]
    assert AdmissionController.current_wip(items) == 5


def test_current_wip_excludes_stages_past_in_flight():
    items = [
        _pr(ChangeCondition.CLEAN, id="p1"),
        _pr(ChangeCondition.CLEAN, id="p2", stage=ItemStage.MERGED),
        _pr(ChangeCondition.CLEAN, id="p3", stage=ItemStage.ABANDONED),
        _pr(ChangeCondition.CLEAN, id="p4", stage=ItemStage.DONE),
    ]
    assert AdmissionController.current_wip(items) == 1


def test_current_wip_excludes_pre_change_items():
    items = [
        _pr(ChangeCondition.CLEAN, id="p1"),
        _issue(id="i1"),
    ]
    assert AdmissionController.current_wip(items) == 1


def test_current_wip_charges_one_unit_once():
    """An item whose change is enumerated as a separate record is not
    double-charged: the intent record carries a ref, not the change."""
    items = [
        Item(id="i1", stage=ItemStage.IN_FLIGHT, change_ref="p1"),
        _pr(ChangeCondition.CLEAN, id="p1"),
    ]
    assert AdmissionController.current_wip(items) == 1


def test_at_ceiling_true_when_saturated():
    items = [_pr(ChangeCondition.CLEAN, id=f"p{n}") for n in range(3)]
    ctrl = AdmissionController(3)
    assert ctrl.at_ceiling(items) is True


def test_at_ceiling_false_when_below():
    items = [_pr(ChangeCondition.CLEAN, id="p1")]
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
    prs = [_pr(ChangeCondition.CLEAN, id=f"p{n}") for n in range(3)]
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


def test_rejects_item_already_carrying_a_change():
    pr = _pr()
    ctrl = AdmissionController(5)
    decision = ctrl.can_admit(pr, [pr], _world())
    assert decision.admitted is False
    assert "already carries a change" in decision.reason


def test_rejects_terminal_item():
    issue = _issue(ItemStage.DONE)
    ctrl = AdmissionController(5)
    decision = ctrl.can_admit(issue, [issue], _world())
    assert decision.admitted is False
    assert "pre-change stage" in decision.reason


def test_rejects_item_whose_change_is_enumerated_separately():
    """The intent record of a unit already in flight is not re-admitted —
    that is how one unit would get two changes."""
    issue = _issue(ItemStage.IN_FLIGHT, change_ref="p1")
    ctrl = AdmissionController(5)
    decision = ctrl.can_admit(issue, [issue], _world())
    assert decision.admitted is False
    assert "pre-change stage" in decision.reason


def test_admits_a_recorded_ready_item():
    """`ready` is derived, but a harness may record it; admission accepts both
    pre-change stages and lets gate 3 decide, rather than re-deriving."""
    issue = _issue(ItemStage.READY)
    ctrl = AdmissionController(5)
    assert ctrl.can_admit(issue, [issue], _world()).admitted is True


def test_undecidable_dep_does_not_deny():
    """An undecidable dep is not a definite blocker — admission is not denied."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    issue = _issue(declared_dependencies=[dep])
    ctrl = AdmissionController(5)
    # s3_objects is None → undecidable → not blocked → admitted
    world = ObservedWorld(s3_objects=None)
    decision = ctrl.can_admit(issue, [issue], world)
    assert decision.admitted is True
