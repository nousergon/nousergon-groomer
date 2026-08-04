"""The §5.5 skip token computed over the transitive closure (§3.4).

**The defect these tests exist to prevent is a silent one, and it only appears
once the store is persistent.** With an in-memory store constructed fresh each
cycle, nothing is ever skipped, so the skip logic has never actually run. The
moment a persistent backend lands, a skip token built from an item's own
declarations would skip exactly the population that must never be skipped: a
*blocked* item's spec never changes — what changes is the world beneath it, one
or more hops away — so it would keep a stale ``BLOCKED`` disposition forever
while its blocker was long since satisfied.

That is the whole reason the closure-aware token ships before persistence and
not after.

The tests are ordered as the argument runs:

1. The closure sees a leaf two hops away, and its *state*, not just its name.
2. A leaf flipping changes the token even though nothing about the item did.
3. A token with no closure never matches — including against another absent
   one — so an upgrade costs a cycle rather than granting a wrong skip.
4. The reconciler actually skips, and a skipped item reports the disposition it
   reached rather than a hole.
5. Satisfaction timestamps record a transition, never a state, and are never
   overwritten once taken.
"""
from __future__ import annotations

from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.dependency_graph import DependencyGraph
from nousergon_groomer.models import (
    Dependency,
    DependencyKind,
    DispositionKind,
    Item,
    ItemStage,
)
from nousergon_groomer.observed_gen import (
    GenerationStore,
    compute_fingerprint,
    newly_satisfied,
    record_evaluation,
    should_skip,
)
from nousergon_groomer.reconciler import Reconciler, ReconcilerConfig


def _issue(item_id: str, deps: list[Dependency] | None = None, **kw) -> Item:
    defaults = {
        "id": item_id,
        "stage": ItemStage.PROPOSED,
        "declared_dependencies": deps or [],
    }
    defaults.update(kw)
    return Item(**defaults)


def _s3(target: str) -> Dependency:
    return Dependency(kind=DependencyKind.S3_OBJECT, target=target)


def _on_item(target: str) -> Dependency:
    return Dependency(kind=DependencyKind.ISSUE_TERMINAL, target=target)


# ---------------------------------------------------------------------------
# 1. The closure reaches through intermediate items
# ---------------------------------------------------------------------------


def test_closure_reaches_a_leaf_two_hops_away():
    """``a`` declares nothing but a dependency on ``b``; ``b`` rests on S3.

    ``a.deps_hash`` covers exactly one token — ``issue_terminal:b`` — and says
    nothing whatever about the S3 object that is the actual blocker. The
    closure must carry it, or the token cannot see the thing that will move.
    """
    a = _issue("a", [_on_item("b")])
    b = _issue("b", [_s3("s3://bucket/key")])
    world = ObservedWorld(s3_objects=set(), terminal_items=set())

    closure = DependencyGraph([a, b], world).closure_state("a")

    assert "issue_terminal:b=unsatisfied" in closure
    assert "s3_object:s3://bucket/key=unsatisfied" in closure, (
        "the closure must reach the external leaf, not stop at the item edge"
    )


def test_closure_records_satisfied_edges_too():
    """A walk that stopped at the first blocker could not see a satisfied
    dependency revert, which must also invalidate the token."""
    a = _issue("a", [_s3("s3://bucket/present"), _s3("s3://bucket/absent")])
    world = ObservedWorld(s3_objects={"s3://bucket/present"})

    closure = DependencyGraph([a], world).closure_state("a")

    assert "s3_object:s3://bucket/present=satisfied" in closure
    assert "s3_object:s3://bucket/absent=unsatisfied" in closure


def test_closure_distinguishes_undecidable_from_unsatisfied():
    """``None`` means 'we did not look', which is not 'the object is absent'.
    Collapsing them would make the token stable across a surface going dark."""
    a = _issue("a", [_s3("s3://bucket/key")])
    unlooked = DependencyGraph([a], ObservedWorld()).closure_state("a")
    looked = DependencyGraph([a], ObservedWorld(s3_objects=set())).closure_state("a")

    assert unlooked == ["s3_object:s3://bucket/key=undecidable"]
    assert looked == ["s3_object:s3://bucket/key=unsatisfied"]
    assert unlooked != looked


# ---------------------------------------------------------------------------
# 2. A leaf flip invalidates the token — the defect itself
# ---------------------------------------------------------------------------


