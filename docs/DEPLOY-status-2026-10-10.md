# SPOTlight deployment status — 2026-10-10

Deployment is **partially complete**: Neon is initialized and verified. Vercel deployment is blocked by account permissions; no service has been deployed by this run.

## Source and accounts

- Repository: `JylerTackson/SpotTheDOT`.
- Source: `main`, commit `57e016f4998fd5eda7c04516c6ba7feed2d2b073`; `vercel-deploy` commit `f4e723c38928563985dee6840f361293cb544391` is already an ancestor of `main`.
- Connected GitHub account: `Ammar-btye`.
- Intended Vercel team: `SpotTheDog` (`team_ZS8AOTk4NYMOLgKpCGXpcChk`).
- New Neon project: `SPOTlight`, ID `summer-sound-57299654`, AWS `us-east-2`, Postgres 17, database `neondb`.
- Neon main branch: `br-wispy-hat-b54morim`.
- Safety snapshot: `demo-snapshot`, ID `br-still-dawn-b55ga6ag` (no compute).

## Neon verification

Followed `docs/DEPLOY-neon.md`: migrations 001–010, service login roles, non-destructive seed, status, and pooled service-connection checks.

| Check | Observed result |
|---|---|
| Migrations | All ten applied successfully |
| pgvector | 0.8.0, `vector(1024)`, exact search |
| Locations / assets / missions | 15 / 20 / 27 |
| Actions / readings / anomalies | 270 / 220 / 40 |
| Events / flags | 500 / 58 |
| Documents / graph nodes / graph edges | 0 / 0 / 0 |
| Raw replay rows / kit files / kit rows | 500 / 6 / 592 |
| Backend login | Can connect; flags count 58, documents count 0 |
| MCP login | Can connect; flags count 58, documents count 0 |
| MCP reads | Allowed on flags, documents, raw event log |
| MCP restricted reads | Denied on raw kit rows and full model-call rows |
| MCP public writes | UPDATE flags and DELETE documents denied (tested with false predicates) |
| MCP logging | INSERT with RETURNING permitted; verification insert rolled back |

Passwords and MCP/admin tokens were generated with `openssl rand -hex 24`. Values are held only in a permission-restricted, git-ignored local `.env`; none appear in this report or commits. Vercel has not received them yet.

The host provides Python 3.9.6 for setup tooling, rather than the guide's 3.12. Migration, seeding, status, and service-role verification succeeded with that interpreter; Vercel Python 3.12 runtime behavior remains unverified.

The existing `SpotTheDog` Neon project uses Postgres 18. An initial migration attempt on its isolated `spotlight-vercel-deploy` branch applied 001–003, then failed on `hnsw.iterative_scan` permissions. No code was changed. The runbook's Postgres 17 project was created instead and all migrations passed. The old isolated branch remains; its parent production branch was not modified.

## Vercel blocker

1. Importing `JylerTackson/SpotTheDOT` returned HTTP 400: the Vercel GitHub integration needs access to this repository.
2. Creating `spotlight-mcp` explicitly in the intended team returned HTTP 403: no permission to create the project.
3. No authenticated Vercel CLI fallback is available on this machine.

The account owner needs to authorize project creation in the intended team and grant the Vercel GitHub app access to `JylerTackson/SpotTheDOT`. Then resume the ordered runbook: MCP → backend → web. Use production domains for cross-project URLs and redeploy after every environment change.

## Requested service verification

| Item | Result |
|---|---|
| MCP production URL | Not deployed |
| Backend production URL | Not deployed |
| Web production URL | Not deployed |
| MCP `/healthz` output | Not run; Vercel permission blocker |
| MCP authenticated tools/list and unauthorized check | Not run; Vercel permission blocker |
| Backend `/health?deep=false`, `/health`, `/docs` | Not run; Vercel permission blocker |
| `/api/v1/flags` count | HTTP endpoint unverified; database count is 58 |
| Dashboard and `/api/dashboard` | Not run; Vercel permission blocker |
| Stepped replay to completion | Not run; backend not deployed |

## Skipped steps

- `amd/embed/ingest.py` and `db/graph_build.py`: no reachable embedding service was provided. Both must be run later with `Qwen/Qwen3-Embedding-0.6B`, dimension 1024.
- Embedding/RAG smoke queries: depend on the absent embedder; documents remain empty, so a positive search-result check cannot pass yet.
- `db/smoke_test.py --roles`: the script runs embedding queries before its role check and creates a temporary login with a fixed password. Equivalent least-privilege checks were run directly against the actual MCP login instead, with the logging insert rolled back.
- AMD environment variables and related API keys: intentionally not provisioned until the AMD services exist; deploy with rules-only triage, templated explanations, and text-search fallback.
- All Vercel service verification and stepped replay: blocked by account permissions above.

No application code was changed. This report does not claim deployment or end-to-end success.
