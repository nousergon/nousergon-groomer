"""Contract tests for the lane classifier (issue #11)."""
from __future__ import annotations

from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.lane_classifier import MergeLane, classify_lane
from nousergon_groomer.models import (
    Change,
    ChangeCondition,
    Dependency,
    DependencyKind,
    Item,
    ItemStage,
)


def _world(**kw) -> ObservedWorld:
    """A fully-reported world, overridable per surface."""
    defaults = {
        "s3_objects": set(), "s3_prefixes": set(), "terminal_items": set(),
        "pipeline_runs": set(),
    }
    defaults.update(kw)
    return ObservedWorld(**defaults)


def _pr(condition=ChangeCondition.CLEAN, **kw):
    """An item in flight, carrying a change in ``condition``.

    The cleanliness facts live on the change now (§3): ``mergeable``,
    ``ci_green`` and ``security_threads`` describe the pull request rendering
    the stage, not the unit of intended change itself.
    """
    item_id = kw.pop("id", "p1")
    change = Change(
        ref=item_id,
        condition=condition,
        mergeable=kw.pop("mergeable", None),
        ci_green=kw.pop("ci_green", None),
        security_threads=kw.pop("security_threads", 0),
    )
    defaults = {
        "id": item_id,
        "stage": kw.pop("stage", ItemStage.IN_FLIGHT),
        "change": change,
    }
    defaults.update(kw)
    return Item(**defaults)


def _issue(**kw):
    defaults = {"id": "i1", "stage": ItemStage.PROPOSED}
    defaults.update(kw)
    return Item(**defaults)


# ---------------------------------------------------------------------------
# Gate A — clean green lane PR passes
# ---------------------------------------------------------------------------

def test_clean_green_lane_pr_passes():
    item = _pr(
        mergeable=True, ci_green=True, labels=["groom-reviewed"],
    )
    result = classify_lane(item)
    assert result.eligible is True
    assert result.lane is MergeLane.GROOM_REVIEWED


def test_scanner_remediation_lane_passes():
    item = _pr(
        mergeable=True, ci_green=True, labels=["scanner-remediation"],
    )
    result = classify_lane(item)
    assert result.eligible is True
    assert result.lane is MergeLane.SCANNER_REMEDIATION


def test_standing_exception_lane_passes():
    item = _pr(
        mergeable=True, ci_green=True, labels=["standing-exception"],
    )
    result = classify_lane(item)
    assert result.eligible is True
    assert result.lane is MergeLane.STANDING_EXCEPTION


# ---------------------------------------------------------------------------
# Gate A failures
# ---------------------------------------------------------------------------

def test_draft_fails_gate_a():
    item = _pr(ChangeCondition.DRAFT, mergeable=True, ci_green=True,
               labels=["groom-reviewed"])
    result = classify_lane(item)
    assert result.eligible is False
    assert "not_draft" in result.gate_a.failed_checks


def test_unbacked_gate_label_fails_gate_a():
    """A gate:* projection with nothing declared behind it is not confirmable."""
    item = _pr(mergeable=True, ci_green=True, labels=["groom-reviewed", "gate:weekly-sf"])
    result = classify_lane(item, _world())
    assert result.eligible is False
    assert "no_open_dependency" in result.gate_a.failed_checks
    assert any("gate:weekly-sf" in r for r in result.gate_a.open_dependency_reasons)


def test_unsatisfied_declared_dependency_fails_gate_a():
    """Gate A re-derives rather than assuming the caller checked the chain.

    groom-sweep-policy §3.3 names a single upstream check owning the gate
    exclusion as the 2026-07-22 shape; this is the second, independent derivation.
    """
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/weekly.json")
    item = _pr(mergeable=True, ci_green=True, labels=["groom-reviewed"],
               declared_dependencies=[dep])
    result = classify_lane(item, _world())
    assert result.eligible is False
    assert "no_open_dependency" in result.gate_a.failed_checks
    assert any("s3://b/weekly.json" in r for r in result.gate_a.open_dependency_reasons)


