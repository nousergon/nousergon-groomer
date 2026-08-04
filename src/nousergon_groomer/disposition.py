"""Disposition function — the §5.1 total reconciliation verdict.

For every carried item, the reconciler computes exactly one
:class:`Disposition`: ACT, BLOCKED, TERMINAL, or UNDECIDABLE. This module is
the totality surface: it must map **every** :class:`ItemStage` to exactly one
disposition, and an uncovered stage is a defect in this function, not an
acceptable gap.

**Every rule here is defined over an item, scoped to a stage** (§3's Forbids).
There is no rule about pull requests — a pull request is the rendering of the
``in_flight`` stage, so what used to be a per-PR-state dispatch is now one
stage handler that reads the change's *condition*. The difference is not
cosmetic: it is what makes a conflicted change ``ACT`` rather than a state the
item rests in (§3.5), and a draft a condition the cycle must exit rather than a
place to leave things (§4.4).

The function is pure over ``(item, graph, world)`` — no network, no model, no
side effects. It consults the transitive :class:`DependencyGraph` (§3.4) for
blocked-ness, :func:`stage.effective_stage` for where the item actually is, and
:func:`lane_classifier.classify_lane` for auto-merge eligibility. The admission
controller is **not** consulted here: admission gates *PR creation*, and this
function's ACT for a ready item names "create PR (subject to admission)" — the
reconciler loop applies the admission gate before acting, so the disposition
stays pure and testable.

Precedence (evaluated top-down, first match wins):

1. **Human-owned** (§9 carve-out 1) → TERMINAL (surface and stop; the loop
   never acts on a human-owned item).
2. **Excluded** (``do_not_groom``, §9 carve-out 2) → TERMINAL.
3. **Terminal stage** (``done`` / ``abandoned``) → TERMINAL. Note that
   ``merged`` is *not* here: §3.8 gives it a successor stage and an obligation.
   Checked *before* the identity-conflict step below so a terminal item is
   never displaced from TERMINAL by a stray duplicate-change observation
   (§5.6's own invariant: a terminal item's disposition never regresses).
4. **Identity conflict** (§5.6, alpha-engine-config#6316) → UNDECIDABLE,
   naming the extra change refs. See :attr:`models.Item.additional_change_refs`.
5. **Undecidable dependency** (§3 honest degradation) → UNDECIDABLE, naming
   the undecidable dependency. This is checked before stage-specific logic
   so a missing world observation is never silently coerced into an ACT or
   BLOCKED.
6. **Unrepresented gate projection** (§3.3) → UNDECIDABLE, naming the labels.
   An item carrying a ``gate:*`` label while declaring nothing has a status
   projection with no spec behind it; see
   :attr:`models.Item.unrepresented_gate_labels`.
7. **Stage-specific** disposition, over the *effective* stage (see
   ``_STAGE_DISPATCH``).

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
    ChangeCondition,
    DependencyEvaluation,
    Disposition,
    DispositionKind,
    Item,
    ItemStage,
)
from .stage import deadline_passed, effective_stage, evaluate_verification

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
# Change conditions — the in-flight stage's sub-dispatch
# ---------------------------------------------------------------------------
#
# Every handler below is reached only *inside* the in_flight stage and only
# after the item has been shown unblocked. None of them is a rule about pull
# requests: they are rules about one item, scoped to one stage, reading the
# condition of the change rendering it.


def _condition_conflicted(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A conflicted change → ACT.

    **This is §3.5 in one line.** A merge conflict is inside the loop's own
    write authority: rebasing is something it can do, this cycle, without
    anything in the world changing. A condition the loop could satisfy itself
    is *work*, not a dependency — and modelling the pull request as its own
    item with its own blocked state is what let a conflict be declared a
    dependency of the item and park it. At last measurement most blocked PRs in
    the fleet were merely conflicted or behind ``main``: the loop's own branch
    decay, declared as a blocker on the work.
    """
    return _act(
        "resolve_conflicts",
        reason="the change is conflicted — inside the loop's own authority, "
               "so this is work, never a dependency (§3.5)",
    )


