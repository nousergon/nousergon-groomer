# Contributing

## Licence and sign-off

This project is **MIT** as of 0.3.0. It was AGPL-3.0-only through 0.2.1, and that
matters for contributions: **AGPL carried inbound contribution terms implicitly
through copyleft, and MIT is silent on them.** Dropping the copyleft without
adding something in its place would leave nothing recording the terms a patch
arrives under.

So every commit must carry a **Developer Certificate of Origin** sign-off:

```bash
git commit -s -m "your message"
```

which appends:

```
Signed-off-by: Your Name <your.email@example.com>
```

That line is your statement that you wrote the patch, or have the right to
submit it, under this project's licence — the full text is at
[developercertificate.org](https://developercertificate.org/). No CLA, no
copyright assignment; the sign-off is the whole requirement.

A pull request without sign-offs will be asked for them before review.

## Development

```bash
pip install -e ".[dev,config]"
pytest
ruff check src tests
```

The core is deterministic and fixture-runnable by design — a change to the
disposition function, the dependency evaluator, the admission controller or the
lane classifier belongs with a fixture that fails against the old behaviour.
A test that passes both before and after is not evidence the change did
anything.

## Scope

This repo is the **control plane**, not the fleet that runs it. Bucket names,
tokens, repository lists, cadences and anything else specific to one deployment
belong in that deployment's configuration, never in this source. A patch that
compiles an operator's topology into the core will be declined regardless of
how convenient it is.
