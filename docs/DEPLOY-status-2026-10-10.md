# SPOTlight deployment status — 2026-10-10

Deployment is **live**: Neon and all three Vercel projects are deployed and verified. Full stepped replay completed successfully with 500/500 events and zero errors. The documentation and workflow are prepared for review on a separate deployment branch.

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

Passwords and MCP/admin tokens were generated with `openssl rand -hex 24`. Values are held only in a permission-restricted, git-ignored local `.env` and the respective Vercel Production environments; none appear in this report or commits.

The host provides Python 3.9.6 for setup tooling, rather than the guide's 3.12. Migration, seeding, status, and service-role verification succeeded with that interpreter; Vercel Python 3.12 runtime behavior remains unverified.

The existing `SpotTheDog` Neon project uses Postgres 18. An initial migration attempt on its isolated `spotlight-vercel-deploy` branch applied 001–003, then failed on `hnsw.iterative_scan` permissions. No code was changed. The runbook's Postgres 17 project was created instead and all migrations passed. The old isolated branch remains; its parent production branch was not modified.

## Production deployments

All services were deployed from a clean archive of main commit `57e016f4998fd5eda7c04516c6ba7feed2d2b073`, using Vercel CLI 63.1.2 and the supplied account token. No application code was changed.

| Service | Production URL | Project ID | Deployment ID | Region |
|---|---|---|---|---|
| MCP | https://spotlight-mcp.vercel.app | `prj_6DPppa3WnD7U0Qowx9oWINqqZVDV` | `dpl_GAGTaTfVdUEAqnyR6NneaQMbFRJS` | `cle1` |
| Backend | https://spotlight-backend-tau.vercel.app | `prj_thNz0oFEPYIReKzBtAsM7mxlplje` | `dpl_4BEWbs9tEFXNx12pqjK7ejwb5P8e` | `cle1` |
| Web | https://spotlight-web-mu.vercel.app | `prj_GwWaw6sfw5AAju2lLrqLCTaTjU7b` | `dpl_vtzWSGroYLrxW3vRTYq63EsN4Fqr` | `iad1` |

All three deployments report READY. MCP and backend functions are in Cleveland next to Neon. The build machines themselves ran in iad1; that does not determine function region.

### Configuration

Generated database passwords, MCP bearer token, and backend admin token were placed in the corresponding Vercel Production environments as sensitive values. Cross-project URLs use the production domains in the table. Backend CORS was configured for the web production domain before its deployment. Every environment configuration was applied before the affected production build.

The initial Git import failed because the connected integration lacked repository access; project creation through the connector also returned 403. The user subsequently supplied a personal deployment token. It permitted direct Vercel API project creation and CLI deployment in the intended org, without a Git import. No Git auto-deploy integration is configured.

The first MCP build with the Other framework preset failed: `asgi.py` did not match a Serverless Function in the api directory. After checking the runbook troubleshooting table and official Vercel Python entrypoint guidance, the project framework was set to FastAPI (the ASGI-compatible Python builder). This recognized the existing entrypoint and deployed successfully. No code or requirements were changed.

### Verified routes and behavior

- MCP `/healthz`: HTTP 200, `{"ok":true,"server":"spot-context","policy":"vendored-policy.py@2026-10-10.1"}`.
- MCP authenticated tools/list: HTTP 200, 17 tools. Missing and incorrect tokens: HTTP 401, `{"error":"unauthorized"}`.
- Backend `/health?deep=false`: HTTP 200, ok true, replay mode stepped, bundled kit includes 27 missions, 270 actions and the replay file.
- Backend `/health`: HTTP 200, ok true, database and logs_database up, MCP up (HTTP 200); system1 and embed down, system2 and orchestrator unset as expected without AMD URLs.
- Backend `/docs`: HTTP 200, HTML returned.
- Backend `/api/v1/flags?limit=500` before replay: HTTP 200, 58 returned and page.total 58.
- Flag context: HTTP 200, `source: mcp`.
- Flag explanation: HTTP 200, `source: template`.
- Search: HTTP 200, `mode: fulltext`.
- Web `/`: HTTP 200. `/api/dashboard`: HTTP 200, real metrics including 27 missions, 21 hidden failures, 33.3% low-battery success and +5.3 matched-battery points.
- Chromium: dashboard visibly rendered, backend dashboard request returned 200, no page errors. Queue counts were observed changing while the replay ingested events.
- Error-log scan: Vercel CLI logs --level error --since 1h returned zero error records for all three projects at the time of scanning.

### Replay verification

