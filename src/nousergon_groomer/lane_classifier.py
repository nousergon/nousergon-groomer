"""Lane classifier — auto-merge Gate A and Gate B lane assignment.

An item is eligible for auto-merge only if it carries a change, passes **Gate
A** (five cleanliness checks) AND matches **exactly one** auto-merge lane (Gate
B). This module is a pure function over an :class:`Item`; it makes no network
calls and consults no model. The lane *labels* are the framework-level
convention documented in the auto-merge policy; the specific lane membership of
a given item is adapter data carried on ``item.labels``.

The eligibility question is asked of the **item**, scoped to its ``in_flight``
stage (§3's Forbids: no rule is defined over pull requests alone). The
cleanliness facts come from the change rendering that stage — an item with no
change has nothing to merge, which is a different answer from "unclean".

Gate A checks (all must pass; an undecidable observation fails the check —
we never auto-merge what we cannot confirm clean):

1. ``not_draft``      — the change is not a draft.
2. ``no_open_dependency`` — every dependency the PR **declares** is observed
   satisfied, and it carries no ``gate:*`` projection that nothing declares.
3. ``mergeable_clean`` — GitHub reports ``mergeable == True``.
4. ``ci_green``       — CI is reported green.
5. ``no_security_threads`` — the PR has zero open security review threads.

Gate B lane match: exactly one of the lane labels must be present. Zero or
multiple lane labels means the PR is not eligible (no lane to run).

Check 2 replaces the ``no_gate_label`` label test retired by
``alpha-engine-config#6137`` (parent ``nous-ergon-ops#356``). It is derived,
so a **cleared** gate stops blocking auto-merge the moment the world says so
rather than when someone edits a label, and an **uncleared** one blocks on the
condition it actually names. It is also deliberately redundant with
:func:`disposition.compute_disposition`'s chain check: groom-sweep-policy §3.3
names a single upstream check owning the gate exclusion as the shape that
produced the 2026-07-22 incident, so this module re-derives rather than
assuming the caller already did.

``world`` is optional only so a caller can ask the label-and-field questions
without an observation. Without it, an item that declares dependencies fails
check 2 — unconfirmed is not clean.
"""
from __future__ import annotations

import enum
from typing import Optional

from pydantic import BaseModel

from .dependency_evaluator import ObservedWorld, evaluate_item_dependencies
from .models import Item

# ---------------------------------------------------------------------------
# Lane enumeration
# ---------------------------------------------------------------------------


class MergeLane(str, enum.Enum):
    """The auto-merge lanes (Gate B). Exactly one must match for eligibility.

    The label names below are the framework-level lane conventions; the
    private harness may add or retire lanes by changing which labels it
    attaches to PRs, not by editing this core.
    """

    SCANNER_REMEDIATION = "scanner-remediation"
    STANDING_EXCEPTION = "standing-exception"
    GROOM_REVIEWED = "groom-reviewed"


# Label that marks a PR as belonging to each lane. Kept as a mapping so the
# match is data-driven and easy to audit.
_LANE_LABELS = {
    MergeLane.SCANNER_REMEDIATION: "scanner-remediation",
    MergeLane.STANDING_EXCEPTION: "standing-exception",
    MergeLane.GROOM_REVIEWED: "groom-reviewed",
}


# ---------------------------------------------------------------------------
# Gate A
# ---------------------------------------------------------------------------


class GateAResult(BaseModel):
    """The outcome of the five Gate A cleanliness checks.

    ``passed`` is True iff every check passed. ``failed_checks`` names the
    checks that failed (in declaration order) so the caller can report *why*
    a PR is not auto-mergeable rather than just that it isn't.
    """

    not_draft: bool
    no_open_dependency: bool
    mergeable_clean: bool
    ci_green: bool
    no_security_threads: bool
    passed: bool
    failed_checks: list[str]
    #: Why ``no_open_dependency`` failed, when it did — the unsatisfied or
    #: undecidable declarations, or the unbacked ``gate:*`` projection. Empty
    #: when the check passed. Kept so a blocked auto-merge names its condition
    #: rather than only the check that rejected it.
    open_dependency_reasons: list[str] = []


def _evaluate_open_dependencies(
    item: Item, world: Optional[ObservedWorld]
) -> tuple[bool, list[str]]:
    """Derive Gate A check 2: is this PR under any unsatisfied condition?

    Returns ``(passed, reasons)``. Fails when the item carries a ``gate:*``
    projection nothing declares (§3.3 — unbacked, undecidable in both
    directions), or when any declared dependency is unsatisfied, undecidable,
    or simply cannot be evaluated because no ``world`` was supplied.
    """
    unrepresented = item.unrepresented_gate_labels
    if unrepresented:
        return False, [
            f"gate label {label} projects a status no declared dependency backs"
            for label in unrepresented
        ]

    if not item.declared_dependencies:
        return True, []

    if world is None:
        return False, [
            f"{len(item.declared_dependencies)} declared dependency/ies and no "
            "observed world to evaluate them against (unconfirmed is not clean)"
        ]

    reasons: list[str] = []
    for ev in evaluate_item_dependencies(item, world):
        if ev.satisfied and not ev.undecidable:
            continue
        token = f"{ev.dependency.kind.value}:{ev.dependency.target}"
        state = "undecidable" if ev.undecidable else "unsatisfied"
        reasons.append(f"{token} {state}" + (f" ({ev.reason})" if ev.reason else ""))
    return (not reasons), reasons


