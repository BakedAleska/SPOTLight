# SPOTlight deployment status — 2026-10-10

Deployment is **live**: Neon and all three Vercel projects are deployed and verified. Full stepped replay completed successfully with 500/500 events and zero errors. GitHub Actions now successfully runs production migrations and all three Vercel deployments after successful main CI.

> **Update 2026-10-11:** the replay below (run 1, 51 flags) was a deploy check made while System 1 was unreachable, so all 477 triaged events fell back to rules-only (`laya_unavailable`). The data cleanup of 2026-10-11 removes it and restores the seed baseline: 500 events, 58 flags, plus the documents and graph that this report lists as skipped. See "Data cleanup 2026-10-11" at the end and `DEPLOY-neon.md`. The rest of this report is the historical record of 2026-10-10.

## Source and accounts

- Repository: `JylerTackson/SpotTheDOT`.
- Initial manual deployment source: `main`, commit `57e016f4998fd5eda7c04516c6ba7feed2d2b073`; `vercel-deploy` commit `f4e723c38928563985dee6840f361293cb544391` is already an ancestor of `main`.
- Connected GitHub account: `Ammar-btye`.
- Intended Vercel team: `SpotTheDog` (`team_ZS8AOTk4NYMOLgKpCGXpcChk`).
- New Neon project: `SPOTlight`, ID `summer-sound-57299654`, AWS `us-east-2`, Postgres 17, database `neondb`.
- Neon main branch: `br-wispy-hat-b54morim`.
- Safety snapshot: `demo-snapshot`, ID `br-still-dawn-b55ga6ag` (no compute).

## Initial Neon verification (before replay)

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

The host provides Python 3.9.6 for setup tooling, rather than the guide's 3.12. Migration, seeding, status, and service-role verification succeeded with that interpreter; The deployed Python services and later GitHub Actions migrations/builds were verified with Python 3.12.

The existing `SpotTheDog` Neon project uses Postgres 18. An initial migration attempt on its isolated `spotlight-vercel-deploy` branch applied 001–003, then failed on `hnsw.iterative_scan` permissions. No code was changed. The runbook's Postgres 17 project was created instead and all migrations passed. The old isolated branch remains; its parent production branch was not modified. Later investigation reproduced the same error on PG17 and confirmed session initialization as the cause; PG version selection alone is not a fix.

## Production deployments

The initial services were deployed from a clean archive of main commit `57e016f4998fd5eda7c04516c6ba7feed2d2b073`, using Vercel CLI 63.1.2 and the supplied account token. No application code was changed.

| Service | Production URL | Project ID | Deployment ID | Region |
|---|---|---|---|---|
| MCP | https://spotlight-mcp.vercel.app | `prj_6DPppa3WnD7U0Qowx9oWINqqZVDV` | `dpl_GAGTaTfVdUEAqnyR6NneaQMbFRJS` | `cle1` |
| Backend | https://spotlight-backend-tau.vercel.app | `prj_thNz0oFEPYIReKzBtAsM7mxlplje` | `dpl_4BEWbs9tEFXNx12pqjK7ejwb5P8e` | `cle1` |
| Web | https://spotlight-web-mu.vercel.app | `prj_GwWaw6sfw5AAju2lLrqLCTaTjU7b` | `dpl_vtzWSGroYLrxW3vRTYq63EsN4Fqr` | `iad1` |

All three initial deployments reported READY; the IDs above are historical and have since been superseded by successful Actions deployments. MCP and backend functions are in Cleveland next to Neon. The build machines themselves ran in iad1; that does not determine function region.

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

