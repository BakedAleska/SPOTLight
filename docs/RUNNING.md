# Running the stack

There is ONE `docker-compose.yml`, at the repo root, for the whole project. Modes are profiles inside it; there
are no per-service or per-environment compose files. Every new or changed container must update that file (its
header table too) and this page. Ports and contracts are in `docs/backend-services-contract.md` and
`docs/system2-rag-contract.md`. The integration history is in `docs/integration-log-2026-10-10.md`.

| Service | Port | Profiles | Image / folder | Notes |
|---|---|---|---|---|
| `web` | 3000 | local, hybrid, neon, frontend | `node:24`, `web/` | Next.js development dashboard. `/api/dashboard` reads backend data. Polls every 10 seconds; shows connection errors and retains the last successful update. |
| `db` | 5433 (host: `DB_PORT`) | local, hybrid | `pgvector/pgvector:pg17` | Local stand-in for Neon. Named volume `pgdata`. |
| `db-init` | n/a | local, hybrid, neon | `db/Dockerfile` | One-shot job: migrate, create login users, seed, ingest (embeds the RAG corpus), graph (rebuilds the Graph RAG `kg_nodes`/`kg_edges`, contract §7; embeds only changed node summaries; `DB_GRAPH_ARGS=--no-embed` skips embedding), status. Idempotent and non-destructive: `seed` only inserts missing rows and loads the demo baseline (events + flags) on a database's first seed, so `make up` never overwrites live or acknowledged flags or refills a reset demo (audit M5). |
| `embed` | 8081 | local, models | `amd/embed/` | Qwen3-Embedding-0.6B. `TORCH_VARIANT` build arg. HF cache volume `hf-cache`. `EMBED_BATCH=8` (32 OOM-killed it). |
| `system1` | 8082 | local, models | `amd/system1/service/` | Laya. `TORCH_VARIANT` build arg. Same `hf-cache` volume. Fine-tuned checkpoints are mounted at `/models/checkpoints`. |
| `mcp` | 8090 | local, hybrid, neon | `amd/mcp/` | Logs in as `spot_mcp_user` (in `spot_mcp_reader`: read-only, plus INSERT on `app.*`). |
| `backend` | 8000 | local, hybrid, neon | `backend/` | Logs in as `spot_app_user` (in `spot_app`). API docs at http://localhost:8000/docs. `ADMIN_TOKEN` (header `X-Admin-Token`) guards `/ops/*`, `/replay/*` and `POST /events*`; CORS allows `CORS_ORIGINS` only (default `http://localhost:3000`); `REQUEST_DEADLINE_S` (45) bounds `/flags/{id}/context` and `/explain`. |
| `system2` | 8083 | local, hybrid, neon, system2 | `amd/system2/` | Non-root authenticated `/v1/assess` (contract `2026-10-10.backend.2`): packet-grounded stub (`model: stub`) by default, live checked pipeline when `SYSTEM2_LLAMA_URL`/`SYSTEM2_MODEL` are set (through the gateway, `http://gateway:4000`, or llama-server directly; explanation cache in `SYSTEM2_CACHE_DIR`). Optional multimodal inputs: `context.vision[]` V# rows, `context.ocr[]`, `image_b64` (requests with an image use `SYSTEM2_VISION_MODEL`, default `system2-vision`). Backend/orchestrator supply context; no MCP/database access. The image includes `data/` (approved saved packets); the kit is mounted read-only at `/app/kit` (`SPOT_KIT_DIR`). |
| `gateway` | 4000 (host: `GATEWAY_PORT`) | local, hybrid, neon, system2 | `amd/gateway/` | LiteLLM proxy (`litellm[proxy]==1.104.1`, hash-locked). One OpenAI-compatible endpoint, `Authorization: Bearer $LITELLM_MASTER_KEY`. Aliases `system2` / `system2-vision` -> llama-server (`LLAMA_URL`, `VISION_LLAMA_URL`; default the SSH tunnel on host port 18080), `system1` -> Laya `/v1/predict`, `embed` -> embed; hidden `qwen3.5-9b-q4km` = `system2`. Retries 0, no fallbacks, no DB/UI/telemetry. `GET /health/liveliness` is public. |
| `vision` | 8084 | local, hybrid, neon, vision | `amd/vision/` | CPU-only OCR (RapidOCR/onnxruntime) + code readers: thermal colorbar, dial gauge, spectrogram, Orbit Key Results. `POST /v1/vision` returns `V#` readings, vision-vs-log `comparison`, Orbit-style `key_results`/`alert` and an annotated JPEG; never calls a model. Required `VISION_API_KEY` (header `X-API-Key`; the service refuses to start without one; `/health` open). JPEG/PNG/WebP only, 12 MP max, 2 concurrent analyses (503 when busy; plan ~2 GB RSS). Models baked into the image; no runtime internet. Contract: `docs/vision-contract.md`. |
| `orchestrator` | 8085 | local, hybrid, neon | `amd/orchestrator/` | LangGraph. `POST /v1/assess`: MCP context -> System 1 (packet answers, else gateway `system1`) -> route (escalate / P1-P2 / rules / operator image -> full, else short System 1 answer) -> vision (with an image or `DEMO_IMAGES=1`) -> System 2 -> template fallback, all within `ORCH_DEADLINE_S` (40), with a per-node `trace`. `POST /v1/ask`: the System 2 model with the read-only MCP tools, at most 4 calls in 30 s. `X-API-Key: $ORCHESTRATOR_API_KEY`. `GET /v1/graph` = mermaid. The backend uses it only when `ORCHESTRATOR_URL` is set. Contract: `docs/orchestrator-contract.md`. |

