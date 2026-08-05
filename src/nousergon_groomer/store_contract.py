"""The conformance suite every :class:`GenerationStoreProtocol` backend must pass.

**Why this ships in the package rather than in the package's tests.** The
in-memory store is the core's default; the stores that actually matter live in
private harnesses (S3 today, plausibly SSM or DynamoDB tomorrow), and each of
them is where a wrong skip would silently do damage. A suite that only ever ran
against the in-memory implementation would test the one backend whose failure
costs nothing. Shipping it as importable code makes the contract executable by
whoever implements it — the backend proves conformance in its own CI, against
its own fake client, without the core needing to know the backend exists.

This is the same shape as ``fsspec``'s ``AbstractFileSystemTests`` or the
stdlib's ``test.support`` mixins: a base class of assertions plus a factory
hook. Subclass it in a test module, implement :meth:`make_store`, and the
suite is collected by pytest as ordinary test methods.

    class TestMyBackend(GenerationStoreContract):
        def make_store(self, scope="contract"):
            return MyBackend(scope, client=FakeClient())

        def reopen(self, store):            # persistent backends only
            store.flush()
            return MyBackend(store.scope, client=store.client)

:meth:`reopen` is the hinge. For an in-memory store it is the identity, and the
suite still checks every round-trip and skip property within one process. For a
persistent store it must *actually go through the durable medium* — flush, then
construct a fresh instance that reads it back. Every property below is then
being asserted across the serialisation boundary, which is where a backend
breaks: a field that does not survive ``model_dump``/parse turns a skip into a
non-skip (merely wasteful) or, far worse, turns a stale record into a match.

**No pytest import.** The suite is plain ``assert`` in plain methods so the
runtime package does not grow a test-framework dependency; pytest collects it
by name when subclassed. ``S101`` is ignored for this module in ``pyproject``
for exactly that reason — these asserts are the deliverable, not a debug aid.
"""
from __future__ import annotations

from typing import Optional

from .dependency_evaluator import ObservedWorld
from .models import (
    Dependency,
    DependencyKind,
    Disposition,
    DispositionKind,
    Item,
    ItemStage,
    ObservedGeneration,
)
from .observed_gen import GenerationStoreProtocol, record_evaluation
from .reconciler import Reconciler, ReconcilerConfig

__all__ = ["GenerationStoreContract", "sample_generation"]

_LEAF = "s3://bucket/blocker"


def sample_generation(item_id: str = "repo#1") -> ObservedGeneration:
    """A record with **every** field populated to a distinguishable value.

    Every field, deliberately. A backend that drops one is not obviously
    broken — the store still loads, the loop still runs, and the loss shows up
    as a skip rate that is merely lower than it should be, or as an F7 latency
    series that is quietly empty. Defaults would let a dropped field pass by
    matching the default it was dropped to.
    """
    return ObservedGeneration(
        item_id=item_id,
        generation=7,
        label_set_hash="a" * 64,
        deps_hash="b" * 64,
        head_sha="c" * 40,
        body_hash="d" * 64,
        closure_hash="e" * 64,
        disposition_kind="blocked",
        disposition_reason="blocked by s3_object:s3://bucket/blocker",
        disposition_action=None,
        # alpha-engine-config#6500: a populated, multi-link value — a backend
        # that drops this field (or flattens a list to a scalar) must fail
        # test_every_field_round_trips, not pass by matching an empty default.
        disposition_blocking_chain=["issue_terminal:repo#2", "s3_object:s3://bucket/blocker"],
        dependency_satisfied_at={"s3_object:s3://bucket/other": "2026-08-04T00:00:00+00:00"},
        satisfied_tokens=["s3_object:s3://bucket/other"],
        stage="in_flight",
        # Deliberately several stages, with distinct timestamps: F7 is the
        # difference between two of these, so a backend that keeps the map but
        # flattens it to one entry loses the measurement while still round-
        # tripping something that looks right.
        stage_entered_at={
            "proposed": "2026-08-01T00:00:00+00:00",
            "ready": "2026-08-02T00:00:00+00:00",
            "in_flight": "2026-08-03T00:00:00+00:00",
        },
    )


def _issue(item_id: str, deps: Optional[list] = None) -> Item:
    return Item(
        id=item_id,
        stage=ItemStage.PROPOSED,
        declared_dependencies=deps or [],
    )


