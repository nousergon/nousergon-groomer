"""Contract tests for the domain models (issue #11, §3.1 + §3.3)."""
from __future__ import annotations

import pytest

from nousergon_groomer.models import (
    STAGE_ORDER,
    Change,
    ChangeCondition,
    Dependency,
    DependencyEvaluation,
    DependencyKind,
    Disposition,
    DispositionKind,
    Item,
    ItemStage,
    ObservedGeneration,
    VerificationObligation,
    can_transition,
    human_due_date,
    human_owner,
    parse_dependency_declaration,
    passes_agency_test,
)

# ---------------------------------------------------------------------------
# §3.1 — Dependency rejects empty/whitespace targets at the write boundary
# ---------------------------------------------------------------------------

def test_dependency_rejects_empty_target():
    with pytest.raises(ValueError, match="non-empty"):
        Dependency(kind=DependencyKind.S3_OBJECT, target="")


def test_dependency_rejects_whitespace_target():
    with pytest.raises(ValueError, match="non-empty"):
        Dependency(kind=DependencyKind.S3_OBJECT, target="   ")


def test_dependency_rejects_none_target():
    """Pydantic rejects None at the type level (target: str is not Optional)."""
    from pydantic import ValidationError as PydanticValidationError
    with pytest.raises(PydanticValidationError):
        Dependency(kind=DependencyKind.S3_OBJECT, target=None)


def test_dependency_accepts_valid_target():
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    assert dep.target == "s3://b/k"


# ---------------------------------------------------------------------------
# §3.1 + §3.5 — the raw-declaration chokepoint (alpha-engine-config#6309)
#
# ``Dependency.model_validate(raw)`` / ``parse_dependency_declaration(raw)``
# is the write boundary a harness calls when translating a `Verified-when:`
# body line or an issue custom field into a declared dependency. Typed
# construction (``Dependency(kind=..., target=...)``, exercised above) is a
# separate, unaffected path.
# ---------------------------------------------------------------------------

def test_declaration_rejects_unparseable_prose():
    """The measured defect: alpha-engine-research-PR549 carried prose in a
    Verified-when field with nothing rejecting it at authorship. A closed
    allowlist of permitted subjects (§3.5's gotcha) has no pattern for
    branch state, so this is rejected here rather than discovered unevaluable
    by a later sweep.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="does not name a permitted subject"):
        parse_dependency_declaration("branch is not behind main")


def test_declaration_rejects_empty_string():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="empty dependency declaration"):
        parse_dependency_declaration("   ")


def test_declaration_rejects_unqualified_issue_reference():
    """A bare ``owner/repo#N`` is ambiguous between issue and PR — not
    machine-evaluable as written, so it is not in the allowlist either.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_dependency_declaration("nousergon/alpha-engine-config#6309")


@pytest.mark.parametrize(
    ("raw", "kind", "target"),
    [
        ("s3://bucket/key.json", DependencyKind.S3_OBJECT, "s3://bucket/key.json"),
        ("s3://bucket/prefix/", DependencyKind.S3_PREFIX, "s3://bucket/prefix/"),
        (
            "issue:nousergon/alpha-engine-config#6309",
            DependencyKind.ISSUE_TERMINAL,
            "nousergon/alpha-engine-config#6309",
        ),
        (
            "pr:nousergon/nousergon-groomer#39",
            DependencyKind.PR_TERMINAL,
            "nousergon/nousergon-groomer#39",
        ),
        ("pipeline_run:weekly-sf-20260804", DependencyKind.PIPELINE_RUN, "weekly-sf-20260804"),
        ("2026-08-10", DependencyKind.DATE, "2026-08-10"),
        ("milestone:m1", DependencyKind.MILESTONE_REACHED, "m1"),
        (
            "human:brianmcmahon:2026-08-15",
            DependencyKind.HUMAN,
            "brianmcmahon:2026-08-15",
        ),
    ],
)
def test_declaration_parses_every_permitted_subject(raw, kind, target):
    dep = parse_dependency_declaration(raw)
    assert dep.kind is kind
    assert dep.target == target


