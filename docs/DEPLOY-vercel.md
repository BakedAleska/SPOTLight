# Deploy: Vercel (web, backend, MCP server)

How to put the app on Vercel. Written for the agent doing the deploy. Set up Neon first (`docs/DEPLOY-neon.md`).

Per-project detail is in `web/README.md`, `backend/README.md`, and `amd/mcp/README.md` (each has a "Deploy on Vercel" section). This file is the ordered runbook.

## The layout
```
browser ─▶ web (Next.js)  ── server-side BACKEND_URL ──▶ backend (FastAPI) ──▶ Neon (spot_app_user)
                                                            │
                                                            ├──▶ MCP server (Vercel) ──▶ Neon (spot_mcp_user)
                                                            │         └──▶ EMBED_URL (AMD cluster)
                                                            └──▶ AMD cluster: SYSTEM1_URL (Laya), SYSTEM2_URL,
                                                                 ORCHESTRATOR_URL, EMBED_URL
```
**Three Vercel projects, one repo** (`JylerTackson/SpotTheDOT`), Hobby plan. They're separate projects so each gets its own env vars: the MCP server never sees the backend's write-capable database user.

| Project | Root Directory | Preset | Region | Max duration |
|---|---|---|---|---|
| MCP server | `amd/mcp` | FastAPI (ASGI-compatible Python builder) | `cle1` | 60 s |
| backend | `backend` | FastAPI | `cle1` | 300 s |
| web | `web` | Next.js (Node 24.x) | default | default |

The regions are pinned in each `vercel.json` to sit next to Neon `aws-us-east-2`. If Neon is elsewhere, change both.

**The AMD cluster isn't required to deploy.** If its URLs aren't ready yet, the backend degrades instead of failing:
- Laya down → rules-only triage.
- System 2 down → templated explanations.
- Embeddings down → full-text search.

Set the AMD URLs when they exist, then redeploy.

## Rules
- **Deploy in order: MCP server → backend → web.** Each project needs the previous one's URL.
- **Cross-project URLs must be production domains** (`https://<project>.vercel.app`). Preview URLs sit behind Deployment Protection, so server-to-server calls to them get a login page.
- **Env var changes apply only to new deployments.** Redeploy after setting them.
- **Never commit secrets.** `.vercelignore` in `backend/` and `amd/mcp/` keeps a local `.env` out of CLI uploads. Don't remove it.
- **Generate tokens with `openssl rand -hex 24`.** The same value must be set on both ends of each connection: `MCP_AUTH_TOKEN` on MCP and backend, `SYSTEM2_API_KEY` on backend and System 2, `ORCHESTRATOR_API_KEY` on backend and orchestrator.

## 1. MCP server
1. **New project:** Add New → Project → import `JylerTackson/SpotTheDOT`. Set Root Directory to **`amd/mcp`**, select the **FastAPI** preset, and leave the build command empty. The Other preset failed to detect the existing ASGI entrypoint during deployment.
2. **Env vars (Production):**

   | Var | Value |
   |---|---|
   | `DATABASE_URL` | Neon **pooled** URL as `spot_mcp_user` (from `DEPLOY-neon.md` step 5) |
   | `MCP_AUTH_TOKEN` | **required**; the app refuses to start without it |
   | `EMBED_URL` | the AMD cluster's public embeddings URL (leave unset until it exists). Unset, it is `localhost:8081`, which fails on Vercel: every packet then has the gap `retrieval skipped` |
   | `EMBED_MODEL`, `EMBED_DIM` | `Qwen/Qwen3-Embedding-0.6B`, `1024` (must match what was ingested) |

   Leave `EMBED_QUERY_PREFIX` unset on both projects: the default is the Qwen3 query instruction that matches how ingest embedded the documents (with no document prefix).

3. **Deploy and verify:**
   ```bash
   M=https://<mcp>.vercel.app
   curl -s $M/healthz                                             # {"ok":true,...}, no token needed
   curl -s $M/mcp -H "Authorization: Bearer $MCP_AUTH_TOKEN" -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
     | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["result"]["tools"]), "tools")'   # 17 tools
   ```
   A wrong or missing token returns `401`. A `500` on `/mcp` means the app's startup step (ASGI lifespan) didn't run; check the function logs.