def test_a_leaf_flipping_two_hops_away_forces_re_evaluation():
    """**This is the test the whole change exists for.**

    ``a``'s labels, body, head SHA and declared dependencies are byte-identical
    across the two cycles. Only the world moved, and it moved two hops away.
    The item must not be skipped.
    """
    a = _issue("a", [_on_item("b")])
    b = _issue("b", [_s3("s3://bucket/key")])
    store = GenerationStore()

    before = ObservedWorld(s3_objects=set(), terminal_items=set())
    closure_before = DependencyGraph([a, b], before).closure_state("a")
    record_evaluation(a, store, generation=1, closure_state=closure_before)

    # Same item. Same declarations. The blocker is now satisfied.
    after = ObservedWorld(s3_objects={"s3://bucket/key"}, terminal_items={"b"})
    closure_after = DependencyGraph([a, b], after).closure_state("a")

    assert a.deps_hash == a.deps_hash, "the item's own spec is unchanged"
    assert closure_before != closure_after
    assert not should_skip(a, store, closure_state=closure_after), (
        "an item whose transitive blocker was satisfied MUST re-evaluate — "
        "skipping it is how a blocked item stays blocked forever"
    )


def test_an_unchanged_world_still_skips():
    """The optimization must survive being made correct, or it is not one."""
    a = _issue("a", [_on_item("b")])
    b = _issue("b", [_s3("s3://bucket/key")])
    world = ObservedWorld(s3_objects=set(), terminal_items=set())
    closure = DependencyGraph([a, b], world).closure_state("a")

    store = GenerationStore()
    record_evaluation(a, store, generation=1, closure_state=closure)

    assert should_skip(a, store, closure_state=closure)


# ---------------------------------------------------------------------------
# 3. An absent closure never matches
# ---------------------------------------------------------------------------


def test_a_fingerprint_without_a_closure_never_matches_even_itself():
    """A record written before the closure existed describes inputs the writer
    could not fully see. Honouring it as a match grants exactly the unsound
    skip the field was added to prevent, so the answer is a re-evaluation."""
    a = _issue("a", [_s3("s3://bucket/key")])
    fp = compute_fingerprint(a)

    assert fp.closure_hash is None
    assert not fp.matches(fp), (
        "two closure-less fingerprints must NOT match — the safe direction is "
        "one wasted cycle, never a wrong skip"
    )


def test_a_pre_upgrade_record_forces_one_re_evaluation():
    a = _issue("a", [_s3("s3://bucket/key")])
    world = ObservedWorld(s3_objects=set())
    store = GenerationStore()

    record_evaluation(a, store, generation=1)  # no closure — the old shape
    closure = DependencyGraph([a], world).closure_state("a")

    assert not should_skip(a, store, closure_state=closure)
    # ...and having re-evaluated once, it settles.
    record_evaluation(a, store, generation=2, closure_state=closure)
    assert should_skip(a, store, closure_state=closure)


# ---------------------------------------------------------------------------
# 4. The reconciler skips, and a skipped item still reports a verdict
# ---------------------------------------------------------------------------


def test_reconciler_skips_an_unchanged_item_and_reports_its_disposition():
    a = _issue("a", [_s3("s3://bucket/key")])
    world = ObservedWorld(s3_objects=set())
    store = GenerationStore()
    config = ReconcilerConfig(wip_ceiling=10, generation=1)

    first = Reconciler(config).reconcile([a], world, store)
    assert first.items[0].skipped is False
    first_kind = first.items[0].disposition.kind

    second = Reconciler(
        ReconcilerConfig(wip_ceiling=10, generation=2)
    ).reconcile([a], world, store)

    assert second.items[0].skipped is True
    assert second.items[0].disposition.kind is first_kind, (
        "a skipped item reports the verdict it reached, not a placeholder — "
        "otherwise a skip is indistinguishable from an unprocessed item"
    )


def test_a_record_with_no_stored_disposition_is_not_skipped():
    """A skip that cannot say what was decided is a hole, not an optimization.

    Reproduces a pre-upgrade record: the fingerprint matches, so the token
    would allow a skip, but there is no verdict to report."""
    a = _issue("a", [_s3("s3://bucket/key")])
    world = ObservedWorld(s3_objects=set())
    closure = DependencyGraph([a], world).closure_state("a")
    store = GenerationStore()

    record_evaluation(a, store, generation=1, closure_state=closure)
    stale = store.get("a")
    assert stale.disposition_kind is None  # record_evaluation was given no disposition

    result = Reconciler(
        ReconcilerConfig(wip_ceiling=10, generation=2)
    ).reconcile([a], world, store)
    assert result.items[0].skipped is False
    assert result.items[0].disposition.kind in set(DispositionKind)


# ---------------------------------------------------------------------------
# 5. Satisfaction timestamps — a transition, taken once
# ---------------------------------------------------------------------------