Full replay was started with `{"speed":120,"reset":true}` on the new project's main branch, after creating demo-snapshot. Steps use a 20-second budget and maximum 100 events; due-time scheduling is retained. The run finished at 2026-10-10T23:06:44Z after about six minutes: 500/500 sent, 51 escalations and flags created, 477 Laya-unavailable fallbacks, 23 skipped events, and zero errors. The skipped events are included in the sent count; the run reached cursor 500 and state finished. After replay, both `/api/v1/flags?limit=500` and `/api/dashboard` report 51 flags (the pre-replay seed had 58).

## Publishing and automation

The documentation and GitHub Actions workflow are on `deploy/spotlight-production-20261010`, based on the latest main. The GitHub connector returned 403 when creating a branch; local GitHub CLI authentication now succeeds as `Ammar-btye` and enables publishing through Git.

`deploy-vercel.yml` passes actionlint and deploys MCP → backend → web after successful CI on main, using a protected production environment. It has not been run. `DEPLOY-github-actions.md` contains the verified project IDs, production domains and account scope. A repository admin must configure a fresh Vercel deployment token directly in that GitHub environment before running the workflow; no token values are included in documentation.

## Skipped steps

- `amd/embed/ingest.py` and `db/graph_build.py`: no reachable embedding service was provided. Both must be run later with `Qwen/Qwen3-Embedding-0.6B`, dimension 1024.
- Embedding/RAG smoke queries: depend on the absent embedder; documents remain empty, so a positive search-result check cannot pass yet.
- `db/smoke_test.py --roles`: the script runs embedding queries before its role check and creates a temporary login with a fixed password. Equivalent least-privilege checks were run directly against the actual MCP login instead, with the logging insert rolled back.
- AMD environment variables and related API keys: intentionally not provisioned until the AMD services exist; deploy with rules-only triage, templated explanations, and text-search fallback.
- GitHub Actions execution: pending workflow merge and production environment configuration; direct CLI deployment succeeded.

No application code was changed. All requested live service, dashboard and replay checks completed; embeddings checks remain deferred. Automated deployment remains pending workflow merge and production environment configuration.

## Final health outputs

### MCP /healthz (HTTP 200)

```json
{
  "ok": true,
  "server": "spot-context",
  "policy": "vendored-policy.py@2026-10-10.1"
}
```

### Backend /health (HTTP 200)

```json
{
  "ok": true,
  "service": "backend",
  "version": "1.0.0",
  "triage_mode": "serial",
  "database": {
    "status": "up"
  },
  "logs_database": {
    "status": "up"
  },
  "system1": {
    "status": "down",
    "url": "http://localhost:8082",
    "error": "ConnectError: All connection attempts failed",
    "breaker": "closed"
  },
  "embed": {
    "status": "down",
    "url": "http://localhost:8081",
    "error": "ConnectError: All connection attempts failed",
    "breaker": "closed"
  },
  "mcp": {
    "status": "up",
    "url": "https://spotlight-mcp.vercel.app/mcp",
    "breaker": "closed",
    "http": 200
  },
  "system2": {
    "status": "unset",
    "breaker": "closed"
  },
  "orchestrator": {
    "status": "unset",
    "breaker": "closed"
  },
  "replay": {
    "state": "finished",
    "mode": "stepped",
    "run": 1,
    "job_run_id": 2,
    "speed": 120.0,
    "max_gap_s": 2.0,
    "triage_mode": "serial",
    "total": 500,
    "sent": 500,
    "escalated": 51,
    "flags_created": 51,
    "laya_unavailable": 477,
    "skipped": 23,
    "errors": 0,
    "cursor": 500,
    "next_due_in_s": null,
    "last_event": {
      "flag_id": null,
      "event_id": "E00500",
      "timestamp": "2026-09-09T02:16:48",
      "event_type": "ACTION_STARTED",
      "mission_id": "M023"
    },
    "started_at": "2026-10-10T23:00:42.270479+00:00",
    "finished_at": "2026-10-10T23:06:44.054631+00:00",
    "error": null
  },
  "kit": {
    "dir": "/var/task/_shared/amd/system1/data/kit",
    "missions": 27,
    "actions": 270,
    "replay_file": true
  }
}
```

### Replay status (HTTP 200)

```json
{
  "state": "finished",
  "mode": "stepped",
  "run": 1,
  "job_run_id": 2,
  "speed": 120.0,
  "max_gap_s": 2.0,
  "triage_mode": "serial",
  "total": 500,
  "sent": 500,
  "escalated": 51,
  "flags_created": 51,
  "laya_unavailable": 477,
  "skipped": 23,
  "errors": 0,
  "cursor": 500,
  "next_due_in_s": null,
  "last_event": {
    "flag_id": null,
    "event_id": "E00500",
    "timestamp": "2026-09-09T02:16:48",
    "event_type": "ACTION_STARTED",
    "mission_id": "M023"
  },
  "started_at": "2026-10-10T23:00:42.270479+00:00",
  "finished_at": "2026-10-10T23:06:44.054631+00:00",
  "error": null
}
```