## 2. Backend
1. **New project** from the same repo. Set Root Directory to **`backend`**; the preset is FastAPI. In the Root Directory section, **keep "Include source files outside of the Root Directory in the Build Step" ON** (the default). The build (`vercel_build.py`) copies the shared rules from `amd/system1/triage/` and the kit data into `backend/_shared/`. With the setting off, the build fails with a message naming it.
2. **Env vars (Production):**

   | Var | Value |
   |---|---|
   | `DATABASE_URL` | Neon **pooled** URL as `spot_app_user` |
   | `SERVERLESS` | `1` |
   | `REPLAY_MODE` | `stepped` (the default when serverless; set it explicitly anyway) |
   | `ADMIN_TOKEN` | guards `/replay/*`, `/ops/*`, `POST /events` (header `X-Admin-Token`) |
   | `MCP_URL`, `MCP_AUTH_TOKEN` | `https://<mcp>.vercel.app/mcp` and the MCP token |
   | `SYSTEM1_URL`, `EMBED_URL`, `SYSTEM2_URL`, `ORCHESTRATOR_URL` | the AMD cluster's public URLs (when ready). `EMBED_URL` must be the same URL the MCP project has |
   | `EMBED_MODEL`, `EMBED_DIM` | `Qwen/Qwen3-Embedding-0.6B`, `1024`, the same as MCP and ingest (leave `EMBED_QUERY_PREFIX` unset) |
   | `GEMINI_API_KEY` | the AI Assistance key, with **no trailing newline**: `printf %s "$KEY" \| vercel env add GEMINI_API_KEY production` (an `echo` adds one; it broke the header and leaked the key into an error log) |
   | `SYSTEM2_API_KEY`, `ORCHESTRATOR_API_KEY` | the same values the AMD services use |
   | `SYSTEM1_INGEST_TIMEOUT_S` | `10` |
   | `DB_POOL_MAX` | `5` |
   | `CORS_ORIGINS` | `https://<web>.vercel.app` |

   Don't set `TRIAGE_DIR` or `KIT_DIR`; the defaults find the bundled copy. Leave `LOG_REQUESTS` unset: the default,
   `writes`, logs writes, errors and slow requests to `app.request_log`, and skips the dashboard's polling reads.
