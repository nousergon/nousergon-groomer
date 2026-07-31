"""Transitive dependency graph (§3.4).

Dependencies compose: an item may depend on another carried item being
terminal, which in turn may depend on an external condition. This module
walks that closure and reports the blocking chain back to the *root external
condition* — the leaf that is actually unsatisfiable right now, as opposed to
an intermediate item that merely inherits its blocked-ness.

Item-pointing dependencies (:attr:`DependencyKind.ISSUE_TERMINAL` and
:attr:`DependencyKind.PR_TERMINAL`) are followed transitively. Every other
dependency kind is an external leaf resolved against the
:class:`ObservedWorld` and is not followed further.

Cycles are a §3.1 write-boundary defect (a dependency declaration that
forms a cycle should have been rejected when recorded). The graph defends
against them anyway: a cycle encountered during traversal raises
:class:`DependencyCycleError` rather than recursing forever.
"""
from __future__ import annotations

from .dependency_evaluator import (
    ObservedWorld,
    evaluate_dependency,
    is_item_blocked,
)
from .models import DependencyEvaluation, DependencyKind, Item


class DependencyCycleError(RuntimeError):
    """Raised when a dependency cycle is detected during traversal (§3.1).

    A cycle in declared dependencies is a write-boundary defect; this error
    names the cycle path so the caller can file a corrective issue.
    """

    def __init__(self, cycle_path: list[str]) -> None:
        self.cycle_path = cycle_path
        path = " -> ".join(cycle_path)
        super().__init__(f"dependency cycle detected (§3.1 defect): {path}")


# Kinds that point at other carried items and are followed transitively.
_ITEM_POINTING_KINDS = frozenset(
    {DependencyKind.ISSUE_TERMINAL, DependencyKind.PR_TERMINAL}
)


class DependencyGraph:
    """A directed graph of transitive dependencies over a set of items.

    Built once from ``items`` and an :class:`ObservedWorld`. The graph is
    immutable thereafter; re-evaluation against a new world requires a new
    graph. Queries are O(items + deps) and do not allocate new evaluations
    on the hot path beyond what :func:`evaluate_dependency` already produces.
    """

    def __init__(self, items: list[Item], world: ObservedWorld) -> None:
        self._world = world
        self._items: dict[str, Item] = {item.id: item for item in items}
        # Detect duplicate ids at construction — a snapshot with dupes is a
        # harness defect; fail loud rather than silently shadowing one item.
        if len(self._items) != len(items):
            seen: set[str] = set()
            dupes: set[str] = set()
            for item in items:
                (dupes if item.id in seen else seen).add(item.id)
            raise ValueError(f"duplicate item ids in graph input: {sorted(dupes)}")

    # -- public API --------------------------------------------------------

    def get_blocked_chain(self, item_id: str) -> list[DependencyEvaluation]:
        """Return the blocking chain from ``item_id`` to a root external condition.

        Returns an empty list if ``item_id`` is not transitively blocked. If
        it is blocked, returns one chain (the first found in declaration
        order) of :class:`DependencyEvaluation` objects starting at a
        dependency of ``item_id`` and ending at the unsatisfied *external*
        leaf condition that is the root cause. Intermediate entries are
        item-pointing dependencies whose target item is itself blocked.

        Raises :class:`DependencyCycleError` if traversal encounters a cycle.
        """
        if item_id not in self._items:
            raise KeyError(f"unknown item id: {item_id!r}")
        return self._chain(item_id, path=(item_id,))

    def is_transitively_blocked(self, item_id: str) -> bool:
        """True iff ``item_id`` is blocked by some transitive dependency chain."""
        return bool(self.get_blocked_chain(item_id))

    # -- internals ---------------------------------------------------------

    def _chain(self, item_id: str, path: tuple) -> list[DependencyEvaluation]:
        item = self._items[item_id]
        for dep in item.declared_dependencies:
            ev = evaluate_dependency(dep, self._world)
            if ev.satisfied or ev.undecidable:
                # Satisfied: not a blocker. Undecidable: not a definite
                # blocker; the reconciler surfaces it elsewhere. Neither
                # contributes to a blocked chain.
                continue
            # Definitely unsatisfied.
            if dep.kind in _ITEM_POINTING_KINDS:
                target = dep.target
                if target in path:
                    # Cycle: a carried item (transitively) depends on itself.
                    raise DependencyCycleError(list(path) + [target])
                if target in self._items:
                    sub = self._chain(target, path + (target,))
                    if sub:
                        return [ev] + sub
                    # Target item is not itself blocked, yet this terminal
                    # dependency is unsatisfied — the target is not terminal
                    # in the world. That makes *this* the root external leaf.
                    return [ev]
                # Target item not carried in this graph: treat as an external
                # leaf (the world already said it is not terminal).
                return [ev]
            # External leaf kind — this is the root cause.
            return [ev]
        return []

    # -- convenience -------------------------------------------------------

    def blocked_items(self) -> list[str]:
        """Return the ids of all carried items that are transitively blocked."""
        return [item_id for item_id in self._items if self.is_transitively_blocked(item_id)]

    def directly_blocked_items(self) -> list[str]:
        """Return ids of items blocked by one of their *own* declared dependencies.

        This is the §3 single-item view (no transitive walk); useful for
        distinguishing "blocked by an external leaf" from "blocked through
        an intermediate item."
        """
        out: list[str] = []
        for item_id, item in self._items.items():
            blocked, _ = is_item_blocked(item, self._world)
            if blocked:
                out.append(item_id)
        return out