Published ports bind to `127.0.0.1` by default (`BIND_ADDR`). Inside the network the services use service names
(`http://system1:8082`).

**Adding or changing a container:**
1. Add a service block under the right section (data / models / application).
2. Use `<<: *service` for the restart policy and `host.docker.internal`.
3. Use `<<: *embed-env` if it calls the embedder.
4. Every setting is `${VAR:-local default}`.
5. List it in the header table of `docker-compose.yml` and here.
6. Use `required: false` for any dependency that some profile doesn't start.

## Setup
The dashboard starts with the stack at **http://localhost:3000**; no host Node.js installation is needed.
Its server reads `WEB_BACKEND_URL` (default `http://backend:8000/api/v1` in Docker). Backend secrets stay on
the backend; the dashboard's read endpoints require no admin token. This wires the overview and queue;
flag details, explanations and acknowledge/resolve/reopen actions are available. The search box remains
unfinished. `FEATURE_FLAG_DETAILS`, `FEATURE_FLAG_ACTIONS`, and `FEATURE_FLAG_EXPLANATIONS` independently
disable features with `false` (disabling details also disables its dependent features). After changing
the root `.env`, run `docker compose up -d --no-deps web`. `WEB_EXPLAIN_TIMEOUT_MS` (default 50000, 5000–120000) sets how
long the dashboard waits for an explanation; raise it when System 2 runs a slow local model. Re-enable with `true`; data is retained.

```bash
cp .env.example .env                # pick COMPOSE_PROFILES; dev passwords are fine locally
sed -i.bak "s/^ADMIN_TOKEN=.*/ADMIN_TOKEN=$(openssl rand -hex 24)/" .env   # required for /ops, /replay, POST /events
unset DATABASE_URL EMBED_URL SYSTEM1_URL   # your shell environment overrides .env in compose
```
**Service keys.** `SYSTEM2_API_KEY`, `ORCHESTRATOR_API_KEY`, `LITELLM_MASTER_KEY` and `VISION_API_KEY` have dev defaults in
`docker-compose.yml` (anchors under `x-keys`, the same values as `.env.example`), so `make up` works without them
and no service crash-loops on an empty key (audit P-1). Each key is read by both ends of its hop. Replace all four
(`openssl rand -hex 24`; the gateway key must start with `sk-`) before `BIND_ADDR=0.0.0.0` or any shared host.
**Admin endpoints (audit H3).** `/api/v1/ops/*`, `/api/v1/replay/*` and `POST /api/v1/events` / `/events/batch`
need the header `X-Admin-Token: $ADMIN_TOKEN`. Without `ADMIN_TOKEN` in `.env` they answer 503 (closed, not open);
a missing or wrong header gets 401. `make e2e` reads the token from `.env`. Set `CORS_ORIGINS` to the dashboard's
origin (default `http://localhost:3000`); the dashboard sends `X-Admin-Token` only from the presenter's console.
If `db/local_pgvector.sh` already holds port 5433, set `DB_PORT=5434` in `.env`.