3. **Deploy and verify:**
   ```bash
   B=https://<backend>.vercel.app
   curl -s "$B/health?deep=false"            # instant liveness
   curl -s $B/health | jq                    # database: up; model URLs up or down (down is OK before AMD is live)
   curl -s "$B/api/v1/flags?limit=500" | jq .page.total   # 58 seeded flags (51 after a full replay with reset: DEPLOY-neon.md §6)
   curl -s $B/api/v1/flags/1/context | jq '{source, top_similarity, gaps, notes}'
   #   source "mcp", top_similarity > 0.5, no "retrieval skipped" in gaps (empty searches are notes, not gaps)
   ```
   Then confirm in Neon that the MCP's embedding call logged `ok = true` (`select ok, device, endpoint from
   app.model_calls where system = 'embed' order by call_id desc limit 3`).
   **Embedding URL after a tunnel relaunch.** A `cloudflared tunnel --url` quick tunnel gets a new random URL every
   time it starts. After each relaunch, run `deploy/amd-native/laptop/show_vercel_env.sh <urls>`, set `EMBED_URL` on
   **both** the backend and the MCP project, and redeploy both. Then repeat the two checks above. A named tunnel
   keeps one URL; prefer it for judging.
4. **Replay works differently on Vercel.** A function can freeze as soon as it responds, so the replay is **stepped**: its state lives in Neon (`replay_runs`), and it only advances while something keeps calling `POST /api/v1/replay/step`. The web app has no replay control, so drive it with this loop (also in `backend/README.md`):
   ```bash
   H=(-H "X-Admin-Token: $ADMIN_TOKEN" -H 'content-type: application/json')
   curl -s "${H[@]}" -XPOST $B/api/v1/replay/start -d '{"speed":120,"reset":true}'
   while :; do s=$(curl -s "${H[@]}" -XPOST $B/api/v1/replay/step -d '{}'); echo "$s" | jq -c '{state,sent,total,next_due_in_s,step}'
     [ "$(echo "$s" | jq -r .state)" = running ] || break; sleep 1; done
   ```
   - **Two steps at once:** a second step that arrives while one is running gets `busy`. That's expected.
   - **Abandoned runs:** a run nobody steps stays `running` until `POST /api/v1/replay/stop`.
   - **Stuck runs:** if a step is killed mid-run, the run stays locked for up to about 90 s.

## 3. Web
1. **New project** from the same repo. Set Root Directory to **`web`**; the preset is Next.js. Node 24.x is pinned by `engines` in `package.json`.
2. **Env var:** `BACKEND_URL=https://<backend>.vercel.app/api/v1`. It's server-only: the web functions proxy to the backend, and the browser never calls the backend directly.
3. **Deploy and verify:** open `https://<web>.vercel.app`; the dashboard loads with real numbers. `curl -s https://<web>.vercel.app/api/dashboard` returns 200. "Cannot reach the dashboard data" means `BACKEND_URL` is wrong or missing, or the deploy happened before it was set.
4. **Finish up:** set the backend's `CORS_ORIGINS` to this domain and redeploy the backend.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| backend build fails mentioning "Include source files outside of the Root Directory" | that project setting is off; turn it on and redeploy |
| backend `/health` shows `database: down` | wrong user or password, the non-pooled host, or migrations not run (`DEPLOY-neon.md`) |
| `/replay/step` or `/ops/reset-demo` return 503 | migration `010_replay_runs.sql` isn't applied on Neon: `python db/setup.py migrate` |
| backend explanations are always `source: template` | `SYSTEM2_URL` or `ORCHESTRATOR_URL` unset or unreachable, or the API keys don't match |
| MCP context missing, backend shows `source: sql_fallback` | `MCP_URL` wrong (it must end in `/mcp`), a token mismatch, or the MCP project is down |
| server-to-server call gets an HTML login page | a preview URL was used; use the production domain |
| MCP vector search finds nothing | ingest hasn't run, or `EMBED_URL`, `EMBED_MODEL` or `EMBED_DIM` differ from what was ingested |
| packets carry `retrieval skipped: ...` in `gaps` and explanations say "insufficient context" | the MCP project's `EMBED_URL` is unset or points at a dead tunnel; update it on both projects and redeploy |
| chat fails with `gemini_auth`, or `app.model_calls` shows `Illegal header value [redacted]` | `GEMINI_API_KEY` was saved with a trailing newline or is wrong; re-add it with `printf %s` and redeploy (rotate it if it ever reached a log) |
| a few rows missing in `app.*` logs | rare: on Vercel the MCP server waits up to 2 s for its log inserts before finishing each response, and the backend finishes its writes before responding; a row is lost only if the insert takes longer; tool calls are unaffected |

## What stays local
Docker Compose (`docker-compose.yml`, `docs/RUNNING.md`) is unchanged and is still how you run everything on one machine. The Vercel files only affect Vercel: `vercel.json`, `.vercelignore`, `.python-version`, `backend/index.py`, `backend/vercel_build.py`, `amd/mcp/asgi.py`.

## Existing production and GitHub Actions

The three projects already exist in team SpotTheDog (`spot-the-dog`):

| Service | Production origin |
|---|---|
| MCP | https://spotlight-mcp.vercel.app |
| Backend | https://spotlight-backend-tau.vercel.app |
| Web | https://spotlight-web-mu.vercel.app |

Use [DEPLOY-github-actions.md](DEPLOY-github-actions.md) for existing project IDs,
secrets, environment names, and the working Neon → MCP → backend → web pipeline.
This path does not require Vercel's GitHub app to import the repository. Successful
CI on main triggers deployment; pushes to feature branches run CI without
production deployment. Python prebuilt builds require uv on the GitHub runner.
Vercel environment changes require a new deployment. Browser, authenticated MCP,
and replay checks remain manual; automatic deployments do not reset demo data.
