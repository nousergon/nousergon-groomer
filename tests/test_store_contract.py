"""The store conformance suite, run against the core's own in-memory store.

The suite itself lives in :mod:`nousergon_groomer.store_contract` so a private
backend can import and run it — see that module's docstring for why it ships as
package code rather than as test code.

Two subclasses run here:

- :class:`TestInMemoryStore` — the default store, ``reopen`` as identity.
- :class:`TestSerializingStore` — the same store, but ``reopen`` forces every
  record through a JSON round-trip. That is what a *persistent* backend does to
  a record, and it is where a field silently fails to survive. Without it the
  suite could not distinguish a store that persists correctly from one that
  never persists at all, because the in-memory store trivially passes every
  round-trip assertion by holding the identical object.
"""
from __future__ import annotations

import json

from nousergon_groomer.models import ObservedGeneration
from nousergon_groomer.observed_gen import (
    GenerationStore,
    GenerationStoreProtocol,
    PersistentGenerationStore,
)
from nousergon_groomer.store_contract import GenerationStoreContract, sample_generation


class TestInMemoryStore(GenerationStoreContract):
    """The core's default store must satisfy its own contract."""

    def make_store(self, scope: str = "contract") -> GenerationStore:
        return GenerationStore()


class _JsonRoundTripStore(GenerationStore):
    """An in-memory store whose ``reopen`` goes through JSON, as S3 would.

    Not a backend — a *stand-in for the serialisation boundary*, so the suite
    exercises the path that actually loses fields without the core acquiring a
    dependency on any storage system.
    """

    def serialize(self) -> str:
        return json.dumps({k: v.model_dump(mode="json") for k, v in self.snapshot().items()})

    @classmethod
    def deserialize(cls, blob: str) -> _JsonRoundTripStore:
        store = cls()
        store.load_records({k: ObservedGeneration(**v) for k, v in json.loads(blob).items()})
        return store


class TestSerializingStore(GenerationStoreContract):
    """The contract across a real serialisation boundary."""

    def make_store(self, scope: str = "contract") -> _JsonRoundTripStore:
        return _JsonRoundTripStore()

    def reopen(self, store):
        return _JsonRoundTripStore.deserialize(store.serialize())


# ---------------------------------------------------------------------------
# The protocols themselves
# ---------------------------------------------------------------------------


def test_in_memory_store_satisfies_the_reconciler_protocol():
    assert isinstance(GenerationStore(), GenerationStoreProtocol)


def test_in_memory_store_is_deliberately_not_persistent():
    """It does not survive the process, and must not claim it does.

    A no-op ``flush`` returning success would let a harness wire the in-memory
    store by accident and watch a green, silent, amnesiac loop — every cycle
    reporting more work done, not less. The negative is the assertion.
    """
    assert not isinstance(GenerationStore(), PersistentGenerationStore)


def test_a_structurally_conforming_backend_needs_no_inheritance():
    """Substitutability: conformance is structural, not by base class.

    A backend that imports nothing from this module still satisfies the
    protocol. That is the property that makes the store swappable without the
    core knowing which one is wired.
    """

    class Duck:
        def __init__(self):
            self._d = {}

        def get(self, item_id):
            return self._d.get(item_id)

        def put(self, item_id, generation):
            self._d[item_id] = generation

        def __contains__(self, item_id):
            return item_id in self._d

        def __len__(self):
            return len(self._d)

        def flush(self, *, written_at=None):
            return bool(self._d)

        @property
        def uri(self):
            return "duck://x"

        @property
        def loaded_count(self):
            return 0

        @property
        def loaded_at(self):
            return None

    duck = Duck()
    assert isinstance(duck, GenerationStoreProtocol)
    assert isinstance(duck, PersistentGenerationStore)


def test_a_backend_missing_flush_is_not_persistent():
    """The negative case — otherwise the protocol asserts nothing."""
    assert not isinstance(GenerationStore(), PersistentGenerationStore)


# ---------------------------------------------------------------------------
# The extension points that replaced reaching into ``_records``
# ---------------------------------------------------------------------------


def test_load_records_replaces_rather_than_merges():
    """A merge could not distinguish an empty object from a failed parse."""
    store = GenerationStore()
    store.put("repo#1", sample_generation("repo#1"))
    store.load_records({"repo#2": sample_generation("repo#2")})

    assert len(store) == 1
    assert store.get("repo#1") is None
    assert store.get("repo#2") is not None


def test_snapshot_is_a_copy():
    """A backend must not be able to hand a serialiser its live mapping."""
    store = GenerationStore()
    store.put("repo#1", sample_generation("repo#1"))

    snap = store.snapshot()
    snap.pop("repo#1")

    assert len(store) == 1, "mutating the snapshot changed the store"


def test_records_may_be_supplied_at_construction():
    store = GenerationStore({"repo#1": sample_generation("repo#1")})
    assert len(store) == 1 and "repo#1" in store
