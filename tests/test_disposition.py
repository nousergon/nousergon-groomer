"""Contract tests for the disposition function (issue #7, §5.1 totality).

These tests assert:

1. **Exhaustiveness** — every ``ItemStage`` is handled (either dispatched or
   terminal) and every ``ChangeCondition`` has a handler, so neither a new
   stage nor a new condition can silently fall through.
2. **Per-stage correctness** — each stage yields the documented disposition.
3. **Precedence** — human-owned > excluded > terminal > undecidable >
   unbacked-gate > stage-specific.
4. **F5 readiness** — an item in flight blocked on a dep returns BLOCKED, not ACT.

Every rule asserted here is defined over an item and scoped to a stage. There
is deliberately no test named "a PR does X": §3 forbids a rule over pull
requests alone, and a test written that way would be asserting the model this
change removed.
"""
from __future__ import annotations

import datetime as _dt

from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.dependency_graph import DependencyGraph
from nousergon_groomer.disposition import (
    _CONDITION_DISPATCH,
    _STAGE_DISPATCH,
    _TERMINAL_STAGES,
    compute_disposition,
)
from nousergon_groomer.models import (
    Change,
    ChangeCondition,
    Dependency,
    DependencyKind,
    DispositionKind,
    Item,
    ItemStage,
    VerificationObligation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _world_full() -> ObservedWorld:
    """A world where every surface is reported (no undecidables)."""
    return ObservedWorld(
        s3_objects=set(),
        s3_prefixes=set(),
        terminal_items=set(),
        pipeline_runs=set(),
        today=None,
    )


def _item(stage: ItemStage = ItemStage.PROPOSED, **kw) -> Item:
    """An item with no change — every stage before one exists."""
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
        security_threads=kw.pop("security_threads", 0),
    )
    defaults = {
        "id": item_id,
        "stage": kw.pop("stage", ItemStage.IN_FLIGHT),
        "change": change,
    }
    defaults.update(kw)
    return Item(**defaults)


def _graph(items: list[Item], world: ObservedWorld) -> DependencyGraph:
    return DependencyGraph(items, world)


# ---------------------------------------------------------------------------
# 1. Exhaustiveness — every ItemStage and every ChangeCondition is covered
# ---------------------------------------------------------------------------

def test_every_stage_is_terminal_or_dispatched():
    """§5.1 totality: every ItemStage is in the dispatch table or terminal set."""
    uncovered = set(ItemStage) - (set(_STAGE_DISPATCH) | _TERMINAL_STAGES)
    assert not uncovered, f"ItemStage values with no handler: {uncovered}"


def test_no_stage_is_both_terminal_and_dispatched():
    """A stage should not appear in both the terminal set and the dispatch table."""
    overlap = _TERMINAL_STAGES & set(_STAGE_DISPATCH)
    assert not overlap, f"stages in both terminal and dispatch: {overlap}"


def test_every_change_condition_is_dispatched():
    """The second totality surface the stage model introduces.

    Demoting the PR sub-states from item states to stage conditions moved them
    out of ``_STAGE_DISPATCH``'s exhaustiveness check. Without this test a new
    condition would reach a ``NotImplementedError`` in production rather than a
    red test here.
    """
    uncovered = set(ChangeCondition) - set(_CONDITION_DISPATCH)
    assert not uncovered, f"ChangeCondition values with no handler: {uncovered}"


# ---------------------------------------------------------------------------
# 2. Precedence — human-owned > excluded > terminal > undecidable >
#    unbacked-gate > stage-specific
# ---------------------------------------------------------------------------

def test_human_owned_takes_precedence_over_terminal():
    """§9 carve-out 1: a human-owned merged item is TERMINAL (surface and stop)."""
    item = _item(ItemStage.MERGED, human_owned=True)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.TERMINAL
    assert "human-owned" in d.reason


def test_human_owned_takes_precedence_over_ready():
    """A human-owned ready item is TERMINAL, not ACT."""
    item = _item(human_owned=True)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.TERMINAL


