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

- **Public** (MIT). Framework, contracts, and eval logic — the
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
- **No fleet-specific configuration.** Gate families, lane definitions, and
  issue-body formats are adapter data passed in, not hardcoded.
- **No label read as state.** A `gate:*` label is a status projection, never
  a source of truth: blocked-ness is derived from declared dependencies
  evaluated against the observed world, so a gate that has cleared unblocks
  its item with no actor and no label edit. A projection with no declaration
  behind it is a representation defect, and the disposition is UNDECIDABLE —
  never resolved in either direction.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  nousergon-groomer (PUBLIC — this repo)                  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Deterministic core                               │   │
│  │                                                   │   │
│  │  models.py          Item(+stage), Change, Dependency│   │
│  │  dependency_        (Dependency, ObservedWorld)   │   │
│  │    evaluator.py      → DependencyEvaluation (§3)  │   │
│  │  dependency_        transitive blocked-ness (§3.4)│   │
│  │    graph.py                                       │   │
│  │  admission.py       WIP ceiling + unblocked (§4)  │   │
│  │  lane_classifier    Gate A + Gate B pure fn       │   │
│  │  stage.py           the derived stage boundaries  │   │
│  │  disposition.py     §5.1 total over ItemStage     │   │
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
| §4.2 | Every carried item charged | `admission.current_wip` — one predicate over the `in_flight` stage, no exempt sub-states |
| §4.3 | PR opened only for unblocked | `admission.can_admit` — rejects blocked issues |
| §3 | One item with stages, not two populations | `models.ItemStage` — `proposed → ready → in_flight → merged → verified → done`, `abandoned` from any |
| §3.5 | A self-resolvable condition is work, not a dependency | `disposition._condition_conflicted` — a conflicted change is ACT |
| §3.8 | Post-merge verification lives on the item | `models.VerificationObligation` + the `merged → verified` transition |
| §4.4 | Draft is not a resting state | `disposition._condition_draft` — a draft change is ACT |
| §5.1 | One total reconciler | `disposition.py` — every `ItemStage` maps to exactly one disposition |
| §5.3 | Idempotent and resumable | `reconciler.py` — re-running over identical state = same output |
| §5.4 | Deterministic core, model at leaf | the core has no model import; the leaf is a strategy interface |
| §5.5 | Observed-generation skip | `observed_gen.py` — records last-evaluated generation, skips unchanged |
| §8.1 | Core runs against fixtures | `fixtures/` + `tests/` — no credentials needed |
| F5 | No advanceable-but-unadvanced changes | `disposition.py` — an item the reconciler could advance but didn't is ACT |
| F6/F7 | Residence and lead time | `ObservedGeneration.stage_entered_at` — per-stage entry stamps, the only source for both |

## Quick start

```bash
git clone https://github.com/nousergon/nousergon-groomer.git
cd nousergon-groomer
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

That's it — the suite runs over the recorded fixture scenarios with no network, no
credentials, and no model. If the last command exits 0, the core is
correct against its recorded fixtures.

## Using the core

```python
from nousergon_groomer import (
    Reconciler, ReconcilerConfig, Item, ItemStage, Dependency, DependencyKind,
)
from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.observed_gen import GenerationStore

# One item — a unit of intended change — at the `proposed` stage, with a
# declared dependency. There is no "issue" type and no "PR" type: an issue and
# its pull request are the same item at two points in its life.
item = Item(
    id="i1", stage=ItemStage.PROPOSED,
    declared_dependencies=[
        Dependency(kind=DependencyKind.S3_OBJECT, target="s3://bucket/key"),
    ],
)

# Observe the world (s3_objects is empty → the dep is unsatisfied)
world = ObservedWorld(s3_objects=set())

# Run one reconciliation pass
config = ReconcilerConfig(wip_ceiling=5, generation=1)
reconciler = Reconciler(config)
result = reconciler.reconcile([item], world, GenerationStore())

