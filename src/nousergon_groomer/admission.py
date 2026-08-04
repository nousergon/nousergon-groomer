"""Admission controller — the WIP ceiling and unblocked gate (§4).

Admission bounds the *queue*, not the rate (§4.1): at most
``wip_ceiling`` in-flight PRs may be carried at once, and a new PR is opened
only for an issue that is unblocked (§4.3). This module is pure logic over
recorded state — it does not open PRs; it returns a decision the private
harness acts on.

§4.2 charging: **every item in flight is charged, with no exemption class** —
a draft, a conflicted change and a change in review all occupy the same slot.
The ceiling is a count of items at the ``in_flight`` stage, which is what makes
§2.2 ("closing a change is a success") economically true rather than merely
stated: the fleet accumulated 116 drafts because nothing counted them.

The rules here are defined over items scoped to a stage, never over pull
requests (§3's Forbids). That is not a rename: it is why an item whose change
is a draft cannot slip the charge by being "not really open yet".
"""
from __future__ import annotations

from pydantic import BaseModel

from .dependency_evaluator import ObservedWorld, is_item_blocked
from .models import Item, ItemStage


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
        """Count items at the ``in_flight`` stage carrying a change (§4.2).

        One condition, no enumeration of exempt sub-states: the whole point of
        §3's one-item model is that "is a change in flight for this item?" has
        a single answer. Every condition a change can be in — draft,
        conflicted, red, pending, clean — is charged identically, because each
        occupies the same reviewer/CI slot and each decays against a moving
        ``main`` at the same rate.

        Items before ``in_flight`` are not charged (no change exists yet), and
        ``merged`` / ``verified`` / ``done`` / ``abandoned`` are past it. An
        item at ``in_flight`` that only carries a ``change_ref`` — the
        issue-side twin of a separately-enumerated change — is deliberately
        **not** counted, so one unit of work is charged once rather than twice.
        """
        return sum(
            1
            for item in items
            if item.stage is ItemStage.IN_FLIGHT and item.change is not None
        )

    def at_ceiling(self, items: list[Item]) -> bool:
        """True iff the WIP count is at or above the ceiling (§4.1)."""
        return self.current_wip(items) >= self.wip_ceiling

    # -- §4.3: admission gate ---------------------------------------------

    #: The stages a change may be opened *from*. ``ready`` is the commitment
    #: point; ``proposed`` is included because readiness is derived, not
    #: stored — gate 3 below is what separates them, and duplicating that
    #: derivation here would create two answers to one question.
    _ADMISSIBLE_STAGES = frozenset({ItemStage.PROPOSED, ItemStage.READY})

    def can_admit(
        self, item: Item, items: list[Item], world: ObservedWorld
    ) -> AdmissionDecision:
        """Decide whether ``item`` may be admitted (a change opened for it).

        Three gates, checked in order; the first failing gate denies and
        names itself in ``reason``:

        1. **Pre-change stage** — the item must be at ``proposed`` / ``ready``
           and not already carrying a change. An item at any later stage has
           already been admitted.
        2. **WIP not saturated** (§4.1) — the ceiling must not be reached.
        3. **No unsatisfied dependencies** (§4.3) — the item must not be
           blocked by any of its declared dependencies against ``world``.
           An undecidable dependency does *not* deny admission here (it is
           not a definite blocker); the reconciler surfaces undecidability
           separately.
        """
        wip = self.current_wip(items)

        # Gate 1: the item is at a pre-change stage.
        if item.carries_change:
            return AdmissionDecision(
                admitted=False,
                reason="item already carries a change",
                wip=wip,
                ceiling=self.wip_ceiling,
            )
        if item.stage not in self._ADMISSIBLE_STAGES:
            return AdmissionDecision(
                admitted=False,
                reason=f"item not at a pre-change stage (stage={item.stage.value})",
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
        blocked, evaluations = is_item_blocked(item, world)
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
