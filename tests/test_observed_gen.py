"""Contract tests for the observed-generation skip (issue #8, §5.5).

These tests assert:

1. **First evaluation never skips** — no recorded generation → must evaluate.
2. **Unchanged inputs skip** — same fingerprint → skip is safe.
3. **Changed labels re-evaluate** — a label change breaks the fingerprint.
4. **Changed deps re-evaluate** — a dependency change breaks the fingerprint.
5. **Changed head_sha re-evaluates** — a force-push breaks the fingerprint.
6. **Changed body re-evaluates** — a body edit breaks the fingerprint.
7. **Idempotency within a cycle** — already evaluated this generation → skip.
8. **Store is separate from spec** — the store never mutates the Item.
"""
from __future__ import annotations

from nousergon_groomer.models import (
    Dependency,
    DependencyKind,
    Item,
    ItemStage,
    ObservedGeneration,
)
from nousergon_groomer.observed_gen import (
    GenerationStore,
    compute_fingerprint,
    record_evaluation,
    should_skip,
)


def _item(**kw) -> Item:
    defaults = {"id": "i1", "stage": ItemStage.PROPOSED}
    defaults.update(kw)
    return Item(**defaults)


def _dep(target: str = "s3://b/k") -> Dependency:
    return Dependency(kind=DependencyKind.S3_OBJECT, target=target)


# ---------------------------------------------------------------------------
# 1. First evaluation never skips
# ---------------------------------------------------------------------------

def test_first_evaluation_never_skips():
    item = _item()
    store = GenerationStore()
    assert should_skip(item, store) is False


# ---------------------------------------------------------------------------
# 2. Unchanged inputs skip
# ---------------------------------------------------------------------------

def test_unchanged_inputs_skip():
    # An empty closure is an OBSERVATION ("this item reaches no dependencies"),
    # which is a different thing from `None` ("the caller did not look"). Only
    # the first may ground a skip — see test_no_closure_never_skips below.
    item = _item()
    store = GenerationStore()
    record_evaluation(item, store, generation=1, closure_state=[])
    assert should_skip(item, store, closure_state=[]) is True


# ---------------------------------------------------------------------------
# 3. Changed labels re-evaluate
# ---------------------------------------------------------------------------

def test_changed_labels_re_evaluate():
    item = _item(labels=["a"])
    store = GenerationStore()
    record_evaluation(item, store, generation=1)
    # Add a label → fingerprint changes → must re-evaluate
    item2 = _item(labels=["a", "b"])
    assert should_skip(item2, store) is False


def test_reordered_labels_skip():
    """Label order is irrelevant — the set hash is order-independent."""
    item = _item(labels=["a", "b"])
    store = GenerationStore()
    record_evaluation(item, store, generation=1, closure_state=[])
    item2 = _item(labels=["b", "a"])
    assert should_skip(item2, store, closure_state=[]) is True


# ---------------------------------------------------------------------------
# 4. Changed deps re-evaluate
# ---------------------------------------------------------------------------

def test_changed_deps_re_evaluate():
    item = _item(declared_dependencies=[_dep("s3://b/k1")])
    store = GenerationStore()
    record_evaluation(item, store, generation=1)
    item2 = _item(declared_dependencies=[_dep("s3://b/k2")])
    assert should_skip(item2, store) is False


def test_added_dep_re_evaluates():
    item = _item(declared_dependencies=[_dep("s3://b/k1")])
    store = GenerationStore()
    record_evaluation(item, store, generation=1)
    item2 = _item(declared_dependencies=[_dep("s3://b/k1"), _dep("s3://b/k2")])
    assert should_skip(item2, store) is False


def test_removed_dep_re_evaluates():
    item = _item(declared_dependencies=[_dep("s3://b/k1"), _dep("s3://b/k2")])
    store = GenerationStore()
    record_evaluation(item, store, generation=1)
    item2 = _item(declared_dependencies=[_dep("s3://b/k1")])
    assert should_skip(item2, store) is False


# ---------------------------------------------------------------------------
# 5. Changed head_sha re-evaluates
# ---------------------------------------------------------------------------

def test_changed_head_sha_re_evaluates():
    item = _item()
    store = GenerationStore()
    record_evaluation(item, store, generation=1, head_sha="sha1")
    # Same item, but head_sha changed → must re-evaluate
    assert should_skip(item, store, head_sha="sha2") is False


def test_unchanged_head_sha_skips():
    item = _item()
    store = GenerationStore()
    record_evaluation(item, store, generation=1, head_sha="sha1", closure_state=[])
    assert should_skip(item, store, head_sha="sha1", closure_state=[]) is True


# ---------------------------------------------------------------------------
# 6. Changed body re-evaluates
# ---------------------------------------------------------------------------

def test_changed_body_re_evaluates():
    item = _item()
    store = GenerationStore()
    record_evaluation(item, store, generation=1, body="hello")
    assert should_skip(item, store, body="world") is False


def test_unchanged_body_skips():
    item = _item()
    store = GenerationStore()
    record_evaluation(item, store, generation=1, body="hello", closure_state=[])
    assert should_skip(item, store, body="hello", closure_state=[]) is True


# ---------------------------------------------------------------------------
# 7. Idempotency within a cycle
# ---------------------------------------------------------------------------

def test_idempotent_within_same_generation():
    item = _item()
    store = GenerationStore()
    record_evaluation(item, store, generation=5)
    # Already evaluated at generation 5 → skip even if asked again
    assert should_skip(item, store, current_generation=5) is True


def test_new_generation_skips_if_unchanged():
    """A new cycle counter does NOT force re-evaluation if inputs are unchanged.

    §5.5: the fingerprint decides, not the counter. An unchanged item skips
    across cycles — that is the whole point of the optimization.
    """
    item = _item()
    store = GenerationStore()
    record_evaluation(item, store, generation=5, closure_state=[])
    assert should_skip(item, store, current_generation=6, closure_state=[]) is True


# ---------------------------------------------------------------------------
# 8. Store is separate from spec (§3.3)
# ---------------------------------------------------------------------------

def test_store_does_not_mutate_item():
    item = _item(labels=["a"])
    original_labels = list(item.labels)
    store = GenerationStore()
    record_evaluation(item, store, generation=1)
    assert item.labels == original_labels
    assert item.observed_generation is None  # spec carries no status


def test_store_returns_observed_generation():
    item = _item()
    store = GenerationStore()
    record = record_evaluation(item, store, generation=1)
    assert isinstance(record, ObservedGeneration)
    assert record.item_id == item.id
    assert record.generation == 1


def test_store_get_returns_none_for_unknown():
    store = GenerationStore()
    assert store.get("unknown") is None
    assert "unknown" not in store


def test_fingerprint_is_deterministic():
    item = _item(labels=["a", "b"], declared_dependencies=[_dep("s3://b/k")])
    fp1 = compute_fingerprint(item, closure_state=["s3_object:s3://b/k=unsatisfied"])
    fp2 = compute_fingerprint(item, closure_state=["s3_object:s3://b/k=unsatisfied"])
    assert fp1.matches(fp2)


def test_no_closure_never_skips():
    """A fingerprint with no closure describes inputs its writer could not
    fully see — the world moves underneath a blocked item without touching
    anything on the item itself (§3.4). Matching on it would grant exactly the
    unsound skip the closure was added to prevent, so it never matches, not
    even against another absent closure. Cost: one re-evaluation at upgrade."""
    item = _item()
    store = GenerationStore()
    record_evaluation(item, store, generation=1)
    assert should_skip(item, store) is False
