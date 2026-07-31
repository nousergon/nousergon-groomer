"""Dependency evaluator — pure function over (Dependency, ObservedWorld) (§3).

This module is the entire §3 mechanism: blocked-ness is *derived* from
declared dependencies evaluated against an observed snapshot of the world,
never asserted by an author. The evaluator is a pure function — same
``(Dependency, ObservedWorld)`` always yields the same
:class:`DependencyEvaluation`. It makes no network calls and consults no
model; the world is passed in.

A critical property of :class:`ObservedWorld`: every field is optional and
``None`` means "the world did not report this." An unset field is **never**
coerced to false — it produces an ``undecidable`` evaluation. This is the
honest-degradation requirement: a missing observation is reported as
undecidable, not as "satisfied=False," so the reconciler can surface it
rather than silently blocking on a phantom condition.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from pydantic import BaseModel

from .models import Dependency, DependencyEvaluation, DependencyKind, Item

# ---------------------------------------------------------------------------
# ObservedWorld — a snapshot of external conditions
# ---------------------------------------------------------------------------


class ObservedWorld(BaseModel):
    """A snapshot of the external conditions dependencies may reference.

    Every field is optional. ``None`` (the default) means "the harness did not
    report this surface," which yields an *undecidable* evaluation — never a
    false one. This is the difference between "we looked and the object is
    absent" (``s3_objects`` is a set not containing the target → not satisfied)
    and "we did not look" (``s3_objects is None`` → undecidable).

    ``terminal_items`` is the set of carried-item ids (issues or PRs) that are
    in a terminal state (CLOSED or MERGED); it backs both ``issue_terminal``
    and ``pr_terminal`` dependencies. ``pipeline_runs`` is the set of pipeline
    run ids that completed successfully. ``today`` is the wall-clock date used
    to resolve ``date`` dependencies (target date has arrived).
    """

    s3_objects: Optional[set[str]] = None
    s3_prefixes: Optional[set[str]] = None
    terminal_items: Optional[set[str]] = None
    pipeline_runs: Optional[set[str]] = None
    today: Optional[_dt.date] = None
    absent_labels: Optional[set[str]] = None


# ---------------------------------------------------------------------------
# Single-dependency evaluation
# ---------------------------------------------------------------------------


def evaluate_dependency(dep: Dependency, world: ObservedWorld) -> DependencyEvaluation:
    """Evaluate one :class:`Dependency` against ``world`` (§3).

    One branch per :class:`DependencyKind`; the trailing ``else`` raises
    ``NotImplementedError`` so adding a new kind without an evaluator is a
    loud failure (totality — no silent default).
    """
    kind = dep.kind

    if kind is DependencyKind.S3_OBJECT:
        if world.s3_objects is None:
            return DependencyEvaluation(
                dependency=dep, satisfied=False, undecidable=True,
                reason="s3_objects not reported",
            )
        return DependencyEvaluation(
            dependency=dep, satisfied=dep.target in world.s3_objects,
            reason="" if dep.target in world.s3_objects else f"{dep.target} absent",
        )

    if kind is DependencyKind.S3_PREFIX:
        if world.s3_prefixes is None:
            return DependencyEvaluation(
                dependency=dep, satisfied=False, undecidable=True,
                reason="s3_prefixes not reported",
            )
        return DependencyEvaluation(
            dependency=dep, satisfied=dep.target in world.s3_prefixes,
            reason="" if dep.target in world.s3_prefixes else f"prefix {dep.target} empty",
        )

    if kind in (DependencyKind.ISSUE_TERMINAL, DependencyKind.PR_TERMINAL):
        if world.terminal_items is None:
            return DependencyEvaluation(
                dependency=dep, satisfied=False, undecidable=True,
                reason="terminal_items not reported",
            )
        present = dep.target in world.terminal_items
        return DependencyEvaluation(
            dependency=dep, satisfied=present,
            reason="" if present else f"{dep.target} not terminal",
        )

    if kind is DependencyKind.PIPELINE_RUN:
        if world.pipeline_runs is None:
            return DependencyEvaluation(
                dependency=dep, satisfied=False, undecidable=True,
                reason="pipeline_runs not reported",
            )
        present = dep.target in world.pipeline_runs
        return DependencyEvaluation(
            dependency=dep, satisfied=present,
            reason="" if present else f"pipeline run {dep.target} not successful",
        )

    if kind is DependencyKind.DATE:
        if world.today is None:
            return DependencyEvaluation(
                dependency=dep, satisfied=False, undecidable=True,
                reason="today not reported",
            )
        try:
            target = _dt.date.fromisoformat(dep.target)
        except ValueError as exc:
            # A malformed date target is a §3.1 write-boundary defect that
            # escaped validation; fail loud rather than silently treating it
            # as "not yet arrived."
            raise ValueError(
                f"DATE dependency target {dep.target!r} is not an ISO date: {exc}"
            ) from exc
        arrived = world.today >= target
        return DependencyEvaluation(
            dependency=dep, satisfied=arrived,
            reason="" if arrived else f"date {dep.target} not yet reached",
        )

    if kind is DependencyKind.LABEL_ABSENT:
        if world.absent_labels is None:
            return DependencyEvaluation(
                dependency=dep, satisfied=False, undecidable=True,
                reason="absent_labels not reported",
            )
        is_absent = dep.target in world.absent_labels
        return DependencyEvaluation(
            dependency=dep, satisfied=is_absent,
            reason="" if is_absent else f"label {dep.target} is present",
        )

    # Totality: an unknown kind is a programming error, not a default.
    raise NotImplementedError(f"evaluate_dependency: unsupported DependencyKind {kind!r}")


# ---------------------------------------------------------------------------
# Item-level evaluation
# ---------------------------------------------------------------------------


def evaluate_item_dependencies(item: Item, world: ObservedWorld) -> list[DependencyEvaluation]:
    """Evaluate every declared dependency of ``item`` against ``world``.

    Returns one :class:`DependencyEvaluation` per declared dependency, in
    declaration order. An item with no declared dependencies returns an empty
    list — it is trivially unblocked.
    """
    return [evaluate_dependency(dep, world) for dep in item.declared_dependencies]


def is_item_blocked(
    item: Item, world: ObservedWorld
) -> tuple[bool, list[DependencyEvaluation]]:
    """Derive whether ``item`` is blocked (§3) and return the evaluations.

    Returns ``(blocked, evaluations)`` where:

    - ``evaluations`` is the full list from :func:`evaluate_item_dependencies`,
      so callers can distinguish "definitely blocked" from "undecidable."
    - ``blocked`` is ``True`` iff at least one dependency is definitely
      unsatisfied (``satisfied is False`` and ``undecidable is False``). An
      undecidable dependency does **not** set ``blocked`` — it is surfaced via
      the returned evaluations so the reconciler can emit UNDECIDABLE rather
      than silently blocking.

    An item with no declared dependencies is never blocked.
    """
    evaluations = evaluate_item_dependencies(item, world)
    blocked = any(
        (not ev.satisfied) and (not ev.undecidable) for ev in evaluations
    )
    return blocked, evaluations
