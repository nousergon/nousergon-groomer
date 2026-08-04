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

import datetime as _dt
import enum
import hashlib
import re
import warnings
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

#: The framework-level taxonomy prefix a GitHub label uses to *project* a
#: gated status for human readability (``policy-gate-taxonomy``).
#:
#: **The prefix is never read as state** (groom-sweep-policy §3.3: a label may
#: project status but is never a source of truth; consumers re-derive). The one
#: place the core still looks at it is
#: :attr:`Item.unrepresented_gate_labels`, which detects a projection that
#: *nothing declares* — a representation defect the disposition function
#: surfaces as UNDECIDABLE rather than resolving in either direction.
GATE_LABEL_PREFIX = "gate:"

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ItemStage(str, enum.Enum):
    """The stages one **unit of intended change** passes through (§3).

    There is one kind of item and it has stages. An issue and its pull request
    are not two populations the loop maintains in parallel — they are the same
    unit at two points in its life, and a pull request is the *rendering* of
    the :attr:`IN_FLIGHT` stage rather than a thing with a lifecycle of its own.

    Modelling one thing as two is the shared root of four separate defects the
    policy addresses individually: a branch conflict — a property of the
    in-flight stage — gets declared a dependency of the item and the item parks
    (§3.5); "draft" becomes a place to leave things rather than a stage the
    cycle must exit (§4.4); verification that can only happen after merge has
    nowhere to live, so it is held as a pre-merge gate that can never clear
    (§3.8); and the link between the two populations becomes something a sweep
    checks rather than an invariant (§5.6).

    Two of the six forward stages are **derived, never asserted**, and that is
    load-bearing rather than an implementation convenience:

    - :attr:`READY` is :attr:`PROPOSED` with no unsatisfied declared
      dependency. Storing readiness as a field would be asserting blocked-ness,
      which is precisely what §3 abolishes.
    - :attr:`VERIFIED` is :attr:`MERGED` with the item's post-merge
      verification obligation (§3.8) discharged.

    :func:`stage.effective_stage` computes both from the recorded stage and an
    observed world; :attr:`ABANDONED` is reachable from any stage.
    """

    PROPOSED = "proposed"
    READY = "ready"
    IN_FLIGHT = "in_flight"
    MERGED = "merged"
    VERIFIED = "verified"
    DONE = "done"
    ABANDONED = "abandoned"

    @property
    def is_terminal(self) -> bool:
        """True only for :attr:`DONE` and :attr:`ABANDONED`.

        **:attr:`MERGED` is deliberately not terminal** (§3.8). Terminal-at-merge
        is what leaves a post-merge verification obligation with nowhere to
        live, and F7 measures lead time to ``verified``, not to ``merged``.
        """
        return self in _TERMINAL_STAGES


#: The forward progression. ``ABANDONED`` is deliberately absent: it is
#: reachable from any stage rather than being a point on the line.
STAGE_ORDER: tuple[ItemStage, ...] = (
    ItemStage.PROPOSED,
    ItemStage.READY,
    ItemStage.IN_FLIGHT,
    ItemStage.MERGED,
    ItemStage.VERIFIED,
    ItemStage.DONE,
)

_TERMINAL_STAGES = frozenset({ItemStage.DONE, ItemStage.ABANDONED})


def can_transition(current: ItemStage, following: ItemStage) -> bool:
    """True iff ``current`` → ``following`` is a legal stage transition (§3).

    Forward along :data:`STAGE_ORDER`, or to :attr:`ItemStage.ABANDONED` from
    anywhere. Nothing leaves a terminal stage, and a stage never transitions to
    itself (that is residence, not a transition).

    Skipping forward is legal: an item whose change needs no post-merge
    verification goes ``merged`` → ``verified`` with nothing in between, and a
    harness that only ever observes an already-merged, already-closed item
    records ``done`` without having witnessed the intermediate stages.
    """
    if current is following:
        return False
    if current.is_terminal:
        return False
    if following is ItemStage.ABANDONED:
        return True
    return STAGE_ORDER.index(following) > STAGE_ORDER.index(current)