## Mode 1: `local` (default): everything in Docker on CPU
```bash
make up        # = docker compose up -d --build --wait
make e2e       # end-to-end proof, both TRIAGE_MODEs
make test      # unit suites + the cross-service policy drift guard
make down      # or `make wipe` to drop the database volume too
```
- The first start downloads ~1.2 GB (embedding model) and ~842 MB (Laya) into the `hf-cache` volume.
- `db-init` embeds ~500 documents on CPU, which takes a few minutes. The graph step then takes ~20 s the first time
  (~70 node summaries) and ~1 s after that.
- This mode tests the wiring. It is not for the demo: Docker Desktop on a Mac has no GPU.
- CI (`.github/workflows/ci.yml`, every push to main and every PR, Python 3.12, no secrets/DB/model weights, nothing
  pushed) runs the same suites as `make test` (System 1 as `test-system1-fast`), lints and builds `web/`, and:
  - `test-compose`: `docker-compose.yml` vs its header table, the ports table in `docs/backend-services-contract.md`
    and the table above; every service has a healthcheck, every `depends_on` target exists (and is
    `required: false` when a profile doesn't start it), every port binds `${BIND_ADDR:-127.0.0.1}`. Update all four
    together or CI fails.
  - `test-contract`: a synthetic image through the real vision `/v1/vision` -> orchestrator `vision_rows` -> System 2
    request parser and `/v1/assess` (V# ids, trust, flags/bound, demo and LOW-row stripping). Runs in the vision
    venv (`make venvs` installs `tests/contract/requirements.txt` there).
  - The vision job runs all 100 tests (slow Orbit sweep included); the RapidOCR models (~30 MB) are cached.
  - `docker compose config -q` for every profile (with and without `--env-file .env.example`); builds backend,
    db-init, mcp, system2; builds gateway, vision and orchestrator and waits for each image's HEALTHCHECK.
- Measured CPU latencies, 2026-10-10:

  | Step | Time |
  |---|---|
  | Laya, one action event | 2.3-3.0 s |
  | Laya, one mission event | 1.8-1.9 s |
  | Live forecast (5 actions) | ~13 s |
  | Query embedding (cold) | ~0.9 s |
  | Full ingest | ~8 min |
  | Graph build (first / unchanged) | ~21 s / ~1 s |

- Timeouts are sized for CPU:
  - `SYSTEM1_INGEST_TIMEOUT_S=10`: per replay or ingest event; it fails fast to a rules-only verdict.
  - `SYSTEM1_TIMEOUT_S=30`: forecast and batch predict.
  - `MCP_TIMEOUT_S=60` and `SEARCH_EMBED_TIMEOUT_S=8`.
  - Each breaker's cooldown is max(`BREAKER_COOLDOWN_S`, that service's timeout).
  - `REQUEST_DEADLINE_S=45`: `/flags/{id}/context` and `/explain` return their fallback (SQL packet / template,
    marked `fallback_reason: "deadline: ..."`) when it runs out, so a hung MCP or System 2 never spins the UI.
- **Laya serves one request at a time.** While a forecast holds it, replay events skip Laya: they get a
  rules-only verdict marked `busy`. A live forecast asks Laya for at most 5 actions.
- **GPU expectation:** on the AMD GPU a 64-event batch is one forward pass (well under a second), so none of
  this throttling shows. Run the demo in `hybrid` mode, with the contract-default timeouts.
- Memory: give Docker Desktop at least 8 GB. `LAYA_MAX_BATCH=16` keeps System 1 under ~3.5 GB on large batches.
- Fine-tuned Laya is the default: on first start System 1 downloads only `run4_B_lora_lr4/best/` (~850 MB) from the
  private HF repo `Jyler/KnightHacks-Laya-Model` into the `hf-cache` volume, then reuses it. Put a read token in `.env`
  as `HF_TOKEN=hf_...`. Without one, System 1 logs a warning and loads base Laya (`LAYA_FALLBACK_CHECKPOINT`).
  Another run: `LAYA_CHECKPOINT_SUBDIR=<run>/best`. A local run: put it under `amd/system1/checkpoints/` (mounted
  read-only at `/models/checkpoints`) and set `LAYA_CHECKPOINT=/models/checkpoints/<run>/best`. After a change, run
  `docker compose up -d system1`. Check with `curl localhost:8082/v1/model`: it should show `fine_tuned: true`.

## Mode 2: `hybrid`: models native on the AMD GPU box
The current SSH-reachable box (Radeon Pro W7900D, gfx1100, 48 GB; `ssh spot-amd`): specs, proxy, the Hugging Face
block and the model-copy workaround are in `docs/AMD-GPU.md`.
The AMD Radeon Cloud Jupyter VMs have no root and can't run Docker. Run `system1` and `embed` there natively (see
`amd/system1/service/README.md` "Native venv" and `amd/embed/README.md` "Run on the AMD VM"). Run `db`, `db-init`,
`mcp` and `backend` here in Docker.
```bash
# 1. On the AMD VM: python -m service (port 8082) and python amd/embed/server.py (port 8081)
# 2. Here, tunnel both ports:
ssh -N -L 18082:localhost:8082 -L 18081:localhost:8081 <amd-vm> &
# 3. In .env:
COMPOSE_PROFILES=hybrid
SYSTEM1_URL=http://host.docker.internal:18082
EMBED_URL=http://host.docker.internal:18081
SYSTEM1_TIMEOUT_S=5  SYSTEM1_INGEST_TIMEOUT_S=5  MCP_TIMEOUT_S=15  SEARCH_EMBED_TIMEOUT_S=4   # GPU: contract defaults
LLAMA_URL=http://host.docker.internal:18080   # llama-server tunnel (ssh -L 18080:127.0.0.1:8080); gateway runs in Docker
# 4.
make up && make e2e
```
- `db-init` ingests through the tunnelled embedder. The corpus and the queries must use the same embedding backend.
  If the AMD box's embedder differs from what was ingested before, run
  `DB_INGEST_ARGS=--force DB_GRAPH_ARGS=--force-embed make up` (the graph re-embeds by itself when `EMBED_MODEL`
  changes; `--force-embed` covers the same model id on a different backend).
