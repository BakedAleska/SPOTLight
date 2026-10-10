# Backend API (FastAPI, port 8000)

The SPOT operator dashboard's API. Every SPOT event goes through System 1 (Laya) and the deterministic validator.
Escalated events become flags, System 2 explains them, and operators acknowledge them. Every external dependency
has a fallback, so the demo path never returns a 500.

Binding contracts: `docs/backend-services-contract.md` (routes, pipeline, env) and `docs/system2-rag-contract.md`
(tables, search functions, embedding API). Interactive docs: `http://localhost:8000/docs`.

## Routes
| Route | What it does |
|---|---|
| `GET /health` | Liveness, plus the status of the DB, System 1, embed, MCP and System 2 (`?deep=false` = instant) |
| `POST /api/v1/events` | Ingest one SPOT event through the 7-step pipeline; returns every step's result |
| `POST /api/v1/events/batch` | Ingest up to 500 events in order, each with its own request_id |
| `GET /api/v1/events` | Live feed, oldest first; poll with `after=<next_cursor>`, page with `before=` |
| `POST /api/v1/replay/start` | Stream the kit's `replay.ndjson` through the pipeline (`speed`, `max_gap_s`, `missions`, `start_at`, `limit`, `triage_mode`, `reset`): a background task, or with `REPLAY_MODE=stepped` a run stored in `replay_runs` |
| `POST /api/v1/replay/step` | Stepped mode only (Vercel): ingest the run's due events within `budget_s` (≤ 60, default 10) / `max_events`; returns status + `step` + `next_due_in_s`. Call in a loop until `state != "running"`; 409 in background mode |
| `POST /api/v1/replay/stop` | Stop the replay |
| `GET /api/v1/replay/status` | Progress: sent/total, escalations, flags created, Laya fallbacks, last event (`mode`; stepped adds `cursor`, `next_due_in_s`) |
| `GET /api/v1/flags` | Operator queue (open first, by score); filter by status/severity/mission/asset/location |
| `GET /api/v1/flags/{id}` | Flag + event + acks + assessment + Laya's stored answers |
| `GET /api/v1/flags/{id}/context` | System 2's context packet (MCP `get_flag_context`, else a SQL-built packet) |
| `POST/GET /api/v1/flags/{id}/explain` | Orbit-style question/answer/reasoning/recommended action (System 2, else a template); cached in `assessments` (System 2 answers are final; a template is stored as `state=TEMPLATE` and System 2 is retried when configured), `?refresh=true` regenerates. Bounded by `REQUEST_DEADLINE_S` |
| `POST /api/v1/flags/{id}/ack` | ACKNOWLEDGE / RESOLVE / ESCALATE / NOTE; a note is embedded as RAG doc `note:<ack_id>` |
| `GET /api/v1/missions` | Missions with reported status vs True Mission Health (`status_misleading`, `?misleading_only=true`) |
| `GET /api/v1/missions/{id}` | Mission, every action (reading, anomaly, flags), and its flags |
| `GET /api/v1/missions/{id}/timeline` | Actions in robot order, merged with ingested events and their verdicts |
| `GET /api/v1/missions/{id}/debrief` | After-action summary + recommendations (System 2 `/v1/debrief`, else a template) |
| `POST /api/v1/system1/predict` | Laya proxy (canonical `event` or a kit `action_id`); rules fallback if Laya is down |
| `GET /api/v1/system1/forecast` | Pre-action failure forecast for a mission or action: stored → live Laya → empirical baseline, with actual outcomes |
| `GET /api/v1/assets`, `/assets/{id}` | Assets with success rate, anomalies, readings, flags |
| `GET /api/v1/locations`, `/locations/{id}` | Waypoints with success rate (95% CI), failure types, anomalies (map heat layer) |
| `GET /api/v1/insights/overview` | Headline insights computed live, each with a one-line pitch (green-but-broken, battery cliff, V1 vs V2 at matched battery, worst waypoints, ...) |
| `GET /api/v1/insights/reliability` | Success rate + Wilson CI by `group_by` with filters (e.g. `group_by=configuration_version&min_battery=40`) |
| `GET /api/v1/search` | RAG: `EMBED_QUERY_PREFIX` + q → `match_documents_hybrid`; full-text fallback |
| `GET /api/v1/ops/model-calls` | p50/p95/mean latency and tokens/s per system/model from `app.model_calls`, plus System 1 routing |
| `GET /api/v1/ops/jobs` | `app.job_runs` + replay status |
| `POST /api/v1/ops/reset-demo` | `{"confirm": true}`: clears events, flags, assessments, acks and demo docs (kit tables and raw/app logs stay) |

