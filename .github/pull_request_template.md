## What & why

<!-- What does this change and why? Link any related issue. -->

## Checklist

- [ ] Tests added/updated for the behavior change — a fixture in `fixtures/` plus the logic that reads it, not logic alone
- [ ] `pytest` passes locally — the coverage floor in `pyproject.toml` is a ratchet, raised as coverage improves and never lowered to make a change pass
- [ ] `ruff check .` is clean for files I touched
- [ ] `CHANGELOG.md` updated
- [ ] `pyproject.toml::version` bumped if `src/` changed (see `CLAUDE.md` — the tag is by hand, a forgotten bump is silent)
- [ ] No network call, credential, or model call introduced (the core stays pure — see README "What this repo is NOT")
- [ ] No secrets or proprietary logic committed
- [ ] Fail-loud preserved — no new silent `except: pass` swallows

## Test plan

<!-- How you verified this works. -->

---

**Prepared by:** <!-- model name from the session prompt, e.g. claude-sonnet-5, deepseek-v4-flash, claude-haiku-4-5 — replaces the generic "Generated with Claude Code" footer -->
