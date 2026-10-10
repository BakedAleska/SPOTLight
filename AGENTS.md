# Agent instructions (Codex and other coding agents)

**The project rules live in [`CLAUDE.md`](CLAUDE.md) and apply to you in full. Read it before doing anything.**
That covers the event and deadline, the engineering rules (containerize every service, one root `docker-compose.yml`,
contract-first in `docs/`), the context protocol (`research/INDEX.md` and `research/decisions.md` at session start)
and the research and decision-logging rules.

Agent-specific rules on top of that:
- **Never commit to `main`.** Work on the branch your task names, commit in small, reviewable steps and push it.
  Don't merge, rebase or force-push other people's branches.
- **Stay inside your task's scope.** Each area has an owner; if a change is needed outside your scope, stop and say
  what and why instead of making it:
  - System 2 (`amd/system2/`, its contract and schemas): branch `aatif/system2`
  - System 1 / Laya (`amd/system1/`)
  - backend, MCP, embed, db (`backend/`, `amd/mcp/`, `amd/embed/`, `db/`)
  - web console (`web/`)
- **Prove changes before pushing.** Run the relevant `make` test target (`make help` lists them) or the service's own
  `pytest`. If you add a service, add its test target to the `Makefile` `test` aggregate. For any compose change,
  `docker compose --profile local config --quiet` must pass.
- **Never invent results.** Numbers in READMEs, reports and research files come from a run you actually did (say
  where it ran) or are marked unverified. GPU numbers come only from the AMD box.
- **No secrets in git.** Config comes from env vars, with examples in `.env.example`.
- Task briefs and runbooks for agents live in `docs/` (for example `docs/AMD-RUNS-2026-10-10.md`).