def test_satisfied_declared_dependency_passes_gate_a():
    """A cleared gate no longer blocks the lane — even with the label attached."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/weekly.json")
    item = _pr(mergeable=True, ci_green=True,
               labels=["groom-reviewed", "gate:weekly-sf"],
               declared_dependencies=[dep])
    result = classify_lane(item, _world(s3_objects={"s3://b/weekly.json"}))
    assert result.eligible is True
    assert result.lane is MergeLane.GROOM_REVIEWED


def test_undecidable_declared_dependency_fails_gate_a():
    """Unobserved is not satisfied — never auto-merge what we cannot confirm."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/weekly.json")
    item = _pr(mergeable=True, ci_green=True, labels=["groom-reviewed"],
               declared_dependencies=[dep])
    result = classify_lane(item, ObservedWorld())  # nothing reported
    assert result.eligible is False
    assert "no_open_dependency" in result.gate_a.failed_checks


def test_declared_dependency_without_a_world_fails_gate_a():
    """Omitting the world is legal and conservative, never permissive."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/weekly.json")
    item = _pr(mergeable=True, ci_green=True, labels=["groom-reviewed"],
               declared_dependencies=[dep])
    result = classify_lane(item)
    assert result.eligible is False
    assert "no_open_dependency" in result.gate_a.failed_checks


def test_not_mergeable_fails_gate_a():
    item = _pr(ChangeCondition.CONFLICTED, mergeable=False, ci_green=True,
               labels=["groom-reviewed"])
    result = classify_lane(item)
    assert result.eligible is False
    assert "mergeable_clean" in result.gate_a.failed_checks


def test_red_ci_fails_gate_a():
    item = _pr(ChangeCondition.CI_RED, mergeable=True, ci_green=False,
               labels=["groom-reviewed"])
    result = classify_lane(item)
    assert result.eligible is False
    assert "ci_green" in result.gate_a.failed_checks


def test_security_threads_fails_gate_a():
    item = _pr(mergeable=True, ci_green=True, labels=["groom-reviewed"], security_threads=2)
    result = classify_lane(item)
    assert result.eligible is False
    assert "no_security_threads" in result.gate_a.failed_checks


def test_undecidable_mergeable_fails_gate_a():
    """mergeable=None is NOT a pass — auto-merge requires positive confirmation."""
    item = _pr(mergeable=None, ci_green=True, labels=["groom-reviewed"])
    result = classify_lane(item)
    assert result.eligible is False
    assert "mergeable_clean" in result.gate_a.failed_checks


def test_undecidable_ci_fails_gate_a():
    item = _pr(mergeable=True, ci_green=None, labels=["groom-reviewed"])
    result = classify_lane(item)
    assert result.eligible is False
    assert "ci_green" in result.gate_a.failed_checks


# ---------------------------------------------------------------------------
# Gate B — lane match
# ---------------------------------------------------------------------------

def test_no_lane_label_fails_gate_b():
    item = _pr(mergeable=True, ci_green=True, labels=[])
    result = classify_lane(item)
    assert result.eligible is False
    assert "no auto-merge lane" in result.reason


def test_multiple_lanes_fails_gate_b():
    item = _pr(mergeable=True, ci_green=True, labels=["groom-reviewed", "scanner-remediation"])
    result = classify_lane(item)
    assert result.eligible is False
    assert "ambiguous" in result.reason


# ---------------------------------------------------------------------------
# Items with no change, and stages past in_flight
# ---------------------------------------------------------------------------

def test_item_with_no_change_is_not_eligible():
    item = _issue()
    result = classify_lane(item)
    assert result.eligible is False
    assert "carries no change" in result.reason
    assert result.gate_a.failed_checks == ["no_change"]


def test_intent_record_of_an_in_flight_unit_is_not_eligible():
    """The record holding the change is the one classified — not its twin.

    Both records are the same item at the same stage; only one of them can
    answer "is this mergeable?", and it is the one holding the pull request.
    """
    item = Item(id="i1", stage=ItemStage.IN_FLIGHT, change_ref="p1",
                labels=["groom-reviewed"])
    result = classify_lane(item)
    assert result.eligible is False
    assert result.gate_a.failed_checks == ["no_change"]


def test_merged_item_is_classified_on_its_fields_not_its_stage():
    item = _pr(mergeable=True, ci_green=True, labels=["groom-reviewed"],
               stage=ItemStage.MERGED)
    result = classify_lane(item)
    # Gate A checks mergeable/ci which are True, but the item has merged —
    # the classifier doesn't special-case the stage; the reconciler does.
    # The classifier is a pure function over the item's fields; it reports
    # eligibility from them, not from the lifecycle.
    assert result.gate_a is not None
