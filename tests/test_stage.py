"""Contract tests for the derived stage boundaries (§3, §3.8).

Two of the six forward stages are computed rather than stored, and both
derivations are load-bearing:

- ``proposed`` → ``ready`` is where blocked-ness stops being asserted (§3).
- ``merged`` → ``verified`` is where the post-merge verification obligation
  lives, and it is what makes ``merged`` a stage rather than the end (§3.8).

The second is the one nothing else can catch. F7 measures lead time to
``verified``; if ``merged`` were terminal the number would simply be shorter
and still look plausible, so the failure would arrive as a good-looking metric
rather than as a broken test.
"""
from __future__ import annotations

import datetime as _dt

from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.dependency_graph import DependencyGraph
from nousergon_groomer.models import (
    Change,
    ChangeCondition,
    Dependency,
    DependencyKind,
    Item,
    ItemStage,
    VerificationObligation,
)
from nousergon_groomer.stage import deadline_passed, effective_stage, evaluate_verification


def _world(**kw) -> ObservedWorld:
    defaults = {
        "s3_objects": set(), "s3_prefixes": set(), "terminal_items": set(),
        "pipeline_runs": set(), "today": _dt.date(2026, 8, 4),
    }
    defaults.update(kw)
    return ObservedWorld(**defaults)


def _stage_of(item: Item, world: ObservedWorld) -> ItemStage:
    return effective_stage(item, DependencyGraph([item], world), world)


def _obligation(target="s3://b/verify.json", deadline="2026-08-10"):
    return VerificationObligation(
        predicate=Dependency(kind=DependencyKind.S3_OBJECT, target=target),
        deadline=deadline,
    )


# ---------------------------------------------------------------------------
# proposed → ready
# ---------------------------------------------------------------------------

def test_an_unblocked_proposed_item_is_ready():
    item = Item(id="i1", stage=ItemStage.PROPOSED)
    assert _stage_of(item, _world()) is ItemStage.READY


def test_a_blocked_proposed_item_stays_proposed():
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = Item(id="i1", stage=ItemStage.PROPOSED, declared_dependencies=[dep])
    assert _stage_of(item, _world()) is ItemStage.PROPOSED


def test_a_recorded_ready_item_is_re_derived():
    """Storing readiness would be asserting blocked-ness — §3 abolishes that."""
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = Item(id="i1", stage=ItemStage.READY, declared_dependencies=[dep])
    assert _stage_of(item, _world()) is ItemStage.PROPOSED


def test_an_undecidable_dependency_is_not_ready():
    """Unconfirmed is not ready.

    An undecidable dependency is deliberately not a definite blocker, so the
    blocking chain is empty — but the item has not been *shown* ready either,
    and calling it ready would enter it in F3's conversion denominator and
    start F7's residence clock on a fact nobody established.
    """
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    item = Item(id="i1", stage=ItemStage.PROPOSED, declared_dependencies=[dep])
    assert _stage_of(item, ObservedWorld()) is ItemStage.PROPOSED


