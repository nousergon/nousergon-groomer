"""Observed-generation skip — the §5.5 efficiency optimization.

§3 mandates that blocked-ness is *re-derived* every cycle from declared
dependencies against an :class:`ObservedWorld`. Re-evaluating ~200 items
3×/day is wasteful when most have not changed. The naive optimization —
cache dispositions in labels and trust them — re-introduces the §3.3
forbidden trust (a cached label is an asserted status, not a derived one).

The SOTA answer is the Kubernetes ``observedGeneration`` pattern: record
the *fingerprint of the inputs* an item was last evaluated against, and
skip re-evaluation only when that fingerprint still matches. The skip is
an optimization of the **evaluation**, never a substitute for re-derivation.
A consumer that trusts a cached disposition label without re-deriving
violates §3.3.

This module records per-item input fingerprints and answers two questions:

- :func:`should_skip` — have the item's inputs changed since last evaluation?
- :func:`record_evaluation` — persist the current input fingerprint.

The fingerprint is composed of:

- ``label_set_hash`` — sorted label set (§5.5).
- ``deps_hash`` — declared dependency tokens (kind:target), sorted (§5.5).
- ``head_sha`` — the GitHub-side head SHA of a PR (a force-push changes it
  even when labels and deps are unchanged). ``None`` for issues.
- ``body_hash`` — a hash of the issue/PR body text (a body edit can change
  the disposition without touching labels or deps).

The store is **status**, kept separate from the spec (§3.3): it lives in
:class:`GenerationStore`, never on the :class:`Item` itself. An
:class:`ObservedGeneration` record is the persisted status; the
:class:`Item` carries only the spec.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from pydantic import BaseModel

from .models import Disposition, Item, ObservedGeneration

__all__ = [
    "GenerationStore",
    "InputFingerprint",
    "should_skip",
    "record_evaluation",
    "compute_fingerprint",
]


# ---------------------------------------------------------------------------
# Input fingerprint — the §5.5 skip token
# ---------------------------------------------------------------------------


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class InputFingerprint(BaseModel):
    """The fingerprint of an item's inputs at a point in time.

    Two fingerprints with equal ``label_set_hash``, ``deps_hash``,
    ``head_sha``, and ``body_hash`` describe identical inputs — the
    disposition computed against one is valid against the other. The
    reconciler may skip re-evaluation in that case (§5.5).
    """

    label_set_hash: str
    deps_hash: str
    head_sha: Optional[str] = None
    body_hash: Optional[str] = None
    closure_hash: Optional[str] = None

    def matches(self, other: InputFingerprint) -> bool:
        """True iff every component matches, with one asymmetry.

        ``closure_hash`` is ``None`` on records written before it existed and
        on callers with no dependency graph. **A ``None`` on either side never
        matches**, even against another ``None``: a record with no closure was
        written by logic that could not see the world moving underneath it, so
        honouring it as a match would grant exactly the unsound skip the field
        was added to prevent. The cost is one non-skipped cycle per item at
        upgrade; the alternative is a stale disposition that never re-derives.
        """
        if self.closure_hash is None or other.closure_hash is None:
            return False
        return (
            self.label_set_hash == other.label_set_hash
            and self.deps_hash == other.deps_hash
            and self.head_sha == other.head_sha
            and self.body_hash == other.body_hash
            and self.closure_hash == other.closure_hash
        )


def compute_fingerprint(
    item: Item,
    head_sha: Optional[str] = None,
    body: Optional[str] = None,
    closure_state: Optional[list[str]] = None,
) -> InputFingerprint:
    """Compute the current input fingerprint for ``item``.

    ``head_sha`` and ``body`` are observed facts from the harness snapshot.
    They default to ``None`` (e.g. for issues that have no head SHA); a
    ``None`` component matches only another ``None``.

    ``closure_state`` is :meth:`DependencyGraph.closure_state` for this item —
    every transitively reachable dependency with its observed state. Omitting
    it produces a fingerprint that can never match (see
    :meth:`InputFingerprint.matches`), which is the safe direction: a caller
    that cannot describe the world does not get to skip on the strength of it.
    """
    return InputFingerprint(
        label_set_hash=item.label_set_hash,
        deps_hash=item.deps_hash,
        head_sha=head_sha,
        body_hash=_sha(body) if body is not None else None,
        closure_hash=_sha("\n".join(closure_state)) if closure_state is not None else None,
    )


# ---------------------------------------------------------------------------
# GenerationStore — the persisted status (§3.3 separation)
# ---------------------------------------------------------------------------


class GenerationStore:
    """A per-item record of the last-evaluated input fingerprint (§5.5).

    This is **status**, kept separate from the spec (§3.3): the store holds
    what was observed and evaluated, never what was declared. The store is
    in-memory by default; a persistent backend (S3/SSM) is the private
    harness's concern, not the core's.
    """

    def __init__(self) -> None:
        self._records: dict[str, ObservedGeneration] = {}

    def get(self, item_id: str) -> Optional[ObservedGeneration]:
        """Return the last-recorded generation for ``item_id``, or None."""
        return self._records.get(item_id)

    def put(self, item_id: str, generation: ObservedGeneration) -> None:
        self._records[item_id] = generation

    def __contains__(self, item_id: str) -> bool:
        return item_id in self._records

    def __len__(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# Skip logic
# ---------------------------------------------------------------------------


def should_skip(
    item: Item,
    store: GenerationStore,
    *,
    head_sha: Optional[str] = None,
    body: Optional[str] = None,
    closure_state: Optional[list[str]] = None,
    current_generation: int = 0,
) -> bool:
    """True if the item's inputs have not changed since last evaluation (§5.5).

    Returns ``False`` (do NOT skip) when:

    - the item has no recorded generation yet (first evaluation), or
    - any fingerprint component has changed.

    Returns ``True`` (skip is safe) only when the recorded fingerprint
    matches the current fingerprint. The skip is an optimization of the
    evaluation; the reconciler must still re-derive the disposition for
    any item it does NOT skip, and must never trust a cached disposition
    label for a skipped item (§3.3).

    ``current_generation`` is the reconciler's monotonically increasing
    cycle counter. It is used for idempotency within a cycle: if the
    recorded generation equals it, the item was already evaluated this
    cycle — skip. Across cycles, the fingerprint (not the counter) decides:
    an unchanged item skips regardless of the counter value.
    """
    recorded = store.get(item.id)
    if recorded is None:
        return False  # first evaluation — must evaluate

    # Idempotency within a cycle: if already evaluated this cycle, skip.
    if current_generation > 0 and recorded.generation == current_generation:
        return True

    current_fp = compute_fingerprint(
        item, head_sha=head_sha, body=body, closure_state=closure_state
    )
    recorded_fp = InputFingerprint(
        label_set_hash=recorded.label_set_hash,
        deps_hash=recorded.deps_hash,
        head_sha=recorded.head_sha,
        body_hash=recorded.body_hash,
        closure_hash=recorded.closure_hash,
    )
    return current_fp.matches(recorded_fp)


def satisfied_tokens_of(closure_state: list[str]) -> list[str]:
    """The ``"<kind>:<target>"`` tokens observed satisfied in ``closure_state``."""
    return sorted(
        entry.rsplit("=", 1)[0]
        for entry in closure_state
        if entry.endswith("=satisfied")
    )


def newly_satisfied(
    closure_state: list[str], previous: Optional[ObservedGeneration]
) -> list[str]:
    """Dependency tokens that are satisfied now and were **not** last cycle (§3.6).

    A transition, not a state. Returns ``"<kind>:<target>"`` tokens so the
    caller can stamp each with the moment it observed the flip.

    Two cases yield nothing, both deliberately:

    - **No prior record.** The transition happened at an unknown time before
      the loop was watching. Dating it to first-observation would fabricate a
      latency measurement; declining is the honest answer, and F7 undercounts
      by the items alive at rollout rather than reporting a wrong number.
    - **A prior record written without a closure** (``closure_hash is None``) —
      the pre-upgrade shape. Same reasoning: that writer could not observe
      dependency states at all, so its silence is *unknown*, and treating it as
      *nothing was satisfied* would stamp every long-satisfied dependency on
      the first cycle after upgrade.

    ``closure_hash`` is the marker for the second case, **not** an empty
    ``satisfied_tokens``. The two are easy to conflate and the difference is
    the whole measurement: a record written *with* a closure in which nothing
    was satisfied is precisely the "before" side of a transition, and skipping
    it would mean no transition is ever detected at all.
    """
    if previous is None or previous.closure_hash is None:
        return []
    return sorted(set(satisfied_tokens_of(closure_state)) - set(previous.satisfied_tokens))


def record_evaluation(
    item: Item,
    store: GenerationStore,
    *,
    generation: int,
    head_sha: Optional[str] = None,
    body: Optional[str] = None,
    closure_state: Optional[list[str]] = None,
    disposition: Optional[Disposition] = None,
    observed_at: Optional[str] = None,
) -> ObservedGeneration:
    """Persist the current input fingerprint into ``store`` after a cycle.

    Called by the reconciler after it has evaluated (or skipped) ``item``
    at ``generation``. Returns the persisted :class:`ObservedGeneration`.

    ``observed_at`` is the caller's timestamp for this cycle, used to stamp
    dependencies observed satisfied for the first time (§3.6). It is a
    parameter rather than a clock read because the reconciler must remain a
    pure function of its inputs (§5.4) — a hidden clock would make two
    identical cycles produce different records, and the determinism tests
    would be the first casualty.

    Previously-recorded satisfaction timestamps are **carried forward**. The
    moment a dependency was first observed satisfied is the measurement;
    re-stamping it every cycle would overwrite the only copy of it with the
    current time and quietly destroy F7's detection latency.
    """
    fp = compute_fingerprint(
        item, head_sha=head_sha, body=body, closure_state=closure_state
    )
    previous = store.get(item.id)
    satisfied_at: dict[str, str] = dict(previous.dependency_satisfied_at) if previous else {}
    if closure_state is not None and observed_at is not None:
        for token in newly_satisfied(closure_state, previous):
            satisfied_at.setdefault(token, observed_at)
    record = ObservedGeneration(
        item_id=item.id,
        generation=generation,
        label_set_hash=fp.label_set_hash,
        deps_hash=fp.deps_hash,
        head_sha=fp.head_sha,
        body_hash=fp.body_hash,
        closure_hash=fp.closure_hash,
        disposition_kind=disposition.kind.value if disposition is not None else None,
        disposition_reason=disposition.reason if disposition is not None else None,
        disposition_action=disposition.action if disposition is not None else None,
        dependency_satisfied_at=satisfied_at,
        satisfied_tokens=(
            satisfied_tokens_of(closure_state) if closure_state is not None else []
        ),
    )
    store.put(item.id, record)
    return record
