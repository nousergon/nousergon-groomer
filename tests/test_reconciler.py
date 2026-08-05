"""Contract tests for the reconciler (issue #9, §5.1 + §5.3).

These tests assert:

1. **Totality** — every carried item gets a disposition; no silent skips.
2. **Idempotency (§5.3)** — re-running over identical state yields identical
   output.
3. **One enumeration** — a single pass produces the result; no multi-pass.
4. **Admission** — an actionable issue at the WIP ceiling is BLOCKED, not ACT.
5. **Aggregate counts** — the throughput number and flow-outcome breakdown
   are correct.
6. **Action ordering** — fix_ci before automerge before create_pr.
"""
from __future__ import annotations

from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.models import (
    Change,
    ChangeCondition,
    Dependency,
    DependencyKind,
    Disposition,
    DispositionKind,
    Item,
    ItemStage,
)
from nousergon_groomer.observed_gen import GenerationStore, record_evaluation
from nousergon_groomer.reconciler import (
    Reconciler,
    ReconcilerConfig,
)


def _world_full() -> ObservedWorld:
    return ObservedWorld(
        s3_objects=set(),
        s3_prefixes=set(),
        terminal_items=set(),
        pipeline_runs=set(),
    )


def _issue(stage: ItemStage = ItemStage.PROPOSED, **kw) -> Item:
    """An item with no change yet."""
    defaults = {"id": "i1", "stage": stage}
    defaults.update(kw)
    return Item(**defaults)


def _pr(condition: ChangeCondition = ChangeCondition.CLEAN, **kw) -> Item:
    """An item in flight, carrying a change in ``condition``."""
    item_id = kw.pop("id", "p1")
    change = Change(
        ref=item_id,
        condition=condition,
        mergeable=kw.pop("mergeable", None),
        ci_green=kw.pop("ci_green", None),
    )
    defaults = {
        "id": item_id,
        "stage": kw.pop("stage", ItemStage.IN_FLIGHT),
        "change": change,
    }
    defaults.update(kw)
    return Item(**defaults)


def _reconcile(items, world=None, ceiling=5, generation=1, store=None):
    if world is None:
        world = _world_full()
    if store is None:
        store = GenerationStore()
    config = ReconcilerConfig(wip_ceiling=ceiling, generation=generation)
    r = Reconciler(config)
    return r.reconcile(items, world, store)


# ---------------------------------------------------------------------------
# 1. Totality — every item gets a disposition
# ---------------------------------------------------------------------------

def test_every_item_gets_a_disposition():
    items = [
        _issue(id="i1"),
        _pr(ChangeCondition.CLEAN, id="p1", mergeable=True, ci_green=True),
        _issue(ItemStage.DONE, id="i2"),
    ]
    result = _reconcile(items)
    assert result.enumerated == 3
    assert len(result.items) == 3
    for r in result.items:
        assert r.disposition is not None


def test_no_silent_skips_in_result():
    """A skipped item still appears in the result with its disposition."""
    items = [_issue(id="i1")]
    store = GenerationStore()
    # First pass — records the generation
    _reconcile(items, store=store, generation=1)
    # Second pass — same inputs → skipped, but still in the result
    result = _reconcile(items, store=store, generation=2)
    assert result.enumerated == 1
    assert result.items[0].skipped is True
    assert result.items[0].disposition is not None


# ---------------------------------------------------------------------------
# 2. Idempotency (§5.3)
# ---------------------------------------------------------------------------

def test_idempotent_same_state_same_output():
    """Re-running over identical state produces identical output."""
    items = [
        _issue(id="i1"),
        _pr(ChangeCondition.CI_RED, id="p1", ci_green=False),
    ]
    r1 = _reconcile(items, generation=1)
    r2 = _reconcile(items, generation=1)
    # Same dispositions
    assert [r.disposition.kind for r in r1.items] == [r.disposition.kind for r in r2.items]
    assert [r.disposition.action for r in r1.items] == [r.disposition.action for r in r2.items]
    # Same aggregate counts
    assert r1.acted == r2.acted
    assert r1.blocked == r2.blocked
    assert r1.throughput == r2.throughput


# ---------------------------------------------------------------------------
# 3. One enumeration
# ---------------------------------------------------------------------------

def test_one_pass_one_population():
    """The result covers exactly the input population, no more, no less."""
    items = [_issue(id=f"i{n}") for n in range(5)]
    result = _reconcile(items)
    assert result.enumerated == 5
    ids = {r.item_id for r in result.items}
    assert ids == {f"i{n}" for n in range(5)}