# ---------------------------------------------------------------------------
# §3.7 — a HUMAN dependency names the person and a due date
# ---------------------------------------------------------------------------


def test_human_declaration_rejects_bare_name_no_due_date():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_dependency_declaration("human:brianmcmahon")


def test_human_declaration_rejects_malformed_due_date():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_dependency_declaration("human:brianmcmahon:2026-13-40")


def test_human_typed_construction_rejects_bare_target():
    """§3.7's shape guarantee also holds for typed construction, not only
    the raw-string parser (mirrors how §3.1's agency guard is defence in
    depth on both paths)."""
    with pytest.raises(ValueError, match="owner.*due date|due date"):
        Dependency(kind=DependencyKind.HUMAN, target="brianmcmahon")


def test_human_owner_and_due_date_helpers():
    dep = parse_dependency_declaration("human:brianmcmahon:2026-08-15")
    assert human_owner(dep) == "brianmcmahon"
    assert human_due_date(dep) == "2026-08-15"


def test_human_owner_rejects_non_human_dependency():
    dep = Dependency(kind=DependencyKind.DATE, target="2026-08-10")
    with pytest.raises(ValueError, match="not DependencyKind.HUMAN"):
        human_owner(dep)


# ---------------------------------------------------------------------------
# §3.5 — the agency test
# ---------------------------------------------------------------------------

#: A target that is valid for every DependencyKind's own shape validator, so
#: this loop exercises "is this kind agency-safe" without also exercising
#: each kind's independent shape rules (covered by their own tests above).
_VALID_TARGET_FOR_KIND = {
    DependencyKind.HUMAN: "brianmcmahon:2026-08-15",
}


def test_agency_test_passes_every_permitted_kind():
    for kind in DependencyKind:
        target = _VALID_TARGET_FOR_KIND.get(kind, "x")
        dep = Dependency(kind=kind, target=target)
        assert passes_agency_test(dep) is True


def test_agency_allowlist_covers_every_dependency_kind():
    """Pins today's equality so a future ``DependencyKind`` added without a
    matching addition to the agency allowlist goes red here rather than
    silently passing as external (alpha-engine-config#6309's gotcha: fail
    closed on a new forbidden subject).
    """
    from nousergon_groomer.models import _AGENCY_EXTERNAL_KINDS

    assert _AGENCY_EXTERNAL_KINDS == set(DependencyKind)


# ---------------------------------------------------------------------------
# §5.1 — Disposition validators
# ---------------------------------------------------------------------------

def test_disposition_undecidable_requires_reason():
    with pytest.raises(ValueError, match="UNDECIDABLE"):
        Disposition(kind=DispositionKind.UNDECIDABLE)


def test_disposition_undecidable_with_reason_ok():
    d = Disposition(kind=DispositionKind.UNDECIDABLE, reason="world not reported")
    assert d.reason == "world not reported"


def test_disposition_act_requires_action():
    with pytest.raises(ValueError, match="ACT"):
        Disposition(kind=DispositionKind.ACT)


def test_disposition_act_with_action_ok():
    d = Disposition(kind=DispositionKind.ACT, action="fix_ci")
    assert d.action == "fix_ci"


def test_disposition_blocked_ok():
    d = Disposition(kind=DispositionKind.BLOCKED, reason="blocked on x")
    assert d.kind is DispositionKind.BLOCKED


def test_disposition_terminal_ok():
    d = Disposition(kind=DispositionKind.TERMINAL, reason="merged")
    assert d.kind is DispositionKind.TERMINAL


# ---------------------------------------------------------------------------
# §3.3 — spec and status are separate types
# ---------------------------------------------------------------------------

def test_dependency_and_evaluation_are_different_types():
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    ev = DependencyEvaluation(dependency=dep, satisfied=True)
    assert type(dep) is not type(ev)
    assert ev.dependency is dep
    assert ev.satisfied is True