def test_readiness_sees_a_blocker_two_hops_away():
    """The closure is walked, not the item's own declarations (§3.4)."""
    a = Item(id="a", stage=ItemStage.PROPOSED, declared_dependencies=[
        Dependency(kind=DependencyKind.ISSUE_TERMINAL, target="b")])
    b = Item(id="b", stage=ItemStage.PROPOSED, declared_dependencies=[
        Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")])
    world = _world()
    graph = DependencyGraph([a, b], world)
    assert effective_stage(a, graph, world) is ItemStage.PROPOSED


# ---------------------------------------------------------------------------
# merged → verified (§3.8) — closes-when 3
# ---------------------------------------------------------------------------

def test_merged_with_no_obligation_is_verified():
    """Merging *was* the verification when nothing is only checkable after it."""
    item = Item(id="m1", stage=ItemStage.MERGED)
    assert _stage_of(item, _world()) is ItemStage.VERIFIED


def test_merged_with_a_satisfied_obligation_is_verified():
    item = Item(id="m1", stage=ItemStage.MERGED, verification=_obligation())
    world = _world(s3_objects={"s3://b/verify.json"})
    assert _stage_of(item, world) is ItemStage.VERIFIED


def test_merged_with_an_unsatisfied_obligation_stays_merged():
    """`merged` is a stage the item genuinely rests in, not the end."""
    item = Item(id="m1", stage=ItemStage.MERGED, verification=_obligation())
    assert _stage_of(item, _world()) is ItemStage.MERGED


def test_merged_with_an_undecidable_obligation_stays_merged():
    """"We could not look" is not "it has not held" — and neither is verified."""
    item = Item(id="m1", stage=ItemStage.MERGED, verification=_obligation())
    assert _stage_of(item, ObservedWorld()) is ItemStage.MERGED


def test_verified_is_reachable_from_a_real_reconciler_population():
    """closes-when 3, end to end: `merged` is not terminal and `verified` is
    reached without any actor advancing the item."""
    from nousergon_groomer.observed_gen import GenerationStore
    from nousergon_groomer.reconciler import Reconciler, ReconcilerConfig

    item = Item(id="m1", stage=ItemStage.MERGED, verification=_obligation())
    store = GenerationStore()
    config = ReconcilerConfig(wip_ceiling=5, observed_at="2026-08-04T00:00:00+00:00")

    before = Reconciler(config).reconcile([item], _world(), store)
    assert before.items[0].stage is ItemStage.MERGED

    # Nothing about the item changed. The artifact its merged code produces
    # appeared, and the next cycle computes the transition — no actor, no
    # label edit, no clearing sweep (§3).
    after = Reconciler(
        ReconcilerConfig(wip_ceiling=5, generation=2,
                         observed_at="2026-08-05T00:00:00+00:00")
    ).reconcile([item], _world(s3_objects={"s3://b/verify.json"}), store)
    assert after.items[0].stage is ItemStage.VERIFIED

    # And F7 has both ends of its measurement, stamped at the cycles that
    # observed them rather than at whichever cycle happened to look last.
    record = store.get("m1")
    assert record.stage_entered_at["merged"] == "2026-08-04T00:00:00+00:00"
    assert record.stage_entered_at["verified"] == "2026-08-05T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Deadlines (§3.6)
# ---------------------------------------------------------------------------

def test_deadline_passed_is_none_without_an_obligation():
    assert deadline_passed(Item(id="m1", stage=ItemStage.MERGED), _world()) is None


def test_deadline_passed_is_none_when_the_world_reports_no_date():
    """An expired obligation must not read as "not yet" because a surface went
    dark — that is the unbounded wait §3.6 exists to forbid."""
    item = Item(id="m1", stage=ItemStage.MERGED, verification=_obligation())
    assert deadline_passed(item, _world(today=None)) is None


def test_deadline_passed_is_true_on_and_after_the_date():
    item = Item(id="m1", stage=ItemStage.MERGED,
                verification=_obligation(deadline="2026-08-04"))
    assert deadline_passed(item, _world()) is True


def test_deadline_passed_is_false_before_the_date():
    item = Item(id="m1", stage=ItemStage.MERGED, verification=_obligation())
    assert deadline_passed(item, _world()) is False


def test_evaluate_verification_is_none_without_an_obligation():
    """`None` (no obligation) and an unsatisfied evaluation are different
    facts and must not be collapsed: the first means merging verified it."""
    assert evaluate_verification(Item(id="m1", stage=ItemStage.MERGED), _world()) is None


# ---------------------------------------------------------------------------
# Stages reported as recorded
# ---------------------------------------------------------------------------

def test_in_flight_done_and_abandoned_are_reported_as_recorded():
    world = _world()
    in_flight = Item(id="p1", stage=ItemStage.IN_FLIGHT,
                     change=Change(ref="p1", condition=ChangeCondition.CLEAN))
    assert _stage_of(in_flight, world) is ItemStage.IN_FLIGHT
    assert _stage_of(Item(id="d1", stage=ItemStage.DONE), world) is ItemStage.DONE
    assert _stage_of(Item(id="a1", stage=ItemStage.ABANDONED), world) is ItemStage.ABANDONED