# ---------------------------------------------------------------------------
# 4. Admission — WIP ceiling downgrades ACT to BLOCKED
# ---------------------------------------------------------------------------

def test_actionable_issue_unblocked_is_act_create_pr():
    items = [_issue(id="i1")]
    result = _reconcile(items, ceiling=5)
    r = result.items[0]
    assert r.disposition.kind is DispositionKind.ACT
    assert r.disposition.action == "create_pr"
    assert result.admitted == 1


def test_actionable_issue_at_wip_ceiling_is_blocked():
    """An actionable issue is BLOCKED when the WIP ceiling is saturated."""
    # 3 PRs already in flight, ceiling=3 → a 4th PR cannot be admitted
    prs = [
        _pr(ChangeCondition.CLEAN, id=f"p{n}", mergeable=True, ci_green=True)
        for n in range(3)
    ]
    issue = _issue(id="i1")
    items = prs + [issue]
    result = _reconcile(items, ceiling=3)
    # The issue is downgraded from ACT to BLOCKED
    issue_result = next(r for r in result.items if r.item_id == "i1")
    assert issue_result.disposition.kind is DispositionKind.BLOCKED
    assert "admission denied" in issue_result.disposition.reason
    assert "WIP" in issue_result.disposition.reason
    assert result.admission_denied == 1
    assert result.admitted == 0
    # alpha-engine-config#6500: admission denial is a WIP-queue constraint,
    # not a §3.4 dependency chain — no blocking_chain to report.
    assert issue_result.disposition.blocking_chain == []