## Run
Local (Python 3.12):
```bash
cd backend && python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
../db/local_pgvector.sh                                       # local pgvector on :5433 (or use Neon)
export DATABASE_URL=postgresql://postgres:postgres@localhost:5433/spot
.venv/bin/python ../db/setup.py migrate && .venv/bin/python ../db/setup.py seed
.venv/bin/uvicorn app.main:app --reload --port 8000         # http://localhost:8000/docs
```
Docker (the build context is the repo root):
```bash
docker build -f backend/Dockerfile -t spot-backend .
docker run --rm -p 8000:8000 \
  -e DATABASE_URL=postgresql://postgres:postgres@host.docker.internal:5433/spot \
  -e SYSTEM1_URL=http://host.docker.internal:8082 spot-backend
```
`backend/Dockerfile.dockerignore` is a symlink to `.dockerignore`: with a repo-root context, BuildKit only reads the
ignore file next to the Dockerfile under that name.

Tests: `.venv/bin/python -m pytest` (unit tests mock every service; `tests/test_integration.py` starts
`db/local_pgvector.sh`, runs `db/setup.py migrate` + `seed`, ingests replay events end to end against real SQL, then
re-seeds. It skips without Docker. Use `SKIP_INTEGRATION=1` to skip it, or `INTEGRATION_DATABASE_URL` to point it at
another DB).

Try it: `curl -X POST localhost:8000/api/v1/replay/start -H "X-Admin-Token: $ADMIN_TOKEN" -H 'content-type: application/json' -d '{"speed":120,"reset":true}'`.
`/ops/*`, `/replay/*` and `POST /events*` need `X-Admin-Token: $ADMIN_TOKEN` (401 without it; 503 if `ADMIN_TOKEN` is unset).

## Environment
| Var | Meaning | Default |
|---|---|---|
| `DATABASE_URL` | Neon or local Postgres (public tables). In production use a login role in `spot_app` (`db/README.md`); `tests/test_integration.py` runs the whole write path as one | unset → reads 503, ingest returns verdicts unpersisted |
| `LOGS_DATABASE_URL` | where `raw.*` and `app.*` live | `DATABASE_URL` |
| `TRIAGE_MODE` | `serial` (Laya flags, rules confirm) or `parallel` (either escalates; rules floor) | `serial` |
| `SYSTEM1_URL`, `SYSTEM1_TIMEOUT_S` | Laya service | `http://localhost:8082` (image: `http://system1:8082`), `5` |
| `SYSTEM1_INGEST_TIMEOUT_S` | per-event System 1 timeout on ingest/replay (fail fast to rules-only) | `SYSTEM1_TIMEOUT_S` |
| `EMBED_URL`, `EMBED_MODEL`, `EMBED_DIM` | embedding service (OpenAI-compatible) | `:8081`, `Qwen/Qwen3-Embedding-0.6B`, `1024` |
| `EMBED_TIMEOUT_S`, `SEARCH_EMBED_TIMEOUT_S` | embed timeout for notes / for `/search` before full-text fallback (CPU embedder ~2.3 s) | `10`, `4` |
| `EMBED_QUERY_PREFIX`, `EMBED_DOC_PREFIX` | prepended to search queries / note documents (`\n` allowed) | Qwen3 instruction / empty |
| `MCP_URL`, `MCP_AUTH_TOKEN`, `MCP_TIMEOUT_S` | MCP server (streamable HTTP) | `http://localhost:8090/mcp`, unset, `15` |
| `SYSTEM2_URL`, `SYSTEM2_API_KEY`, `SYSTEM2_TIMEOUT_S` | System 2 (`/v1/assess`, `/v1/debrief`) | unset → templates, unset, `60` |
| `KIT_DIR`, `TRIAGE_DIR` | kit CSVs + replay.ndjson; folder holding `triage/` (relative = repo root; when the repo copy is absent, as on Vercel, `backend/_shared/<same path>`) | `amd/system1/data/kit`, `amd/system1` |
| `REPLAY_SPEED`, `REPLAY_MAX_GAP_S` | robot seconds per wall second; cap per wait | `60`, `2` |
| `BREAKER_COOLDOWN_S` | after a failure, skip that service for this long (instant fallback) | `10` |
| `ADMIN_TOKEN` | `X-Admin-Token` for `/ops/*`, `/replay/*`, `POST /events*` (audit H3); unset → those endpoints answer 503 | unset |
| `REQUEST_DEADLINE_S` | overall deadline for `/flags/{id}/context` and `/explain` (audit M4) | `45` |
| `CORS_ORIGINS` | allowed browser origins, comma-separated (audit H3) | `http://localhost:3000` |
| `DB_POOL_MAX`, `DB_CONNECT_TIMEOUT_S`, `LOG_REQUESTS` | | `10`, `3`, `1` |
| `DB_POOL_MIN` | pool connections kept open | `1` (`0` when serverless) |
| `SERVERLESS` | no per-process assumptions: pool `min_size` 0 + connection check on checkout, log writes finished before each response | auto: on when `VERCEL` is set |
| `REPLAY_MODE`, `REPLAY_STEP_BUDGET_S` | `background` (asyncio task) or `stepped` (`replay_runs` + `/replay/step`); default step budget | `background` (`stepped` when serverless), `10` |

