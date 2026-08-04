# Changelog

All notable changes to `nousergon-groomer` are recorded here. Versions follow
[semantic versioning](https://semver.org/).

While the major version is `0`, the public API may change between minor versions.

## [0.10.0] — 2026-08-04

### Added

- **The §3.1 write-boundary chokepoint for raw dependency declarations**
  (alpha-engine-config#6309). `Dependency.model_validate(raw)` /
  `parse_dependency_declaration(raw)` parses a raw predicate string — as it
  arrives from a `Verified-when:` body line or an issue custom field — into
  a validated `Dependency`, or rejects it, at declaration, if it does not
  parse into one of a closed allowlist of permitted subjects: `s3://bucket/key`,
  `s3://bucket/prefix/`, `issue:owner/repo#N`, `pr:owner/repo#N`,
  `pipeline_run:<id>`, an ISO-8601 date, or `milestone:<id>`. Typed
  construction (`Dependency(kind=..., target=...)`) is unaffected.

  The measured defect this closes: `alpha-engine-research-PR549` carried
  prose in a `Verified-when` field that entered freely, because nothing
  converted a raw predicate string into an evaluable `Dependency` before it
  reached the tracker. Validating at evaluation time — the only alternative
  — is what produces a population of unevaluable items with the author long
  gone; this is now caught at authorship instead.

- **`passes_agency_test(dep)`** — §3.5's agency test as an explicit,
  independently callable function. Every `DependencyKind` today names a
  condition genuinely outside the loop's own write authority (there is
  deliberately no kind for branch, CI, review, label or draft state — see
  `nousergon-groomer-PR39`), so `passes_agency_test` is always `True` for a
  well-typed `Dependency` today. Its allowlist (`_AGENCY_EXTERNAL_KINDS`) is
  a closed, explicit set rather than derived from `DependencyKind` itself,
  so a future kind added to the enum without a matching, deliberate addition
  here fails closed — `test_agency_allowlist_covers_every_dependency_kind`
  pins today's equality and goes red the moment the two drift.

  Wired into `Dependency` as a second, independent check (`§3.5`: "both
  [evaluability and agency] are checked at declaration") guarding the typed
  construction path against a future forbidden kind, on top of the raw-string
  parser above, which can never produce one by construction.

## [0.9.0] — 2026-08-04

### Changed — BREAKING

- **One item with stages, replacing `ItemKind` (`ISSUE | PR`) and the flat
  `ItemState`.** `Item.stage` is an `ItemStage`:
  `proposed → ready → in_flight → merged → verified → done`, with `abandoned`
  reachable from any stage. `ItemKind` and `ItemState` are **removed**, not
  deprecated — a shim would let a consumer keep asking "is this an issue or a
  PR?", which is the question this change abolishes.

  **Why this is one change and not four.** An issue and its pull request are
  one unit of intended change at two points in its life, and modelling them as
  two populations is the shared root of four separate defects that had each
  been treated separately:

  | Modelled as two | Consequence |
  |---|---|
  | The PR has its own blocked state | A branch conflict — a property of the in-flight stage — gets declared a dependency of the *item*, and the item parks |
  | The PR is terminal at merge | Verification that can only happen after merge has nowhere to live, so it is held as a pre-merge gate that can never clear |
  | The PR has its own resting states | "Draft" becomes a place to leave things rather than a stage the cycle must exit |
  | Issues and PRs are separate populations | Outcomes are written for one and absent for the other, and the link between them is something a sweep checks rather than an invariant |

  At last measurement most blocked PRs in the fleet were merely conflicted or
  behind `main` — the loop's own branch decay, declared as a blocker on the
  work — and four fifths of the open queue was in draft with not one draft in
  a clean merge state. Neither population was a bug in a rule; both were the
  representation admitting them.

- **The PR sub-states are now `ChangeCondition` values on `Item.change`,** not
  item states. `draft`, `conflicted`, `ci_red`, `ci_pending` and `clean` are
  conditions of the `in_flight` stage. A conflicted change is an item **in
  flight whose change is conflicted**, and its disposition is `ACT`
  (`resolve_conflicts`) — resolving it is inside the loop's own write
  authority, so it is work, never a dependency. Likewise a draft is
  `ACT(advance_draft)`: it is a transient condition inside the cycle that
  opened the change, not a resting state.

- **`merged` is not terminal.** Only `done` and `abandoned` are.
  `Item.verification` carries a `VerificationObligation` — a predicate, a
  **required** deadline, and a named revert action — for a criterion that can
  only be satisfied by an artifact the merged code itself produces. Held as a
  pre-merge dependency such a criterion is structurally unsatisfiable: the item
  rests forever on a condition its own resting prevents. It is now an
  obligation on the `merged → verified` transition. An item awaiting one is
  `BLOCKED` naming the predicate and its deadline; past the deadline it is
  `ACT(<revert_action>)`, because expiry is an event with an action rather than
  another interval of silent waiting.

- **Two stages are derived, never stored.** `stage.effective_stage(item, graph,
  world)` resolves `proposed → ready` (nothing it declares is unsatisfied) and
  `merged → verified` (the obligation is discharged). Storing readiness would
  be asserting blocked-ness, which is the property the §3 rewrite exists to
  remove; a recorded `ready` is re-derived rather than trusted. An item with an
  **undecidable** dependency stays at `proposed`: unconfirmed is not ready, and
  reporting it otherwise would enter it in the conversion denominator on a fact
  nobody established.

- **Per-stage entry timestamps.** `ObservedGeneration` gains `stage` and
  `stage_entered_at` (an `ItemStage` value → ISO-8601 map), written to the
  status store every cycle. **Lead time and residence are computed from this
  map and from nothing else** — neither is recoverable from GitHub, which
  records when an issue opened and when a PR merged and nothing at all about
  when an item became *ready*, because readiness is derived and only the loop
  deriving it can know the moment it flipped. First entry wins and re-entry
  never re-stamps: an item can regress when a change is closed, and re-stamping
  would silently shorten exactly the measurements that had a setback.

- **`ItemDisposition.stage`** and **`ReconcilerResult.stage_counts`** are
  emitted every cycle. The counts are seeded with every stage at zero, because
  a stage absent from the map and a stage with no items are different claims
  and only one of them is honest.

- **Admission and the lane classifier are stage-scoped.** `current_wip` counts
  items at `in_flight` carrying a change — one predicate, where there was an
  enumeration of five PR states, which is where an exemption class hides.
  `can_admit` gates on the pre-change stages. `classify_lane` reports
  `no_change` for an item with nothing to merge, which is a different answer
  from "not clean".

- **The snapshot adapter emits staged items.** An open pull request is always
  `in_flight`; an issue a pull request names is the **same item** at
  `in_flight`, carrying `change_ref` rather than being a second population
  "waiting on review". The two records are one unit, and the disposition
  function defers to whichever holds the change instead of producing two
  verdicts about one piece of work — including for WIP, which charges the unit
  once.

### Migration

Replace `Item(id=…, kind=ItemKind.ISSUE, state=ItemState.OPEN_ISSUE_ACTIONABLE)`
with `Item(id=…, stage=ItemStage.PROPOSED)`, and
`Item(kind=ItemKind.PR, state=ItemState.OPEN_RED_CI, mergeable=…, ci_green=…)`
with `Item(stage=ItemStage.IN_FLIGHT, change=Change(ref=…,
condition=ChangeCondition.CI_RED, mergeable=…, ci_green=…))`. Route on
`ItemDisposition.stage` and `Item.change.condition` rather than on `item.kind`
and `item.state`. `DO_NOT_GROOM` is now the `do_not_groom` flag, which is a
declared exclusion rather than a stage — an excluded item is at some real point
in its life, and a pseudo-stage would destroy the record of it.

Zero external adopters at the time of writing (stars = 0), which is why the
breaking change lands now rather than accumulating a compatibility surface
around a model known to be wrong.

## [0.8.0] — 2026-08-04

### Added

- **`DependencyKind.MILESTONE_REACHED`** (issue #33) — resolves a milestone id
  against `ObservedWorld.reached_milestones: Optional[set[str]]`. Unblocks the
  full migration of `gate:milestone` from label-based blocked-ness to a
  derived `Dependency` (`alpha-engine-config#5877`, epic `#5871`).

### Removed

- **`DependencyKind.LABEL_ABSENT` and its evaluator branch** (§3.5 of
  `groom-sweep-policy.md`: "a declared dependency names a condition no
  action within the loop's own write authority would satisfy; if the loop
  could satisfy it, the disposition is `act`"). A label is written BY the
  loop itself, so a "blocked until label X is absent" dependency was a
  forbidden state by construction — the loop could simply remove the label
  rather than being blocked on it. `ObservedWorld.absent_labels` is removed
  along with it: it had no other consumer. Any item that declared this kind
  must be re-dispositioned as `ACT` (remove the label, or do the work the
  label stood for) — not migrated to a narrower label-based kind, which
  would reintroduce the same defect. This is a breaking change to the
  `Dependency`/`ObservedWorld` schema: constructing a `Dependency` with the
  removed kind now raises `ValidationError` at parse (§6.3 fail loud, not a
  silent drop) rather than silently accepting it; `ObservedWorld.absent_labels`
  is simply gone from the schema (pydantic's default `extra="ignore"` means a
  caller still passing it gets no error, but the value is no longer read by
  anything).

## [0.7.0] — 2026-08-04

### Added

- **`GitHubSnapshot.fetch()` now sources item-to-item dependencies from
  GitHub's native issue-dependencies primitive** (`blocked_by`), rather than
  requiring a private harness to regex-parse `Blocked-by:`/`Depends-on:`
  lines out of a body. Every open issue and PR is checked (issues via the
  free inline `issue_dependencies_summary` count; PRs unconditionally, since
  PR-shaped issue objects never carry that summary), and each blocker's
  `Dependency` target is a fully-qualified `owner/name#number` id read from
  the response's own `repository.full_name` — never inferred from a URL or
  a bare number, since `GET /repos/{o}/{r}/issues/{n}` silently follows a
  transfer (config#6320).
- **`Item` gains `sub_issue_ids` and `custom_fields`.** `sub_issue_ids` is
  the qualified ids of an issue's own sub-issues (probed only when
  `sub_issues_summary.total > 0`). `custom_fields` is a generic,
  interpretation-free passthrough of `issue_field_values` keyed by field
  name — the package assigns no meaning to any specific field.
- **`GitHubSnapshot.fetch_issue_field_conformance(org)`** reads
  `GET /orgs/{org}/issue-fields` and returns an `IssueFieldConformance` row
  (`used`/`cap`/`free`/`conformance_row()`) so a caller budgets a new
  org-level field against what is actually free (25-slot cap, shared
  fleet-wide) rather than guessing.
- `ObservedWorld.terminal_items` returned by `fetch()` now also carries a
  qualified (`owner/name#number`) twin of every same-repo closed item,
  alongside the existing bare-number entries (backward compatible — additive
  only), plus any cross-repo blocker observed closed via a native dependency
  response.

## [0.6.0] — 2026-08-04

### Changed

- **No branch in the core reads a `gate:*` label as state** (`alpha-engine-config#6137`,
  parent `nous-ergon-ops#356` — Brian ruled the taxonomy retired as a state
  representation on 2026-07-31; groom-sweep-policy §3.3). Gate-ness now reaches
  a disposition exactly one way: the harness translates a gate into declared
  dependencies, the observer reports the surfaces they name, and the evaluator
  decides.

  - `disposition._disposition_open_draft` no longer returns TERMINAL for a draft
    carrying a `gate:*` label. A draft whose declared conditions are satisfied is
    ACT `advance_draft`, even with the label still attached; one whose conditions
    are not is BLOCKED naming **the condition** — an S3 key, a pipeline run, a
    blocking item — instead of the label that hid it. This is the property the
    migration was for: a cleared gate stops blocking when the world says so, not
    when someone edits a label.
  - `lane_classifier` Gate A check 2 is renamed `no_gate_label` →
    `no_open_dependency` and is now derived: it fails on an unbacked `gate:*`
    projection, on any declared dependency observed unsatisfied or undecidable,
    and on declared dependencies with no `world` to evaluate them against.
    `classify_lane` takes an optional `world`; `GateAResult` gains
    `open_dependency_reasons` so a rejected auto-merge names its condition.
    The check is deliberately redundant with the disposition function's chain
    check — §3.3 names a *single* upstream check owning the gate exclusion as
    the shape that produced the 2026-07-22 incident (six gated, CI-red PRs
    auto-merged in minutes), so both surfaces derive independently.

- **An unbacked gate projection is UNDECIDABLE, at the top of the disposition
  function.** `Item.unrepresented_gate_labels` names the `gate:*` labels on an
  item that declares nothing at all; a non-empty list short-circuits every
  state handler. Neither branch is derivable — reading the label as blocked
  asserts blocked-ness (what §3 abolishes), and reading it as clear is the
  2026-07-22 incident — so the core declines and surfaces it. This also makes
  true a promise the private harness already prints: an item whose enrichment
  failed is "left without derived dependencies (will surface as undecidable,
  not silently unblocked)".

### Deprecated

- **`Item.has_gate_label`** now reads declared dependencies rather than the
  label prefix, and warns. Nothing in this package calls it. Its value is
  `has_declared_dependency or unrepresented_gate_labels`, which is identical to
  the retired label test for a freshly snapshotted item and stays true once that
  item is enriched — so a harness using it to *select* which items to fetch
  declarations for (an observation use) is unaffected, while a harness using it
  as *state* now gets a warning pointing at `Item.has_declared_dependency`,
  `Item.unrepresented_gate_labels`, or `dependency_evaluator.is_item_blocked`.
- **`config.GateFamily`** is deprecated as a state surface. No logic reads it;
  it is retained only so a deployed YAML config still loads unchanged.

### Added

- `Item.has_declared_dependency`, `Item.unrepresented_gate_labels`, and the
  `models.GATE_LABEL_PREFIX` constant.
- Fixture `gate_derived_dependency` — the migrated gated population: a cleared
  gate auto-merging with its stale label still attached, an uncleared one
  blocking on the S3 key it actually names, and a draft the loop advances
  rather than resting on. Fixture `gate_labeled_pr` now records the unbacked
  projection case (UNDECIDABLE), stated against the worst case: the PR is
  green, mergeable and carries an auto-merge lane label.

## [0.5.0] — 2026-08-04

### Added

- **`GenerationStoreProtocol` and `PersistentGenerationStore` — the store is
  now an interface, not a class.** `Reconciler.reconcile`, `should_skip` and
  `record_evaluation` are typed against the narrow protocol; the persistent
  surface a harness needs (`flush`, `uri`, `loaded_count`, `loaded_at`) is a
  separate protocol the core never depends on.

  **Why this is a correctness concern and not tidiness.** The module docstring
  has always said a persistent backend is "the private harness's concern", but
  the only way to write one was to subclass `GenerationStore` and populate its
  private `_records` dict — which made a private attribute the real extension
  point. A backend built that way is coupled to an internal the core is free to
  change and cannot see, and it is coupled *silently*: the failure mode is not
  an import error, it is a store that loads nothing and skips nothing while
  every surface stays green. `principles.md` §2.8 asks that a swappable
  component be addressed through a declared adapter; this is that declaration.

  Conformance is **structural**, so a backend need not import or inherit
  anything from this package to satisfy it. Use `isinstance`, never
  `issubclass` — a runtime-checkable protocol with non-method members rejects
  the latter by design.

- **`GenerationStore.load_records()` and `.snapshot()`** — the two operations a
  backend actually needs (bulk-load what was read, serialise what is held),
  stated as public API. `load_records` **replaces** rather than merges, and
  deliberately does not route through `put`: loading is not a write and must
  not mark a store dirty, or a cycle that recorded nothing would still refresh
  the object and report freshness for a loop that did no work.
  `GenerationStore(records=...)` accepts an initial mapping.

- **`nousergon_groomer.store_contract` — a conformance suite shipped as package
  code.** `GenerationStoreContract` is a subclassable set of assertions with a
  `make_store` factory and a `reopen` hook; a private backend runs it in its own
  CI against its own fake client. It asserts every field round-trips, that an
  unchanged cycle skips every item, that a **changed transitive leaf forces
  re-evaluation of a dependent item whose own fields did not change**, and that
  a first-observed-satisfied timestamp (§3.6) is taken once and never
  overwritten.

  It ships in `src/` rather than `tests/` on purpose: the stores whose failure
  costs anything are the private ones, and a suite that only ran here would be
  testing the single backend where a wrong skip is free. It imports no test
  framework — plain asserts in plain methods, collected by pytest when
  subclassed.

  `tests/test_store_contract.py` runs it twice against the in-memory store,
  once with `reopen` as identity and once forcing every record through a JSON
  round-trip — without the second, the suite could not tell a store that
  persists correctly from one that never persists at all.

### Notes

- `GenerationStore` deliberately does **not** satisfy `PersistentGenerationStore`,
  and a test asserts the negative. Giving the in-memory default a no-op `flush`
  returning success would let a harness wire the wrong store and watch a green,
  silent, amnesiac loop — one reporting *more* work done each cycle, not less.
- Fully backward compatible: no existing signature, name or behaviour changed.


## [0.4.0] — 2026-08-04

### Changed

- **The §5.5 skip token is now computed over the transitive dependency
  closure, not over an item's own declarations.** `DependencyGraph` gains
  `closure_state(item_id)`, which enumerates every transitively reachable
  dependency with its *observed* state (`satisfied` / `unsatisfied` /
  `undecidable`); `InputFingerprint` and `ObservedGeneration` gain
  `closure_hash`; `should_skip` and `record_evaluation` accept `closure_state`.

  **Why this is a correctness fix and not an optimization.** The previous token
  covered `label_set_hash`, `deps_hash`, `head_sha` and `body_hash` — all
  properties of the item itself. A *blocked* item's own properties never
  change: what changes is the world underneath it, one or more hops away. So
  the token was blind in exactly one direction, and it was the direction that
  matters — an item resting on a dependency that had since been satisfied would
  keep its stale `BLOCKED` disposition indefinitely, because nothing about the
  item had moved.

  The defect has never fired, for a reason that is itself worth recording: the
  only shipped `GenerationStore` is in-memory and is constructed empty every
  cycle, so nothing has ever been skipped and the skip path has never run. It
  would have fired on the first cycle after a persistent backend landed. This
  ships first so that it cannot.

- **A fingerprint with no closure never matches — including against another
  fingerprint with no closure.** A record written without a closure was written
  by logic that could not observe the world moving beneath it, so honouring it
  as a match would grant precisely the unsound skip above. The cost is one
  non-skipped cycle per item at upgrade; the alternative is a stale disposition
  that never re-derives. Note the distinction between `closure_state=[]` (an
  observation: this item reaches no dependencies) and `closure_state=None` (the
  caller did not look) — only the first can ground a skip.

- **`Reconciler.reconcile` now honours the skip.** It previously computed
  `should_skip` and then re-derived the disposition anyway, so the flag was
  reported and never acted on. Skipping is safe only because the token is now
  closure-aware.

### Added

- **`ObservedGeneration.disposition_kind` / `.disposition_reason` /
  `.disposition_action`** — the verdict an item last resolved to, so a skipped
  item reports what it decided rather than a hole. All three are stored because
  `Disposition` enforces that `ACT` carries an action and `UNDECIDABLE` carries
  a reason; a record holding only the kind cannot reconstruct a valid one. A
  record with no stored disposition is **not** skipped — a skip that cannot say
  what was decided is a hole, not an optimization.

- **`ObservedGeneration.dependency_satisfied_at`** — `"<kind>:<target>"` to the
  ISO-8601 moment that dependency was *first observed satisfied* (§3.6), plus
  `satisfied_tokens` recording the satisfied set at each evaluation so the next
  cycle computes a **transition** rather than a state.

  This is the term §2.7 names as the one nothing records. F3's 24-hour clock
  starts when an item becomes unblocked and F7's detection latency runs from
  dependency-satisfied to disposition; neither is computable from GitHub,
  because no surface stores the moment a condition flipped. Only the loop
  observing it can know, and only if it writes it down.

  Two cases deliberately record nothing: a dependency already satisfied at
  first sight (its transition predates observation, and dating it to
  first-sight would fabricate a latency rather than decline to measure one),
  and a prior record written before closures existed. Once taken, a timestamp
  is never overwritten — the first observation *is* the measurement.

- **`ReconcilerConfig.observed_at`** — the caller's timestamp for the cycle. A
  parameter rather than a clock read inside the core, because `reconcile` must
  stay a pure function of `(config, items, world, store)` (§5.4).

## [0.3.0] — 2026-08-03

### Changed

- **Relicensed from AGPL-3.0-only to MIT.** Brian's ruling, 2026-08-03. The
  criterion is monetisation intent, not archetype: this tool is not monetised,
  and — unlike `nousergon-auth` and `nousergon-console` — nothing paid depends on
  operating it. The fleet runs it; no customer would. Network-use copyleft was
  therefore protecting revenue nobody intends to earn, while taxing the only
  outcome the repo exists for: being installed and tried.

  It had been AGPL under a rule that has since been retired — *"licensing follows
  archetype, dogfooded tools AGPL"* — which is to say it was never actually
  judged, only inherited.

  **Not retroactive.** Releases up to and including 0.2.1 remain available under
  AGPL-3.0-only; anyone who took the code under those terms keeps them. This
  release is the first published under MIT, and PyPI metadata only moves on a new
  release — which is why this is a version bump rather than a licence edit.

  Relicensing is cheapest while the author is the sole copyright holder, and that
  window closes on the first outside commit without a CLA or DCO. It closed here
  by choice rather than by expiry.

### Added

- **`CONTRIBUTING.md` with a DCO sign-off requirement.** AGPL carried inbound
  contribution terms implicitly through copyleft; MIT is silent on them. Dropping
  the copyleft without adding the sign-off would leave nothing recording the terms
  a contributor's patch arrives under.
- This changelog. The repo had none, so the 0.2.1 release and everything before it
  is recorded in git history alone.
