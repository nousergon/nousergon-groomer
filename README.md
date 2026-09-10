# badges

Machine-written. Do not edit by hand.

This orphan branch carries generated badge-endpoint documents for
nousergon-groomer's README, published by
`scripts/publish_coverage_badge.sh` from `.github/workflows/test.yml` on
every push to `main` (py3.12 leg only). Each file here is a shields.io
`endpoint` JSON document — see
https://shields.io/badges/endpoint-badge — consumed by
`https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nousergon/nousergon-groomer/badges/<file>`.

repository-baseline-policy.md §5.1: a badge value must be generated, never
hand-set. `coverage.json` starts at "pending" until the first CI run on
`main` after this branch is seeded publishes a real measurement.