class ChangeCondition(str, enum.Enum):
    """The observed condition of the change rendering the in-flight stage.

    These are the pull-request sub-states of the pre-0.9.0 flat ``ItemState``,
    demoted from item states to **conditions of one stage**. The distinction is
    the whole point of §3: a conflicted change is not a state the item rests in
    — the item is ``in_flight`` and its condition is ``conflicted``, and the
    disposition is ``ACT`` because resolving it is inside the loop's own write
    authority (§3.5). The same holds for :attr:`CI_RED` and :attr:`DRAFT`.

    :attr:`CI_PENDING` is the one condition the loop cannot act out of, because
    the only thing that resolves it is time passing on a check it does not own.
    """

    DRAFT = "draft"
    CONFLICTED = "conflicted"
    CI_RED = "ci_red"
    CI_PENDING = "ci_pending"
    CLEAN = "clean"


class DependencyKind(str, enum.Enum):
    """The kinds of external conditions a dependency may target.

    ``issue_terminal`` / ``pr_terminal`` point at *other carried items* and
    are followed transitively by :mod:`dependency_graph` (§3.4). Every other
    kind is an external leaf condition resolved against :class:`ObservedWorld`.

    ``human`` (§3.7, alpha-engine-config#6311) names a condition satisfiable
    only by a named person acting — legal per §3.5's agency test (a person is
    outside the loop's own write authority) and, per §3.7, never legal as a
    bare label: the target always carries both the person and a due date.
    """

    S3_OBJECT = "s3_object"
    S3_PREFIX = "s3_prefix"
    ISSUE_TERMINAL = "issue_terminal"
    PR_TERMINAL = "pr_terminal"
    PIPELINE_RUN = "pipeline_run"
    DATE = "date"
    MILESTONE_REACHED = "milestone_reached"
    HUMAN = "human"


class DispositionKind(str, enum.Enum):
    """The four dispositions the reconciler may emit for an item (§5.1).

    Exactly one of these is produced for every ``ItemStage``. ``ACT`` carries
    a concrete action; ``BLOCKED`` names the blocking chain; ``TERMINAL`` is
    final; ``UNDECIDABLE`` requires a reason and is never silently coerced.
    """

    ACT = "act"
    BLOCKED = "blocked"
    TERMINAL = "terminal"
    UNDECIDABLE = "undecidable"