def _evaluate_gate_a(item: Item, world: Optional[ObservedWorld] = None) -> GateAResult:
    """Run the five Gate A checks against ``item``.

    ``item.change`` is guaranteed non-``None`` here: :func:`classify_lane`
    rejects an item with no change before reaching this function, because "no
    change to merge" is a different answer from "the change is not clean".

    An undecidable observation (``mergeable is None``, ``ci_green is None``, an
    unevaluable declared dependency) fails the corresponding check — auto-merge
    requires positive confirmation of cleanliness, never absence of evidence.
    """
    change = item.change
    not_draft = not change.is_draft
    no_open_dependency, open_dependency_reasons = _evaluate_open_dependencies(item, world)
    mergeable_clean = change.mergeable is True
    ci_green = change.ci_green is True
    no_security_threads = change.security_threads == 0

    failed: list[str] = []
    if not not_draft:
        failed.append("not_draft")
    if not no_open_dependency:
        failed.append("no_open_dependency")
    if not mergeable_clean:
        failed.append("mergeable_clean")
    if not ci_green:
        failed.append("ci_green")
    if not no_security_threads:
        failed.append("no_security_threads")

    return GateAResult(
        not_draft=not_draft,
        no_open_dependency=no_open_dependency,
        mergeable_clean=mergeable_clean,
        ci_green=ci_green,
        no_security_threads=no_security_threads,
        passed=(not failed),
        failed_checks=failed,
        open_dependency_reasons=open_dependency_reasons,
    )


# ---------------------------------------------------------------------------
# Gate B — lane match
# ---------------------------------------------------------------------------


def _match_lane(item: Item) -> tuple:
    """Return ``(lane, reason)`` for the Gate B lane match.

    Exactly one lane label must be present. Zero labels → no lane; two or
    more → ambiguous, no lane. Returns ``(None, reason)`` when ineligible.
    """
    matched: list[MergeLane] = [
        lane for lane, label in _LANE_LABELS.items() if label in item.labels
    ]
    if len(matched) == 0:
        return (None, "no auto-merge lane label present")
    if len(matched) > 1:
        names = ", ".join(sorted(lane.value for lane in matched))
        return (None, f"ambiguous lane match ({names})")
    return (matched[0], "")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class LaneClassification(BaseModel):
    """The full auto-merge classification of a PR.

    ``eligible`` is True iff Gate A passed AND exactly one lane matched.
    ``gate_a`` is always populated (even on failure, so the caller can report
    the failing checks). ``lane`` is ``None`` when Gate B did not match
    exactly one lane. ``reason`` explains the first disqualifying condition
    (or "eligible" on success).
    """

    eligible: bool
    gate_a: GateAResult
    lane: Optional[MergeLane]
    reason: str


def classify_lane(
    item: Item, world: Optional[ObservedWorld] = None
) -> LaneClassification:
    """Classify ``item`` for auto-merge: Gate A first, then Gate B (exactly one lane).

    ``world`` is the observed snapshot Gate A check 2 evaluates the item's
    declared dependencies against. Omitting it is legal but conservative: an
    item declaring any dependency then fails check 2, because auto-merge
    requires positive confirmation and no observation was supplied.

    An item carrying no change is never eligible; this is reported with a
    populated (but trivially-failing) Gate A so the caller sees a consistent
    shape.
    """
    # An item with no change has nothing to merge. This covers every stage
    # before `in_flight` and the issue-side twin of a separately-enumerated
    # change — the record holding the change is the one classified.
    if item.change is None:
        gate_a = GateAResult(
            not_draft=False,
            no_open_dependency=False,
            mergeable_clean=False,
            ci_green=False,
            no_security_threads=False,
            passed=False,
            failed_checks=["no_change"],
        )
        return LaneClassification(
            eligible=False,
            gate_a=gate_a,
            lane=None,
            reason=f"item carries no change (stage={item.stage.value})",
        )

    # Gate A.
    gate_a = _evaluate_gate_a(item, world)
    if not gate_a.passed:
        detail = (
            f" ({'; '.join(gate_a.open_dependency_reasons)})"
            if gate_a.open_dependency_reasons
            else ""
        )
        return LaneClassification(
            eligible=False,
            gate_a=gate_a,
            lane=None,
            reason=f"gate A failed: {', '.join(gate_a.failed_checks)}{detail}",
        )

    # Gate B: exactly one lane.
    lane, lane_reason = _match_lane(item)
    if lane is None:
        return LaneClassification(
            eligible=False,
            gate_a=gate_a,
            lane=None,
            reason=f"gate B: {lane_reason}",
        )

    return LaneClassification(
        eligible=True,
        gate_a=gate_a,
        lane=lane,
        reason="eligible",
    )
