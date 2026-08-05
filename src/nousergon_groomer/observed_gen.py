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
from collections.abc import Mapping
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel

from .models import Disposition, Item, ItemStage, ObservedGeneration

__all__ = [
    "GenerationStore",
    "GenerationStoreProtocol",
    "PersistentGenerationStore",
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
# The store interface — the substitutability seam (`principles.md` §2.8)
# ---------------------------------------------------------------------------


@runtime_checkable
class GenerationStoreProtocol(Protocol):
    """The store surface the **reconciler** depends on — and nothing more.

    Declared as a :class:`typing.Protocol` rather than left implicit in the
    concrete in-memory class so a persistent backend is written against a
    stated contract instead of against another class's internals. Before this
    existed the only way to write one was to subclass :class:`GenerationStore`
    and populate its private ``_records`` dict — which makes a private
    attribute of this module the de-facto extension point, and means any change
    to it silently breaks a backend the core cannot see. Structural typing also
    means a backend need not import or inherit anything from here to conform,
    which is what "address the backend through an adapter, never an SDK client
    at the call site" actually requires.

    Deliberately narrow. The reconciler reads and writes per-item records; it
    knows nothing about loading, flushing, bucket names or credentials. A
    backend that needs those declares :class:`PersistentGenerationStore`
    instead, which the *harness* depends on — the split keeps the core unable
    to accidentally grow a dependency on persistence.

    Use ``isinstance``, never ``issubclass``: a runtime-checkable protocol
    carrying non-method members rejects ``issubclass`` by design.
    """

    def get(self, item_id: str) -> Optional[ObservedGeneration]:
        """The last-recorded generation for ``item_id``, or ``None``.

        ``None`` MUST mean "no record", never "unreadable". A backend that
        cannot reach its storage raises; returning ``None`` there would make
        every item read as new, which silently re-derives the whole fleet.
        """
        ...

    def put(self, item_id: str, generation: ObservedGeneration) -> None:
        """Record ``generation`` for ``item_id``, replacing any prior record."""
        ...

    def __contains__(self, item_id: str) -> bool: ...

    def __len__(self) -> int: ...


@runtime_checkable
class PersistentGenerationStore(GenerationStoreProtocol, Protocol):
    """A store that survives the process — the surface a **harness** needs.

    The reconciler never sees this. A dispatcher does: it constructs the
    backend, reports what was loaded, and flushes once at the end of a cycle.
    Stating it here rather than leaving each harness to invent its own shape is
    what keeps two harnesses (and a future SSM or DynamoDB backend) mutually
    substitutable.

    Implementations MUST satisfy:

    - **Fail loud on an unreachable store.** Construction/load raises rather
      than degrading to empty. An empty store means *every item is new*; a
      cycle that proceeds on that re-derives the whole fleet and re-opens
      disposed work. Only a *proven* absence (a 404 on a genuine first run) may
      start empty.
    - **Interruption leaves the store consistent** (§5.3). Interruption is the
      normal case on spot. A reader must never observe a half-written store.
    - **An idle cycle writes nothing.** :meth:`flush` returns ``False`` when
      nothing was recorded, and MUST NOT touch the backing object — otherwise
      the freshness signal reports health for a loop that did no work, which is
      the §6.2 failure of rendering absence as green.
    """

    def flush(self, *, written_at: Optional[str] = None) -> bool:
        """Persist the store. ``True`` iff something was actually written.

        The return value is load-bearing, not informational: a cycle that
        recorded dispositions and got ``False`` back has silently lost its
        memory, and the caller is expected to fail loud on that.
        """
        ...

    @property
    def uri(self) -> str:
        """Where this store lives, for logs and alarms (e.g. an ``s3://`` URI)."""
        ...

    @property
    def loaded_count(self) -> int:
        """How many records were read at construction.

        Zero on a genuine first run. Reported so a store that quietly stopped
        loading shows up as a step change rather than as a slow loss of skips.
        """
        ...

    @property
    def loaded_at(self) -> Optional[str]:
        """When the loaded state was written, or ``None`` if nothing loaded."""
        ...


# ---------------------------------------------------------------------------
# GenerationStore — the persisted status (§3.3 separation)
# ---------------------------------------------------------------------------


class GenerationStore:
    """The in-memory :class:`GenerationStoreProtocol` — the core's default.

    This is **status**, kept separate from the spec (§3.3): the store holds
    what was observed and evaluated, never what was declared. The store is
    in-memory by default; a persistent backend (S3/SSM) is the private
    harness's concern, not the core's.

    It is deliberately **not** a :class:`PersistentGenerationStore`: it does not
    survive the process, and claiming the persistent surface with a no-op
    ``flush`` would let a harness wire the wrong store and see a green,
    silent, amnesiac loop. A backend declares that protocol; this one does not.
    """

    def __init__(self, records: Optional[Mapping[str, ObservedGeneration]] = None) -> None:
        self._records: dict[str, ObservedGeneration] = dict(records or {})

    def get(self, item_id: str) -> Optional[ObservedGeneration]:
        """Return the last-recorded generation for ``item_id``, or None."""
        return self._records.get(item_id)

    def put(self, item_id: str, generation: ObservedGeneration) -> None:
        self._records[item_id] = generation

    def __contains__(self, item_id: str) -> bool:
        return item_id in self._records

    def __len__(self) -> int:
        return len(self._records)

    # -- extension points for a persistent backend -------------------------
    #
    # A backend subclassing this class needs to bulk-load what it read and to
    # serialise what it holds. Both used to require reaching into ``_records``,
    # which made a private attribute the real interface. These are that
    # interface, stated.

    def load_records(self, records: Mapping[str, ObservedGeneration]) -> None:
        """Replace the store's contents wholesale with ``records``.

        For a backend populating itself from durable storage at construction.
        Deliberately a *replace*, not a merge: a load that merged into whatever
        was already held could not distinguish "the object was empty" from
        "the object failed to parse and we kept stale state", and it would not
        go through :meth:`put`, whose override in a backend is what marks the
        store dirty — loading is not a write, and must not schedule one.
        """
        self._records = dict(records)

    def snapshot(self) -> dict[str, ObservedGeneration]:
        """A copy of every record held, for serialisation.

        A copy, so a backend cannot hand its live mapping to a serialiser and
        have it mutate underneath.
        """
        return dict(self._records)


# ---------------------------------------------------------------------------
# Skip logic
# ---------------------------------------------------------------------------


def should_skip(
    item: Item,
    store: GenerationStoreProtocol,
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
    store: GenerationStoreProtocol,
    *,
    generation: int,
    head_sha: Optional[str] = None,
    body: Optional[str] = None,
    closure_state: Optional[list[str]] = None,
    disposition: Optional[Disposition] = None,
    stage: Optional[ItemStage] = None,
    observed_at: Optional[str] = None,
) -> ObservedGeneration:
    """Persist the current input fingerprint into ``store`` after a cycle.

    Called by the reconciler after it has evaluated (or skipped) ``item``
    at ``generation``. Returns the persisted :class:`ObservedGeneration`.

    ``observed_at`` is the caller's timestamp for this cycle, used to stamp
    dependencies observed satisfied for the first time (§3.6) and stages
    entered for the first time (§3). It is a parameter rather than a clock read
    because the reconciler must remain a pure function of its inputs (§5.4) — a
    hidden clock would make two identical cycles produce different records, and
    the determinism tests would be the first casualty.

    ``stage`` is the item's **effective** stage this cycle. Passing it is what
    makes F7 (lead time, intent → verified) and F6 (residence) computable:
    both are differences between entries in ``stage_entered_at``, and nothing
    else in the system records when an item entered a stage that is derived
    rather than stored.

    Previously-recorded satisfaction and stage-entry timestamps are **carried
    forward**. The moment a dependency was first observed satisfied, and the
    moment an item first reached a stage, are the measurements; re-stamping
    either every cycle would overwrite the only copy of it with the current
    time and quietly destroy F7 — every latency would read as zero.
    """
    fp = compute_fingerprint(
        item, head_sha=head_sha, body=body, closure_state=closure_state
    )
    previous = store.get(item.id)
    satisfied_at: dict[str, str] = dict(previous.dependency_satisfied_at) if previous else {}
    if closure_state is not None and observed_at is not None:
        for token in newly_satisfied(closure_state, previous):
            satisfied_at.setdefault(token, observed_at)

    # First entry wins. `setdefault`, not assignment: an item can regress —
    # a closed change sends it from `in_flight` back to `ready` — and lead
    # time is measured from the original intent, not the latest attempt.
    # Re-stamping would silently shorten exactly the measurements that had a
    # setback, which is the population F7 exists to expose.
    stage_entered_at: dict[str, str] = dict(previous.stage_entered_at) if previous else {}
    if stage is not None and observed_at is not None:
        stage_entered_at.setdefault(stage.value, observed_at)

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
        disposition_blocking_chain=(
            list(disposition.blocking_chain) if disposition is not None else []
        ),
        dependency_satisfied_at=satisfied_at,
        satisfied_tokens=(
            satisfied_tokens_of(closure_state) if closure_state is not None else []
        ),
        # A caller that did not supply a stage carries the previous one forward
        # rather than blanking it: "this writer did not say" is not "the item
        # has no stage", and overwriting with None would destroy the only
        # record of where it was.
        stage=stage.value if stage is not None else (previous.stage if previous else None),
        stage_entered_at=stage_entered_at,
    )
    store.put(item.id, record)
    return record
