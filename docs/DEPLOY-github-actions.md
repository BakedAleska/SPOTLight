# Deploy SPOTlight through GitHub Actions

The workflow `.github/workflows/deploy-vercel.yml` deploys the repository to your Vercel account without connecting Vercel's GitHub app to this repository. It authenticates with a Vercel token and deploys production database migrations, then three existing projects in order: Neon → MCP → backend → web. The working deployment was verified in [run 38101671492](https://github.com/JylerTackson/SpotTheDOT/actions/runs/38101671492) at commit `a0ba13dc5527a61a404bed94f9e7755881febce4`.

This replaces the Git import step, not account permissions or database setup. The token must be allowed to deploy all three projects, and a repository admin must configure the two GitHub environments. Never put a token or connection string in the workflow file.

## 1. Finish account setup

Create three empty Vercel projects in the account/team you intend to deploy to, without importing a Git repository. You can use the authenticated CLI:

```bash
vercel project add spotlight-mcp --scope <your-vercel-scope>
vercel project add spotlight-backend --scope <your-vercel-scope>
vercel project add spotlight-web --scope <your-vercel-scope>
```

Configure each project's settings:

| Project | Root Directory | Framework | Other settings |
|---|---|---|---|
| MCP | `amd/mcp` | FastAPI (ASGI-compatible Python builder) | Build command unset; config pins `cle1`; Other fails to recognize `asgi.py` |
| Backend | `backend` | FastAPI | Include source files outside Root Directory enabled; build command from `vercel.json`; `cle1` |
| Web | `web` | Next.js | Node 24.x |

Record each project's ID, the owning account/team ID, the CLI scope slug, and its stable production `*.vercel.app` domain. The workflow checks IDs, owner and root directories before deploying. Production domains must allow the cross-project HTTP calls required by the runbook; a Vercel login page fails verification.

Create a Vercel deployment token with access to that account/team at <https://vercel.com/account/tokens>. Enter it directly into GitHub's protected environment secret below. Do not paste it into chat, commit it, or pass it in command-line arguments.

## 2. Configure application secrets in Vercel

Follow `DEPLOY-neon.md` and `DEPLOY-vercel.md` before the first run. The verified Neon project from this deployment is `summer-sound-57299654` (Postgres 17, AWS us-east-2). Since the 2026-10-11 cleanup, its main branch holds the seed baseline: 500 events, 58 flags, 503 embedded documents and the 574-node knowledge graph (`DEPLOY-neon.md` "What you end up with" and "Data cleanup 2026-10-11"). A full replay with reset leaves 51 flags, because the replay covers M001–M023 only.

Use the generated credentials in the private, git-ignored local `.env` from deployment setup. Copy them directly into each project's **Production** environment settings:

- MCP: pooled `DATABASE_URL` as `spot_mcp_user`, `MCP_AUTH_TOKEN`, `EMBED_URL`, `EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B`, `EMBED_DIM=1024`.
- Backend: pooled `DATABASE_URL` as `spot_app_user`, `ADMIN_TOKEN`, the same `MCP_AUTH_TOKEN`, `MCP_URL=https://<mcp-production-domain>/mcp`, `SERVERLESS=1`, `REPLAY_MODE=stepped`, `SYSTEM1_INGEST_TIMEOUT_S=10`, `DB_POOL_MAX=5`, `CORS_ORIGINS=https://<web-production-domain>`, the same `EMBED_URL` / `EMBED_MODEL` / `EMBED_DIM` as MCP, and `GEMINI_API_KEY` (saved with no trailing newline).
- Leave `EMBED_QUERY_PREFIX` and `LOG_REQUESTS` unset on both. `EMBED_URL` is a tunnel URL that changes on every relaunch; update it on both projects and run the workflow again (`DEPLOY-vercel.md` "Embedding URL after a tunnel relaunch").
- Web: `BACKEND_URL=https://<backend-production-domain>/api/v1`.

Leave AMD service URLs and related API keys unset until those services exist. The workflow does not create, rotate or transfer application credentials through GitHub. It pulls Vercel's production configuration for builds; the pulled files are removed after deployment and are never uploaded as workflow artifacts. Changes to Vercel environment settings require another workflow run to redeploy.

## 3. Configure the two GitHub environments

In `JylerTackson/SpotTheDOT`, go to **Settings → Environments**. The workflow uses these exact names:

