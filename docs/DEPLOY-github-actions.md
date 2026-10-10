# Deploy SPOTlight through GitHub Actions

The workflow `.github/workflows/deploy-vercel.yml` deploys the repository to your Vercel account without connecting Vercel's GitHub app to this repository. It authenticates with a Vercel token and deploys three existing projects in order: MCP → backend → web.

This replaces the Git import step, not account permissions or database setup. The token must be allowed to deploy all three projects, and a repository maintainer must merge the workflow and configure the GitHub environment. Never put a token or connection string in the workflow file.

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
| MCP | `amd/mcp` | Python/auto-detect | Build command unset; config pins `cle1` |
| Backend | `backend` | FastAPI | Include source files outside Root Directory enabled; build command from `vercel.json`; `cle1` |
| Web | `web` | Next.js | Node 24.x |

Record each project's ID, the owning account/team ID, the CLI scope slug, and its stable production `*.vercel.app` domain. The workflow checks IDs, owner and root directories before deploying. Production domains must allow the cross-project HTTP calls required by the runbook; a Vercel login page fails verification.

Create a Vercel deployment token with access to that account/team at <https://vercel.com/account/tokens>. Enter it directly into GitHub's protected environment secret below. Do not paste it into chat, commit it, or pass it in command-line arguments.

## 2. Configure application secrets in Vercel

Follow `DEPLOY-neon.md` and `DEPLOY-vercel.md` before the first run. The verified Neon project from this deployment is `summer-sound-57299654` (Postgres 17, AWS us-east-2); its main branch has 500 seeded events and 58 flags.

Use the generated credentials in the private, git-ignored local `.env` from deployment setup. Copy them directly into each project's **Production** environment settings:

- MCP: pooled `DATABASE_URL` as `spot_mcp_user`, `MCP_AUTH_TOKEN`, `EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B`, `EMBED_DIM=1024`.
- Backend: pooled `DATABASE_URL` as `spot_app_user`, `ADMIN_TOKEN`, the same `MCP_AUTH_TOKEN`, `MCP_URL=https://<mcp-production-domain>/mcp`, `SERVERLESS=1`, `REPLAY_MODE=stepped`, `SYSTEM1_INGEST_TIMEOUT_S=10`, `DB_POOL_MAX=5`, `CORS_ORIGINS=https://<web-production-domain>`.
- Web: `BACKEND_URL=https://<backend-production-domain>/api/v1`.

Leave AMD service URLs and related API keys unset until those services exist. The workflow does not create, rotate or transfer application credentials through GitHub. It pulls Vercel's production configuration for builds; the pulled files are removed after deployment and are never uploaded as workflow artifacts. Changes to Vercel environment settings require another workflow run to redeploy.

## 3. Configure the GitHub production environment

In `JylerTackson/SpotTheDOT`, go to **Settings → Environments → production**. Add a required reviewer and restrict deployment branches to `main`. Protect `main` with pull-request review and required CI checks so only reviewed code receives production secrets.

Add this **environment secret**:

| Secret | Value |
|---|---|
| `VERCEL_TOKEN` | Your deployment token, entered directly in GitHub |

Add these **environment variables** (IDs and domains are not credentials):

| Variable | Value |
|---|---|
| `VERCEL_ORG_ID` | ID of the owning Vercel account/team |
| `VERCEL_SCOPE` | Matching CLI account/team slug |
| `VERCEL_CLI_VERSION` | An exact reviewed CLI release, such as `X.Y.Z`; aliases like `latest` are rejected |
| `VERCEL_MCP_PROJECT_ID` | MCP project's `prj_...` ID |
| `VERCEL_BACKEND_PROJECT_ID` | Backend project's `prj_...` ID |
| `VERCEL_WEB_PROJECT_ID` | Web project's `prj_...` ID |
| `MCP_PRODUCTION_URL` | `https://<mcp>.vercel.app`, no path or trailing slash |
| `BACKEND_PRODUCTION_URL` | `https://<backend>.vercel.app`, no path or trailing slash |
| `WEB_PRODUCTION_URL` | `https://<web>.vercel.app`, no path or trailing slash |

For multiple services, `VERCEL_ORG_ID` and `VERCEL_TOKEN` alone are insufficient: each project needs its own ID. Do not use deployment-specific or preview URLs for the three origin variables.

## 4. Merge and run

Merge this workflow into `main`. After the existing **CI** workflow succeeds for a push to `main`, the deployment workflow checks out that exact tested commit and waits for the production environment reviewer. It never deploys pull requests or fork commits. Automatic production deployments run serially and are not canceled midway.

For a configuration-only redeploy, select **Actions → Deploy SPOTlight to Vercel → Run workflow → main**. Manual runs are limited to `main`; they do not automatically wait for a new CI run, so select a commit whose CI has passed.

The job installs Node 24, Python 3.12 and the pinned Vercel CLI. Each project is pulled, built with production settings and deployed with `--prebuilt --prod` from the repository root. The backend build therefore has access to the shared code and kit data. Deployment stops at the first failed build or HTTP check; earlier services may already be live, so this is not an atomic release.

## 5. Verification and remaining manual checks

The workflow checks:

- MCP `/healthz` reports `ok: true`.
- Backend liveness and `/health`: database and MCP must be up; absent AMD services are permitted.
- Backend `/docs` and `/api/v1/flags?limit=500`; the summary reports returned and total flag counts.
- Web `/` and `/api/dashboard` return successfully with dashboard fields.

The summary records the deployed commit and production origins. This does not verify browser rendering, authenticated MCP tools, or replay. Complete **all** remaining checks in `DEPLOY-vercel.md`: authenticated MCP tools/list (17 tools), missing/wrong token rejection, loading the dashboard in a browser, and stepped replay through completion. Drive replay from a trusted local shell with the generated admin token; do not reset demo data automatically on every push.

Embeddings ingestion, graph building and embedding smoke tests remain deferred until an embedding endpoint is available. See `DEPLOY-status-2026-10-10.md` for database evidence and current account blockers.

## References

- [Vercel CLI project deployment](https://vercel.com/docs/projects/deploy-from-cli)
- [Vercel build and prebuilt deployment](https://vercel.com/docs/cli/deploy)
- [Vercel CI/CD with GitHub Actions](https://vercel.com/docs/deployments/configure-a-build#using-github-actions)

This workflow has been prepared locally. It has not been pushed, run or used to deploy production because repository write permission and a configured deployment token are still unavailable.
