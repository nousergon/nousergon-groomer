"""Domain model for the groomer core (§3, §3.1, §3.3, §5.5).

This module is pure data: it records what was observed and what was declared,
never what should be done. Blocked-ness is never an asserted field on an
``Item`` — it is derived (§3) by :mod:`dependency_evaluator` from the declared
dependencies against an :class:`ObservedWorld`. Declaration and evaluated
status are kept as separate types (§3.3): ``Dependency`` is the spec written
by an author, ``DependencyEvaluation`` is the status produced by evaluating
that spec against the world.
"""
from __future__ import annotations

import enum
import hashlib
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ItemKind(str, enum.Enum):
    """The two carried item kinds. The reconciler enumerates both (§5.1)."""

    ISSUE = "issue"
    PR = "pr"


class ItemState(str, enum.Enum):
    """Observed lifecycle states of a carried item.

    This enumeration is the **totality surface** for §5.1: the disposition
    function must map every value here to exactly one disposition. An
    uncovered state is a defect in the function, not an acceptable gap.

    The detailed states distinguish PR sub-states (green/red/dirty/draft/
    pending) and issue sub-states (actionable/blocked/waiting) because the
    disposition function's action depends on which — a red PR gets
    "fix CI", a dirty PR gets "resolve conflicts", a green PR gets
    "automerge or human review". Collapsing these into a single ``OPEN``
    would force the disposition function to re-derive the sub-state from
    other fields, which is exactly the §1.1 defect (mechanism leaking
    into the spec).
    """

    OPEN_CLEAN_GREEN = "open_clean_green"
    OPEN_RED_CI = "open_red_ci"
    OPEN_DIRTY = "open_dirty"
    OPEN_DRAFT = "open_draft"
    OPEN_PENDING_CI = "open_pending_ci"
    OPEN_ISSUE_ACTIONABLE = "open_issue_actionable"
    OPEN_ISSUE_BLOCKED = "open_issue_blocked"
    OPEN_ISSUE_WAITING = "open_issue_waiting"
    MERGED = "merged"
    CLOSED = "closed"
    DO_NOT_GROOM = "do_not_groom"


class DependencyKind(str, enum.Enum):
    """The kinds of external conditions a dependency may target.

    ``issue_terminal`` / ``pr_terminal`` point at *other carried items* and
    are followed transitively by :mod:`dependency_graph` (§3.4). Every other
    kind is an external leaf condition resolved against :class:`ObservedWorld`.
    """

    S3_OBJECT = "s3_object"
    S3_PREFIX = "s3_prefix"
    ISSUE_TERMINAL = "issue_terminal"
    PR_TERMINAL = "pr_terminal"
    PIPELINE_RUN = "pipeline_run"
    DATE = "date"
    LABEL_ABSENT = "label_absent"


class DispositionKind(str, enum.Enum):
    """The four dispositions the reconciler may emit for an item (§5.1).

    Exactly one of these is produced for every ``ItemState``. ``ACT`` carries
    a concrete action; ``BLOCKED`` names the blocking chain; ``TERMINAL`` is
    final; ``UNDECIDABLE`` requires a reason and is never silently coerced.
    """

    ACT = "act"
    BLOCKED = "blocked"
    TERMINAL = "terminal"
    UNDECIDABLE = "undecidable"


# ---------------------------------------------------------------------------
# Spec layer (§3.3) — what an author declared
# ---------------------------------------------------------------------------


class Dependency(BaseModel):
    """A declared dependency — the *spec* (§3.3).

    A dependency says "this item cannot be acted on until ``target`` of
    ``kind`` is satisfied." It is a declaration, not an evaluation: it
    carries no ``satisfied`` flag. Validation at the write boundary (§3.1)
    rejects empty/whitespace targets here, at construction, so a recorded
    dependency can never be silently unevaluable due to a missing target.
    """

    kind: DependencyKind
    target: str

    @field_validator("target")
    @classmethod
    def _target_nonempty(cls, v: str) -> str:
        if v is None or not str(v).strip():
            raise ValueError(
                "Dependency.target must be a non-empty string (§3.1: "
                "dependencies are validated at the write boundary)"
            )
        return v