def test_writing_status_does_not_mutate_spec():
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    ev = DependencyEvaluation(dependency=dep, satisfied=False, undecidable=False)
    # Mutating the status does not touch the spec
    ev.satisfied = True
    assert dep.target == "s3://b/k"
    assert dep.kind is DependencyKind.S3_OBJECT


def test_item_carries_spec_not_status():
    """An Item carries declared dependencies (spec), not evaluated dispositions (status)."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = Item(id="i1", stage=ItemStage.PROPOSED, declared_dependencies=[dep])
    assert item.declared_dependencies == [dep]
    assert item.observed_generation is None  # status is not on the spec


# ---------------------------------------------------------------------------
# §3 — one item with stages (alpha-engine-config#6307)
# ---------------------------------------------------------------------------

def test_the_stage_enum_carries_all_seven_values():
    """closes-when 1: ``ItemKind`` is gone and ``Item.stage`` has all seven."""
    assert [stage.value for stage in ItemStage] == [
        "proposed", "ready", "in_flight", "merged", "verified", "done", "abandoned",
    ]


def test_item_kind_no_longer_exists():
    """The two-population model is not deprecated — it is removed.

    Leaving a shim would let a consumer keep asking "is this an issue or a
    PR?", which is the question §3 abolishes: both answers describe the same
    unit at different points in its life.
    """
    import nousergon_groomer.models as models

    assert not hasattr(models, "ItemKind")
    assert not hasattr(models, "ItemState")


def test_stage_order_is_the_forward_progression():
    assert [stage.value for stage in STAGE_ORDER] == [
        "proposed", "ready", "in_flight", "merged", "verified", "done",
    ]
    assert ItemStage.ABANDONED not in STAGE_ORDER


def test_merged_is_not_terminal_and_verified_is_reachable():
    """closes-when 3, at the model.

    Terminal-at-merge is what leaves a post-merge verification obligation with
    nowhere to live (§3.8), and F7 measures lead time to ``verified``.
    """
    assert ItemStage.MERGED.is_terminal is False
    assert can_transition(ItemStage.MERGED, ItemStage.VERIFIED) is True
    assert can_transition(ItemStage.VERIFIED, ItemStage.DONE) is True


def test_only_done_and_abandoned_are_terminal():
    terminal = {stage for stage in ItemStage if stage.is_terminal}
    assert terminal == {ItemStage.DONE, ItemStage.ABANDONED}


def test_abandoned_is_reachable_from_every_non_terminal_stage():
    for stage in ItemStage:
        if stage.is_terminal:
            continue
        assert can_transition(stage, ItemStage.ABANDONED) is True


def test_nothing_leaves_a_terminal_stage():
    for terminal in (ItemStage.DONE, ItemStage.ABANDONED):
        for stage in ItemStage:
            assert can_transition(terminal, stage) is False


def test_a_stage_does_not_transition_to_itself():
    for stage in ItemStage:
        assert can_transition(stage, stage) is False


def test_backward_transitions_are_rejected():
    assert can_transition(ItemStage.IN_FLIGHT, ItemStage.PROPOSED) is False
    assert can_transition(ItemStage.VERIFIED, ItemStage.MERGED) is False


def test_item_is_terminal_only_at_done_and_abandoned():
    for stage in (ItemStage.DONE, ItemStage.ABANDONED):
        assert Item(id="x", stage=stage).is_terminal
    for stage in (ItemStage.PROPOSED, ItemStage.READY, ItemStage.MERGED,
                  ItemStage.VERIFIED):
        assert not Item(id="x", stage=stage).is_terminal


def test_do_not_groom_is_a_flag_not_a_stage():
    """An excluded item keeps the stage it is really at (§9 carve-out 2)."""
    item = Item(id="x", stage=ItemStage.PROPOSED, do_not_groom=True)
    assert item.stage is ItemStage.PROPOSED
    assert item.is_terminal is False


# ---------------------------------------------------------------------------
# §3 — Change is a condition of one stage, not an item
# ---------------------------------------------------------------------------

def test_change_draftness_is_derived_from_its_condition():
    """One surface for one fact — a separate boolean could disagree with it."""
    assert Change(ref="p1", condition=ChangeCondition.DRAFT).is_draft is True
    assert Change(ref="p1", condition=ChangeCondition.CLEAN).is_draft is False


def test_change_rejects_an_empty_ref():
    with pytest.raises(ValueError, match="non-empty"):
        Change(ref="  ", condition=ChangeCondition.CLEAN)


def test_in_flight_requires_a_change_or_a_ref():
    """"In flight" means a change exists (§3); an item claiming it must name one."""
    with pytest.raises(ValueError, match="neither a change nor a change_ref"):
        Item(id="x", stage=ItemStage.IN_FLIGHT)


def test_change_ref_is_filled_from_the_change():
    item = Item(id="x", stage=ItemStage.IN_FLIGHT,
                change=Change(ref="p9", condition=ChangeCondition.CLEAN))
    assert item.change_ref == "p9"
    assert item.carries_change is True


def test_a_pre_change_stage_cannot_carry_a_change():
    """A change existing IS the in_flight stage — the two cannot disagree."""
    with pytest.raises(ValueError, match="carries a change at stage"):
        Item(id="x", stage=ItemStage.PROPOSED,
             change=Change(ref="p1", condition=ChangeCondition.CLEAN))


def test_an_intent_record_carries_a_ref_without_the_change():
    item = Item(id="i1", stage=ItemStage.IN_FLIGHT, change_ref="p1")
    assert item.carries_change is False
    assert item.change_ref == "p1"


# ---------------------------------------------------------------------------
# §3.8 — the post-merge verification obligation
# ---------------------------------------------------------------------------

def test_verification_obligation_requires_a_deadline():
    """§3.6: absence of a stated residence is a rejected declaration, never a
    default of forever."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    with pytest.raises(ValueError):
        VerificationObligation(predicate=dep)