# ---------------------------------------------------------------------------
# Declaration chokepoint (§3.1 + §3.5) — parsing a raw predicate string
# ---------------------------------------------------------------------------
#
# A harness (the GitHub snapshot adapter, a `Verified-when:` body line, an
# issue custom field) hands the loop a raw string, not a typed {kind, target}
# pair. Nothing upstream of this module parsed that string before
# alpha-engine-config#6309 — it passed through as prose, which is exactly how
# `alpha-engine-research-PR549` carried an unparseable predicate in a
# `Verified-when` field with nothing rejecting it at authorship.
#
# The patterns below are a **closed allowlist of permitted subjects**
# (groom-sweep-policy §3.5's gotcha), not a denylist of forbidden ones: a
# raw string that names branch state, CI state, review state, label state or
# draft state has no pattern here to match, so it is rejected by omission
# rather than by a rule that has to name every forbidden shape and can be
# outrun by a new one. Every pattern maps onto a `DependencyKind` that
# already names a condition outside the loop's own write authority — see
# `passes_agency_test` below, which asserts that invariant explicitly rather
# than leaving it implicit in "the patterns happened to be written this way."
_S3_PREFIX_RE = re.compile(r"^s3://[^/\s]+/\S*/$")
_S3_OBJECT_RE = re.compile(r"^s3://[^/\s]+/\S*[^/\s]$")
_ISSUE_TERMINAL_RE = re.compile(r"^issue:(\S+/\S+#\d+)$")
_PR_TERMINAL_RE = re.compile(r"^pr:(\S+/\S+#\d+)$")
_PIPELINE_RUN_RE = re.compile(r"^pipeline_run:(\S+)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MILESTONE_RE = re.compile(r"^milestone:(\S+)$")
#: §3.7: a human dependency names the person AND a due date — never a bare
#: name. The person is a GitHub handle (bare or ``@``-prefixed) or a plain
#: name; the due date is the same ISO-8601 shape ``_DATE_RE`` already
#: validates for DATE dependencies. ``target`` retains both halves, joined by
#: the same ``:`` the other qualified kinds use, so :func:`human_owner` /
#: :func:`human_due_date` can split it back out.
_HUMAN_RE = re.compile(r"^human:(@?[\w.\-]+):(\d{4}-\d{2}-\d{2})$")


def _parse_declaration_text(raw: str) -> dict[str, Any]:
    """Parse a raw predicate string into a ``{kind, target}`` mapping (§3.1).

    Tried in order against the closed allowlist above; the first match wins.
    Raises ``ValueError`` — which pydantic wraps into ``ValidationError`` when
    this runs inside :meth:`Dependency.model_validate` — for anything that
    does not name one of the permitted subjects, including empty/whitespace
    input, a bare label name, an unqualified ``owner/repo#N`` (ambiguous
    between an issue and a PR, so not machine-evaluable as written), and
    prose such as ``"branch is not behind main"``: both that string and
    ``"s3://…/weekly-sf/"`` are machine-evaluable in the sense that a
    computer could check them, but only the second names a condition outside
    the loop's own write authority (§3.5's gotcha — the distinction the
    allowlist exists to enforce rather than a string-shape check).
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError(
            "empty dependency declaration (§3.1: a dependency must name a "
            "condition; nothing was declared)"
        )
    if _S3_PREFIX_RE.match(text):
        return {"kind": DependencyKind.S3_PREFIX, "target": text}
    if _S3_OBJECT_RE.match(text):
        return {"kind": DependencyKind.S3_OBJECT, "target": text}
    m = _ISSUE_TERMINAL_RE.match(text)
    if m:
        return {"kind": DependencyKind.ISSUE_TERMINAL, "target": m.group(1)}
    m = _PR_TERMINAL_RE.match(text)
    if m:
        return {"kind": DependencyKind.PR_TERMINAL, "target": m.group(1)}
    m = _PIPELINE_RUN_RE.match(text)
    if m:
        return {"kind": DependencyKind.PIPELINE_RUN, "target": m.group(1)}
    if _DATE_RE.match(text):
        return {"kind": DependencyKind.DATE, "target": text}
    m = _MILESTONE_RE.match(text)
    if m:
        return {"kind": DependencyKind.MILESTONE_REACHED, "target": m.group(1)}
    m = _HUMAN_RE.match(text)
    if m:
        return {"kind": DependencyKind.HUMAN, "target": f"{m.group(1)}:{m.group(2)}"}
    raise ValueError(
        f"dependency declaration {text!r} does not name a permitted subject "
        "(§3.1 + §3.5: only s3://bucket/key, s3://bucket/prefix/, "
        "issue:owner/repo#N, pr:owner/repo#N, pipeline_run:<id>, an "
        "ISO-8601 date, milestone:<id>, or human:<owner>:<YYYY-MM-DD> parse "
        "into an evaluable, externally-owned DependencyKind — branch, CI, "
        "review, label and draft state are inside the loop's own write "
        "authority and are never a dependency, so no pattern exists for "
        "them here; a human name with no due date is likewise rejected — "
        "§3.7 requires both halves)"
    )


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

    **The write boundary also accepts a raw predicate string** (§3.1
    deliverable, alpha-engine-config#6309): ``Dependency.model_validate(raw)``
    where ``raw`` is a bare ``str`` — as it arrives from a ``Verified-when:``
    body line or an issue custom field — parses it via
    :func:`_parse_declaration_text` before the field validators below run,
    and rejects it here, at declaration, if it does not parse into one of
    the closed allowlist's permitted subjects. Typed construction
    (``Dependency(kind=..., target=...)``) is unaffected — the raw-string
    path only fires when the *whole model* is handed a bare string rather
    than a mapping.
    """

    kind: DependencyKind
    target: str

    @model_validator(mode="before")
    @classmethod
    def _parse_raw_declaration(cls, data: Any) -> Any:
        if isinstance(data, str):
            return _parse_declaration_text(data)
        return data

    @field_validator("target")
    @classmethod
    def _target_nonempty(cls, v: str) -> str:
        if v is None or not str(v).strip():
            raise ValueError(
                "Dependency.target must be a non-empty string (§3.1: "
                "dependencies are validated at the write boundary)"
            )
        return v

    @model_validator(mode="after")
    def _check_agency(self) -> Dependency:
        # Defence in depth (§3.5: "both [evaluability and agency] are checked
        # at declaration"): every kind reachable from the raw-string parser
        # above is already agency-safe by construction, so this can never
        # fire via that path. It fires for a *typed* construction
        # (``Dependency(kind=..., target=...)``) carrying a kind a future
        # change added to `DependencyKind` without extending
        # `_AGENCY_EXTERNAL_KINDS` to match — failing closed rather than
        # silently trusting a new kind as external.
        if not passes_agency_test(self):
            raise ValueError(
                f"DependencyKind {self.kind.value!r} is not in the agency-safe "
                "allowlist (§3.5: a dependency must name a condition outside "
                "the loop's own write authority) — extend "
                "_AGENCY_EXTERNAL_KINDS if this kind is genuinely external, "
                "or this construction is a §3.5 violation"
            )
        return self

    @model_validator(mode="after")
    def _check_human_shape(self) -> Dependency:
        """§3.7: a HUMAN dependency's target must split into a non-empty
        owner and a real ISO-8601 due date — enforced here too (not only in
        the raw-string parser above) so a *typed* construction
        (``Dependency(kind=DependencyKind.HUMAN, target=...)``) gets the same
        write-boundary guarantee rather than only the string-declaration
        path. §3.6 forbids an open-ended wait; a due date that fails to parse
        is the same defect as an absent one.
        """
        if self.kind is not DependencyKind.HUMAN:
            return self
        parts = self.target.split(":", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ValueError(
                f"HUMAN dependency target {self.target!r} must be "
                "'<owner>:<YYYY-MM-DD>' — §3.7 requires both the person's "
                "name and a due date, never a bare name"
            )
        owner, due = parts
        try:
            _dt.date.fromisoformat(due.strip())
        except ValueError as exc:
            raise ValueError(
                f"HUMAN dependency due date {due!r} is not an ISO-8601 date "
                f"(YYYY-MM-DD): {exc}"
            ) from exc
        return self


def parse_dependency_declaration(raw: str) -> Dependency:
    """Parse a raw predicate string into a validated :class:`Dependency`.

    The named entry point for the §3.1 write-boundary chokepoint
    (alpha-engine-config#6309): a harness translating a ``Verified-when:``
    body line or an issue custom field into a declared dependency calls this
    rather than constructing ``Dependency`` from an already-split
    ``{kind, target}`` pair. Thin wrapper over
    ``Dependency.model_validate(raw)`` — kept as a function so the chokepoint
    has one discoverable name rather than requiring every caller to know the
    raw-string construction path is even supported.

    Raises ``pydantic.ValidationError`` (via :func:`_parse_declaration_text`)
    if ``raw`` does not parse into one of the closed allowlist's permitted
    subjects.
    """
    return Dependency.model_validate(raw)


#: Declared-dependency kinds naming a condition genuinely outside the loop's
#: own write authority (§3.5's agency test) — a closed **allowlist**, not a
#: denylist of forbidden subjects, per the gotcha in alpha-engine-config#6309:
#: a new `DependencyKind` added without also being added here fails closed
#: (`passes_agency_test` returns ``False`` for it) rather than being silently
#: trusted as external.
#:
#: Deliberately an **explicit literal set**, not ``frozenset(DependencyKind)``
#: — the whole point is that this list does *not* grow automatically when a
#: new member is added to the enum. Every kind `DependencyKind` currently
#: defines is external by construction (there is deliberately no kind for
#: branch, CI, review, label or draft state: those are `ChangeCondition`
#: values, reconciled by `disposition.py` as `act`, never rested on), so this
#: set equals `set(DependencyKind)` today, but a future kind added to the
#: enum without a matching addition *here* is agency-unsafe by default.
#: `test_models.py::test_agency_allowlist_covers_every_dependency_kind` pins
#: today's equality so it goes red — not silently passes — the moment
#: `DependencyKind` gains a member this set does not also gain.
_AGENCY_EXTERNAL_KINDS: frozenset[DependencyKind] = frozenset({
    DependencyKind.S3_OBJECT,
    DependencyKind.S3_PREFIX,
    DependencyKind.ISSUE_TERMINAL,
    DependencyKind.PR_TERMINAL,
    DependencyKind.PIPELINE_RUN,
    DependencyKind.DATE,
    DependencyKind.MILESTONE_REACHED,
    DependencyKind.HUMAN,
})


def passes_agency_test(dep: Dependency) -> bool:
    """§3.5's agency test: does ``dep`` name a condition outside the loop's
    own write authority?

    Returns ``True`` iff the action that would satisfy ``dep`` is not one
    the reconciler could take itself — i.e. ``dep.kind`` is in the closed
    allowlist :data:`_AGENCY_EXTERNAL_KINDS`. A merge conflict, a stale
    branch, a red re-runnable check and an absent review are each already
    unrepresentable as a ``Dependency`` (there is no kind for them — see
    :class:`DependencyKind`'s docstring); when one of those is declared as
    prose it is rejected by :func:`_parse_declaration_text` before it ever
    reaches this function. This function is the second, independent half of
    the same test (§3.5: "both are checked at declaration"), guarding the
    *typed* construction path a caller may use to bypass the string parser.

    Callers computing a disposition should treat a dependency failing this
    test as ``act``, never ``blocked`` — in practice that disposition is
    already reached by :mod:`disposition`'s ``ChangeCondition`` handlers
    (``_condition_conflicted``, ``_condition_ci_red``) once the bogus
    declaration has been rejected at the door and never reaches
    ``declared_dependencies``.
    """
    return dep.kind in _AGENCY_EXTERNAL_KINDS


def human_owner(dep: Dependency) -> str:
    """The person named by a ``HUMAN`` dependency (§3.7).

    Raises ``ValueError`` if ``dep.kind`` is not ``HUMAN`` — a caller asking
    for the owner of a non-human dependency is a programming error, not a
    missing value. ``Dependency``'s own ``_check_human_shape`` validator
    already guarantees the split below succeeds for any constructed HUMAN
    dependency, so this never re-raises the shape error.
    """
    if dep.kind is not DependencyKind.HUMAN:
        raise ValueError(f"human_owner: {dep.kind.value!r} is not DependencyKind.HUMAN")
    return dep.target.split(":", 1)[0]


def human_due_date(dep: Dependency) -> str:
    """The ISO-8601 due date named by a ``HUMAN`` dependency (§3.7 / §3.6).

    See :func:`human_owner` for the error contract.
    """
    if dep.kind is not DependencyKind.HUMAN:
        raise ValueError(f"human_due_date: {dep.kind.value!r} is not DependencyKind.HUMAN")
    return dep.target.split(":", 1)[1]


class Change(BaseModel):
    """The change rendering an item's :attr:`ItemStage.IN_FLIGHT` stage (§3).

    A pull request, in the fleet's substrate. It is **not** an item: it has no
    stages of its own, no dispositions are defined over it, and no outcome is
    measured over it. What it carries is the observed *condition* of the work
    in flight, which the disposition function reads to decide which action the
    item needs next.

    ``mergeable`` and ``ci_green`` are three-valued on purpose: ``None`` means
    "the substrate has not reported a value", which the lane classifier treats
    as a failed cleanliness check rather than as a pass. Absence of evidence is
    never confirmation of cleanliness.
    """

    #: The change's identifier in the substrate — a PR number, typically
    #: qualified (``owner/name#number``) by a fleet-wide harness.
    ref: str
    condition: ChangeCondition
    mergeable: Optional[bool] = None
    ci_green: Optional[bool] = None
    security_threads: int = 0
    head_sha: Optional[str] = None

    @field_validator("ref")
    @classmethod
    def _ref_nonempty(cls, v: str) -> str:
        if v is None or not str(v).strip():
            raise ValueError(
                "Change.ref must be a non-empty string — a change with no "
                "identifier cannot be acted on or reported"
            )
        return v

    @property
    def is_draft(self) -> bool:
        """True iff the change is a draft.

        Derived from :attr:`condition` rather than stored beside it. A separate
        boolean would be a second surface for the same fact, and the two can
        disagree; draft-ness is a condition like any other (§4.4).
        """
        return self.condition is ChangeCondition.DRAFT


class VerificationObligation(BaseModel):
    """A criterion satisfiable only **after** the change is merged (§3.8).

    A change whose acceptance criterion is an artifact its own merged code
    produces cannot satisfy that criterion before merging. Held as a pre-merge
    dependency it is structurally unsatisfiable — the item rests forever on a
    condition its own resting prevents.

    So it is not a dependency. It is an obligation attached to the
    ``merged`` → ``verified`` transition: the item merges when the change is
    correct and reviewable, advances to ``verified`` when ``predicate`` holds,
    and carries :attr:`revert_action` as the action if the predicate has not
    held by :attr:`deadline`.

    The deadline is **required**, not optional (§3.6): a wait with no deadline
    is a wontfix with better manners, and an unverified obligation past its
    deadline is an F6 residence violation and an F4 candidate. Rejecting the
    declaration here is the §3.1 write-boundary chokepoint doing its job.
    """

    predicate: Dependency
    #: ISO-8601 date (``YYYY-MM-DD``) by which ``predicate`` must hold.
    deadline: str
    #: The ACT action emitted when the deadline passes with the predicate
    #: unsatisfied. Named on the obligation rather than hardcoded so a harness
    #: can route it (§3.8 requires a *named* revert, not an alert).
    revert_action: str = "revert"

    @field_validator("deadline")
    @classmethod
    def _deadline_is_a_date(cls, v: str) -> str:
        if v is None or not str(v).strip():
            raise ValueError(
                "VerificationObligation.deadline is required (§3.6: absence of "
                "a stated residence is a rejected declaration, not a default "
                "of forever)"
            )
        try:
            _dt.date.fromisoformat(str(v).strip())
        except ValueError as exc:
            raise ValueError(
                f"VerificationObligation.deadline {v!r} is not an ISO-8601 date "
                f"(YYYY-MM-DD): {exc}"
            ) from exc
        return str(v).strip()

    @field_validator("revert_action")
    @classmethod
    def _revert_action_nonempty(cls, v: str) -> str:
        if v is None or not str(v).strip():
            raise ValueError(
                "VerificationObligation.revert_action must be a non-empty "
                "action name (§3.8: the failure path is a named revert)"
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

    #: :attr:`Disposition.blocking_chain`, carried forward for the same
    #: reason the three fields above are (alpha-engine-config#6500): a
    #: skipped item's rebuilt disposition (:func:`reconciler._recorded_disposition`)
    #: reconstructs from exactly what is stored here, never by recomputing —
    #: a record missing the chain would silently degrade a skipped BLOCKED
    #: item's rendered comment from the full §3.4 chain back to prose-only on
    #: every cycle it skips. Empty on records written before this field
    #: existed or for a BLOCKED verdict that carries no chain (e.g. admission
    #: denial).
    disposition_blocking_chain: list[str] = Field(default_factory=list)

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

    #: The item's **effective** stage at this evaluation (:class:`ItemStage`
    #: value). Recorded so the next cycle can compute the *transition* rather
    #: than the state, exactly as ``satisfied_tokens`` does for dependencies.
    stage: Optional[str] = None

    #: :class:`ItemStage` value → ISO-8601 timestamp at which the item was
    #: **first observed** in that stage (§3, deliverable 4).
    #:
    #: **F7 and F6 are computed from this map and from nothing else.** F7 is
    #: lead time from intent recorded to change verified —
    #: ``stage_entered_at["verified"] - stage_entered_at["proposed"]`` — and F6
    #: is residence, measured from the entry stamp of the stage an item is
    #: currently in. Neither is recoverable from GitHub: the substrate records
    #: when an issue was opened and when a PR merged, and nothing anywhere
    #: records when an item became *ready*, because readiness is derived and
    #: only the loop that derives it can know the moment it flipped.
    #:
    #: **First entry wins, and re-entry never re-stamps.** An item can regress
    #: — a closed PR sends it from ``in_flight`` back to ``ready`` — and lead
    #: time is measured from the *original* intent, not from the most recent
    #: attempt. Re-stamping would silently shorten every measurement that had a
    #: setback, which is the population the number exists to expose.
    #:
    #: Timestamps are supplied by the caller for the same reason
    #: ``dependency_satisfied_at``'s are: the reconciler is a pure function of
    #: ``(config, items, world, store)`` (§5.4), and a hidden clock would make
    #: two identical inputs produce different records.
    stage_entered_at: dict[str, str] = Field(default_factory=dict)


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

    ``blocking_chain`` (§3.4, alpha-engine-config#6500) is the **structured**
    form of a BLOCKED verdict's root-cause chain: ``"<kind>:<target>"`` tokens
    in traversal order, starting at the item's own declared dependency and
    ending at the root external condition — the same sequence
    :func:`dependency_graph.DependencyGraph.get_blocked_chain` returns, kept
    as data rather than requiring a consumer to parse it back out of
    ``reason``'s ``" -> "``-joined prose. Empty for a BLOCKED verdict that
    names no dependency chain at all — an admission-denied (WIP ceiling)
    BLOCKED is a queue constraint, not a §3.4 chain, and a post-merge
    verification BLOCKED carries a single-token chain (its own predicate).
    Never populated for any non-BLOCKED kind.
    """

    kind: DispositionKind
    reason: Optional[str] = None
    action: Optional[str] = None
    blocking_chain: list[str] = Field(default_factory=list)

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
        if self.kind is not DispositionKind.BLOCKED and self.blocking_chain:
            raise ValueError(
                f"Disposition({self.kind.value}) carries a non-empty "
                "blocking_chain — a root-cause chain is only meaningful on a "
                "BLOCKED verdict (§3.4)"
            )
        return self


# ---------------------------------------------------------------------------
# Item — the carried unit
# ---------------------------------------------------------------------------


def _hash_string(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Item(BaseModel):
    """One carried **unit of intended change**, at a stage (§3).

    There is exactly one item type. What used to be two — an issue and its pull
    request, carried as separate populations with a foreign key between them —
    is one item whose :attr:`stage` says where in its life it is, and whose
    :attr:`change` describes the pull request rendering the ``in_flight`` stage
    when it has one.

    Per §3, an ``Item`` carries its *declared* dependencies
    (:attr:`declared_dependencies`) and never an asserted ``blocked`` flag —
    blocked-ness is always derived by :func:`dependency_evaluator.is_item_blocked`
    against an :class:`ObservedWorld`. Per §5.5, an item may carry its last
    observed generation so the reconciler can skip unchanged items.

    Two stages are likewise derived rather than stored (see :class:`ItemStage`):
    a recorded ``proposed`` resolves to ``ready`` when nothing it declares is
    unsatisfied, and a recorded ``merged`` resolves to ``verified`` when its
    :attr:`verification` obligation is discharged. Use
    :func:`stage.effective_stage`, never :attr:`stage`, to ask where an item
    actually is.
    """

    id: str
    stage: ItemStage
    title: str = ""
    labels: list[str] = []
    declared_dependencies: list[Dependency] = []

    #: The change rendering this item's ``in_flight`` stage, when this item is
    #: the one carrying it. ``None`` at every stage before ``in_flight``, and
    #: also on an item whose change is enumerated as a *separate* record — see
    #: :attr:`change_ref`.
    change: Optional[Change] = None

    #: The identifier of this item's change, even when :attr:`change` itself is
    #: not carried here.
    #:
    #: This exists because the substrate still hands the loop an issue and its
    #: pull request as two API objects, and a harness that enumerates both
    #: produces two records for one unit. Rather than let that reintroduce two
    #: populations, the issue-side record is ``in_flight`` with a
    #: ``change_ref`` and no ``change``: it is the same item, and the
    #: disposition function says so — it defers to the record that actually
    #: carries the change instead of inventing a second verdict about it.
    change_ref: Optional[str] = None

    #: The §3.8 post-merge verification obligation, when the item's acceptance
    #: criterion cannot be satisfied before merging. Its presence is what makes
    #: ``merged`` a stage the item can rest in rather than pass straight
    #: through; its absence means merging *is* verification.
    verification: Optional[VerificationObligation] = None

    human_owned: bool = False

    #: Declared exclusion from the loop entirely (§9 carve-out 2). Not a stage:
    #: an excluded item has not been abandoned and may be at any point in its
    #: life — the loop simply does not act on it. Modelling it as a stage would
    #: destroy the stage it was actually in.
    do_not_groom: bool = False

    observed_generation: Optional[ObservedGeneration] = None

    #: Org-level issue-field values observed on this item (config#6320
    #: deliverable 2), keyed by field name (``issue_field_name`` in the
    #: GitHub API response). Read-only passthrough of whatever GitHub
    #: reports — this package creates no fields and assigns no meaning to
    #: any particular name; interpreting a specific field (predicate, owner,
    #: due date) is the write-boundary chokepoint's job (§3.1, issue #6309),
    #: not the snapshot adapter's. Empty when the item carries no field
    #: values, which is indistinguishable from "not probed" only because
    #: GitHub always reports this surface inline — never gated behind a
    #: conditional fetch the way dependencies and sub-issues are.
    custom_fields: dict[str, Any] = {}

    #: Qualified ids (``owner/name#number``) of this item's own sub-issues
    #: (config#6320 deliverable 3), populated only when GitHub reports at
    #: least one via ``sub_issues_summary``. Epic/child containment is a
    #: distinct relationship from a declared ``Dependency`` — it is not
    #: walked by :mod:`dependency_graph`, which follows only
    #: ``ISSUE_TERMINAL`` / ``PR_TERMINAL`` "depends on" edges. This field
    #: feeds whichever consumer implements §3.4's epic-progress semantics.
    sub_issue_ids: list[str] = []

    # -- write-boundary invariants (§3.1) ----------------------------------

    @model_validator(mode="after")
    def _check_stage_invariants(self) -> Item:
        """Reject item shapes the stage model makes meaningless.

        Validated here, at construction, rather than discovered by a sweep: an
        item whose stage and change contradict each other is unevaluable, and
        §3.1 makes that a rejected write rather than a population to repair.
        """
        if self.change is not None and self.change_ref is None:
            self.change_ref = self.change.ref
        if self.stage is ItemStage.IN_FLIGHT and self.change is None and self.change_ref is None:
            raise ValueError(
                f"Item {self.id!r} is at stage in_flight with neither a change "
                "nor a change_ref — 'in flight' means a change exists (§3), so "
                "an item claiming it must name one"
            )
        if self.change is not None and self.stage in (ItemStage.PROPOSED, ItemStage.READY):
            raise ValueError(
                f"Item {self.id!r} carries a change at stage {self.stage.value} — "
                "a change existing IS the in_flight stage (§3); an item cannot "
                "be pre-change and carry one"
            )
        return self

    # -- derived properties ------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        """True iff the item's stage is terminal (``done`` or ``abandoned``).

        Note what is **not** here: ``merged`` (§3.8 gives it a successor
        stage), and ``do_not_groom`` (an exclusion, not a stage — the
        disposition function surfaces it separately so the item's real stage
        survives).
        """
        return self.stage.is_terminal

    @property
    def carries_change(self) -> bool:
        """True iff this record carries the change itself, not just a ref."""
        return self.change is not None

    @property
    def has_declared_dependency(self) -> bool:
        """True if the item declares at least one dependency (§3).

        This is the only "is this item under an external condition" question
        the core asks of an item's own fields. *Whether* the condition holds is
        never answered here — that needs an :class:`ObservedWorld` and is
        answered by :mod:`dependency_evaluator` / :mod:`dependency_graph`.
        """
        return bool(self.declared_dependencies)

    @property
    def unrepresented_gate_labels(self) -> list[str]:
        """``gate:*`` labels on an item that declares **no** dependency (§3.3).

        A ``gate:*`` label is a *status projection*. Once blocked-ness is
        derived (§3), a projection with no declaration behind it is a
        representation defect: the label asserts a condition the reconciler
        cannot evaluate, in either direction. Reading it as "blocked" is
        asserting blocked-ness (the thing §3 abolishes); reading its clearance
        as "unblocked" is the 2026-07-22 incident, in which six gated, CI-red
        PRs auto-merged because one upstream check owned the exclusion.

        So the core reports it and refuses to decide:
        :func:`disposition.compute_disposition` maps a non-empty list to
        UNDECIDABLE. This is not the label used as state — it is the absence
        of a declaration used as a reason to decline.

        The test is deliberately coarse: *any* declared dependency means the
        harness translated this item's gates into declarations (that is what
        ``gate_dependency_adapter.build_declared_dependencies`` does), so the
        label is a projection of a condition the evaluator now owns. An item
        with no declarations at all has nothing to derive from, whatever its
        labels say — which is exactly the state the harness's own enrichment
        failure path leaves an item in, and it already documents that the
        reconciler will "surface [it] as undecidable, not silently unblocked."
        """
        if self.declared_dependencies:
            return []
        return [label for label in self.labels if label.startswith(GATE_LABEL_PREFIX)]

    @property
    def has_gate_label(self) -> bool:
        """DEPRECATED — reads declared dependencies, not labels.

        Retired as a state input by ``alpha-engine-config#6137`` (parent
        ``nous-ergon-ops#356``: Brian ruled the ``gate:*`` taxonomy retired as
        a state representation on 2026-07-31). **Nothing in this package calls
        it**; the disposition function and the lane classifier derive from
        declared dependencies evaluated against an :class:`ObservedWorld`.

        It survives one release as a shim for out-of-tree harnesses that used
        it to select which items to enrich with declarations — a legitimate
        *observation* use, unlike the state use it is retired from. Its value
        is now ``has_declared_dependency or unrepresented_gate_labels``, which
        is identical to the old label test on a freshly snapshotted item (no
        declarations yet) and remains true once that item is enriched.

        Use :attr:`has_declared_dependency` (is this item under a declared
        condition?), :attr:`unrepresented_gate_labels` (is a projection
        unbacked?), or :func:`dependency_evaluator.is_item_blocked` (is the
        condition actually unsatisfied?) instead.
        """
        warnings.warn(
            "Item.has_gate_label is deprecated (alpha-engine-config#6137): "
            "gate:* labels are status projections, never state. Use "
            "Item.has_declared_dependency, Item.unrepresented_gate_labels, or "
            "dependency_evaluator.is_item_blocked.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.has_declared_dependency or bool(self.unrepresented_gate_labels)

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