Deployment documentation and workflow were published in [PR #13](https://github.com/JylerTackson/SpotTheDOT/pull/13). GitHub connector branch writes returned 403 despite the user's contributor access; local GitHub CLI authentication as `Ammar-btye` enabled pushes and PR creation. The workflow's GitHub environments are `NEON_API_KEY` for migration credentials and `vercel deploy` for Vercel configuration. The initial `production` environment reference was corrected to match these names.

The pipeline checks out the successful main CI commit, runs `db/setup.py migrate` and `status` using the owner connection secret, and deploys MCP → backend → web with project ownership and production HTTP checks. Feature-branch CI does not deploy production. Migration failure skips Vercel. Production SQL changes currently run directly against main; temporary test branches are not automatically created.

[PR #15](https://github.com/JylerTackson/SpotTheDOT/pull/15) fixed fresh-session pgvector initialization. The failure persisted after correcting the database URL and choosing the owner/direct connection. A temporary function probe failed before a vector operation and succeeded after it; all probe changes were rolled back. The renderer now loads pgvector before defining function-level HNSW settings. Validation on isolated branch `migration-init-regression` (`br-muddy-bread-b5xgkolg`) passed the regression test and two complete 001–010 migration passes, retaining 500 events and 51 flags. This test branch was created with expiry `2026-10-12T00:00:00Z`.

[PR #17](https://github.com/JylerTackson/SpotTheDOT/pull/17) installed pinned uv 0.13.0 in the Vercel job after Python prebuilt builds failed with `[UV_ERROR]`. The official setup action is pinned to its v9.0.0 commit. No application secret changes were needed for this build fix.

The complete pipeline succeeded in [run 38101333927](https://github.com/JylerTackson/SpotTheDOT/actions/runs/38101333927) and [run 38101671492](https://github.com/JylerTackson/SpotTheDOT/actions/runs/38101671492). The latest verified source is `a0ba13dc5527a61a404bed94f9e7755881febce4`: both `neon-migrate` and `deploy` succeeded. Logs confirm migration 010 applied, flags count 51, and uv 0.13.0 used for Python builds. The successful deploy job includes all three production route checks.

A fresh HTTP verification after these runs confirmed MCP health 200/ok, backend health 200/ok with database and MCP up, flags 200 with returned/total 51, and dashboard API 200 with stats and flags fields. The original browser and replay checks below were not repeated after each automated redeploy; they remain evidence from the initial deployment.

Both GitHub environments currently have no required-reviewer or deployment-branch rules. The workflow itself limits production execution to main; adding environment protection is recommended. Credentials are entered directly into the matching environment. Secret rotation after a token was shared was recommended, but completion of that rotation was not independently verified. See `DEPLOY-github-actions.md` for exact configuration and recovery instructions.

## Skipped steps

- `amd/embed/ingest.py` and `db/graph_build.py`: no reachable embedding service was provided. Both must be run later with `Qwen/Qwen3-Embedding-0.6B`, dimension 1024. (2026-10-11: run as part of the data cleanup, through the AMD embedder's SSH bridge.)
- Embedding/RAG smoke queries: depend on the absent embedder; documents remain empty, so a positive search-result check cannot pass yet.
- `db/smoke_test.py --roles`: the script runs embedding queries before its role check and creates a temporary login with a fixed password. Equivalent least-privilege checks were run directly against the actual MCP login instead, with the logging insert rolled back.
- AMD environment variables and related API keys: intentionally not provisioned until the AMD services exist; deploy with rules-only triage, templated explanations, and text-search fallback.

The initial deployment required no application changes. The subsequently authorized migration-runner fix, regression test, and workflow dependency fixes are merged. Live service, initial browser/replay checks, and automated deployment checks passed; embedding checks remain deferred.

## Initial deployment health outputs (historical)

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

## Data cleanup 2026-10-11

After a read-only audit of every table, the owner restores production to the documented seed baseline. The exact
procedure and counts are in `DEPLOY-neon.md` "Data cleanup 2026-10-11". In short:

- **Backup first.** A `pg_dump` taken before any write is kept off-repo (never committed). The `demo-snapshot` branch
  and Neon point-in-time restore stay available as rollbacks.
- **Migrations next:** `004` (exact-id ranking), `014` and `015`, before any data change.
- **Removed (test and verification rows), in one guarded transaction run by an off-repo owner script:**
  - replay run 1: 51 flags, its `replay_runs` and `app.job_runs` rows, and 500 duplicate `raw.spot_event_log` rows
    (the 500 `events` it re-ingested were overwritten in place, not deleted);
  - 6 template assessments and 3 button-test acknowledgements;
  - 2 test chat conversations (4 messages) and 1 never-claimed test live run;
  - 80 `app.model_calls` and 7 `app.mcp_tool_calls` rows (dev failures; 21 error texts held the Gemini key, which
    must be rotated) and 59,848 poll-dominated `app.request_log` rows.

  The kit tables, the seed's `raw.*` rows and the seed's `app.job_runs` row are kept.
- **Restored and filled:**
  - in the same transaction (not `seed --force-reseed`): `seed_events` (forced) upserted the 500 events with the seed
    payload (`enriched` block, no `triage`), `seed_flags` wrote 58 flags with ids 1–58 (sequences reset),
    `seed_analytics` filled `mission_kpis` 27, `risk_scores` 189 and `insights` 5, and an `UPDATE` added the
    `raw.inspection_media` hints;
  - then `ingest` (503 documents, all embedded with Qwen3-Embedding-0.6B on the W7900D) and `graph_build` (574 nodes,
    1,968 edges).
- **Verify:** `python db/setup.py status`, then `db/smoke_test.py` and `db/smoke_test.py --roles`. `GET
  /api/v1/flags?limit=500` should report 58.
