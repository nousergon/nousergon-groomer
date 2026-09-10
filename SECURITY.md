# Security Policy

## Reporting a vulnerability

If you find a security vulnerability in nousergon-groomer, please report it privately:

- **Preferred:** open a [GitHub Security Advisory](https://github.com/nousergon/nousergon-groomer/security/advisories/new). This keeps the discussion private until a fix ships.
- **Alternative:** email `security@nousergon.ai` with a description and reproduction steps.

Please **do not** open a public issue for security reports. I aim to acknowledge within 72 hours and ship a fix or mitigation within 14 days for high-severity issues.

## Scope

nousergon-groomer is the **deterministic control plane** for the autonomous backlog-and-PR loop: a pure function over recorded JSON snapshots — no network, no credentials, no model call (see `README.md`, "What this repo is NOT"). That narrows the sensitive surface considerably, but it is not zero:

- **Untrusted input handling:** the disposition function, dependency evaluator, and admission controller all parse GitHub-shaped JSON (issue/PR bodies, labels, snapshot state) that in the deployed system originates from public repositories. A parsing path that can be driven to excessive resource use, an unhandled exception that crashes the operational harness, or logic that can be tricked into a wrong disposition (e.g. treating a blocked item as clearable) is in scope.
- **Supply-chain:** a dependency or install path that could execute untrusted code during `pip install nousergon-groomer[...]` or at import time.
- **Config/adapter injection:** `config.py` and the adapter surface accept fleet-specific data (gate families, lane definitions) — a path that lets that data escape its declared shape and reach unintended code is in scope.

Out of scope:

- Anything in the **private operational harness** (dispatch, merge execution, PAT handling) — that code does not live in this repo; report it against the private layer instead.
- Vulnerabilities in upstream dependencies not yet publicly disclosed — report those upstream first.
- DoS from a snapshot you control yourself (this is a library, not a hosted service).

## Threat model assumptions

- **No network, no credentials, no model call.** The core never makes an outbound request and never holds a secret; if a change introduces one, that change has left this repo's scope (§ "What this repo is NOT" in `README.md`) and needs re-evaluating under a different threat model first.
- **Inputs are adversarial-adjacent.** The observed-world snapshot this package evaluates is, in production, derived from public GitHub repositories — issue and PR content an outside contributor can shape. The core must not trust the *shape* of that data beyond what its models validate.
- **The consumer, not this package, holds authority.** Every disposition this package computes (act/blocked/terminal/undecidable) is advisory to the private harness that actually merges, labels, or dispatches — a wrong disposition is a correctness bug, not itself a privilege escalation, unless the harness acts on it uncritically.

## Hardening recommendations for consumers

- Treat every `Item`/`ObservedWorld` this package validates as coming from an untrusted source; do not skip Pydantic validation on the way in.
- Pin an exact version (`nousergon-groomer[...]==X.Y.Z`) rather than a floor — see `CLAUDE.md`'s lockstep note on consumer pins.
- Keep the operational harness (credentials, dispatch, merge authority) in the private layer this repo explicitly excludes.
