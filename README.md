# nousergon-groomer

The **deterministic control plane** for the autonomous backlog-and-PR
maintenance loop — the fixture-runnable core specified by the
[groom-sweep policy](https://github.com/nousergon/nous-ergon-ops/blob/main/policies/groom-sweep-policy.md).

It computes, for every carried issue and PR, exactly one disposition —
**act**, **blocked**, **terminal**, or **undecidable** — from declared
dependencies evaluated against an observed snapshot of the world. The
core is pure logic: no network, no credentials, no model. The operational
harness (dispatch, merge execution, PAT) lives in the private layer.

## What this repo IS

- **Public** (AGPL-3.0-only). Framework, contracts, and eval logic — the
  "how rigorously we measure belief" tier — not strategy edge or secrets.
- **Fixture-runnable (§8.1).** The entire test suite runs over recorded
  JSON snapshots with no network, no credentials, no model.
- **Pure logic over recorded state.** Every function is a pure over its
  inputs; the same `(items, world, store)` always yields the same result.

## What this repo is NOT

- **No GitHub client.** No PAT, no `gh` calls, no API writes.
- **No model calls.** The control loop is deterministic (§5.4); the model
  is at the leaf, invoked by the private harness, not here.
- **No dispatch / scheduler.** When to run, which credentials to use, and
  how to perform a merge are the private harness's concern.
- **No fleet-specific configuration.** Gate labels, lane definitions, and
  issue-body formats are adapter data passed in, not hardcoded.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  nousergon-groomer (PUBLIC — this repo)                  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Deterministic core                               │   │
│  │                                                   │   │
│  │  models.py          Item, Dependency, Disposition │   │
│  │  dependency_        (Dependency, ObservedWorld)   │   │
│  │    evaluator.py      → DependencyEvaluation (§3)  │   │
│  │  dependency_        transitive blocked-ness (§3.4)│   │
│  │    graph.py                                       │   │
│  │  admission.py       WIP ceiling + unblocked (§4)  │   │
│  │  lane_classifier    Gate A + Gate B pure fn       │   │
│  │  disposition.py     §5.1 total over ItemState     │   │
│  │  observed_gen.py    §5.5 skip optimization        │   │
│  │  reconciler.py      the loop — ties it together   │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Fixture harness (§8.1)                           │   │
│  │  fixtures/           recorded PR/issue snapshots  │   │
│  │  tests/              contract + property tests    │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
                          │
                    published as a
                    versioned package
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  alpha-engine-config/scripts (PRIVATE — not this repo)  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Operational harness                             │   │
│  │  groom_driver.py    dispatch + schedule           │   │
│  │  *_merge_sweep.py   merge execution (PAT)         │   │
│  │  record_agent_      attribution recording        │   │
│  │    merge.py                                       │   │
│  │  model leaf         generative candidate changes  │   │
│  │  GitHub client      fetch live state → snapshot   │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

## Policy alignment

| Policy § | Property | Implementation |
|---|---|---|
| §3 | Blocked-ness derived, never asserted | `dependency_evaluator.py` — pure fn over `(Dependency, ObservedWorld)` |
| §3.1 | Declaration validated at write boundary | `models.Dependency` — rejects empty/unevaluable at construction |
| §3.3 | Spec/status stored separately | `models.Dependency` (spec) vs `DependencyEvaluation` (status) — separate types |
| §3.4 | Dependencies compose transitively | `dependency_graph.py` — walks the closure, names the chain |
| §4.1 | WIP ceiling | `admission.AdmissionController` — bounds the queue, not the rate |
| §4.2 | Every carried item charged | `admission.current_wip` — counts drafts, blocked, in-review |
| §4.3 | PR opened only for unblocked | `admission.can_admit` — rejects blocked issues |
| §5.1 | One total reconciler | `disposition.py` — every `ItemState` maps to exactly one disposition |
| §5.3 | Idempotent and resumable | `reconciler.py` — re-running over identical state = same output |
| §5.4 | Deterministic core, model at leaf | the core has no model import; the leaf is a strategy interface |
| §5.5 | Observed-generation skip | `observed_gen.py` — records last-evaluated generation, skips unchanged |
| §8.1 | Core runs against fixtures | `fixtures/` + `tests/` — no credentials needed |
| F5 | No advanceable-but-unadvanced PRs | `disposition.py` — a PR the reconciler could advance but didn't is ACT |

## Quick start

```bash
git clone https://github.com/nousergon/nousergon-groomer.git
cd nousergon-groomer
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

That's it — 94 tests run over 8 fixture scenarios with no network, no
credentials, and no model. If the last command exits 0, the core is
correct against its recorded fixtures.

## Using the core

```python
from nousergon_groomer import (
    Reconciler, ReconcilerConfig, Item, Dependency, DependencyKind,
    ItemKind, ItemState,
)
from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.observed_gen import GenerationStore

# Record an issue with a declared dependency
issue = Item(
    id="i1", kind=ItemKind.ISSUE, state=ItemState.OPEN_ISSUE_ACTIONABLE,
    declared_dependencies=[
        Dependency(kind=DependencyKind.S3_OBJECT, target="s3://bucket/key"),
    ],
)

# Observe the world (s3_objects is empty → the dep is unsatisfied)
world = ObservedWorld(s3_objects=set())

# Run one reconciliation pass
config = ReconcilerConfig(wip_ceiling=5, generation=1)
reconciler = Reconciler(config)
result = reconciler.reconcile([issue], world, GenerationStore())

# The issue is BLOCKED on the S3 object
assert result.items[0].disposition.kind.value == "blocked"
```

## Fixtures

The `fixtures/` directory holds JSON scenarios that record item
populations, observed worlds, and expected dispositions. Each is a
self-contained proof that the core produces the documented outcome for
that scenario:

| Fixture | Scenario | Expected |
|---|---|---|
| `clean_green_lane_pr` | green PR with a lane label | ACT automerge |
| `red_ci_pr` | PR with failing CI | ACT fix_ci |
| `blocked_issue` | issue blocked on an S3 object | BLOCKED |
| `transitive_blocked` | A blocked on B blocked on C | BLOCKED (chain) |
| `at_wip_ceiling` | WIP saturated, new issue | BLOCKED (admission) |
| `gate_labeled_pr` | green PR with a `gate:*` label | TERMINAL |
| `do_not_groom` | item marked do-not-groom | TERMINAL |
| `undecidable` | unobservable dependency | UNDECIDABLE |

## Normative spec

The [groom-sweep policy](https://github.com/nousergon/nous-ergon-ops/blob/main/policies/groom-sweep-policy.md)
is the normative source. Every invariant in this code references its
section number (e.g. §3, §5.1, §5.5) so the code and policy can be
audited against each other.

## Epic and issue breakdown

The v0.1.0 epic lives on `alpha-engine-config` as the single source of
truth: [alpha-engine-config#5853](https://github.com/nousergon/alpha-engine-config/issues/5853).
Implementation issues are filed on this repo (#2–#13).

## License

AGPL-3.0-only. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