- System 1's own `app.model_calls` logging needs a DB URL that the VM can reach. Neon works for that. The local
  `db` is not reachable from the VM, so with it the backend's client-side `system1` rows are the only ones.

## Mode 3: `neon`: Neon instead of the `db` service
```bash
# .env
COMPOSE_PROFILES=neon                 # models from SYSTEM1_URL/EMBED_URL (the AMD box), or:
# COMPOSE_PROFILES=neon,models        # plus embed + system1 on CPU in Docker
DATABASE_URL=postgresql://neondb_owner:<pw>@ep-xxxx-pooler.<region>.aws.neon.tech/neondb?sslmode=require
BACKEND_DATABASE_URL=postgresql://spot_app_user:<pw>@ep-xxxx-pooler.<region>.aws.neon.tech/neondb?sslmode=require
MCP_DATABASE_URL=postgresql://spot_mcp_user:<pw>@ep-xxxx-pooler.<region>.aws.neon.tech/neondb?sslmode=require
SPOT_APP_PASSWORD=<pw>  SPOT_MCP_PASSWORD=<pw>
make up
E2E_DATABASE_URL=$DATABASE_URL backend/.venv/bin/python scripts/e2e.py
```
`db-init` runs as the owner. It migrates, then `scripts/db_login_roles.py` creates `spot_app_user` and
`spot_mcp_user` with those passwords. If Neon refuses `CREATE ROLE`, create the users in the Neon console instead,
then re-run. Make a Neon branch before any `reset`.

### Backend on Vercel (no Docker)
The backend also deploys as its own Vercel project (Root Directory `backend/`); steps, env vars and checks are in
`backend/README.md` "Deploy on Vercel". Run `python db/setup.py migrate` against Neon first (`010_replay_runs.sql`).
On Vercel the replay is `REPLAY_MODE=stepped`: after `POST /api/v1/replay/start`, something must call
`POST /api/v1/replay/step` in a loop (contract "Serverless deployment and stepped replay"). Docker is unchanged.

## Timeouts and keys (how the layers nest)
Each layer gives up before its caller does, so the caller always gets an answer it can store:

