"""Contract tests for the disposition function (issue #7, §5.1 totality).

These tests assert:

1. **Exhaustiveness** — every ``ItemState`` is handled (either dispatched or
   terminal), so a new state can never silently fall through.
2. **Per-state correctness** — each state yields the documented disposition.
3. **Precedence** — human-owned > terminal > undecidable > state-specific.
4. **F5 readiness** — a green PR blocked on a dep returns BLOCKED, not ACT.
"""
from __future__ import annotations

from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.dependency_graph import DependencyGraph
from nousergon_groomer.disposition import _STATE_DISPATCH, _TERMINAL_STATES, compute_disposition
from nousergon_groomer.models import (
    Dependency,
    DependencyKind,
    DispositionKind,
    Item,
    ItemKind,
    ItemState,
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


def _item(state: ItemState, **kw) -> Item:
    defaults = {"id": "i1", "kind": ItemKind.ISSUE, "state": state}
    defaults.update(kw)
    return Item(**defaults)


def _pr(state: ItemState, **kw) -> Item:
    defaults = {"id": "p1", "kind": ItemKind.PR, "state": state}
    defaults.update(kw)
    return Item(**defaults)


def _graph(items: list[Item], world: ObservedWorld) -> DependencyGraph:
    return DependencyGraph(items, world)


# ---------------------------------------------------------------------------
# 1. Exhaustiveness — every ItemState is covered
# ---------------------------------------------------------------------------

def test_every_state_is_terminal_or_dispatched():
    """§5.1 totality: every ItemState must be in the dispatch table or terminal set."""
    all_states = set(ItemState)
    covered = set(_STATE_DISPATCH) | _TERMINAL_STATES
    uncovered = all_states - covered
    assert not uncovered, f"ItemState values with no handler: {uncovered}"


def test_no_state_is_both_terminal_and_dispatched():
    """A state should not appear in both the terminal set and the dispatch table."""
    overlap = _TERMINAL_STATES & set(_STATE_DISPATCH)
    assert not overlap, f"states in both terminal and dispatch: {overlap}"


# ---------------------------------------------------------------------------
# 2. Precedence — human-owned > terminal > undecidable > state-specific
# ---------------------------------------------------------------------------

def test_human_owned_takes_precedence_over_terminal():
    """§9 carve-out 1: a human-owned MERGED item is TERMINAL (surface and stop)."""
    item = _item(ItemState.MERGED, human_owned=True)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.TERMINAL
    assert "human-owned" in d.reason


def test_human_owned_takes_precedence_over_actionable():
    """A human-owned actionable issue is TERMINAL, not ACT."""
    item = _item(ItemState.OPEN_ISSUE_ACTIONABLE, human_owned=True)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.TERMINAL


def test_undecidable_dep_takes_precedence_over_state_action():
    """An actionable issue with an undecidable dep is UNDECIDABLE, not ACT."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = _item(ItemState.OPEN_ISSUE_ACTIONABLE, declared_dependencies=[dep])
    # s3_objects is None → undecidable
    world = ObservedWorld()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.UNDECIDABLE
    assert "undecidable" in d.reason


# ---------------------------------------------------------------------------
# 3. Per-state correctness
# ---------------------------------------------------------------------------

def test_merged_is_terminal():
    item = _item(ItemState.MERGED)
    world = _world_full()
    graph = _graph([item], world)
    assert compute_disposition(item, graph, world).kind is DispositionKind.TERMINAL


def test_closed_is_terminal():
    item = _item(ItemState.CLOSED)
    world = _world_full()
    graph = _graph([item], world)
    assert compute_disposition(item, graph, world).kind is DispositionKind.TERMINAL


def test_do_not_groom_is_terminal():
    item = _item(ItemState.DO_NOT_GROOM)
    world = _world_full()
    graph = _graph([item], world)
    assert compute_disposition(item, graph, world).kind is DispositionKind.TERMINAL


def test_open_issue_actionable_unblocked_is_act():
    item = _item(ItemState.OPEN_ISSUE_ACTIONABLE)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "create_pr"


def test_open_issue_actionable_blocked_is_blocked():
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = _item(ItemState.OPEN_ISSUE_ACTIONABLE, declared_dependencies=[dep])
    world = _world_full()  # s3_objects is empty set → dep unsatisfied
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.BLOCKED
    assert "blocked on" in d.reason


def test_open_issue_blocked_with_chain_is_blocked():
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = _item(ItemState.OPEN_ISSUE_BLOCKED, declared_dependencies=[dep])
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.BLOCKED


def test_open_issue_blocked_without_chain_is_undecidable():
    """State says BLOCKED but no unsatisfied dep → loud inconsistency."""
    item = _item(ItemState.OPEN_ISSUE_BLOCKED)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.UNDECIDABLE
    assert "inconsistency" in d.reason


def test_open_issue_waiting_is_terminal():
    item = _item(ItemState.OPEN_ISSUE_WAITING)
    world = _world_full()
    graph = _graph([item], world)
    assert compute_disposition(item, graph, world).kind is DispositionKind.TERMINAL


def test_open_clean_green_no_lane_is_terminal():
    """Green PR not auto-merge-eligible → terminal (needs human review)."""
    item = _pr(ItemState.OPEN_CLEAN_GREEN, mergeable=True, ci_green=True)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.TERMINAL
    assert "human review" in d.reason


def test_open_clean_green_with_lane_is_act():
    """Green PR with an auto-merge lane → ACT (automerge)."""
    item = _pr(
        ItemState.OPEN_CLEAN_GREEN,
        mergeable=True,
        ci_green=True,
        labels=["groom-reviewed"],
    )
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "automerge"


def test_open_red_ci_is_act():
    item = _pr(ItemState.OPEN_RED_CI, ci_green=False)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "fix_ci"


def test_open_dirty_is_act():
    item = _pr(ItemState.OPEN_DIRTY, mergeable=False)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "resolve_conflicts"


def test_open_draft_no_gate_is_act():
    item = _pr(ItemState.OPEN_DRAFT, is_draft=True)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.ACT
    assert d.action == "advance_draft"


def test_open_draft_with_unbacked_gate_label_is_undecidable():
    """A gate:* label with no declaration is a §3.3 defect, not a state.

    Previously TERMINAL ("at rest on gate label"), which read the label as
    state — retired by alpha-engine-config#6137.
    """
    item = _pr(ItemState.OPEN_DRAFT, is_draft=True, labels=["gate:weekly-sf"])
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.UNDECIDABLE
    assert "gate:weekly-sf" in d.reason
    assert "no declared dependency" in d.reason


def test_open_draft_with_a_satisfied_gate_dependency_is_act():
    """A cleared gate advances the draft — no label edit, no actor (§3)."""
    dep = Dependency(kind=DependencyKind.PIPELINE_RUN, target="ne-weekly-freshness-pipeline")
    item = _pr(ItemState.OPEN_DRAFT, is_draft=True, labels=["gate:weekly-sf"],
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
    item = _pr(ItemState.OPEN_DRAFT, is_draft=True, labels=["gate:weekly-sf"],
               declared_dependencies=[dep])
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.BLOCKED
    assert "s3://b/weekly.json" in d.reason
    assert "gate:weekly-sf" not in d.reason


def test_open_pending_ci_is_terminal():
    item = _pr(ItemState.OPEN_PENDING_CI)
    world = _world_full()
    graph = _graph([item], world)
    assert compute_disposition(item, graph, world).kind is DispositionKind.TERMINAL


# ---------------------------------------------------------------------------
# 4. F5 readiness — blocked deps override advanceable PR states
# ---------------------------------------------------------------------------

def test_green_pr_blocked_on_dep_is_blocked_not_act():
    """F5: a green PR blocked on a declared dep is BLOCKED, not ACT (automerge)."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = _pr(
        ItemState.OPEN_CLEAN_GREEN,
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
        ItemState.OPEN_RED_CI,
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
    i2 = _item(ItemState.OPEN_ISSUE_BLOCKED, id="i2", declared_dependencies=[dep2])
    # i1 depends on i2 being terminal; i2 is not terminal → i1 is transitively blocked
    dep1 = Dependency(kind=DependencyKind.ISSUE_TERMINAL, target="i2")
    i1 = _item(ItemState.OPEN_ISSUE_ACTIONABLE, id="i1", declared_dependencies=[dep1])
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
    item = _pr(ItemState.OPEN_CLEAN_GREEN, labels=["gate:operator", "groom-reviewed"],
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
    item = _pr(ItemState.OPEN_CLEAN_GREEN, labels=["gate:weekly-sf", "groom-reviewed"],
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
    item = _pr(ItemState.OPEN_CLEAN_GREEN, labels=["gate:weekly-sf", "groom-reviewed"],
               mergeable=True, ci_green=True, declared_dependencies=deps)
    world = _world_full()
    graph = _graph([item], world)
    d = compute_disposition(item, graph, world)
    assert d.kind is DispositionKind.BLOCKED
    assert "s3://b/weekly.json" in d.reason


def test_gate_labelled_issue_with_no_declaration_is_undecidable_not_actionable():
    """The issue half of the same population — never silently dispatched."""
    item = _item(ItemState.OPEN_ISSUE_ACTIONABLE, labels=["gate:decision", "P1"])
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
    item = _pr(ItemState.OPEN_CLEAN_GREEN, labels=["gate:data"], mergeable=True,
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