| Job | Environment | Required secret |
|---|---|---|
| `neon-migrate` | `NEON_API_KEY` | `NEON_DATABASE_URL` |
| `deploy` | `vercel deploy` | `VERCEL_TOKEN` |

`NEON_API_KEY` is the name of an environment as well as an optional secret; the current migration job does not use that API-key secret. It connects directly with `NEON_DATABASE_URL`.

For the **NEON_API_KEY** environment, set `NEON_DATABASE_URL` to the direct owner connection string copied from Neon **SPOTlight → main → Connect**, database `neondb`, role `neondb_owner`, connection pooling off. Service URLs in Vercel remain pooled and use the separate service login roles. The migration connection must point to the same branch as those service URLs.

The following Neon variables are configured as account context, but the current workflow does not consume them or use them to select the database. Changing them does not change the connection string secret:

| Variable | Value |
|---|---|
| `NEON_PROJECT_ID` | `summer-sound-57299654` |
| `NEON_PARENT_BRANCH_ID` | `br-wispy-hat-b54morim` |
| `NEON_DATABASE_NAME` | `neondb` |
| `NEON_DATABASE_ROLE` | `neondb_owner` |

For the **vercel deploy** environment, add the secret and variables below. An environment named `production` is not referenced by this workflow. GitHub environment names are separate from Vercel's Production target.

Recommended protection: restrict both environments to `main`, add required reviewers, and protect main with pull-request review and CI checks. At the latest inspection, both environments had no reviewers or deployment branch restrictions; approval is not currently enforced.

Add this **environment secret**:

| Secret | Value |
|---|---|
| `VERCEL_TOKEN` | Your deployment token, entered directly in GitHub |

Add these **environment variables** (IDs and domains are not credentials):

| Variable | Value |
|---|---|
| `VERCEL_ORG_ID` | ID of the owning Vercel account/team |
| `VERCEL_SCOPE` | Matching CLI account/team slug |
| `VERCEL_CLI_VERSION` | `63.1.2` (used for the verified deployments); aliases like `latest` are rejected |
| `VERCEL_MCP_PROJECT_ID` | MCP project's `prj_...` ID |
| `VERCEL_BACKEND_PROJECT_ID` | Backend project's `prj_...` ID |
| `VERCEL_WEB_PROJECT_ID` | Web project's `prj_...` ID |
| `MCP_PRODUCTION_URL` | `https://<mcp>.vercel.app`, no path or trailing slash |
| `BACKEND_PRODUCTION_URL` | `https://<backend>.vercel.app`, no path or trailing slash |
| `WEB_PRODUCTION_URL` | `https://<web>.vercel.app`, no path or trailing slash |

For multiple services, `VERCEL_ORG_ID` and `VERCEL_TOKEN` alone are insufficient: each project needs its own ID. Do not use deployment-specific or preview URLs for the three origin variables.

## 4. Merge and run

The workflow is merged into `main`. After the existing **CI** workflow succeeds for a push to `main`, the deployment workflow checks out that exact tested commit and runs Neon migrations before Vercel deployments. Environment approval is required only if a reviewer rule is configured. It never deploys pull requests or fork commits. Automatic production deployments run serially and are not canceled midway.

For a configuration-only redeploy, select **Actions → Deploy SPOTlight to Vercel → Run workflow → main**. Manual runs are limited to `main`; they do not automatically wait for a new CI run, so select a commit whose CI has passed.

The Neon job installs Python 3.12 and `db/requirements.txt`, then runs `python db/setup.py migrate` and `python db/setup.py status` using the owner secret. It applies migrations directly to production; it does not create a test branch, seed, reset demo data, rotate passwords, or ingest embeddings. Because every migration file runs again on every deploy, a migration must be idempotent and warn rather than fail, and a dropped table needs its `CREATE` removed in the same commit (`DEPLOY-neon.md` "Rules"). Vercel's `needs: neon-migrate` dependency skips deployment on migration failure.

The Vercel job installs Node 24, Python 3.12, uv 0.13.0 with a pinned official setup action, and the pinned Vercel CLI. Each project is pulled, built with production settings and deployed with `--prebuilt --prod` from the repository root. The backend build therefore has access to the shared code and kit data. Deployment stops at the first failed build or HTTP check; earlier services may already be live, so this is not an atomic release.

## 5. Verification and remaining manual checks

The workflow checks:

- MCP `/healthz` reports `ok: true`.
- Backend liveness and `/health`: database and MCP must be up; absent AMD services are permitted.
- Backend `/docs` and `/api/v1/flags?limit=500`; the summary reports returned and total flag counts.
- Web `/` and `/api/dashboard` return successfully with dashboard fields.

The summary records the deployed commit and production origins. This does not verify browser rendering, authenticated MCP tools, or replay. Complete **all** remaining checks in `DEPLOY-vercel.md`: authenticated MCP tools/list (17 tools), missing/wrong token rejection, loading the dashboard in a browser, and stepped replay through completion. Drive replay from a trusted local shell with the generated admin token; do not reset demo data automatically on every push.

Embedding ingestion, the graph build and the embedding smoke tests are not part of the workflow. The owner runs them by hand, through the embedder's SSH bridge, in the order of `DEPLOY-neon.md` §6 (`migrate -> seed -> ingest -> graph_build -> status -> smoke`). After any replay or `reset-demo`, run `ingest --kinds incident` and `graph_build` again. See `DEPLOY-status-2026-10-10.md` for database and production verification evidence.

## References

- [Vercel CLI project deployment](https://vercel.com/docs/projects/deploy-from-cli)
- [Vercel build and prebuilt deployment](https://vercel.com/docs/cli/deploy)
- [Vercel CI/CD with GitHub Actions](https://vercel.com/docs/deployments/configure-a-build#using-github-actions)

The complete workflow has now succeeded in [run 38101333927](https://github.com/JylerTackson/SpotTheDOT/actions/runs/38101333927) and [run 38101671492](https://github.com/JylerTackson/SpotTheDOT/actions/runs/38101671492): migrations, project ownership checks, all three builds/deployments, and production HTTP checks passed. Direct CLI deployment remains available for recovery.

## Verified project mapping

Use these values for the existing projects instead of creating duplicates:

| Variable | Value |
|---|---|
| `VERCEL_ORG_ID` | `team_ZS8AOTk4NYMOLgKpCGXpcChk` |
| `VERCEL_SCOPE` | `spot-the-dog` |
| `VERCEL_MCP_PROJECT_ID` | `prj_6DPppa3WnD7U0Qowx9oWINqqZVDV` |
| `VERCEL_BACKEND_PROJECT_ID` | `prj_thNz0oFEPYIReKzBtAsM7mxlplje` |
| `VERCEL_WEB_PROJECT_ID` | `prj_GwWaw6sfw5AAju2lLrqLCTaTjU7b` |
| `MCP_PRODUCTION_URL` | `https://spotlight-mcp.vercel.app` |
| `BACKEND_PRODUCTION_URL` | `https://spotlight-backend-tau.vercel.app` |
| `WEB_PRODUCTION_URL` | `https://spotlight-web-mu.vercel.app` |

Configure deployment tokens directly in the matching GitHub environment. No token value belongs in this table.

## Troubleshooting and recovery

| Failure | Resolution |
|---|---|
| Missing `VERCEL_ORG_ID` although it is configured | Confirm the deploy job references `vercel deploy`; values from another environment are not inherited. |
| Migration connects to an old Neon project | Replace `NEON_DATABASE_URL`; changing `NEON_PROJECT_ID` alone has no effect. |
| `permission denied to set parameter "hnsw.iterative_scan"` with the owner role | Use the merged pgvector initialization fix in `db/setup.py` ([PR #15](https://github.com/JylerTackson/SpotTheDOT/pull/15)). It loads pgvector in the same connection before rendering function settings. |
| `[UV_ERROR] uv is required but was not found in PATH` | Ensure the Vercel job includes setup-uv ([PR #17](https://github.com/JylerTackson/SpotTheDOT/pull/17)). Python installation alone is insufficient. |
| Vercel pull reports `[SENSITIVE]` placeholders | Sensitive runtime values cannot be pulled. Keep them in Vercel; do not paste credentials into logs or commit pulled files. Current builds passed with these placeholders. |

After a secret-only correction, re-run failed jobs for a run whose code is still valid. After a code fix, let successful CI on the new main commit start a new deployment; rerunning an older run checks out its older commit. Inspect the Actions summary and production health before declaring success. Because the migration runner reapplies all SQL files, migration authors must preserve repeatability and compatibility with the currently deployed application.

Temporary migration branches are not currently automated. Before a risky schema change, create an isolated Neon branch, test the migration there, then review the production change. The pgvector fix was validated this way, including two complete migration passes. Do not delete or reset production as a recovery shortcut.