def test_blocked_issue_is_not_admitted():
    """A blocked issue is BLOCKED via deps, not via admission."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    issue = _issue(id="i1", declared_dependencies=[dep])
    result = _reconcile([issue], ceiling=5)
    r = result.items[0]
    assert r.disposition.kind is DispositionKind.BLOCKED
    assert "blocked on" in r.disposition.reason
    assert r.admission_reason is None  # not an admission denial


# ---------------------------------------------------------------------------
# 5. Aggregate counts (§7)
# ---------------------------------------------------------------------------

def test_aggregate_counts_correct():
    items = [
        _issue(id="i1"),  # ACT create_pr
        _pr(ChangeCondition.CI_RED, id="p1", ci_green=False),  # ACT fix_ci
        _issue(id="i2", labels=["gate:decision"]),  # UNDECIDABLE (unbacked gate)
        _issue(ItemStage.DONE, id="i3"),  # TERMINAL
        _pr(ChangeCondition.CLEAN, id="p2", mergeable=True, ci_green=True),  # TERMINAL (no lane)
    ]
    result = _reconcile(items, ceiling=5)
    assert result.enumerated == 5
    assert result.acted == 2  # create_pr + fix_ci
    assert result.terminal == 2  # merged + green-no-lane
    assert result.undecidable == 1  # unbacked gate projection (§3.3)
    assert result.blocked == 0  # none blocked by deps
    assert result.throughput == 2


def test_throughput_is_acted_count():
    items = [
        _issue(id="i1"),
        _pr(ChangeCondition.CONFLICTED, id="p1", mergeable=False),
    ]
    result = _reconcile(items, ceiling=5)
    assert result.throughput == result.acted == 2


# ---------------------------------------------------------------------------
# 6. Action ordering
# ---------------------------------------------------------------------------

def test_ordered_actions_fix_ci_before_automerge_before_create_pr():
    items = [
        _issue(id="i1"),  # create_pr (priority 4)
        _pr(ChangeCondition.CI_RED, id="p1", ci_green=False),  # fix_ci (priority 0)
        _pr(
            ChangeCondition.CLEAN,
            id="p2",
            mergeable=True,
            ci_green=True,
            labels=["groom-reviewed"],
        ),  # automerge (priority 3)
    ]
    result = _reconcile(items, ceiling=5)
    ordered = Reconciler.ordered_actions(result)
    actions = [r.disposition.action for r in ordered]
    # fix_ci (0) before automerge (3) before create_pr (4)
    assert actions == ["fix_ci", "automerge", "create_pr"]


def test_ordered_actions_only_includes_act():
    items = [
        _issue(id="i1"),  # ACT
        _issue(ItemStage.DONE, id="i2"),  # TERMINAL
    ]
    result = _reconcile(items, ceiling=5)
    ordered = Reconciler.ordered_actions(result)
    assert len(ordered) == 1
    assert ordered[0].disposition.action == "create_pr"


# ---------------------------------------------------------------------------
# 6b. §3.4 — the reconciler returns the FULL transitive chain, not just the
#     direct dependency (alpha-engine-config#6500)
# ---------------------------------------------------------------------------

def test_reconciler_names_full_transitive_chain_not_just_direct_dependency():
    """i1 -> i2 -> s3 root: the reconciler's own result names the whole chain.

    The named example from groom-sweep-policy's own clause registry
    (GS-3.4-dependencies-compose-transitively): a PR blocked on a data
    condition that is itself blocked on a weekly SF run must show the whole
    chain, not just "blocked on data condition" with the real root invisible.
    This is the same shape at the item-graph level: i1's direct dependency is
    i2 (an intermediate carried item), and the real root is i2's own
    dependency on an S3 key nobody has written yet.
    """
    root_dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://bucket/weekly-sf-output")
    i2 = _issue(id="i2", declared_dependencies=[root_dep])
    direct_dep = Dependency(kind=DependencyKind.ISSUE_TERMINAL, target="i2")
    i1 = _issue(id="i1", declared_dependencies=[direct_dep])

    result = _reconcile([i1, i2], ceiling=5)

    r1 = next(r for r in result.items if r.item_id == "i1")
    assert r1.disposition.kind is DispositionKind.BLOCKED
    # Not just the direct dependency (i2) — the full chain to the root.
    assert r1.disposition.blocking_chain == [
        "issue_terminal:i2",
        "s3_object:s3://bucket/weekly-sf-output",
    ]
    # The root cause is named in the human-readable reason too, not only in
    # the direct-dependency's target.
    assert "s3://bucket/weekly-sf-output" in r1.disposition.reason
    assert result.blocked == 2  # both i1 (transitively) and i2 (directly)


def test_skipped_blocked_item_still_carries_the_full_chain():
    """§5.5 skip round-trip: a re-derived skip must not degrade the chain.

    A skipped item's disposition is *rebuilt from the store*, not
    recomputed (§5.5). If the store did not persist blocking_chain, every
    cycle that skips a still-blocked item would silently lose the
    structured chain even though nothing about the block changed —
    exactly the drift the store's docstring already warns about for
    disposition_kind/reason/action (alpha-engine-config#6500).
    """
    root_dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://bucket/weekly-sf-output")
    i2 = _issue(id="i2", declared_dependencies=[root_dep])
    direct_dep = Dependency(kind=DependencyKind.ISSUE_TERMINAL, target="i2")
    i1 = _issue(id="i1", declared_dependencies=[direct_dep])
    items = [i1, i2]
    store = GenerationStore()

    _reconcile(items, store=store, generation=1)
    result = _reconcile(items, store=store, generation=2)

    r1 = next(r for r in result.items if r.item_id == "i1")
    assert r1.skipped is True
    assert r1.disposition.kind is DispositionKind.BLOCKED
    assert r1.disposition.blocking_chain == [
        "issue_terminal:i2",
        "s3_object:s3://bucket/weekly-sf-output",
    ]


# ---------------------------------------------------------------------------
# 7. End-to-end: a mixed backlog
# ---------------------------------------------------------------------------

def test_mixed_backlog_end_to_end():
    """A realistic mixed backlog produces sensible dispositions and counts."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    items = [
        # Actionable issue, unblocked → ACT create_pr
        _issue(id="i1"),
        # Actionable issue, blocked on s3 → BLOCKED
        _issue(id="i2", declared_dependencies=[dep]),
        # Green PR with lane → ACT automerge
        _pr(
            ChangeCondition.CLEAN,
            id="p1",
            mergeable=True,
            ci_green=True,
            labels=["groom-reviewed"],
        ),
        # Red PR → ACT fix_ci
        _pr(ChangeCondition.CI_RED, id="p2", ci_green=False),
        # Done → TERMINAL
        _issue(ItemStage.DONE, id="i3"),
        # The intent record of a unit already in flight → TERMINAL (its change
        # carries the verdict; one unit, one verdict)
        Item(id="i4", stage=ItemStage.IN_FLIGHT, change_ref="p9"),
    ]
    result = _reconcile(items, ceiling=5)
    assert result.enumerated == 6
    assert result.acted == 3  # create_pr + automerge + fix_ci
    assert result.blocked == 1  # i2 blocked on s3
    assert result.terminal == 2  # done + the intent record deferring to its change
    assert result.undecidable == 0
    assert result.throughput == 3


