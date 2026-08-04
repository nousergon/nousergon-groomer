"""Effective stage — the two stage boundaries that are derived, never stored.

:attr:`models.Item.stage` is what a harness *recorded*. It is not where the item
is. Two of the six forward stages are computed from the recorded stage and an
observed world, and both derivations exist for the same reason: storing them
would be asserting a fact the loop is required to re-derive every cycle (§3).

- ``proposed`` → ``ready`` when nothing the item declares is unsatisfied. A
  stored ``ready`` flag is an asserted blocked-ness, which is the state §3
  abolishes; the whole point is that un-blocking needs no actor, because the
  next cycle computes it.
- ``merged`` → ``verified`` when the item's §3.8 post-merge verification
  obligation is discharged. An item with no obligation was verified by the
  checks that let it merge, so it passes straight through.

Everything else is reported as recorded. ``in_flight`` is a fact about the
substrate (a change exists), and ``done`` / ``abandoned`` are decisions.

Pure over ``(item, graph, world)``: no clock, no network, no model.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from .dependency_evaluator import ObservedWorld, evaluate_dependency
from .dependency_graph import DependencyGraph
from .models import DependencyEvaluation, Item, ItemStage

__all__ = [
    "effective_stage",
    "evaluate_verification",
    "deadline_passed",
]


def evaluate_verification(
    item: Item, world: ObservedWorld
) -> Optional[DependencyEvaluation]:
    """Evaluate the item's post-merge verification predicate (§3.8).

    Returns ``None`` when the item declares no obligation — which is not the
    same as an unsatisfied one, and must not be collapsed into it: no
    obligation means merging was the verification, while an unsatisfied
    obligation means the item is still owed one.
    """
    if item.verification is None:
        return None
    return evaluate_dependency(item.verification.predicate, world)


def deadline_passed(item: Item, world: ObservedWorld) -> Optional[bool]:
    """Has the verification obligation's deadline arrived? (§3.6)

    ``None`` when the item declares no obligation, or when the world did not
    report a date. **A date the world did not report is undecidable, never
    "not yet"** — coercing it would let an expired obligation rest forever on
    the one surface that happened to go dark, which is the §3.6 failure the
    deadline exists to prevent.
    """
    if item.verification is None or world.today is None:
        return None
    return world.today >= _dt.date.fromisoformat(item.verification.deadline)


def effective_stage(
    item: Item, graph: DependencyGraph, world: ObservedWorld
) -> ItemStage:
    """Where ``item`` actually is, as opposed to what was recorded.

    See the module docstring for which stages are derived and why. A recorded
    ``ready`` is re-derived rather than trusted: a harness may record it, but
    the world moves underneath it, and trusting it would reintroduce exactly
    the asserted blocked-ness §3 removes.

    An undecidable verification predicate keeps the item at ``merged`` — the
    honest answer, since "we could not look" is not "it has not held". The
    disposition function surfaces the undecidability rather than resolving it.
    """
    if item.stage in (ItemStage.PROPOSED, ItemStage.READY):
        # Unconfirmed is not ready. `get_blocked_chain` reports only *definite*
        # blockers — an undecidable dependency is not one, deliberately, so the
        # reconciler can surface it rather than block on a phantom. But an item
        # whose conditions could not be evaluated has not been shown ready
        # either, and reporting it as `ready` would put it in F3's conversion
        # denominator and start F7's ready-residence clock on a fact nobody
        # established. The closure is walked rather than the item's own
        # declarations so a blocker two hops away is seen (§3.4).
        closure = graph.closure_state(item.id)
        if any(entry.endswith("=undecidable") for entry in closure):
            return ItemStage.PROPOSED
        return ItemStage.PROPOSED if graph.get_blocked_chain(item.id) else ItemStage.READY

    if item.stage is ItemStage.MERGED:
        evaluation = evaluate_verification(item, world)
        if evaluation is None:
            # No post-merge obligation: the checks that permitted the merge
            # were the verification. F7's clock stops here.
            return ItemStage.VERIFIED
        if evaluation.undecidable:
            return ItemStage.MERGED
        return ItemStage.VERIFIED if evaluation.satisfied else ItemStage.MERGED

    return item.stage
