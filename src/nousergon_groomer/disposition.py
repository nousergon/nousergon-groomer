"""Disposition function — the §5.1 total reconciliation verdict.

For every carried item, the reconciler computes exactly one
:class:`Disposition`: ACT, BLOCKED, TERMINAL, or UNDECIDABLE. This module is
the totality surface: it must map **every** :class:`ItemState` to exactly one
disposition, and an uncovered state is a defect in this function, not an
acceptable gap.

The function is a pure over ``(item, graph, world)`` — no network, no model,
no side effects. It consults the transitive :class:`DependencyGraph` (§3.4)
for blocked-ness and the :func:`lane_classifier.classify_lane` for auto-merge
eligibility. The admission controller is **not** consulted here: admission
gates *PR creation*, and this function's ACT for an actionable issue names
"create PR (subject to admission)" — the reconciler loop applies the
admission gate before acting, so the disposition stays pure and testable.

Precedence (evaluated top-down, first match wins):

1. **Human-owned** (§9 carve-out 1) → TERMINAL (surface and stop; the loop
   never acts on a human-owned item).
2. **Terminal states** (MERGED / CLOSED / DO_NOT_GROOM) → TERMINAL.
3. **Undecidable dependency** (§3 honest degradation) → UNDECIDABLE, naming
   the undecidable dependency. This is checked before state-specific logic
   so a missing world observation is never silently coerced into an ACT or
   BLOCKED.
4. **Unrepresented gate projection** (§3.3) → UNDECIDABLE, naming the labels.
   An item carrying a ``gate:*`` label while declaring nothing has a status
   projection with no spec behind it; see
   :attr:`models.Item.unrepresented_gate_labels`.
5. **State-specific** disposition (see ``_STATE_DISPATCH``).

**No branch in this module reads a label as state** (``alpha-engine-config#6137``,
parent ``nous-ergon-ops#356``). Gate-ness reaches the disposition exactly one
way: the harness translates a gate into declared dependencies, the observer
reports the surfaces those dependencies name, and the evaluator decides. A
gate that has cleared therefore unblocks its item with no actor and no label
edit; a gate that has not names its real condition — an S3 key, a pipeline
run, a blocking item — rather than the label that hid it.
"""
from __future__ import annotations

from typing import Callable

from .dependency_evaluator import ObservedWorld, evaluate_item_dependencies
from .dependency_graph import DependencyGraph
from .lane_classifier import classify_lane
from .models import (
    DependencyEvaluation,
    Disposition,
    DispositionKind,
    Item,
    ItemState,
)

__all__ = ["compute_disposition"]


# ---------------------------------------------------------------------------
# Terminal dispositions (no dependency evaluation needed)
# ---------------------------------------------------------------------------

def _terminal(item: Item, reason: str) -> Disposition:
    return Disposition(kind=DispositionKind.TERMINAL, reason=reason)


def _blocked(chain: list[DependencyEvaluation]) -> Disposition:
    """Build a BLOCKED disposition naming the root-cause chain."""
    if not chain:
        # Defensive: callers should only pass a non-empty chain. An empty
        # chain with a BLOCKED request is a logic error — fail loud.
        raise ValueError(
            "compute_disposition: _blocked called with empty chain "
            "(§5.1: a BLOCKED disposition must name its blocking chain)"
        )
    names = " -> ".join(
        f"{ev.dependency.kind.value}:{ev.dependency.target}" for ev in chain
    )
    return Disposition(
        kind=DispositionKind.BLOCKED,
        reason=f"blocked on: {names}",
    )


def _act(action: str, reason: str = "") -> Disposition:
    return Disposition(kind=DispositionKind.ACT, action=action, reason=reason or None)


def _undecidable(reason: str) -> Disposition:
    return Disposition(kind=DispositionKind.UNDECIDABLE, reason=reason)


# ---------------------------------------------------------------------------
# State-specific handlers
# ---------------------------------------------------------------------------