| Layer | Budget | Env (default) | What happens when it runs out |
|---|---|---|---|
| backend `/explain` | 45 s total, 3 s kept back to store | `REQUEST_DEADLINE_S` (45) | template (`fallback_reason: "deadline: ..."`) |
| backend -> orchestrator call | min(42 s, time left - 3 s) | `ORCHESTRATOR_TIMEOUT_S` (42) | the backend's own MCP -> System 2 -> template path with the time left |
| orchestrator `/v1/assess` | 40 s total, `min(3 s, 10 %)` reserve | `ORCH_DEADLINE_S` (40) | template; `trace` shows the skipped/timed-out node |
| orchestrator nodes | context 15 s (<= 40 %), System 1 6 s, vision 12 s, System 2 20 s, or 28 s when an image is sent (`ORCH_SYSTEM2_IMAGE_BUDGET_S`; vision is capped so that budget stays free, at least 3 s); each capped by what is left | `ORCH_*_BUDGET_S` | node `skipped`/`timeout`, the graph continues |
| System 2 generation | 15 s over at most two attempts; **25 s for requests with an image** (`SYSTEM2_IMAGE_DEADLINE_S`): the image call keeps 8 s back and on a timeout the same attempt is re-sent text-only | `SYSTEM2_DEADLINE_S` (15) / `SYSTEM2_IMAGE_DEADLINE_S` (25) | checked template, `path: fallback` |
| gateway upstream | `system2` 15 s, `system2-vision` 30 s, `system1` 10 s, `embed` 10 s | `GATEWAY_*_TIMEOUT_S` | error to the caller (no retries, no fallback model) |

System 2 closes its own socket at 15 s (25 s with an image), so the gateway's 30 s vision timeout only matters for
direct callers (eval scripts). Image chain: backend 45 s -> orchestrator call <= 42 s -> `ORCH_DEADLINE_S` 40
(reserve 3 s) -> vision <= min(12 s, time left - 28 s), at least 3 s -> System 2 node <= 28 s -> System 2 25 s ->
gateway `system2-vision` 30 s (never binding); also in `docs/orchestrator-contract.md` "Timeout chain". The legacy path (no orchestrator) keeps `SYSTEM2_TIMEOUT_S` (60) as a per-call cap under the same 45 s
deadline. `/v1/ask` has its own 30 s (`ASK_DEADLINE_S`, at most 4 tool calls).

| Secret | Header | Sent by -> checked by |
|---|---|---|
| `SYSTEM2_API_KEY` | `X-API-Key` | backend, orchestrator -> system2 (>= 16 printable chars; refuses to start without one) |
| `ORCHESTRATOR_API_KEY` | `X-API-Key` | backend -> orchestrator (>= 16 chars; refuses to start without one) |
| `LITELLM_MASTER_KEY` | `Authorization: Bearer` | system2 (`SYSTEM2_LLAMA_API_KEY`), orchestrator (`GATEWAY_KEY`) -> gateway (`sk-`, >= 16 chars) |
| `VISION_API_KEY` | `X-API-Key` | orchestrator -> vision (required, >= 16 chars; refuses to start without one) |
| `MCP_AUTH_TOKEN` | `Authorization: Bearer` | backend, orchestrator -> mcp (optional; required by the Vercel deploy) |

## Useful commands
```bash
docker compose run --rm --no-deps --entrypoint db/job.sh db-init status        # row counts
make smoke                                                                     # 3 RAG queries + graph queries + role checks
docker compose run --rm --no-deps --entrypoint db/job.sh db-init graph         # rebuild the knowledge graph now
make test-graph                                                                # graph build + functions on a throwaway DB
docker compose up -d --force-recreate backend                                  # after editing .env (e.g. TRIAGE_MODE)
curl -s localhost:8000/health | jq                                             # every dependency's status
ADMIN_TOKEN=$(sed -n 's/^ADMIN_TOKEN=//p' .env)
curl -s -XPOST localhost:8000/api/v1/replay/start -H "X-Admin-Token: $ADMIN_TOKEN" \
     -H 'content-type: application/json' -d '{"speed":600,"limit":20}'
docker compose run --rm --no-deps --entrypoint db/job.sh db-init seed --force-reseed   # DESTRUCTIVE: restore the
                                                                               # seeded events/flags (overwrites)
```

## System 2 on 8083 (stub or live)