## Deploy on Vercel
The backend is its own Vercel project (Hobby, Python on Fluid compute), next to `web/` and `amd/mcp/`. Docker Compose
is unaffected. Contract: `docs/backend-services-contract.md` "Serverless deployment and stepped replay".

**Files.** `index.py` (entrypoint: Vercel picks `index.py` before `app/main.py`; it re-exports `app.main:app`),
`vercel.json` (build command, region `cle1`, `maxDuration` 300, `excludeFiles` for tests), `.python-version` (3.12, as in
the Dockerfile), `requirements.txt` (dependencies; no torch), `vercel_build.py` (build step, below), `.vercelignore`
(keeps any `.env`, keys, venvs and tests out of CLI uploads; a denylist, so the build step still sees `../amd`).

**Shared code and kit data.** The Python runtime bundles only files under the Root Directory (it globs `backend/`;
`includeFiles` is not read for Python), so `vercel_build.py` copies the six torch-free modules of
`amd/system1/triage/` and `amd/system1/data/kit/` into `backend/_shared/amd/system1/` at build time (git-ignored).
Config falls back to that copy only when the repo copy is missing, so local, Docker and CI keep using
`amd/system1/` and `tests/test_policy_consistency.py` still guards the code that ships.

**Project settings (dashboard).**
1. New project from this repo; **Root Directory** = `backend`. Framework preset: FastAPI (auto-detected).
2. Root Directory section: keep **"Include source files outside of the Root Directory in the Build Step"** on (default for
   new projects; the build fails with a message naming it when off).
3. Build Command: leave empty (`vercel.json` sets `python3 vercel_build.py`).
4. Region: `vercel.json` pins `cle1` (Cleveland), next to Neon `aws-us-east-2` (`.env.example`) and the MCP project
   (`amd/mcp/vercel.json`). If Neon is elsewhere, change it (`aws-us-east-1` → `iad1`, `aws-us-west-2` → `pdx1`,
   `aws-eu-central-1` → `fra1`); Hobby allows one region. Every request makes several DB round trips, so this matters.
5. Neon first: `python db/setup.py migrate` (adds `010_replay_runs.sql`), seed, and create `spot_app_user`
   (`docs/RUNNING.md` Mode 3).

