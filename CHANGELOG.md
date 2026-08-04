# Changelog

All notable changes to `nousergon-groomer` are recorded here. Versions follow
[semantic versioning](https://semver.org/).

While the major version is `0`, the public API may change between minor versions.

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