`SYSTEM2_API_KEY` is a printable ASCII secret of at least 16 characters, without surrounding whitespace. Compose
passes the same value to System 2, the backend and the orchestrator (dev default in `docker-compose.yml`; set your
own in `.env`); it is never baked into the image. Native launch accepts `AMD_API_KEY` as a backward-compatible alias; different values in both variables fail before binding.

```bash
docker compose --profile system2 up --build -d system2
docker compose --profile local config --quiet
curl http://127.0.0.1:8083/health
# Native (export SYSTEM2_API_KEY first):
uv run --no-project --with-requirements amd/system2/requirements.txt python amd/system2/scripts/serve.py
# Remote native service via tunnel:
ssh -L 8083:127.0.0.1:8083 <box>
```

`/health` is public; `POST /v1/assess` requires `X-API-Key` and accepts the request in `amd/system2/fixtures/backend/equipment.json` under `request`. Missing authentication returns 401. Native defaults are `127.0.0.1:8083`; `SYSTEM2_HOST`/`SYSTEM2_PORT` and `--host`/`--port` override them. Docker binds all container interfaces, while published ports bind `${BIND_ADDR:-127.0.0.1}`.

The offline grounding, dataset and evaluation modules read the sponsor kit from `SPOT_KIT_DIR` (default: the checked-in `amd/system1/data/kit` when run from the repo). The image sets `SPOT_KIT_DIR=/app/kit` and compose mounts `./amd/system1/data/kit` there read-only; a plain `docker run` needs `-v "$PWD/amd/system1/data/kit:/app/kit:ro"` before those modules can load. The stub does not read the kit; live mode loads the entity catalog from it at startup.

Local, hybrid and neon include the stub. Backend defaults `SYSTEM2_URL` to `http://system2:8083`; an explicit remote URL overrides it (for a host SSH tunnel use `http://host.docker.internal:18083`). Backend retains its template fallback when the service is unavailable or rejects the key. System 2 receives the prepared MCP packet from backend and does not retrieve packets itself. `model: stub` is deterministic packet evidence, not live LLM inference.

### Live mode (checked pipeline against llama-server)

`SYSTEM2_LLAMA_URL` unset or empty means stub (everything above). Setting it switches `/v1/assess` to the checked
pipeline: at most two llama-server attempts inside a 15 s deadline, then the checked template (`path: fallback`,
`model: template`); insufficient packets return `insufficient_context` without calling the model.