**Environment variables (Production).**
| Var | Value |
|---|---|
| `DATABASE_URL` | Neon **pooled** URL as `spot_app_user` (`...@ep-xxxx-pooler.<region>.aws.neon.tech/neondb?sslmode=require`) |
| `ADMIN_TOKEN` | `openssl rand -hex 24` (replay/ops/ingest; the presenter enters it in the web app) |
| `CORS_ORIGINS` | the web project's URL, e.g. `https://<web>.vercel.app` (comma-separate preview URLs if needed) |
| `SYSTEM1_URL`, `EMBED_URL`, `SYSTEM2_URL`, `ORCHESTRATOR_URL` | the AMD cluster's public HTTPS URLs (Laya, embedder, System 2, orchestrator) |
| `SYSTEM2_API_KEY`, `ORCHESTRATOR_API_KEY` | as on the AMD services |
| `MCP_URL`, `MCP_AUTH_TOKEN` | `https://<mcp>.vercel.app/mcp` and its bearer token |
| `SYSTEM1_INGEST_TIMEOUT_S` | `10` over the internet (as in hybrid mode) |
| `DB_POOL_MAX` | `5` (one pool per instance; Neon's pooler fans in) |
| `SERVERLESS` | `1` (also auto-detected from Vercel's `VERCEL` variable, which exists only while "Enable access to System Environment Variables" is checked; setting it explicitly removes that dependency) |
| optional | `LOGS_DATABASE_URL`, `TRIAGE_MODE`, `REPLAY_SPEED`, `REPLAY_STEP_BUDGET_S`, timeouts as in "Environment" |

Do not set `TRIAGE_DIR`/`KIT_DIR` (the defaults find the bundled copy). `REPLAY_MODE` defaults to `stepped` when
serverless.

**Replay on Vercel.** An instance can freeze once a response is sent, so the replay is stepped: start, then call
`step` until it is done (any instance can serve any call; state is in `replay_runs`):
```bash
B=https://<backend>.vercel.app; H=(-H "X-Admin-Token: $ADMIN_TOKEN" -H 'content-type: application/json')
curl -s "${H[@]}" -XPOST $B/api/v1/replay/start -d '{"speed":120,"reset":true}'
while :; do s=$(curl -s "${H[@]}" -XPOST $B/api/v1/replay/step -d '{}'); echo "$s" | jq -c '{state,sent,total,next_due_in_s,step}'
  [ "$(echo "$s" | jq -r .state)" = running ] || break; sleep 1; done
```

**Verify.** `curl $B/health` (`ok`, `database: up`, each model URL `up`), `curl $B/health?deep=false` (instant),
`$B/docs`, `curl $B/api/v1/flags | jq '.items | length'`, then the replay loop above and `GET
$B/api/v1/replay/status`. Function logs: Vercel dashboard → Logs. Lifespan startup runs per instance; shutdown gets
~500 ms, so pending log writes are finished before each response instead.

**Per-instance state** (each instance has its own; fine for the demo): circuit breakers, the Laya `busy` guard
(concurrent instances can each call Laya), `routing.since_start` counters in `/ops/model-calls`, `/health`'s
`replay` (last status that instance saw) and the ACTION_STARTED cache for actions not in the kit.

## How the pieces connect
```
 web UI ──HTTP──▶ backend :8000 ──▶ Neon / local pgvector  (public: kit + events, flags, predictions, documents;
   │                │                                       raw.*: verbatim events; app.*: model/request/job logs)
   │                ├──▶ system1 :8082  POST /v1/predict    canonical events → Laya answers   (ingest, forecast)
   │                ├──▶ embed   :8081  POST /v1/embeddings  query/note vectors               (search, ack)
   │                ├──▶ mcp     :8090  get_flag_context     RAG + SQL context packet → Neon  (context, explain)
   │                └──▶ system2 :8083  POST /v1/assess      Orbit-style explanation          (explain, debrief)
```
Ingest (`POST /events`, and every replay event): (1) raw → `raw.spot_event_log`, (2) normalize → `events`
(payload enriched with the reading/anomaly), (3) canonical event (`amd/system1/triage/events.py`), (4) System 1,
(5) `system1_predictions`, (6) validator (`policy.py`) per `TRIAGE_MODE`, with both verdicts in `flags.reasons`,
(7) upsert `flags` when escalated. One `request_id` ties the event's rows in `raw.spot_event_log`,
`app.model_calls`, `app.request_log` and `events.payload.triage` together.

## Resilience (the demo never 500s)
| Down | Behavior |
|---|---|
| System 1 | rules-only verdict, `system1.status = laya_unavailable`, a `laya_unavailable` reason on the flag; `/system1/predict` returns the validator's answers; `/forecast` serves stored answers, then the empirical baseline |
| MCP | `/context` returns a SQL-built packet (`source: sql_fallback`): flag, event, reading trust, Laya's answers, waypoint and battery reliability, asset history |
| System 2 (unset or down) | `/explain` returns a templated Orbit-style explanation from the flag's reasons (`source: template`); `/debrief` is templated |
| Embedding service | `/search` switches to Postgres full-text on `content_tsv` (`mode: fulltext`); acks are stored without the RAG note |
| Database | ingest still returns the verdict (`persisted: false`); reads return 503 `database_unavailable`; a 5 s cooldown makes repeated calls fail fast |

Failed services trip a circuit breaker whose cooldown is max(`BREAKER_COOLDOWN_S`, that service's timeout), so a hung
dependency costs at most one timeout per cooldown window, not one per event. Ingest and replay call System 1 with the
short `SYSTEM1_INGEST_TIMEOUT_S` and skip Laya (rules-only, `busy`) while a forecast or `/system1/predict` batch holds it.
A live forecast asks Laya for at most 5 actions per request.