def test_undecidable_dep_takes_precedence_over_stage_action():
    """A ready item with an undecidable dep is UNDECIDABLE, not ACT."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = _item(declared_dependencies=[dep])
    # s3_objects is None → undecidable
    world = ObservedWorld()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.UNDECIDABLE
    assert "undecidable" in d.reason


# ---------------------------------------------------------------------------
# 3. Per-stage correctness
# ---------------------------------------------------------------------------

def test_done_is_terminal():
    item = _item(ItemStage.DONE)
    world = _world_full()
    graph = _graph([item], world)
    assert compute_disposition(item, graph, world).kind is DispositionKind.TERMINAL


def test_abandoned_is_terminal():
    item = _item(ItemStage.ABANDONED)
    world = _world_full()
    graph = _graph([item], world)
    assert compute_disposition(item, graph, world).kind is DispositionKind.TERMINAL


def test_do_not_groom_is_terminal_without_overwriting_the_stage():
    item = _item(ItemStage.PROPOSED, do_not_groom=True)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.TERMINAL
    assert "do_not_groom" in d.reason


def test_ready_item_is_act_create_pr():
    item = _item()
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "create_pr"


def test_proposed_item_with_an_unsatisfied_dependency_is_blocked():
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = _item(declared_dependencies=[dep])
    world = _world_full()  # s3_objects is empty set → dep unsatisfied
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.BLOCKED
    assert "blocked on" in d.reason


def test_a_recorded_ready_stage_is_re_derived_not_trusted():
    """A harness may record `ready`; the loop re-derives it every cycle (§3).

    Trusting the recorded value would reintroduce asserted blocked-ness — the
    exact property §3 abolishes — one bad write away.
    """
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = _item(ItemStage.READY, declared_dependencies=[dep])
    world = _world_full()
    graph = _graph([item], world)
    assert compute_disposition(item, graph, world).kind is DispositionKind.BLOCKED


def test_an_in_flight_item_whose_change_is_enumerated_separately_defers():
    """One unit, one verdict — the record holding the change owns it (§3)."""
    item = Item(id="i1", stage=ItemStage.IN_FLIGHT, change_ref="p1")
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.TERMINAL
    assert "p1" in d.reason


def test_clean_change_no_lane_is_terminal():
    """A clean, green change not auto-merge-eligible → terminal (needs review)."""
    item = _pr(mergeable=True, ci_green=True)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.TERMINAL
    assert "human review" in d.reason


def test_clean_change_with_lane_is_act():
    """A clean, green change with an auto-merge lane → ACT (automerge)."""
    item = _pr(mergeable=True, ci_green=True, labels=["groom-reviewed"])
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "automerge"


def test_ci_red_change_is_act():
    item = _pr(ChangeCondition.CI_RED, ci_green=False)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "fix_ci"


def test_conflicted_change_is_act_not_blocked():
    """alpha-engine-config#6307 closes-when 2, at the disposition function.

    §3.5: a conflict is inside the loop's own write authority, so it is work.
    Under the pre-0.9.0 model the pull request had its own blocked state, and a
    conflict — a property of the in-flight stage — got declared a dependency of
    the item, which is how most blocked PRs in the fleet came to be merely
    conflicted or behind ``main``.
    """
    item = _pr(ChangeCondition.CONFLICTED, mergeable=False)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "resolve_conflicts"
    assert d.kind is not DispositionKind.BLOCKED


def test_draft_change_is_act():
    item = _pr(ChangeCondition.DRAFT)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "advance_draft"


def test_draft_with_unbacked_gate_label_is_undecidable():
    """A gate:* label with no declaration is a §3.3 defect, not a state.

    Previously TERMINAL ("at rest on gate label"), which read the label as
    state — retired by alpha-engine-config#6137.
    """
    item = _pr(ChangeCondition.DRAFT, labels=["gate:weekly-sf"])
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.UNDECIDABLE
    assert "gate:weekly-sf" in d.reason
    assert "no declared dependency" in d.reason


def test_open_draft_with_a_satisfied_gate_dependency_is_act():
    """A cleared gate advances the draft — no label edit, no actor (§3)."""
    dep = Dependency(kind=DependencyKind.PIPELINE_RUN, target="ne-weekly-freshness-pipeline")
    item = _pr(ChangeCondition.DRAFT, labels=["gate:weekly-sf"],
               declared_dependencies=[dep])
    world = ObservedWorld(
        s3_objects=set(), s3_prefixes=set(), terminal_items=set(),
        pipeline_runs={"ne-weekly-freshness-pipeline"},
    )
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "advance_draft"


def test_open_draft_with_an_unsatisfied_gate_dependency_names_the_condition():
    """BLOCKED names the S3 key the gate stood for, not the label."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/weekly.json")
    item = _pr(ChangeCondition.DRAFT, labels=["gate:weekly-sf"],
               declared_dependencies=[dep])
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.BLOCKED
    assert "s3://b/weekly.json" in d.reason
    assert "gate:weekly-sf" not in d.reason


