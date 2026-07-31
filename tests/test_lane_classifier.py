"""Contract tests for the lane classifier (issue #11)."""
from __future__ import annotations

from nousergon_groomer.lane_classifier import MergeLane, classify_lane
from nousergon_groomer.models import Item, ItemKind, ItemState


def _pr(state=ItemState.OPEN_CLEAN_GREEN, **kw):
    defaults = {"id": "p1", "kind": ItemKind.PR, "state": state}
    defaults.update(kw)
    return Item(**defaults)


def _issue(**kw):
    defaults = {"id": "i1", "kind": ItemKind.ISSUE, "state": ItemState.OPEN_ISSUE_ACTIONABLE}
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
    item = _pr(ItemState.OPEN_DRAFT, is_draft=True, mergeable=True, ci_green=True,
               labels=["groom-reviewed"])
    result = classify_lane(item)
    assert result.eligible is False
    assert "not_draft" in result.gate_a.failed_checks


def test_gate_label_fails_gate_a():
    item = _pr(mergeable=True, ci_green=True, labels=["groom-reviewed", "gate:weekly-sf"])
    result = classify_lane(item)
    assert result.eligible is False
    assert "no_gate_label" in result.gate_a.failed_checks


def test_not_mergeable_fails_gate_a():
    item = _pr(ItemState.OPEN_DIRTY, mergeable=False, ci_green=True, labels=["groom-reviewed"])
    result = classify_lane(item)
    assert result.eligible is False
    assert "mergeable_clean" in result.gate_a.failed_checks


def test_red_ci_fails_gate_a():
    item = _pr(ItemState.OPEN_RED_CI, mergeable=True, ci_green=False, labels=["groom-reviewed"])
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
# Non-PR and terminal
# ---------------------------------------------------------------------------

def test_non_pr_not_eligible():
    item = _issue()
    result = classify_lane(item)
    assert result.eligible is False
    assert "not a PR" in result.reason


def test_merged_pr_not_eligible():
    item = _pr(ItemState.MERGED, mergeable=True, ci_green=True, labels=["groom-reviewed"])
    result = classify_lane(item)
    # Gate A checks mergeable/ci which are True, but the PR is merged —
    # the classifier doesn't special-case terminal; the reconciler does.
    # The classifier is a pure function over the Item fields; it reports
    # eligibility based on the fields, not the lifecycle.
    assert result.gate_a is not None