# BLOCKED on the S3 object — and still at `proposed`, because `ready` is
# derived from an empty blocking chain rather than stored.
assert result.items[0].disposition.kind.value == "blocked"
assert result.items[0].stage is ItemStage.PROPOSED
```

Once the world reports the object, the same item reconciles to `ready` and
`ACT(create_pr)` with no actor and no label edit — the next cycle simply
computes it.

### The stage machine

```
proposed ──▶ ready ──▶ in_flight ──▶ merged ──▶ verified ──▶ done
    │          │           │            │           │
    └──────────┴───────────┴────────────┴───────────┴──▶ abandoned
```

`ready` and `verified` are **derived, never stored**: the first is `proposed`
with no unsatisfied dependency, the second is `merged` with its post-merge
verification obligation discharged. Use `effective_stage(item, graph, world)`,
not `item.stage`, to ask where an item is.

A pull request is the *rendering* of `in_flight`. Its green/red/dirty/draft
distinctions are a `ChangeCondition` on `Item.change` — conditions of one
stage, not states an item rests in. That is what makes a conflicted change
`ACT(resolve_conflicts)` rather than a blocked item (§3.5), and a draft
`ACT(advance_draft)` rather than a parking space (§4.4).

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
| `gate_labeled_pr` | green PR with a `gate:*` label and no declaration | UNDECIDABLE |
| `gate_derived_dependency` | gated PRs whose gates are declared dependencies | ACT / BLOCKED / ACT |
| `do_not_groom` | item marked do-not-groom | TERMINAL |
| `undecidable` | unobservable dependency | UNDECIDABLE |

## Roadmap — v0.2.0 (usable tool) and beyond

The v0.1.0 core is a **library** — pure logic over recorded state. To make it
a **usable tool** that external users can point at their GitHub repo and run,
v0.2.0 adds the operational adapters (public, credentials injected at runtime):

- **GitHub snapshot adapter** — `gh`/API → `Item[]` + `ObservedWorld` (PAT via env var)
- **GitHub executor adapter** — `ReconcilerResult` → merge, comment, create PR (PAT via env var)
- **Pluggable model interface** — a `ModelProvider` protocol (see below)
- **CLI** — `groomer run --repo foo/bar --config config.yaml --dry-run`
- **Configuration system** — YAML for lanes, gates, WIP ceiling, model tiers

The private layer (fleet-specific config, krepis router adapter, spot
bootstrap, prompt templates) stays in `alpha-engine-config`.

### Model provider — provider-agnostic, never Anthropic

The model interface is a **provider-agnostic protocol**. The default
implementation uses **direct API calls to OpenAI-compatible endpoints** — the
common denominator across xAI (Grok), Moonshot (Kimi), Zhipu (GLM), DeepSeek,
and other providers. **Anthropic is never a default and never a dependency.**

```toml
# pyproject.toml — optional dependencies, none required for the pure core
[project.optional-dependencies]
github = ["httpx>=0.24"]       # snapshot + executor adapters
config = ["pyyaml>=6.0"]       # config system
model  = ["openai>=1.0"]       # default OpenAI-compatible provider
cli    = ["typer>=0.9"]        # CLI entry point
# NOTE: "anthropic" is NEVER a dependency of this package.
```

The `ModelProvider` protocol:

```python
class ModelProvider(Protocol):
    def complete(self, prompt: str, *, model: str, temperature: float = 0.0) -> str:
        """Generate a completion via the provider's API."""
        ...
```

The default `OpenAICompatibleProvider` works against any provider that
exposes an OpenAI-compatible `/v1/chat/completions` endpoint (xAI, Moonshot,
Zhipu, DeepSeek, local models via vLLM/Ollama). Configuration is by base_url
+ api_key + model_name — no vendor lock-in:

```yaml
# config.yaml
model:
  provider: openai-compatible
  base_url: https://api.deepseek.com/v1
  api_key_env: DEEPSEEK_API_KEY
  model: deepseek-chat
```

The nousergon private layer implements this protocol with a krepis router
adapter (proprietary). External users plug in any provider they choose.

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

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Relicensed from AGPL-3.0-only in 0.3.0 (2026-08-03): this tool is not monetised and
nothing paid depends on operating it, so network-use copyleft protected revenue
nobody intends to earn while taxing the only outcome the repo exists for — being
installed and tried. Releases up to 0.2.1 remain available under AGPL-3.0-only.
