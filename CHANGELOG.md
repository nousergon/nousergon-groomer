# Changelog

All notable changes to `nousergon-groomer` are recorded here. Versions follow
[semantic versioning](https://semver.org/).

While the major version is `0`, the public API may change between minor versions.

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
