"""Admission controller — the WIP ceiling and unblocked gate (§4).

Admission bounds the *queue*, not the rate (§4.1): at most
``wip_ceiling`` in-flight PRs may be carried at once, and a new PR is opened
only for an issue that is unblocked (§4.3). This module is pure logic over
recorded state — it does not open PRs; it returns a decision the private
harness acts on.

§4.2 charging: every carried PR that is open and not terminal counts
against the ceiling — drafts, blocked PRs, and PRs in review are all
charged, because they all occupy a reviewer/CI slot. Closed and merged PRs
are terminal and do not count.
"""
from __future__ import annotations

from pydantic import BaseModel

from .dependency_evaluator import ObservedWorld, is_item_blocked
from .models import Item, ItemKind, ItemState


class AdmissionDecision(BaseModel):
    """The verdict of :meth:`AdmissionController.can_admit`.

    ``admitted`` is True iff every gate passed. ``reason`` explains the first
    failing gate (or "admitted" on success). ``wip`` and ``ceiling`` are
    recorded so the caller can report utilization without recomputing.
    """

    admitted: bool
    reason: str
    wip: int
    ceiling: int


class AdmissionController:
    """Bounds admission by a WIP ceiling (§4.1) and an unblocked gate (§4.3).

    The ceiling is a structural property of the loop, not a tunable knob: a
    ceiling below 1 means the loop can never admit any work, which is a
    misconfiguration. The constructor rejects it loudly rather than
    silently producing an always-deny controller.
    """

    def __init__(self, wip_ceiling: int) -> None:
        if wip_ceiling < 1:
            raise ValueError(
                f"wip_ceiling must be >= 1 (got {wip_ceiling}); a ceiling below 1 "
                "would make the loop unable to admit any work (§4.1)"
            )
        self.wip_ceiling = wip_ceiling

    # -- §4.2: WIP accounting ----------------------------------------------

    @staticmethod
    def current_wip(items: list[Item]) -> int:
        """Count open, non-terminal PRs — every carried in-flight PR (§4.2).

        A PR counts iff it is a PR and its state is ``OPEN`` or ``DRAFT``.
        ``CLOSED`` and ``MERGED`` are terminal and do not count. Issues never
        count toward WIP (they do not occupy a merge slot).
        """
        return sum(
            1
            for item in items
            if item.kind is ItemKind.PR
            and item.state in (ItemState.OPEN, ItemState.DRAFT)
        )

    def at_ceiling(self, items: list[Item]) -> bool:
        """True iff the WIP count is at or above the ceiling (§4.1)."""
        return self.current_wip(items) >= self.wip_ceiling

    # -- §4.3: admission gate ---------------------------------------------

    def can_admit(
        self, issue: Item, items: list[Item], world: ObservedWorld
    ) -> AdmissionDecision:
        """Decide whether ``issue`` may be admitted (a PR opened for it).

        Three gates, checked in order; the first failing gate denies and
        names itself in ``reason``:

        1. **Actionable & open** — ``issue`` must be an ``OPEN`` issue. A PR,
           a closed issue, or a draft cannot be admitted.
        2. **WIP not saturated** (§4.1) — the ceiling must not be reached.
        3. **No unsatisfied dependencies** (§4.3) — the issue must not be
           blocked by any of its declared dependencies against ``world``.
           An undecidable dependency does *not* deny admission here (it is
           not a definite blocker); the reconciler surfaces undecidability
           separately.
        """
        wip = self.current_wip(items)

        # Gate 1: actionable & open issue.
        if issue.kind is not ItemKind.ISSUE:
            return AdmissionDecision(
                admitted=False, reason="not an issue", wip=wip, ceiling=self.wip_ceiling
            )
        if issue.state is not ItemState.OPEN:
            return AdmissionDecision(
                admitted=False,
                reason=f"issue not open (state={issue.state.value})",
                wip=wip,
                ceiling=self.wip_ceiling,
            )

        # Gate 2: WIP ceiling (§4.1).
        if wip >= self.wip_ceiling:
            return AdmissionDecision(
                admitted=False,
                reason=f"WIP at ceiling ({wip}/{self.wip_ceiling})",
                wip=wip,
                ceiling=self.wip_ceiling,
            )

        # Gate 3: unblocked (§4.3).
        blocked, evaluations = is_item_blocked(issue, world)
        if blocked:
            blocker = next(
                (ev for ev in evaluations if not ev.satisfied and not ev.undecidable),
                None,
            )
            reason = blocker.reason if blocker is not None and blocker.reason else "blocked"
            return AdmissionDecision(
                admitted=False,
                reason=f"blocked: {reason}",
                wip=wip,
                ceiling=self.wip_ceiling,
            )

        return AdmissionDecision(
            admitted=True,
            reason="admitted",
            wip=wip,
            ceiling=self.wip_ceiling,
        )
