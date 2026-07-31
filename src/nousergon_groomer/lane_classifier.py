"""Lane classifier — auto-merge Gate A and Gate B lane assignment.

A PR is eligible for auto-merge only if it passes **Gate A** (five cleanliness
checks) AND matches **exactly one** auto-merge lane (Gate B). This module is a
pure function over an :class:`Item`; it makes no network calls and consults
no model. The lane *labels* are the framework-level convention documented in
the auto-merge policy; the specific lane membership of a given PR is
adapter data carried on ``item.labels``.

Gate A checks (all must pass; an undecidable observation fails the check —
we never auto-merge what we cannot confirm clean):

1. ``not_draft``      — the PR is not a draft.
2. ``no_gate_label``  — the PR carries no ``gate:*`` label (gated PRs are
   not auto-merge candidates; they require their gate to clear).
3. ``mergeable_clean`` — GitHub reports ``mergeable == True``.
4. ``ci_green``       — CI is reported green.
5. ``no_security_threads`` — the PR has zero open security review threads.

Gate B lane match: exactly one of the lane labels must be present. Zero or
multiple lane labels means the PR is not eligible (no lane to run).
"""
from __future__ import annotations

import enum
from typing import Optional

from pydantic import BaseModel

from .models import Item, ItemKind

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
    no_gate_label: bool
    mergeable_clean: bool
    ci_green: bool
    no_security_threads: bool
    passed: bool
    failed_checks: list[str]


def _evaluate_gate_a(item: Item) -> GateAResult:
    """Run the five Gate A checks against ``item``.

    An undecidable observation (``mergeable is None`` or ``ci_green is None``)
    fails the corresponding check — auto-merge requires positive confirmation
    of cleanliness, never absence of evidence.
    """
    not_draft = (not item.is_draft) and (
        item.state.value != "draft" if hasattr(item.state, "value") else True
    )
    no_gate_label = not item.has_gate_label
    mergeable_clean = item.mergeable is True
    ci_green = item.ci_green is True
    no_security_threads = item.security_threads == 0

    failed: list[str] = []
    if not not_draft:
        failed.append("not_draft")
    if not no_gate_label:
        failed.append("no_gate_label")
    if not mergeable_clean:
        failed.append("mergeable_clean")
    if not ci_green:
        failed.append("ci_green")
    if not no_security_threads:
        failed.append("no_security_threads")

    return GateAResult(
        not_draft=not_draft,
        no_gate_label=no_gate_label,
        mergeable_clean=mergeable_clean,
        ci_green=ci_green,
        no_security_threads=no_security_threads,
        passed=(not failed),
        failed_checks=failed,
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


def classify_lane(item: Item) -> LaneClassification:
    """Classify ``item`` for auto-merge: Gate A first, then Gate B (exactly one lane).

    Non-PR items are never eligible; this is reported with a populated (but
    trivially-failing) Gate A so the caller sees a consistent shape.
    """
    # A non-PR is never an auto-merge candidate.
    if item.kind is not ItemKind.PR:
        gate_a = GateAResult(
            not_draft=False,
            no_gate_label=False,
            mergeable_clean=False,
            ci_green=False,
            no_security_threads=False,
            passed=False,
            failed_checks=["not_a_pr"],
        )
        return LaneClassification(
            eligible=False,
            gate_a=gate_a,
            lane=None,
            reason="not a PR",
        )

    # Gate A.
    gate_a = _evaluate_gate_a(item)
    if not gate_a.passed:
        return LaneClassification(
            eligible=False,
            gate_a=gate_a,
            lane=None,
            reason=f"gate A failed: {', '.join(gate_a.failed_checks)}",
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