def test_verification_obligation_rejects_a_non_date_deadline():
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    with pytest.raises(ValueError, match="not an ISO-8601 date"):
        VerificationObligation(predicate=dep, deadline="soon")


def test_verification_obligation_names_a_revert_action():
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    obligation = VerificationObligation(predicate=dep, deadline="2026-08-10")
    assert obligation.revert_action == "revert"
    with pytest.raises(ValueError, match="non-empty action name"):
        VerificationObligation(predicate=dep, deadline="2026-08-10", revert_action=" ")


# ---------------------------------------------------------------------------
# ObservedGeneration — §5.5 skip token
# ---------------------------------------------------------------------------

def test_observed_generation_carries_fingerprints():
    og = ObservedGeneration(
        item_id="i1", generation=1,
        label_set_hash="abc", deps_hash="def",
    )
    assert og.item_id == "i1"
    assert og.generation == 1
    assert og.label_set_hash == "abc"
    assert og.deps_hash == "def"


def test_observed_generation_head_sha_defaults_none():
    og = ObservedGeneration(
        item_id="i1", generation=1,
        label_set_hash="abc", deps_hash="def",
    )
    assert og.head_sha is None
    assert og.body_hash is None


# ---------------------------------------------------------------------------
# Item label/deps hashing — deterministic and order-independent
# ---------------------------------------------------------------------------

def test_item_label_set_hash_is_order_independent():
    item1 = Item(id="i1", stage=ItemStage.PROPOSED, labels=["a", "b"])
    item2 = Item(id="i1", stage=ItemStage.PROPOSED, labels=["b", "a"])
    assert item1.label_set_hash == item2.label_set_hash


