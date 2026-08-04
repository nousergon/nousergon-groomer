"""Contract tests for the domain models (issue #11, §3.1 + §3.3)."""
from __future__ import annotations

import pytest

from nousergon_groomer.models import (
    Dependency,
    DependencyEvaluation,
    DependencyKind,
    Disposition,
    DispositionKind,
    Item,
    ItemKind,
    ItemState,
    ObservedGeneration,
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
    item = Item(id="i1", kind=ItemKind.ISSUE, state=ItemState.OPEN_ISSUE_ACTIONABLE,
                declared_dependencies=[dep])
    assert item.declared_dependencies == [dep]
    assert item.observed_generation is None  # status is not on the spec


# ---------------------------------------------------------------------------
# ItemState totality — every value is a known string
# ---------------------------------------------------------------------------

def test_item_state_values_are_strings():
    for state in ItemState:
        assert isinstance(state.value, str)
        assert state.value  # non-empty


def test_item_state_has_do_not_groom():
    assert ItemState.DO_NOT_GROOM == "do_not_groom"


def test_item_is_terminal_for_merged_closed_do_not_groom():
    for state in (ItemState.MERGED, ItemState.CLOSED, ItemState.DO_NOT_GROOM):
        item = Item(id="x", kind=ItemKind.ISSUE, state=state)
        assert item.is_terminal


def test_item_is_not_terminal_for_open_states():
    for state in (
        ItemState.OPEN_CLEAN_GREEN, ItemState.OPEN_RED_CI, ItemState.OPEN_DIRTY,
        ItemState.OPEN_DRAFT, ItemState.OPEN_PENDING_CI,
        ItemState.OPEN_ISSUE_ACTIONABLE, ItemState.OPEN_ISSUE_BLOCKED,
        ItemState.OPEN_ISSUE_WAITING,
    ):
        item = Item(id="x", kind=ItemKind.ISSUE, state=state)
        assert not item.is_terminal


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
    item1 = Item(id="i1", kind=ItemKind.ISSUE, state=ItemState.OPEN_ISSUE_ACTIONABLE,
                 labels=["a", "b"])
    item2 = Item(id="i1", kind=ItemKind.ISSUE, state=ItemState.OPEN_ISSUE_ACTIONABLE,
                 labels=["b", "a"])
    assert item1.label_set_hash == item2.label_set_hash


def test_item_deps_hash_is_order_independent():
    dep1 = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k1")
    dep2 = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k2")
    item1 = Item(id="i1", kind=ItemKind.ISSUE, state=ItemState.OPEN_ISSUE_ACTIONABLE,
                 declared_dependencies=[dep1, dep2])
    item2 = Item(id="i1", kind=ItemKind.ISSUE, state=ItemState.OPEN_ISSUE_ACTIONABLE,
                 declared_dependencies=[dep2, dep1])
    assert item1.deps_hash == item2.deps_hash


# ---------------------------------------------------------------------------
# Gate representation (alpha-engine-config#6137) — declarations, not labels
# ---------------------------------------------------------------------------

def _gate_dep(target="s3://b/gate-artifact.json"):
    return Dependency(kind=DependencyKind.S3_OBJECT, target=target)


def test_has_declared_dependency_is_true_when_a_dependency_is_declared():
    item = Item(id="i1", kind=ItemKind.PR, state=ItemState.OPEN_DRAFT,
                labels=["gate:weekly-sf"], declared_dependencies=[_gate_dep()])
    assert item.has_declared_dependency is True


def test_has_declared_dependency_is_false_for_a_bare_gate_label():
    """A gate:* label declares nothing — label presence is not a declaration."""
    item = Item(id="i1", kind=ItemKind.PR, state=ItemState.OPEN_DRAFT,
                labels=["gate:weekly-sf"])
    assert item.has_declared_dependency is False


def test_unrepresented_gate_labels_names_a_projection_with_no_declaration():
    item = Item(id="i1", kind=ItemKind.PR, state=ItemState.OPEN_DRAFT,
                labels=["gate:weekly-sf", "gate:operator", "groom-reviewed"])
    assert item.unrepresented_gate_labels == ["gate:weekly-sf", "gate:operator"]


def test_unrepresented_gate_labels_empty_once_the_item_declares_its_condition():
    """The label survives as a projection; it is no longer unbacked."""
    item = Item(id="i1", kind=ItemKind.PR, state=ItemState.OPEN_DRAFT,
                labels=["gate:weekly-sf"], declared_dependencies=[_gate_dep()])
    assert item.unrepresented_gate_labels == []


def test_unrepresented_gate_labels_empty_for_a_non_gated_item():
    item = Item(id="i1", kind=ItemKind.PR, state=ItemState.OPEN_DRAFT,
                labels=["groom-reviewed"])
    assert item.unrepresented_gate_labels == []


def test_has_gate_label_is_deprecated_and_reads_declared_dependencies():
    """The retired property no longer tests the label prefix.

    An item declaring a dependency and carrying NO gate label is now True —
    the answer comes from the declaration. The old implementation returned
    False here, because it only ever looked at ``labels``.
    """
    item = Item(id="i1", kind=ItemKind.PR, state=ItemState.OPEN_DRAFT,
                labels=["groom-reviewed"], declared_dependencies=[_gate_dep()])
    with pytest.deprecated_call():
        assert item.has_gate_label is True


def test_has_gate_label_still_true_for_an_unenriched_gate_labelled_item():
    """Compatibility for the harness's enrichment SELECTION use.

    Choosing which items to fetch declarations for is an observation use, not
    a state use; a snapshot item has no declarations yet, so the shim must
    still select it.
    """
    item = Item(id="i1", kind=ItemKind.PR, state=ItemState.OPEN_DRAFT,
                labels=["gate:weekly-sf"])
    with pytest.deprecated_call():
        assert item.has_gate_label is True


def test_has_gate_label_false_for_an_item_with_neither():
    item = Item(id="i1", kind=ItemKind.PR, state=ItemState.OPEN_DRAFT,
                labels=["groom-reviewed"])
    with pytest.deprecated_call():
        assert item.has_gate_label is False