# ---------------------------------------------------------------------------
# Status layer (§3.3) — what the evaluator produced
# ---------------------------------------------------------------------------


class DependencyEvaluation(BaseModel):
    """The evaluated status of a single dependency against the world (§3.3).

    Kept as a distinct type from :class:`Dependency` so spec and status can
    never be confused. ``undecidable`` is an explicit third state: the world
    did not provide the information needed to decide, which is *not* the same
    as "satisfied is False". Callers must not collapse ``undecidable`` into
    ``satisfied`` (fail loud).
    """

    dependency: Dependency
    satisfied: bool
    undecidable: bool = False
    reason: str = ""


class ObservedGeneration(BaseModel):
    """§5.5 skip token — the last-evaluated fingerprint of an item.

    Two items with equal :attr:`label_set_hash` and :attr:`deps_hash` have
    no declared-dependency or label change since ``generation`` was recorded,
    so the reconciler may skip re-evaluating them. ``generation`` is a
    monotonically increasing counter owned by the reconciler (the cycle
    number); it is used for idempotency within a cycle, not to force
    re-evaluation across cycles.

    ``head_sha`` and ``body_hash`` extend the fingerprint to cover PR
    force-pushes and body edits — a force-push changes the head SHA without
    touching labels or deps, and a body edit can change the disposition
    without touching deps. Both are ``None`` for items where they don't
    apply (e.g. issues have no head SHA).
    """

    item_id: str
    generation: int
    label_set_hash: str
    deps_hash: str
    head_sha: Optional[str] = None
    body_hash: Optional[str] = None

    #: Hash of the item's whole transitive dependency closure, each edge
    #: rendered with its **observed** state (see
    #: :meth:`DependencyGraph.closure_state`).
    #:
    #: ``deps_hash`` above covers what the item *declares*; this covers what
    #: the world currently *says* about everything it declares, transitively.
    #: The distinction decides whether the skip is sound: a blocked item's own
    #: declarations never change, so a token without this field would skip the
    #: one population that must always be re-evaluated (§3.4, §5.5).
    #:
    #: ``None`` means the caller did not supply a closure — a record written
    #: before this field existed, or a caller with no graph. A ``None`` on
    #: either side forces re-evaluation rather than matching, so an upgrade
    #: costs one non-skipped cycle and never a wrong skip.
    closure_hash: Optional[str] = None

    #: The disposition this item was last evaluated to, so a skipped item can
    #: report what it resolved to rather than a hole. Without it, a live skip
    #: would have to either recompute (defeating the skip) or emit nothing
    #: (making a skipped item indistinguishable from an unprocessed one).
    #:
    #: All three fields are stored, not just the kind: :class:`Disposition`
    #: enforces that ``ACT`` carries an action and ``UNDECIDABLE`` carries a
    #: reason, so a record holding the kind alone cannot reconstruct a valid
    #: one — it could only fabricate a placeholder that the model would reject
    #: or, worse, accept as a different verdict.
    disposition_kind: Optional[str] = None
    disposition_reason: Optional[str] = None
    disposition_action: Optional[str] = None

    #: ``"<kind>:<target>"`` → ISO-8601 timestamp at which that dependency was
    #: **first observed satisfied** (§3.6).
    #:
    #: This is the term §2.7 names as the one nothing records: F3's 24-hour
    #: clock starts when an item becomes unblocked, and F7's detection latency
    #: runs from dependency-satisfied to disposition. Neither is computable
    #: from GitHub, because no surface stores the moment a condition flipped —
    #: only the loop observing it can know, and only if it writes it down.
    #:
    #: Timestamps are **supplied by the caller**, never read from a clock in
    #: here: the reconciler is required to be a pure function of
    #: ``(config, items, world, store)`` (§5.4), and a hidden clock would make
    #: two identical inputs produce different records.
    dependency_satisfied_at: dict[str, str] = Field(default_factory=dict)

    #: The ``"<kind>:<target>"`` tokens observed **satisfied** at this
    #: evaluation. Kept so the next cycle can compute the *transition* rather
    #: than the state.
    #:
    #: Without it the only available comparison is against
    #: ``dependency_satisfied_at``'s keys, which cannot distinguish "satisfied
    #: for the first time just now" from "satisfied for weeks but never
    #: stamped because the first record predates this field". The second would
    #: be dated to the cycle that happened to notice it, fabricating a latency
    #: measurement instead of declining to make one.
    satisfied_tokens: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Disposition (§5.1) — the reconciler's verdict