def _disposition_open_issue_actionable(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """An actionable issue: create a PR if unblocked, else BLOCKED."""
    chain = graph.get_blocked_chain(item.id)
    if chain:
        return _blocked(chain)
    # Unblocked and actionable → ACT (create PR). Admission is applied by
    # the reconciler loop, not here, so the disposition stays pure.
    return _act("create_pr", reason="actionable issue, unblocked")


def _disposition_open_issue_blocked(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A declared-blocked issue: name the chain (it should already be blocked)."""
    chain = graph.get_blocked_chain(item.id)
    if chain:
        return _blocked(chain)
    # The issue's *state* says BLOCKED but no dependency is unsatisfied — a
    # state/world inconsistency. Surface it loudly rather than silently
    # acting. This is the §3.1 spirit: never coerce an inconsistent
    # observation into a quiet action.
    return _undecidable(
        "issue state is OPEN_ISSUE_BLOCKED but no unsatisfied dependency "
        "was found (state/world inconsistency)"
    )


def _disposition_open_issue_waiting(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """An issue with an open PR: the loop's job is done for this item."""
    return _terminal(item, "issue has an open PR; waiting on review")


def _disposition_open_clean_green(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A green, mergeable PR: auto-merge if armed, else surface for review."""
    chain = graph.get_blocked_chain(item.id)
    if chain:
        # F5: a green PR can still be blocked on a declared dependency. The
        # reconciler must not merge a PR whose declared deps are unsatisfied.
        return _blocked(chain)
    # The world is passed so Gate A can re-derive the item's declared
    # conditions rather than trust the chain check above. §3.3's forbidden
    # shape is a single upstream check owning the exclusion — that is the
    # 2026-07-22 incident verbatim — so both surfaces derive independently.
    lane = classify_lane(item, world)
    if lane.eligible:
        return _act(
            "automerge",
            reason=f"green PR eligible for auto-merge lane {lane.lane.value}",
        )
    # Not armed for auto-merge → terminal (needs human review). This is F5
    # at rest: the reconciler cannot advance it further without a human.
    return _terminal(
        item,
        f"green PR not auto-merge-eligible: {lane.reason} (needs human review)",
    )


def _disposition_open_red_ci(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A PR with failing CI: ACT to fix CI (unless blocked on a dep)."""
    chain = graph.get_blocked_chain(item.id)
    if chain:
        return _blocked(chain)
    return _act("fix_ci", reason="PR CI is red")


def _disposition_open_dirty(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A PR with merge conflicts / behind main: ACT to resolve."""
    chain = graph.get_blocked_chain(item.id)
    if chain:
        return _blocked(chain)
    return _act("resolve_conflicts", reason="PR is dirty (merge conflicts or behind)")


def _disposition_open_draft(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A draft PR: ACT to author it forward, or BLOCKED on a declared condition.

    Per pull-request-policy §4.1, a draft must be actively authored or blocked
    by a named gate; a draft that is neither is a defect. "Blocked by a named
    gate" is now the ``chain`` branch above — the gate is named by the
    *condition* it rests on, derived from the item's declarations against the
    observed world.

    A draft whose declared conditions are all satisfied is ACT, even when it
    still carries the ``gate:*`` label its gate was projected with: the label
    is a stale projection and the loop's job is to advance the draft. Reading
    the label here is what made a cleared gate need an actor to clear it —
    the property §3 exists to remove (a draft is never a resting state).
    """
    chain = graph.get_blocked_chain(item.id)
    if chain:
        return _blocked(chain)
    return _act("advance_draft", reason="draft PR must be authored forward")


def _disposition_open_pending_ci(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A PR with CI still running: terminal (wait; re-classified next cycle)."""
    return _terminal(item, "PR CI is pending; carried under pending lease")


# ---------------------------------------------------------------------------
# Dispatch table — the totality surface
# ---------------------------------------------------------------------------

# One handler per ItemState. The handler receives (item, graph, world) and
# returns a Disposition. Adding a new ItemState without a handler here is a
# loud KeyError at dispatch — totality is enforced structurally.
_STATE_DISPATCH: dict[ItemState, Callable[[Item, DependencyGraph, ObservedWorld], Disposition]] = {
    ItemState.OPEN_ISSUE_ACTIONABLE: _disposition_open_issue_actionable,
    ItemState.OPEN_ISSUE_BLOCKED: _disposition_open_issue_blocked,
    ItemState.OPEN_ISSUE_WAITING: _disposition_open_issue_waiting,
    ItemState.OPEN_CLEAN_GREEN: _disposition_open_clean_green,
    ItemState.OPEN_RED_CI: _disposition_open_red_ci,
    ItemState.OPEN_DIRTY: _disposition_open_dirty,
    ItemState.OPEN_DRAFT: _disposition_open_draft,
    ItemState.OPEN_PENDING_CI: _disposition_open_pending_ci,
    # MERGED / CLOSED / DO_NOT_GROOM are handled before dispatch (terminal);
    # they are intentionally absent from this table. The exhaustiveness
    # test in the contract suite asserts that every ItemState is either in
    # this table or in _TERMINAL_STATES.
}

_TERMINAL_STATES: frozenset[ItemState] = frozenset(
    {ItemState.MERGED, ItemState.CLOSED, ItemState.DO_NOT_GROOM}
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_disposition(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """Compute the §5.1 disposition for ``item``.

    Pure over ``(item, graph, world)``. See module docstring for precedence.
    """
    # 1. Human-owned (§9 carve-out 1) — surface and stop, before anything.
    if item.is_human_owned:
        return _terminal(item, "human-owned item (§9 carve-out 1): surface and stop")

    # 2. Terminal states — no dependency evaluation needed.
    if item.state in _TERMINAL_STATES:
        return _terminal(item, f"item is in terminal state {item.state.value}")

    # 3. Undecidable dependency — honest degradation before state logic.
    #    Only evaluated for non-terminal items (terminal items are already
    #    dispatched above and never need their deps evaluated).
    evaluations = evaluate_item_dependencies(item, world)
    undecidable = [ev for ev in evaluations if ev.undecidable]
    if undecidable:
        names = ", ".join(
            f"{ev.dependency.kind.value}:{ev.dependency.target}" for ev in undecidable
        )
        return _undecidable(
            f"undecidable dependency (world did not report the surface): {names}"
        )

    # 4. Unrepresented gate projection (§3.3) — a gate:* label with no
    #    declaration behind it. Neither branch is derivable: treating the
    #    label as blocked asserts blocked-ness (what §3 abolishes), and
    #    treating it as unblocked is the 2026-07-22 auto-merge incident. The
    #    honest disposition is UNDECIDABLE, which also keeps the item out of
    #    every ACT path below — including auto-merge — by construction.
    unrepresented = item.unrepresented_gate_labels
    if unrepresented:
        return _undecidable(
            "gate label(s) project a blocked status with no declared "
            f"dependency behind them: {', '.join(unrepresented)} "
            "(groom-sweep-policy §3.3: a label is a status projection, never a "
            "source of truth — declare the condition the gate names)"
        )

    # 5. State-specific dispatch. A missing handler is a totality defect.
    handler = _STATE_DISPATCH.get(item.state)
    if handler is None:
        # Loud failure: a new ItemState was added without a handler.
        raise NotImplementedError(
            f"compute_disposition: no handler for ItemState {item.state!r} "
            "(§5.1 totality defect — add a handler to _STATE_DISPATCH)"
        )
    return handler(item, graph, world)