| Env | Meaning | Default |
|---|---|---|
| `SYSTEM2_LLAMA_URL` | llama-server base URL (`http(s)://host:port`); loopback or SSH tunnel only | unset = stub |
| `SYSTEM2_MODEL` | served model name, sent to llama-server and part of the cache key; required when the URL is set | unset |
| `SYSTEM2_CACHE_DIR` | explanation cache directory | `amd/system2/cache` in a checkout; none in the image unless set to a writable mount |
| `SYSTEM2_VISION_MODEL` | model for requests that carry `image_b64` (gateway alias `system2-vision`) | compose: `system2-vision`; native: `SYSTEM2_MODEL` |
| `SYSTEM2_DEADLINE_S` | generation deadline for text requests (the 15 s p95 target); raise only for a slow local model; must be in (0, 120] | `15` |
| `SYSTEM2_IMAGE_DEADLINE_S` | generation deadline for requests with `image_b64` (text keeps 15 s); must be in (0, 120] | `25` |
| `SYSTEM2_LLAMA_API_KEY` | sent as `Authorization: Bearer` (the gateway's `LITELLM_MASTER_KEY`); a llama-server without `--api-key` ignores it | compose: `LITELLM_MASTER_KEY`; native: unset |

An incomplete live config (URL without model, non-http URL, few-shots or kit unavailable) exits 2 before binding.
Live `/health` reports `"mode": "live"` and `"model"`.

Native on the AMD box (no Docker there):

```bash
# T1: llama-server on loopback (same flags as the 2026-10-10 eval run)
~/llama.cpp/build/bin/llama-server -m ~/models/q35/Qwen3.5-9B-Q4_K_M.gguf \
  -ngl all -fa on -lm none -c 16384 -np 1 -rea off --host 127.0.0.1 --port 8080 --metrics
# T2: System 2 live (export SYSTEM2_API_KEY first)
cd amd/system2
SYSTEM2_LLAMA_URL=http://127.0.0.1:8080 SYSTEM2_MODEL=qwen3.5-9b-q4km python scripts/serve.py
#   -> "System 2 live (model qwen3.5-9b-q4km via http://127.0.0.1:8080) on http://127.0.0.1:8083"
# Precompute explanations into the same cache the API reads (re-runs over unchanged requests generate nothing):
SYSTEM2_LLAMA_URL=http://127.0.0.1:8080 SYSTEM2_MODEL=qwen3.5-9b-q4km \
  python scripts/precompute.py --requests data/dev_packets.json fixtures/backend/*.json
```

`precompute.py` accepts dataset files (`{"cases": [{"request": ...}]}`), fixture files (`{"request": ...}`), JSON
lists and `.jsonl`; `--refresh` regenerates, `--limit N` is a smoke knob. Exit 0 = done (fallbacks included),
1 = a request was rejected before any model call, 2 = usage/config error.

Response header `X-System2-Cache` (live mode only): `miss` = generated now, `hit` = served from the cache (the body,
including `model`/`latency_ms`/`tokens_per_sec`, is the stored response from the original generation, so skip hits
when logging live speed), `off` = no cache configured. The key is `(flag_id, packet_hash, model, prompt_ver)`: a
changed packet, model or prompt/few-shot pool regenerates. Only `llm`/`retry` results are stored, and every stored
entry is re-checked against the current packet before it is served.

Backend path: from the laptop run `ssh -N -L 18083:127.0.0.1:8083 <amd-box>`, then set backend
`SYSTEM2_URL=http://host.docker.internal:18083` with the same `SYSTEM2_API_KEY`. To run System 2 in compose against
the box instead, tunnel llama-server (`ssh -N -L 18080:127.0.0.1:8080 <amd-box>`) and set
`SYSTEM2_LLAMA_URL=http://host.docker.internal:18080` and `SYSTEM2_MODEL` in `.env`; a container cache needs
`SYSTEM2_CACHE_DIR` on a writable mount.

**Through the gateway (compose):** set `SYSTEM2_LLAMA_URL=http://gateway:4000` and `SYSTEM2_MODEL=qwen3.5-9b-q4km`
(hidden alias of `system2`, keeps existing cache keys) in `.env`; the gateway reaches llama-server at `LLAMA_URL`.
`precompute.py`, `evaluate.py` and `report.py` send `SYSTEM2_LLAMA_API_KEY` too, so they also run through it.

**Images (multimodal, contract `backend.2`):** images need a vision-capable server: llama-server started with
`--mmproj <Qwen3.5-9B mmproj GGUF>` (the gateway's `system2-vision` upstream). Without it System 2 still answers: it
re-sends the attempt text-only, and the response header `X-System2-Image: text_only` shows that (`used` = the
accepted answer saw the image, `not_used` = stub, insufficient context or template fallback; present only when the
request had an image). Smoke test with the worked example:
```bash
python3 -c "import json; print(json.dumps(json.load(open('amd/system2/fixtures/multimodal/gauge_disagree.json'))['request']))" > /tmp/mm.json
curl -si -X POST http://127.0.0.1:8083/v1/assess -H "X-API-Key: $SYSTEM2_API_KEY" -H "Content-Type: application/json" --data @/tmp/mm.json
python amd/system2/scripts/prompt_example.py        # the exact chat request (amd/system2/PROMPT_FORMAT.md)
```
Expect 200, `V1` in `evidence[].ids` and the `X-System2-Image` header.

Concurrency: `llama-server -np 1` runs one generation at a time, so concurrent explains queue and can hit the 15 s
deadline and fall back to the template. That is why the dashboard reads precomputed explanations and live explain
is for one flag at a time.

## Model gateway on 4000 (LiteLLM)
One OpenAI-compatible endpoint for every model: `model` = `system2` | `system2-vision` | `system1` | `embed`,
`Authorization: Bearer $LITELLM_MASTER_KEY`. System 2 and the orchestrator call models through it; the backend
still calls System 1 and the embedder directly. Details, aliases, the System 1 message format and the supply-chain
pin (litellm 1.104.1, hashed lock) are in `amd/gateway/README.md`.

```bash
ssh -N -L 18080:127.0.0.1:8080 <amd-box>          # llama-server reached through the tunnel on 18080
docker compose up -d gateway
curl -s localhost:4000/health/liveliness
curl -s localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq '.data[].id'
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'content-type: application/json' -d '{"model":"system2","messages":[{"role":"user","content":"hi"}]}'
amd/gateway/scripts/smoke.sh                       # one call per alias against the running gateway

# native on the AMD box (no Docker): Python 3.12 venv, hashed install
cd amd/gateway && python3.12 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt
LITELLM_MASTER_KEY=sk-... LLAMA_URL=http://127.0.0.1:8080 SYSTEM1_URL=http://127.0.0.1:8082 \
  EMBED_URL=http://127.0.0.1:8081 .venv/bin/python serve.py
# then System 2 live through it:
SYSTEM2_LLAMA_URL=http://127.0.0.1:4000 SYSTEM2_LLAMA_API_KEY=$LITELLM_MASTER_KEY SYSTEM2_MODEL=qwen3.5-9b-q4km \
  SYSTEM2_VISION_MODEL=system2-vision python scripts/serve.py
```
`LLAMA_URL` is the gateway's upstream (where llama-server listens); `SYSTEM2_LLAMA_URL` is where System 2 sends its
chat calls (the gateway, or llama-server directly). For the vision alias, start llama-server with
`--mmproj mmproj-F16.gguf` (or run a second one and set `VISION_LLAMA_URL`). The gateway never retries and never
falls back to another model: a System 2 failure reaches System 2, which then serves its checked template.

## Vision / OCR on 8084
```bash
docker compose --profile vision up -d --build vision
curl -s localhost:8084/health            # {"status":"ok","ocr_ready":true,...}  (only /health is open)
curl -s -H "X-API-Key: $VISION_API_KEY" -H 'content-type: application/json' localhost:8084/v1/vision \
  -d "{\"kind\":\"thermal\",\"image_b64\":\"$(base64 < web/public/kit/inspection/thermal_pump_bearing_01.jpg | tr -d '\n')\"}" | jq .readings
# Native (AMD VM, no Docker):
cd amd/vision && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
export VISION_API_KEY=...                # required (>= 16 chars), same value the orchestrator sends
.venv/bin/python scripts/serve.py        # 127.0.0.1:8084; tunnel with ssh -L 8084:127.0.0.1:8084 <box>
# Every kit image through every reader, offline, with annotated JPEGs:
.venv/bin/python scripts/run_kit.py --out /tmp/vision-kit
```
Readings are computed by code; the orchestrator turns them into System 2's `context.vision[]` `V#` rows
(`docs/vision-contract.md` "Feeding System 2") and sends the annotated JPEG as `image_b64`.

## Orchestrator on 8085 (LangGraph)
```bash
docker compose up -d --build orchestrator
curl -s 'localhost:8085/health?deep=true' | jq
curl -s -H "X-API-Key: $ORCHESTRATOR_API_KEY" -H 'content-type: application/json' \
  localhost:8085/v1/assess -d '{"flag_id": 1}' | jq '{route, route_reasons, source, answer, trace}'
curl -s -H "X-API-Key: $ORCHESTRATOR_API_KEY" -H 'content-type: application/json' \
  localhost:8085/v1/ask -d '{"question": "How should Spot handle docking at low battery?"}' | jq
curl -s 'localhost:8085/v1/graph?format=text&graph=assess'      # paste into mermaid.live for the demo
```
Route `/explain` through it: `ORCHESTRATOR_URL=http://orchestrator:8085` in `.env`, then
`docker compose up -d backend`. The backend then also serves `POST /api/v1/ask` and accepts an optional
`{"image_b64", "image_kind"}` body on `POST /api/v1/flags/{id}/explain`; the response carries `orchestrator`
(`route`, `route_reasons`, `trace`, `vision` rows). If the orchestrator is unset, down or has no context, `/explain`
uses its existing MCP -> System 2 -> template path (`docs/backend-services-contract.md` "Orchestrator path").
`DEMO_IMAGES=1` pairs the kit sample images by action type (pairing `demo`) so the vision step shows without an
upload. Native on the AMD box: `cd amd/orchestrator && python3 -m venv .venv && .venv/bin/pip install -r
requirements.txt && .venv/bin/python -m orchestrator` (env as in `amd/orchestrator/README.md`). `/v1/ask` tool
calling needs llama-server started with `--jinja`. Without it, ask degrades to one retrieval step plus an answer from
the retrieved context (`mode: retrieval_fallback`).