# ---------------------------------------------------------------------------


class Disposition(BaseModel):
    """The reconciler's verdict for one item (§5.1).

    Invariants enforced here, at the model, so a malformed disposition can
    never be constructed:

    - ``UNDECIDABLE`` requires a ``reason`` (never silently coerce an
      undecidable item to ACT or BLOCKED).
    - ``ACT`` requires a concrete ``action`` (an ACT with no action is a
      no-op disguised as work — fail loud).
    """

    kind: DispositionKind
    reason: Optional[str] = None
    action: Optional[str] = None

    @model_validator(mode="after")
    def _check_invariants(self) -> Disposition:
        if self.kind is DispositionKind.UNDECIDABLE:
            if not (self.reason and self.reason.strip()):
                raise ValueError(
                    "Disposition(UNDECIDABLE) requires a non-empty reason "
                    "(§5.1: undecidable is never silently coerced)"
                )
        if self.kind is DispositionKind.ACT:
            if not (self.action and self.action.strip()):
                raise ValueError(
                    "Disposition(ACT) requires a non-empty action "
                    "(§5.1: an ACT disposition must name the concrete action)"
                )
        return self


# ---------------------------------------------------------------------------
# Item — the carried unit
# ---------------------------------------------------------------------------


def _hash_string(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Item(BaseModel):
    """A carried backlog item (issue or PR) as recorded from GitHub.

    Per §3, an ``Item`` carries its *declared* dependencies
    (:attr:`declared_dependencies`) and never an asserted ``blocked`` flag —
    blocked-ness is always derived by :func:`dependency_evaluator.is_item_blocked`
    against an :class:`ObservedWorld`. Per §5.5, an item may carry its last
    observed generation so the reconciler can skip unchanged items.

    Fields describing GitHub-side mergeability (``mergeable``, ``ci_green``,
    ``security_threads``) are observed facts recorded by the private harness
    when snapshotting; ``None`` means "GitHub has not reported a value yet",
    which the lane classifier treats as a failed Gate A check (cannot confirm
    clean) rather than as a pass.
    """

    id: str
    kind: ItemKind
    state: ItemState
    title: str = ""
    labels: list[str] = []
    declared_dependencies: list[Dependency] = []
    is_draft: bool = False
    human_owned: bool = False
    mergeable: Optional[bool] = None
    ci_green: Optional[bool] = None
    security_threads: int = 0
    observed_generation: Optional[ObservedGeneration] = None

    # -- derived properties ------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        """True if the item is in a final state (CLOSED, MERGED, or DO_NOT_GROOM)."""
        return self.state in (ItemState.CLOSED, ItemState.MERGED, ItemState.DO_NOT_GROOM)

    @property
    def has_gate_label(self) -> bool:
        """True if any label is in the ``gate:`` taxonomy namespace.

        The ``gate:`` *prefix* is the framework-level taxonomy grammar (see
        ``policy-gate-taxonomy``); the specific gate family after the colon is
        adapter data. This property only tests the prefix.
        """
        return any(label.startswith("gate:") for label in self.labels)

    @property
    def is_human_owned(self) -> bool:
        """True if the item is owned by a human rather than an agent.

        Set by the private harness when snapshotting (e.g. from the author or
        an ownership label); the core treats it as an observed fact.
        """
        return self.human_owned

    @property
    def label_set_hash(self) -> str:
        """Stable hash of the sorted label set — used by §5.5 skip logic."""
        return _hash_string("\n".join(sorted(self.labels)))

    @property
    def deps_hash(self) -> str:
        """Stable hash of the declared dependencies — used by §5.5 skip logic."""
        payload = "\n".join(
            f"{d.kind.value}:{d.target}" for d in sorted(
                self.declared_dependencies, key=lambda x: (x.kind.value, x.target)
            )
        )
        return _hash_string(payload)
