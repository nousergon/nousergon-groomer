---
name: Feature request
about: Suggest an idea or enhancement
title: ''
labels: enhancement
assignees: ''
---

**The problem**
What are you trying to do that nousergon-groomer doesn't support today?

**Proposed solution**
What you'd like to see.

**Alternatives considered**
Other approaches you've thought about.

**Scope note**
This repo is the **deterministic control plane** only (README "What this repo IS/is NOT"): pure
logic over a recorded snapshot, no network, no credentials, no model call, no dispatch or
scheduler. If what you're proposing needs a GitHub client, a PAT, a model call, or fleet-specific
dispatch behavior, it belongs in the private operational harness, not here — say so and this can
be redirected quickly.