def test_item_deps_hash_is_order_independent():
    dep1 = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k1")
    dep2 = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k2")
    item1 = Item(id="i1", stage=ItemStage.PROPOSED,
                 declared_dependencies=[dep1, dep2])
    item2 = Item(id="i1", stage=ItemStage.PROPOSED,
                 declared_dependencies=[dep2, dep1])
    assert item1.deps_hash == item2.deps_hash


# ---------------------------------------------------------------------------
# Gate representation (alpha-engine-config#6137) — declarations, not labels
# ---------------------------------------------------------------------------

def _gate_dep(target="s3://b/gate-artifact.json"):
    return Dependency(kind=DependencyKind.S3_OBJECT, target=target)


def test_has_declared_dependency_is_true_when_a_dependency_is_declared():
    item = Item(id="i1", stage=ItemStage.PROPOSED,
                labels=["gate:weekly-sf"], declared_dependencies=[_gate_dep()])
    assert item.has_declared_dependency is True


def test_has_declared_dependency_is_false_for_a_bare_gate_label():
    """A gate:* label declares nothing — label presence is not a declaration."""
    item = Item(id="i1", stage=ItemStage.PROPOSED,
                labels=["gate:weekly-sf"])
    assert item.has_declared_dependency is False


def test_unrepresented_gate_labels_names_a_projection_with_no_declaration():
    item = Item(id="i1", stage=ItemStage.PROPOSED,
                labels=["gate:weekly-sf", "gate:operator", "groom-reviewed"])
    assert item.unrepresented_gate_labels == ["gate:weekly-sf", "gate:operator"]


def test_unrepresented_gate_labels_empty_once_the_item_declares_its_condition():
    """The label survives as a projection; it is no longer unbacked."""
    item = Item(id="i1", stage=ItemStage.PROPOSED,
                labels=["gate:weekly-sf"], declared_dependencies=[_gate_dep()])
    assert item.unrepresented_gate_labels == []


def test_unrepresented_gate_labels_empty_for_a_non_gated_item():
    item = Item(id="i1", stage=ItemStage.PROPOSED,
                labels=["groom-reviewed"])
    assert item.unrepresented_gate_labels == []


# ---------------------------------------------------------------------------
# Identity conflict (§5.6, alpha-engine-config#6316) — more than one open
# change observed rendering the same item's in_flight stage
# ---------------------------------------------------------------------------

def test_has_identity_conflict_false_with_no_additional_refs():
    item = Item(id="i1", stage=ItemStage.IN_FLIGHT, change_ref="100")
    assert item.has_identity_conflict is False


def test_has_identity_conflict_true_with_additional_refs():
    item = Item(
        id="i1", stage=ItemStage.IN_FLIGHT, change_ref="100",
        additional_change_refs=["101"],
    )
    assert item.has_identity_conflict is True
    assert item.additional_change_refs == ["101"]


def test_has_gate_label_is_deprecated_and_reads_declared_dependencies():
    """The retired property no longer tests the label prefix.

    An item declaring a dependency and carrying NO gate label is now True —
    the answer comes from the declaration. The old implementation returned
    False here, because it only ever looked at ``labels``.
    """
    item = Item(id="i1", stage=ItemStage.PROPOSED,
                labels=["groom-reviewed"], declared_dependencies=[_gate_dep()])
    with pytest.deprecated_call():
        assert item.has_gate_label is True


def test_has_gate_label_still_true_for_an_unenriched_gate_labelled_item():
    """Compatibility for the harness's enrichment SELECTION use.

    Choosing which items to fetch declarations for is an observation use, not
    a state use; a snapshot item has no declarations yet, so the shim must
    still select it.
    """
    item = Item(id="i1", stage=ItemStage.PROPOSED,
                labels=["gate:weekly-sf"])
    with pytest.deprecated_call():
        assert item.has_gate_label is True


def test_has_gate_label_false_for_an_item_with_neither():
    item = Item(id="i1", stage=ItemStage.PROPOSED,
                labels=["groom-reviewed"])
    with pytest.deprecated_call():
        assert item.has_gate_label is False