def test_a_satisfaction_timestamp_is_recorded_on_the_transition():
    """§3.6 / §2.7: the moment a dependency became satisfied is the term
    nothing records, and F3's 24h clock and F7's detection latency are both
    uncomputable without it."""
    a = _issue("a", [_s3("s3://bucket/key")])
    store = GenerationStore()

    unsatisfied = DependencyGraph([a], ObservedWorld(s3_objects=set())).closure_state("a")
    record_evaluation(a, store, generation=1, closure_state=unsatisfied,
                      observed_at="2026-08-04T00:00:00Z")
    assert store.get("a").dependency_satisfied_at == {}

    satisfied = DependencyGraph(
        [a], ObservedWorld(s3_objects={"s3://bucket/key"})
    ).closure_state("a")
    record_evaluation(a, store, generation=2, closure_state=satisfied,
                      observed_at="2026-08-04T08:00:00Z")

    assert store.get("a").dependency_satisfied_at == {
        "s3_object:s3://bucket/key": "2026-08-04T08:00:00Z"
    }


def test_a_satisfaction_timestamp_is_never_overwritten():
    """The first observation IS the measurement. Re-stamping each cycle would
    overwrite the only copy of it with the current time, and every latency
    would read as zero."""
    a = _issue("a", [_s3("s3://bucket/key")])
    store = GenerationStore()
    world_before = ObservedWorld(s3_objects=set())
    world_after = ObservedWorld(s3_objects={"s3://bucket/key"})

    record_evaluation(a, store, generation=1,
                      closure_state=DependencyGraph([a], world_before).closure_state("a"),
                      observed_at="2026-08-04T00:00:00Z")
    after = DependencyGraph([a], world_after).closure_state("a")
    record_evaluation(a, store, generation=2, closure_state=after,
                      observed_at="2026-08-04T08:00:00Z")
    record_evaluation(a, store, generation=3, closure_state=after,
                      observed_at="2026-08-04T16:00:00Z")

    assert store.get("a").dependency_satisfied_at["s3_object:s3://bucket/key"] == (
        "2026-08-04T08:00:00Z"
    )


def test_a_dependency_already_satisfied_at_first_sight_is_not_dated():
    """Its transition happened before the loop was watching. Dating it to
    first-observation would fabricate a latency rather than decline to
    measure one — F7 undercounts by the items alive at rollout, on purpose."""
    a = _issue("a", [_s3("s3://bucket/key")])
    satisfied = DependencyGraph(
        [a], ObservedWorld(s3_objects={"s3://bucket/key"})
    ).closure_state("a")
    store = GenerationStore()

    record_evaluation(a, store, generation=1, closure_state=satisfied,
                      observed_at="2026-08-04T00:00:00Z")
    assert store.get("a").dependency_satisfied_at == {}

    # ...and the SECOND cycle must not date it either. This is the case the
    # first implementation got wrong: with only `dependency_satisfied_at` to
    # compare against, an empty dict reads as "nothing was ever satisfied",
    # so a long-satisfied dependency gets stamped by whichever cycle notices.
    record_evaluation(a, store, generation=2, closure_state=satisfied,
                      observed_at="2026-08-04T08:00:00Z")
    assert store.get("a").dependency_satisfied_at == {}


def test_newly_satisfied_reports_nothing_without_a_prior_observation():
    satisfied = ["s3_object:s3://b/k=satisfied"]
    assert newly_satisfied(satisfied, None) == []


def test_reconciler_threads_observed_at_from_config():
    a = _issue("a", [_s3("s3://bucket/key")])
    store = GenerationStore()
    Reconciler(
        ReconcilerConfig(wip_ceiling=10, generation=1, observed_at="2026-08-04T00:00:00Z")
    ).reconcile([a], ObservedWorld(s3_objects=set()), store)
    Reconciler(
        ReconcilerConfig(wip_ceiling=10, generation=2, observed_at="2026-08-04T09:00:00Z")
    ).reconcile([a], ObservedWorld(s3_objects={"s3://bucket/key"}), store)

    assert store.get("a").dependency_satisfied_at == {
        "s3_object:s3://bucket/key": "2026-08-04T09:00:00Z"
    }


def test_reconcile_stays_deterministic():
    """§5.4: the same (items, world, store) yields the same result. A clock
    read inside the core rather than passed in would break this first."""
    a = _issue("a", [_s3("s3://bucket/key")])
    world = ObservedWorld(s3_objects=set())
    config = ReconcilerConfig(wip_ceiling=10, generation=1,
                              observed_at="2026-08-04T00:00:00Z")

    one = Reconciler(config).reconcile([a], world, GenerationStore())
    two = Reconciler(config).reconcile([a], world, GenerationStore())
    assert one.model_dump() == two.model_dump()