def _condition_ci_red(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A change with failing CI → ACT (§3.5, same argument as a conflict)."""
    return _act("fix_ci", reason="the change's CI is red")


def _condition_draft(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A draft change → ACT to author it forward (§4.4).

    Draft is a transient condition inside the cycle that opened the change, not
    a resting state, and there is nothing left for it to mean: a blocked item
    never reaches ``in_flight`` (§4.3), a conflicted or red change is ``act``
    (§3.5), and a post-merge-verifiable change is mergeable (§3.8). Every
    historical reason to hold a draft has been reassigned to work or to
    admission.

    Reaching this handler already proves the item is unblocked — the caller
    checks the chain first — so a draft here rests on nothing. A draft that
    genuinely waits on a declared condition is BLOCKED one level up, named by
    the condition rather than by the label the gate was projected with.
    """
    return _act(
        "advance_draft",
        reason="draft is a transient condition, never a resting state (§4.4)",
    )


def _condition_ci_pending(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A change whose CI is still running → TERMINAL for this cycle.

    The one in-flight condition the loop cannot act out of: what resolves it is
    a check it does not own finishing. Re-classified next cycle.
    """
    return _terminal(item, "the change's CI is still running; carried under pending lease")


def _condition_clean(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """A clean, green change: auto-merge if armed, else surface for review."""
    # The world is passed so Gate A can re-derive the item's declared
    # conditions rather than trust the chain check the caller already did.
    # §3.3's forbidden shape is a single upstream check owning the exclusion —
    # that is the 2026-07-22 incident verbatim — so both surfaces derive
    # independently.
    lane = classify_lane(item, world)
    if lane.eligible:
        return _act(
            "automerge",
            reason=f"clean, green change eligible for auto-merge lane {lane.lane.value}",
        )
    # Not armed for auto-merge → terminal (needs human review). This is F5
    # at rest: the reconciler cannot advance it further without a human.
    return _terminal(
        item,
        f"clean, green change not auto-merge-eligible: {lane.reason} (needs human review)",
    )


_CONDITION_DISPATCH: dict[
    ChangeCondition, Callable[[Item, DependencyGraph, ObservedWorld], Disposition]
] = {
    ChangeCondition.CONFLICTED: _condition_conflicted,
    ChangeCondition.CI_RED: _condition_ci_red,
    ChangeCondition.DRAFT: _condition_draft,
    ChangeCondition.CI_PENDING: _condition_ci_pending,
    ChangeCondition.CLEAN: _condition_clean,
}


# ---------------------------------------------------------------------------
# Stage-specific handlers
# ---------------------------------------------------------------------------


def _disposition_pre_change(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """``proposed`` / ``ready``: open a change if unblocked, else BLOCKED.

    One handler for both stages because the boundary between them *is* this
    check — ``ready`` is ``proposed`` with an empty blocking chain. Splitting it
    into two handlers would require each to re-derive the other's condition,
    and the two could then disagree.
    """
    chain = graph.get_blocked_chain(item.id)
    if chain:
        return _blocked(chain)
    # Ready and unblocked → ACT (open the change). Admission is applied by
    # the reconciler loop, not here, so the disposition stays pure.
    return _act("create_pr", reason="item is ready: no unsatisfied declared dependency")


def _disposition_in_flight(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """``in_flight``: dispatch on the condition of the change rendering it."""
    chain = graph.get_blocked_chain(item.id)
    if chain:
        # F5: an item in flight can still rest on a declared dependency, and
        # that outranks every condition below — the reconciler must not merge
        # or advance a change whose declared conditions are unsatisfied.
        return _blocked(chain)

    if item.change is None:
        # This record knows a change exists but does not carry it: the change
        # is enumerated as a separate record (the substrate hands the loop an
        # issue and its pull request as two API objects). One unit gets one
        # verdict, and it belongs to the record holding the change — so this
        # one defers instead of inventing a second opinion about the same work.
        return _terminal(
            item,
            f"change {item.change_ref} is in flight and carries this item's "
            "disposition (§3: one item, one verdict)",
        )

    handler = _CONDITION_DISPATCH.get(item.change.condition)
    if handler is None:
        # Loud failure: a new ChangeCondition was added without a handler.
        raise NotImplementedError(
            f"compute_disposition: no handler for ChangeCondition "
            f"{item.change.condition!r} (§5.1 totality defect — add a handler "
            "to _CONDITION_DISPATCH)"
        )
    return handler(item, graph, world)


def _disposition_merged(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """``merged`` with an outstanding verification obligation (§3.8).

    Reaching this handler means :func:`stage.effective_stage` found an
    obligation that is not discharged — an item with none, or with a satisfied
    one, resolves to ``verified`` and never arrives here.

    **Merging did not discharge the obligation.** ``merged`` is a stage, not
    the end: the item waits on a predicate its own merged code produces, which
    is a real condition outside the loop's authority and therefore a legitimate
    BLOCKED — but a bounded one. Past the deadline the item is force-dispositioned
    to the named revert (§3.6: expiry is an event with an action, never another
    interval of silent waiting).
    """
    obligation = item.verification
    if obligation is None:  # pragma: no cover — effective_stage excludes this
        raise RuntimeError(
            f"compute_disposition: item {item.id!r} resolved to stage merged "
            "with no verification obligation — effective_stage should have "
            "returned verified (§3.8 invariant violated)"
        )

    evaluation = evaluate_verification(item, world)
    token = f"{obligation.predicate.kind.value}:{obligation.predicate.target}"

    if evaluation.undecidable:
        return _undecidable(
            f"post-merge verification predicate {token} is undecidable (world "
            f"did not report the surface): {evaluation.reason or 'not reported'}"
        )

    expired = deadline_passed(item, world)
    if expired is None:
        return _undecidable(
            f"post-merge verification of {token} has deadline "
            f"{obligation.deadline} and the world reported no date — whether "
            "the obligation has expired is unknowable, and 'not yet' is not a "
            "safe default (§3.6)"
        )
    if expired:
        return _act(
            obligation.revert_action,
            reason=(
                f"post-merge verification of {token} did not hold by "
                f"{obligation.deadline} (§3.8: the failure path is a named "
                "revert, not another interval of waiting)"
            ),
        )

    return Disposition(
        kind=DispositionKind.BLOCKED,
        reason=(
            f"merged; awaiting post-merge verification of {token} "
            f"(deadline {obligation.deadline})"
        ),
    )


def _disposition_verified(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> Disposition:
    """``verified``: the change is live and its criterion holds.

    F7's clock stops here — this is the moment lead time is measured to, and
    the reconciler records the stamp regardless of what the substrate does
    next. Advancing to ``done`` is closing the intent record, which the harness
    observes rather than the core asserting.
    """
    return _terminal(item, "change verified in production (§3.8)")


# ---------------------------------------------------------------------------
# Dispatch table — the totality surface
# ---------------------------------------------------------------------------

# One handler per ItemStage. The handler receives (item, graph, world) and
# returns a Disposition. Adding a new ItemStage without a handler here is a
# loud NotImplementedError at dispatch — totality is enforced structurally.
_STAGE_DISPATCH: dict[
    ItemStage, Callable[[Item, DependencyGraph, ObservedWorld], Disposition]
] = {
    ItemStage.PROPOSED: _disposition_pre_change,
    ItemStage.READY: _disposition_pre_change,
    ItemStage.IN_FLIGHT: _disposition_in_flight,
    ItemStage.MERGED: _disposition_merged,
    ItemStage.VERIFIED: _disposition_verified,
    # DONE / ABANDONED are handled before dispatch (terminal); they are
    # intentionally absent from this table. The exhaustiveness test in the
    # contract suite asserts that every ItemStage is either in this table or
    # in _TERMINAL_STAGES.
}

# Derived from the enum rather than re-listed, so the two cannot disagree: a
# stage made terminal in the model but forgotten here would be dispatched, and
# a stage listed here but not terminal would be silently unreachable.
_TERMINAL_STAGES: frozenset[ItemStage] = frozenset(
    stage for stage in ItemStage if stage.is_terminal
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

    # 2. Declared exclusion (§9 carve-out 2). Checked as a flag rather than a
    #    stage: an excluded item is at some real point in its life, and
    #    overwriting that with a pseudo-stage would destroy the fact.
    if item.do_not_groom:
        return _terminal(item, "item is marked do_not_groom (§9 carve-out 2): untouched")

    # 3. Terminal stages — no dependency evaluation needed. `merged` is
    #    deliberately absent: §3.8 gives it a successor stage and an obligation.
    if item.stage.is_terminal:
        return _terminal(item, f"item is at terminal stage {item.stage.value}")

    # 4. Identity conflict (§5.6, alpha-engine-config#6316) — more than one
    #    open change was observed rendering this item's in_flight stage. §3
    #    says the intent-to-change relation is internal to one item and
    #    therefore cannot be inconsistent; a non-empty
    #    `additional_change_refs` means the substrate handed the adapter more
    #    than one change for the same intent, which is exactly the identity
    #    defect `duplicate_pr_sweep.py` used to catch from the outside, after
    #    the fact, on a separate pass. Asserted here instead, at the totality
    #    surface every item passes through: UNDECIDABLE keeps the item out of
    #    every ACT path — including auto-merge — by construction, the same
    #    shape as the unrepresented-gate-projection defect below.
    if item.has_identity_conflict:
        refs = ", ".join(item.additional_change_refs)
        primary = item.change_ref or (item.change.ref if item.change else None)
        return _undecidable(
            f"more than one open change renders this item's in_flight stage "
            f"(primary={primary!r}, additional={refs}) — a second in-flight "
            "change for an item that already has one (§5.6): identity must "
            "be resolved (close or re-target the duplicate) before this item "
            "can be acted on"
        )

    # 5. Undecidable dependency — honest degradation before stage logic.
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

    # 6. Unrepresented gate projection (§3.3) — a gate:* label with no
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

    # 7. Stage-specific dispatch over the EFFECTIVE stage, not the recorded
    #    one. A missing handler is a totality defect.
    stage = effective_stage(item, graph, world)
    handler = _STAGE_DISPATCH.get(stage)
    if handler is None:
        # Loud failure: a new ItemStage was added without a handler.
        raise NotImplementedError(
            f"compute_disposition: no handler for ItemStage {stage!r} "
            "(§5.1 totality defect — add a handler to _STAGE_DISPATCH)"
        )
    return handler(item, graph, world)