# ---------------------------------------------------------------------------
# 8. §5.6 identity invariant (alpha-engine-config#6316) — exactly one
#    in-flight change per item, asserted by the reconciler as an invariant,
#    not repaired after the fact by a separate pass (`duplicate_pr_sweep.py`).
# ---------------------------------------------------------------------------

def test_duplicate_in_flight_change_is_rejected_not_actioned():
    """Construction: an item whose harness observed TWO open changes for the
    same intent. The reconciler must never let this reach ACT or an
    auto-merge lane — it must be flagged (UNDECIDABLE) and counted, by
    construction, for every such item, not merely for a sampled example.
    """
    duplicated = Item(
        id="i1",
        stage=ItemStage.IN_FLIGHT,
        change_ref="100",
        additional_change_refs=["101"],
    )
    result = _reconcile([duplicated], ceiling=5)
    r = result.items[0]
    assert r.disposition.kind is DispositionKind.UNDECIDABLE
    assert r.disposition.kind is not DispositionKind.ACT
    assert r.identity_conflict is True
    assert result.identity_conflicts == 1
    assert result.acted == 0


def test_duplicate_in_flight_change_holds_across_a_family_of_constructions():
    """Not a single sampled check: every member of a small family of
    duplicate-declaring items resolves the same way — UNDECIDABLE, flagged,
    never ACT — regardless of how many extra refs or what stage-adjacent
    shape the item takes.
    """
    duplicates = [
        Item(id="i1", stage=ItemStage.IN_FLIGHT, change_ref="100",
             additional_change_refs=["101"]),
        Item(id="i2", stage=ItemStage.IN_FLIGHT, change_ref="200",
             additional_change_refs=["201", "202"]),
        _pr(ChangeCondition.CLEAN, id="p3", mergeable=True, ci_green=True,
            labels=["groom-reviewed"], additional_change_refs=["p4"]),
        _pr(ChangeCondition.CI_RED, id="p5", ci_green=False,
            additional_change_refs=["p6"]),
    ]
    result = _reconcile(duplicates, ceiling=5)
    assert result.identity_conflicts == len(duplicates)
    for r in result.items:
        assert r.identity_conflict is True
        assert r.disposition.kind is DispositionKind.UNDECIDABLE
    assert result.acted == 0


def test_non_duplicated_items_are_unaffected_by_the_invariant_check():
    """The invariant only fires for items that actually declare a conflict —
    an ordinary population is untouched.
    """
    items = [
        _issue(id="i1"),
        _pr(ChangeCondition.CLEAN, id="p1", mergeable=True, ci_green=True,
            labels=["groom-reviewed"]),
    ]
    result = _reconcile(items, ceiling=5)
    assert result.identity_conflicts == 0
    for r in result.items:
        assert r.identity_conflict is False


def test_terminal_item_disposition_never_regresses_via_a_stale_skip():
    """§5.6's second invariant: a change whose item is terminal is itself
    terminal. `compute_disposition` guarantees this on a fresh evaluation;
    the only way to violate it is a STALE store record replayed through the
    §5.5 skip path (the fingerprint has no component sensitive to
    `Item.stage` itself). Construct exactly that stale record and assert the
    reconciler self-heals to TERMINAL rather than replaying the stale ACT.
    """
    item_in_flight = _pr(ChangeCondition.CI_RED, id="p1", ci_green=False)
    store = GenerationStore()
    world = _world_full()
    # Cycle 1: item is genuinely in flight and red — records a real ACT.
    record_evaluation(
        item_in_flight,
        store,
        generation=1,
        closure_state=[],
        disposition=Disposition(kind=DispositionKind.ACT, action="fix_ci"),
        stage=ItemStage.IN_FLIGHT,
    )
    # Cycle 2: the SAME item id, now recorded as DONE by the harness, with a
    # label/deps/closure fingerprint identical to cycle 1 (so the skip
    # logic — blind to `stage` — would consider it unchanged and replay the
    # stale ACT verbatim if nothing corrected it).
    item_done = Item(id="p1", stage=ItemStage.DONE)
    result = _reconcile([item_done], world=world, store=store, generation=2)
    r = result.items[0]
    assert r.disposition.kind is DispositionKind.TERMINAL
    assert r.disposition.kind is not DispositionKind.ACT
    assert result.terminal == 1
    assert result.acted == 0