def _population() -> list[Item]:
    """Three items, one of which is blocked *transitively*.

    ``dependent`` declares nothing but a dependency on ``blocked``; ``blocked``
    rests on an S3 leaf. So ``dependent``'s own fields say nothing at all about
    the thing that will move, which is the whole point of the closure test
    below.
    """
    return [
        _issue("repo#1"),
        _issue("repo#2", [Dependency(kind=DependencyKind.S3_OBJECT, target=_LEAF)]),
        _issue("repo#3", [Dependency(kind=DependencyKind.ISSUE_TERMINAL, target="repo#2")]),
    ]


def _world(*, leaf_present: bool) -> ObservedWorld:
    return ObservedWorld(s3_objects={_LEAF} if leaf_present else set(), terminal_items=set())


class GenerationStoreContract:
    """Assertions a conforming :class:`GenerationStoreProtocol` must satisfy."""

    # -- hooks a backend implements ----------------------------------------

    def make_store(self, scope: str = "contract") -> GenerationStoreProtocol:
        """Return a fresh, empty store. Overridden by the backend under test."""
        raise NotImplementedError("a GenerationStoreContract subclass must implement make_store")

    def reopen(self, store: GenerationStoreProtocol) -> GenerationStoreProtocol:
        """Return a store reading the same state, as a new cycle would see it.

        A persistent backend MUST flush and construct a fresh instance here.
        The default identity is correct only for an in-memory store, where
        "what the next cycle sees" is the same object.
        """
        return store

    # -- 1. absence is absence ---------------------------------------------

    def test_empty_store_reports_absence(self):
        store = self.make_store()
        assert len(store) == 0
        assert store.get("repo#1") is None
        assert "repo#1" not in store

    # -- 2. the record survives the medium intact --------------------------

    def test_every_field_round_trips(self):
        """A dropped field is a silent defect; compare the whole record."""
        store = self.make_store()
        original = sample_generation()
        store.put(original.item_id, original)

        reloaded = self.reopen(store).get(original.item_id)

        assert reloaded is not None, "the record did not survive the store"
        assert reloaded.model_dump() == original.model_dump(), (
            "a field was lost or altered crossing the storage boundary"
        )

    def test_put_replaces_rather_than_accumulates(self):
        store = self.make_store()
        first = sample_generation()
        store.put(first.item_id, first)
        store.put(first.item_id, first.model_copy(update={"generation": 8}))

        reopened = self.reopen(store)
        assert len(reopened) == 1
        record = reopened.get(first.item_id)
        assert record is not None and record.generation == 8

    # -- 3. a disposition round-trips, and an unchanged cycle skips --------

    def test_disposition_round_trips_and_unchanged_cycle_skips(self):
        """The deliverable this whole layer exists for, end to end.

        Cycle one evaluates everything and records a disposition per item.
        The store is reopened — for a persistent backend that means the state
        genuinely went to durable storage and came back. Cycle two runs the
        *same* inputs and must skip every item, still reporting the disposition
        each one reached rather than a hole.

        The second assertion is the one that catches a backend that "works":
        a store that round-trips records but loses ``closure_hash`` skips
        nothing, and nothing anywhere goes red — the loop just quietly costs
        what it always did.
        """
        items, world = _population(), _world(leaf_present=False)
        store = self.make_store()

        first = Reconciler(
            ReconcilerConfig(wip_ceiling=50, generation=1, observed_at="2026-08-04T00:00:00+00:00")
        ).reconcile(items, world, store)
        assert first.skipped == 0, "nothing can be skipped on a first cycle"
        assert first.enumerated == len(items)

        second = Reconciler(
            ReconcilerConfig(wip_ceiling=50, generation=2, observed_at="2026-08-04T01:00:00+00:00")
        ).reconcile(items, world, self.reopen(store))

        assert second.skipped == second.enumerated == len(items), (
            "an unchanged cycle must skip every item — a store that persists "
            "records but loses the closure hash silently skips none"
        )
        by_id = {d.item_id: d for d in second.items}
        assert by_id["repo#2"].disposition.kind is DispositionKind.BLOCKED, (
            "a skipped item must report the disposition it reached, not a hole"
        )
        # alpha-engine-config#6500: repo#3 is blocked *transitively* through
        # repo#2 — a skipped cycle must still report the full chain to the
        # root leaf, not just repo#3's own direct dependency on repo#2.
        assert by_id["repo#3"].skipped is True
        assert by_id["repo#3"].disposition.blocking_chain == [
            "issue_terminal:repo#2",
            f"s3_object:{_LEAF}",
        ]

    # -- 4. a transitive leaf flip defeats the skip ------------------------

    def test_changed_transitive_leaf_forces_re_evaluation(self):
        """``repo#3``'s own fields never change; its blocker's leaf does.

        This is the failure the persistent store would otherwise activate: with
        a token computed over an item's own declarations, ``repo#3`` would keep
        a stale ``BLOCKED`` disposition forever, because the only thing that
        moved was an S3 object two hops away. Asserted here, in the *backend*
        contract, because the token is only as good as the fields the backend
        actually persisted.
        """
        items = _population()
        store = self.reopen(
            self._restore(self.make_store(), items, _world(leaf_present=False))
        )

        after = Reconciler(
            ReconcilerConfig(wip_ceiling=50, generation=2, observed_at="2026-08-04T01:00:00+00:00")
        ).reconcile(items, _world(leaf_present=True), store)

        by_id = {d.item_id: d for d in after.items}
        assert not by_id["repo#3"].skipped, (
            "a dependent item whose transitive leaf flipped must re-evaluate "
            "even though none of its own fields changed"
        )
        assert not by_id["repo#2"].skipped
        assert by_id["repo#1"].skipped, (
            "an item genuinely unaffected by the flip must still skip — "
            "otherwise the closure hash is not discriminating, just noisy"
        )

    # -- 5. the satisfaction timestamp is taken once and kept --------------

    def test_satisfaction_timestamp_is_taken_once_and_survives(self):
        """§3.6 — the term nothing else records.

        F3's 24-hour conversion clock and F7's detection latency both run from
        the moment a dependency was *first* observed satisfied. No GitHub
        surface stores it. Re-stamping it each cycle would overwrite the only
        copy with the current time, which reads as a working measurement while
        reporting zero latency forever.
        """
        items = _population()
        store = self.reopen(
            self._restore(self.make_store(), items, _world(leaf_present=False))
        )

        # The leaf flips; the timestamp is taken now.
        store = self.reopen(
            self._evaluate(store, items, _world(leaf_present=True), generation=2,
                           observed_at="2026-08-04T01:00:00+00:00")
        )
        record = store.get("repo#2")
        assert record is not None
        token = f"s3_object:{_LEAF}"
        assert record.dependency_satisfied_at.get(token) == "2026-08-04T01:00:00+00:00", (
            "the moment a dependency was first observed satisfied was not recorded"
        )

        # A later cycle must not re-stamp it.
        store = self.reopen(
            self._evaluate(store, items, _world(leaf_present=True), generation=3,
                           observed_at="2026-08-04T09:00:00+00:00")
        )
        later = store.get("repo#2")
        assert later is not None
        assert later.dependency_satisfied_at.get(token) == "2026-08-04T01:00:00+00:00", (
            "the first-satisfied timestamp was overwritten — the measurement is gone"
        )

    # -- 6. the record the reconciler writes is the record stored ----------

    def test_record_evaluation_writes_through_the_store(self):
        """``record_evaluation`` must reach the backend, not a local copy."""
        store = self.make_store()
        item = _issue("repo#9")
        written = record_evaluation(
            item,
            store,
            generation=4,
            closure_state=[],
            disposition=Disposition(kind=DispositionKind.ACT, action="create_pr"),
            observed_at="2026-08-04T00:00:00+00:00",
        )
        reloaded = self.reopen(store).get("repo#9")
        assert reloaded is not None
        assert reloaded.model_dump() == written.model_dump()

    # -- helpers -----------------------------------------------------------

    def _restore(self, store, items, world):
        """Run one reconcile into ``store`` and return it (cycle one)."""
        return self._evaluate(store, items, world, generation=1,
                              observed_at="2026-08-04T00:00:00+00:00")

    def _evaluate(self, store, items, world, *, generation: int, observed_at: str):
        Reconciler(
            ReconcilerConfig(wip_ceiling=50, generation=generation, observed_at=observed_at)
        ).reconcile(items, world, store)
        return store