def test_open_pending_ci_is_terminal():
    item = _pr(ChangeCondition.CI_PENDING)
    world = _world_full()
    graph = _graph([item], world)
    assert compute_disposition(item, graph, world).kind is DispositionKind.TERMINAL


# ---------------------------------------------------------------------------
# 3b. The merged stage and its verification obligation (§3.8)
#
#     `merged` is not terminal. An item resting here is owed a post-merge
#     verification its own merged code produces — a real condition outside the
#     loop's authority, so a legitimate BLOCKED, but a bounded one.
# ---------------------------------------------------------------------------

def _verified_when(target="s3://b/verify.json", deadline="2026-08-10"):
    return VerificationObligation(
        predicate=Dependency(kind=DependencyKind.S3_OBJECT, target=target),
        deadline=deadline,
    )


def _dated_world(**kw) -> ObservedWorld:
    world = _world_full()
    return world.model_copy(update={"today": _dt.date(2026, 8, 4), **kw})


def test_merged_awaiting_verification_is_blocked_not_terminal():
    item = _item(ItemStage.MERGED, verification=_verified_when())
    world = _dated_world()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.BLOCKED
    assert "awaiting post-merge verification" in d.reason
    assert "2026-08-10" in d.reason


def test_merged_with_a_satisfied_predicate_is_verified_and_terminal():
    item = _item(ItemStage.MERGED, verification=_verified_when())
    world = _dated_world(s3_objects={"s3://b/verify.json"})
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.TERMINAL
    assert "verified in production" in d.reason


def test_merged_past_its_deadline_is_act_revert():
    """§3.6: expiry is an event with an action, never another interval."""
    item = _item(ItemStage.MERGED, verification=_verified_when(deadline="2026-08-01"))
    world = _dated_world()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "revert"


def test_merged_honours_a_custom_revert_action():
    obligation = VerificationObligation(
        predicate=Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/verify.json"),
        deadline="2026-08-01",
        revert_action="roll_back_deployment",
    )
    item = _item(ItemStage.MERGED, verification=obligation)
    world = _dated_world()
    graph = _graph([item], world)
    assert compute_disposition(item, graph, world).action == "roll_back_deployment"


def test_merged_with_an_undecidable_predicate_is_undecidable():
    item = _item(ItemStage.MERGED, verification=_verified_when())
    world = _dated_world(s3_objects=None)
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.UNDECIDABLE
    assert "s3://b/verify.json" in d.reason


def test_merged_with_no_reported_date_is_undecidable_not_not_yet():
    """Whether the obligation expired is unknowable, and "not yet" is not a
    safe default — that is how an expired wait becomes an unbounded one."""
    item = _item(ItemStage.MERGED, verification=_verified_when())
    world = _world_full()  # today is None
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.UNDECIDABLE
    assert "no date" in d.reason


# ---------------------------------------------------------------------------
# 4. F5 readiness — a blocked dep outranks every in-flight condition
# ---------------------------------------------------------------------------

def test_green_pr_blocked_on_dep_is_blocked_not_act():
    """F5: a green PR blocked on a declared dep is BLOCKED, not ACT (automerge)."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = _pr(
        ChangeCondition.CLEAN,
        mergeable=True,
        ci_green=True,
        labels=["groom-reviewed"],
        declared_dependencies=[dep],
    )
    world = _world_full()  # s3_objects empty → dep unsatisfied
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.BLOCKED


def test_red_pr_blocked_on_dep_is_blocked_not_act():
    """F5: a red PR blocked on a dep is BLOCKED, not ACT (fix_ci)."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = _pr(
        ChangeCondition.CI_RED,
        ci_green=False,
        declared_dependencies=[dep],
    )
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.BLOCKED


# ---------------------------------------------------------------------------
# 5. Transitive blocked-ness (§3.4) flows through disposition
# ---------------------------------------------------------------------------

