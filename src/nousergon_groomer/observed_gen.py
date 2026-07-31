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

from .models import Item, ObservedGeneration

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

    def matches(self, other: InputFingerprint) -> bool:
        """True iff every component matches (None == None for optional fields)."""
        return (
            self.label_set_hash == other.label_set_hash
            and self.deps_hash == other.deps_hash
            and self.head_sha == other.head_sha
            and self.body_hash == other.body_hash
        )


def compute_fingerprint(item: Item, head_sha: Optional[str] = None, body: Optional[str] = None) -> InputFingerprint:
    """Compute the current input fingerprint for ``item``.

    ``head_sha`` and ``body`` are observed facts from the harness snapshot.
    They default to ``None`` (e.g. for issues that have no head SHA); a
    ``None`` component matches only another ``None``.
    """
    return InputFingerprint(
        label_set_hash=item.label_set_hash,
        deps_hash=item.deps_hash,
        head_sha=head_sha,
        body_hash=_sha(body) if body is not None else None,
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

    current_fp = compute_fingerprint(item, head_sha=head_sha, body=body)
    recorded_fp = InputFingerprint(
        label_set_hash=recorded.label_set_hash,
        deps_hash=recorded.deps_hash,
        head_sha=recorded.head_sha,
        body_hash=recorded.body_hash,
    )
    return current_fp.matches(recorded_fp)


def record_evaluation(
    item: Item,
    store: GenerationStore,
    *,
    generation: int,
    head_sha: Optional[str] = None,
    body: Optional[str] = None,
) -> ObservedGeneration:
    """Persist the current input fingerprint into ``store`` after a cycle.

    Called by the reconciler after it has evaluated (or skipped) ``item``
    at ``generation``. Returns the persisted :class:`ObservedGeneration`.
    """
    fp = compute_fingerprint(item, head_sha=head_sha, body=body)
    record = ObservedGeneration(
        item_id=item.id,
        generation=generation,
        label_set_hash=fp.label_set_hash,
        deps_hash=fp.deps_hash,
        head_sha=fp.head_sha,
        body_hash=fp.body_hash,
    )
    store.put(item.id, record)
    return record
