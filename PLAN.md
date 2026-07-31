# PLAN — nousergon-groomer v0.1.0

## What this repo is

The **deterministic control plane** for the autonomous backlog-and-PR
maintenance loop. It is the §8.1 fixture-runnable core specified by
`nous-ergon-ops/policies/groom-sweep-policy.md` (the "groom-sweep policy").

This repo is **public** (AGPL-3.0), consistent with the nine existing
public alpha-engine repos. It contains framework/contracts/eval logic —
the "how rigorously we measure belief" tier — not strategy edge or
operational secrets.

## What this repo is NOT

- **No GitHub client.** No PAT, no `gh` calls, no API writes. The core
  is pure logic over recorded state.
- **No model calls.** The control loop is deterministic (§5.4); the model
  is at the leaf, invoked by the private harness, not here.
- **No dispatch / scheduler.** The operational harness (when to run,
  which credentials to use, how to perform a merge) is private.
- **No fleet-specific configuration.** Gate labels, lane definitions,
  issue-body formats are adapter data passed in, not hardcoded (§5.4
  swappable-strategy discipline).

The repo boundary is a **forcing function** for §8.1: a public repo
with no credentials can only be fixture-tested, which is the property
the policy requires.

## Architecture — the three-plane split (§5.4)

```
┌─────────────────────────────────────────────────────────┐
│  nousergon-groomer (PUBLIC — this repo)                  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Deterministic core                               │   │
│  │                                                   │   │
│  │  models.py        Item, Dependency, Disposition  │   │
│  │  dependency_      (Dependency, ObservedWorld)    │   │
│  │    evaluator.py    → DependencyEvaluation (§3)    │   │
│  │  dependency_      transitive blocked-ness (§3.4) │   │
│  │    graph.py                                       │   │
│  │  admission.py     WIP ceiling + unblocked (§4)    │   │
│  │  lane_classifier  Gate A + Gate B pure fn          │   │
│  │  disposition.py  §5.1 total over ItemState        │   │
│  │  observed_gen.py  §5.5 skip optimization          │   │
│  │  reconciler.py    the loop — ties it together      │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Fixture harness (§8.1)                           │   │
│  │  fixtures/         recorded PR/issue snapshots     │   │
│  │  tests/            contract + property tests      │   │
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
│  │                                                   │   │
│  │  groom_driver.py    dispatch + schedule           │   │
│  │  *_merge_sweep.py   merge execution (PAT)         │   │
│  │  record_agent_      attribution recording        │   │
│  │    merge.py                                       │   │
│  │  model leaf         generative candidate changes  │   │
│  │  GitHub client      fetch live state → snapshot   │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

## Policy alignment — which section is implemented where

| Policy § | Property | Implementation in this repo |
|---|---|---|
| §3 | Blocked-ness derived, never asserted | `dependency_evaluator.py` — pure fn over (Dependency, ObservedWorld) |
| §3.1 | Declaration validated at write boundary | `models.Dependency` — rejects empty/unevaluable at construction |
| §3.3 | Spec/status stored separately | `models.Dependency` (spec) vs `DependencyEvaluation` (status) — separate types |
| §3.4 | Dependencies compose transitively | `dependency_graph.py` — walks the closure, names the chain |
| §4.1 | WIP ceiling | `admission.AdmissionController` — bounds the queue, not the rate |
| §4.2 | Every carried item charged | `admission.current_wip` — counts drafts, blocked, in-review |
| §4.3 | PR opened only for unblocked | `admission.can_admit` — rejects blocked issues |
| §5.1 | One total reconciler | `disposition.py` — every ItemState maps to exactly one disposition |
| §5.3 | Idempotent and resumable | `reconciler.py` — re-running over identical state = same output |
| §5.4 | Deterministic core, model at leaf | the core has no model import; the leaf is a strategy interface |
| §5.5 | Observed-generation skip | `observed_gen.py` — records last-evaluated generation, skips unchanged |
| §8.1 | Core runs against fixtures | `fixtures/` + `tests/` — no credentials needed |
| F5 | No advanceable-but-unadvanced PRs | `disposition.py` — a PR the reconciler could advance but didn't is ACT |

## Issue breakdown — v0.1.0 epic

Epic: [alpha-engine-config-I5853](https://github.com/nousergon/alpha-engine-config/issues/5853)
Tracking: [groomer#1](https://github.com/nousergon/nousergon-groomer/issues/1)

| Issue | Title | Policy § | Status |
|---|---|---|---|
| [#2](https://github.com/nousergon/nousergon-groomer/issues/2) | Domain model | §3, §3.3, §5.5 | scaffolded |
| [#3](https://github.com/nousergon/nousergon-groomer/issues/3) | Dependency evaluator | §3 | scaffolded |
| [#4](https://github.com/nousergon/nousergon-groomer/issues/4) | Transitive dependency graph | §3.4 | scaffolded |
| [#5](https://github.com/nousergon/nousergon-groomer/issues/5) | Admission controller | §4.1, §4.2, §4.3 | scaffolded |
| [#6](https://github.com/nousergon/nousergon-groomer/issues/6) | Lane classifier | auto-merge §2, §3 | scaffolded |
| [#7](https://github.com/nousergon/nousergon-groomer/issues/7) | Disposition function | §5.1 | not started |
| [#8](https://github.com/nousergon/nousergon-groomer/issues/8) | Observed-generation skip | §5.5 | not started |
| [#9](https://github.com/nousergon/nousergon-groomer/issues/9) | Reconciler | §5.1, §5.3 | not started |
| [#10](https://github.com/nousergon/nousergon-groomer/issues/10) | Fixture harness | §8.1 | not started |
| [#11](https://github.com/nousergon/nousergon-groomer/issues/11) | Contract tests | all | not started |
| [#12](https://github.com/nousergon/nousergon-groomer/issues/12) | README | — | not started |
| [#13](https://github.com/nousergon/nousergon-groomer/issues/13) | Parity test (deferred) | §8.2 | deferred |

## Build order

Issues 2-6 are scaffolded (code written, not yet tested). 7 depends on 3,4.
8 depends on 2. 9 depends on 7,8. 10-12 depend on 9. 13 is deferred.

## Out of scope for v0.1.0

- The operational harness (private — `alpha-engine-config/scripts`)
- The model leaf (private — generative candidate changes)
- Live GitHub state fetching (private)
- The swap from existing sweeps to this core (follow-up epic, gated on #13 parity)