def test_transitive_block_surfaces_root_cause():
    """An issue blocked through an intermediate issue names the root leaf."""
    # i2 is blocked on s3://b/k (root cause)
    dep2 = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    i2 = _item(id="i2", declared_dependencies=[dep2])
    # i1 depends on i2 being terminal; i2 is not terminal → i1 is transitively blocked
    dep1 = Dependency(kind=DependencyKind.ISSUE_TERMINAL, target="i2")
    i1 = _item(id="i1", declared_dependencies=[dep1])
    world = _world_full()
    graph = _graph([i1, i2], world)
    d = compute_disposition(i1, graph, world)
    assert d.kind is DispositionKind.BLOCKED
    # The chain should mention both the issue_terminal dep and the s3 root
    assert "i2" in d.reason
    assert "s3://b/k" in d.reason


# ---------------------------------------------------------------------------
# 6. Gate-label retirement (alpha-engine-config#6137, nous-ergon-ops#356)
#
#    No branch in the disposition function reads a label as state. What the
#    2026-07-22 incident cost — six gated, CI-red PRs auto-merged in minutes
#    because one upstream check owned the gate exclusion — is what these
#    assert cannot recur through the permissive direction.
# ---------------------------------------------------------------------------

def test_green_lane_pr_with_unbacked_gate_label_is_undecidable_not_automerge():
    """The auto-merge safety property, stated against the worst case.

    Everything about this PR says merge it: green, mergeable, no security
    threads, exactly one auto-merge lane label. The only thing standing
    against it is a gate:* label with nothing declared behind it — and a core
    that resolved that projection in the permissive direction would merge it.
    """
    item = _pr(ChangeCondition.CLEAN, labels=["gate:operator", "groom-reviewed"],
               mergeable=True, ci_green=True)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.UNDECIDABLE
    assert "gate:operator" in d.reason
    assert d.action is None


def test_green_lane_pr_with_satisfied_gate_dependency_automerges():
    """A cleared gate stops blocking when the WORLD says so, not the label.

    The stale ``gate:weekly-sf`` label is still attached — under the retired
    implementation that alone made the PR ineligible until a sweep removed it.
    """
    deps = [
        Dependency(kind=DependencyKind.PIPELINE_RUN, target="ne-weekly-freshness-pipeline"),
        Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/weekly.json"),
    ]
    item = _pr(ChangeCondition.CLEAN, labels=["gate:weekly-sf", "groom-reviewed"],
               mergeable=True, ci_green=True, declared_dependencies=deps)
    world = ObservedWorld(
        s3_objects={"s3://b/weekly.json"}, s3_prefixes=set(), terminal_items=set(),
        pipeline_runs={"ne-weekly-freshness-pipeline"},
    )
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "automerge"


def test_green_lane_pr_with_unsatisfied_gate_dependency_is_blocked_on_the_condition():
    deps = [Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/weekly.json")]
    item = _pr(ChangeCondition.CLEAN, labels=["gate:weekly-sf", "groom-reviewed"],
               mergeable=True, ci_green=True, declared_dependencies=deps)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.BLOCKED
    assert "s3://b/weekly.json" in d.reason


def test_gate_labelled_issue_with_no_declaration_is_undecidable_not_actionable():
    """The issue half of the same population — never silently dispatched."""
    item = _item(labels=["gate:decision", "P1"])
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.UNDECIDABLE
    assert "gate:decision" in d.reason


def test_undecidable_dependency_outranks_the_unbacked_projection_check():
    """Precedence: a declared-but-unobservable dep is named before the label.

    An item with declarations has no unbacked projection by construction, so
    this pins the ordering rather than a conflict — the reason the operator
    reads must be the dependency the world failed to report.
    """
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = _pr(ChangeCondition.CLEAN, labels=["gate:data"], mergeable=True,
               ci_green=True, declared_dependencies=[dep])
    world = ObservedWorld()  # nothing reported
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.UNDECIDABLE
    assert "s3://b/k" in d.reason


def test_disposition_module_reads_no_gate_label():
    """Structural: the retired short-circuit cannot creep back in."""
    import inspect

    from nousergon_groomer import disposition as disposition_module

    source = inspect.getsource(disposition_module)
    body = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "has_gate_label" not in body
    assert "item.labels" not in body
