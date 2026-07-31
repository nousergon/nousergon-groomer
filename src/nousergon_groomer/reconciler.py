"""Reconciler — the single level-triggered pass (§5.1, §5.3).

The reconciler ties the core together in one enumeration:

1. **Enumerate** all carried items (one pass, one population — §5.1).
2. **Skip** unchanged items via observed-generation (§5.5). A skipped item
   still gets a disposition (its last-known one is re-derived trivially
   because the inputs are unchanged); the skip is an optimization of the
   *evaluation cost*, not a silent drop.
3. **Evaluate** dependencies via the transitive :class:`DependencyGraph`
   (§3.4).
4. **Compute** the disposition via :func:`compute_disposition` (§5.1).
5. **Apply admission** for ACT-create-PR dispositions (§4). An actionable
   issue that is unblocked but whose WIP ceiling is saturated is downgraded
   from ACT to BLOCKED (the queue is full, not the issue).
6. **Order** actions by priority (act-before-blocked, fix-ci-before-merge,
   etc.) so the private harness executes the most urgent work first.
7. **Emit** a :class:`ReconcilerResult` with per-item dispositions and the
   single aggregate throughput number (§7 forbids per-pass blind counters
   with no aggregate).

Idempotent (§5.3): re-running over identical state produces identical output
with no duplicated side effect. The reconciler returns a result; it does
not perform the actions (the private harness does). The result is a pure
function of ``(config, items, world, store)``.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .admission import AdmissionController
from .dependency_evaluator import ObservedWorld
from .dependency_graph import DependencyGraph
from .disposition import compute_disposition
from .models import Disposition, DispositionKind, Item, ItemKind
from .observed_gen import GenerationStore, record_evaluation, should_skip

__all__ = [
    "ReconcilerConfig",
    "Reconciler",
    "ReconcilerResult",
    "ItemDisposition",
]


# ---------------------------------------------------------------------------
# Configuration (adapter data, not hardcoded)
# ---------------------------------------------------------------------------


class ReconcilerConfig(BaseModel):
    """Configuration for a reconciler pass.

    All fields are adapter data — the core never hardcodes the fleet roster,
    lane definitions, or WIP ceiling. The private harness constructs this
    from its own configuration before each pass.
    """

    wip_ceiling: int
    generation: int = 1  # the cycle counter (§5.5); monotonically increasing


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class ItemDisposition(BaseModel):
    """The disposition for one item, plus skip and admission metadata."""

    item_id: str
    disposition: Disposition
    skipped: bool = False  # §5.5: was this item skipped (inputs unchanged)?
    admission_reason: Optional[str] = None  # set when admission downgraded ACT


class ReconcilerResult(BaseModel):
    """The output of one reconciler pass.

    Per-item dispositions are in ``items`` (one per carried item, in input
    order). Aggregate counts give the single throughput number (§7) and the
    flow-outcome breakdown (F1–F5) without requiring the caller to re-tally.
    """

    items: list[ItemDisposition]
    generation: int
    # Aggregate counts — the single throughput number and its breakdown.
    enumerated: int
    acted: int
    blocked: int
    terminal: int
    undecidable: int
    skipped: int
    admitted: int  # ACT-create-PR dispositions that passed admission
    admission_denied: int  # ACT-create-PR dispositions downgraded by admission

    @property
    def throughput(self) -> int:
        """The single throughput number: items acted on this pass (§7)."""
        return self.acted


# ---------------------------------------------------------------------------
# Action priority — ordering for the private harness
# ---------------------------------------------------------------------------

# Lower number = higher priority. The private harness executes actions in
# this order so the most urgent work goes first. The ordering is a policy
# choice, not a mechanical fact; it lives here so it is auditable in one
# place.
_ACTION_PRIORITY = {
    "fix_ci": 0,           # red CI blocks the whole lane — fix first
    "resolve_conflicts": 1,  # a dirty PR blocks its successors — resolve next
    "advance_draft": 2,    # a stale draft wastes a WIP slot — author forward
    "automerge": 3,        # green PRs clear the queue — merge after fixes
    "create_pr": 4,        # new work is the lowest urgency (queue may be full)
}


def _action_priority(action: Optional[str]) -> int:
    if action is None:
        return 99
    return _ACTION_PRIORITY.get(action, 50)


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class Reconciler:
    """The single level-triggered reconciliation pass (§5.1, §5.3).

    Construct once with a :class:`ReconcilerConfig`; call :meth:`reconcile`
    for each pass. The reconciler is stateless across passes — the
    :class:`GenerationStore` is passed in and mutated, but the reconciler
    itself holds no per-pass state. This makes idempotency (§5.3) trivial:
    the same ``(items, world, store)`` always yields the same result.
    """

    def __init__(self, config: ReconcilerConfig) -> None:
        self._config = config
        self._admission = AdmissionController(wip_ceiling=config.wip_ceiling)

    def reconcile(
        self,
        items: list[Item],
        world: ObservedWorld,
        store: GenerationStore,
    ) -> ReconcilerResult:
        """Run one reconciliation pass over ``items``.

        Returns a :class:`ReconcilerResult` with per-item dispositions and
        aggregate counts. Mutates ``store`` by recording each evaluated
        item's generation fingerprint (§5.5).
        """
        graph = DependencyGraph(items, world)
        results: list[ItemDisposition] = []

        for item in items:
            skipped = should_skip(
                item, store, current_generation=self._config.generation
            )

            disposition = compute_disposition(item, graph, world)

            # §5.5: record the evaluation fingerprint for this cycle. Even
            # skipped items get recorded so the next cycle can skip them too.
            record_evaluation(
                item, store, generation=self._config.generation
            )

            # Admission gate: an ACT-create-PR disposition is downgraded to
            # BLOCKED if the WIP ceiling is saturated (§4). The issue is
            # unblocked but the queue is full — that is a queue constraint,
            # not an issue dependency, so it is surfaced as BLOCKED with a
            # clear reason rather than silently dropped.
            admission_reason: Optional[str] = None
            if (
                disposition.kind is DispositionKind.ACT
                and disposition.action == "create_pr"
                and item.kind is ItemKind.ISSUE
            ):
                decision = self._admission.can_admit(item, items, world)
                if not decision.admitted:
                    disposition = Disposition(
                        kind=DispositionKind.BLOCKED,
                        reason=f"admission denied: {decision.reason}",
                    )
                    admission_reason = decision.reason

            results.append(
                ItemDisposition(
                    item_id=item.id,
                    disposition=disposition,
                    skipped=skipped,
                    admission_reason=admission_reason,
                )
            )

        # Order the ACT dispositions by priority for the harness. We keep
        # the input order in `results` (so per-item lookup is stable) but
        # expose a priority-sorted view via ordered_actions.
        ordered = sorted(
            (r for r in results if r.disposition.kind is DispositionKind.ACT),
            key=lambda r: _action_priority(r.disposition.action),
        )

        return self._build_result(results, ordered)

    def _build_result(
        self,
        results: list[ItemDisposition],
        ordered: list[ItemDisposition],
    ) -> ReconcilerResult:
        """Tally aggregate counts and assemble the result (§7)."""
        acted = blocked = terminal = undecidable = skipped = 0
        admitted = admission_denied = 0

        for r in results:
            if r.skipped:
                skipped += 1
            kind = r.disposition.kind
            if kind is DispositionKind.ACT:
                acted += 1
                if r.admission_reason is None and r.disposition.action == "create_pr":
                    admitted += 1
            elif kind is DispositionKind.BLOCKED:
                blocked += 1
                if r.admission_reason is not None:
                    admission_denied += 1
            elif kind is DispositionKind.TERMINAL:
                terminal += 1
            elif kind is DispositionKind.UNDECIDABLE:
                undecidable += 1

        return ReconcilerResult(
            items=results,
            generation=self._config.generation,
            enumerated=len(results),
            acted=acted,
            blocked=blocked,
            terminal=terminal,
            undecidable=undecidable,
            skipped=skipped,
            admitted=admitted,
            admission_denied=admission_denied,
        )

    @staticmethod
    def ordered_actions(result: ReconcilerResult) -> list[ItemDisposition]:
        """Return the ACT dispositions in priority order for the harness."""
        return sorted(
            (r for r in result.items if r.disposition.kind is DispositionKind.ACT),
            key=lambda r: _action_priority(r.disposition.action),
        )
