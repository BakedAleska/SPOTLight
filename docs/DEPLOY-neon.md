# Deploy: Neon (Postgres + pgvector)

How to set up the production database. Written for the agent doing the deploy. Do this **before** `docs/DEPLOY-vercel.md`, because the backend and the MCP server fail their health checks without it.

The schema, seed, and role details live in `db/README.md`. This file is the ordered runbook.

## What you end up with
- One Neon project, **AWS us-east-2**, Postgres 17, database `neondb`, owner `neondb_owner`.
- Schema from `db/migrations/001–010`:
  - `public`: the kit tables, events, flags, predictions, RAG `documents`, `kg_nodes` / `kg_edges`, and `replay_runs`.
  - `raw`: the data verbatim.
  - `app`: logs.
- Two login users, each with only the access its service needs:

  | User | Group role | Used by | Access |
  |---|---|---|---|
  | `spot_app_user` | `spot_app` | backend (Vercel) | read/write `public`; append-only `raw` and `app` |
  | `spot_mcp_user` | `spot_mcp_reader` | MCP server (Vercel); System 1 call logging (AMD) | read `public`; insert into `app` |

- Seeded kit data, a pre-loaded demo baseline (500 events and 58 flags), embedded RAG documents, and the knowledge graph.

## Rules
- **Never commit a connection string or password.** They go in your shell, `.env` (git-ignored), or the Vercel and AMD env settings.
- **Don't reuse the dev passwords** (`spot_app_dev`, `spot_mcp_dev`). Generate new ones with `openssl rand -hex 24`.
- **Create a Neon branch before anything destructive** (`setup.py reset --yes`, `seed --force-reseed`).
- **Embeddings must use the same model and size** as the MCP server at query time: `Qwen/Qwen3-Embedding-0.6B`, `EMBED_DIM=1024`.

## 1. Create the project (Neon console)
1. Create a new project: region **AWS US East 2 (Ohio)**, Postgres **17**. The Vercel projects are pinned to `cle1` (Cleveland) to sit next to it. If you pick another region, change `regions` in `backend/vercel.json` and `amd/mcp/vercel.json`; `backend/README.md` has the mapping.
2. From **Connect**, copy the owner's connection string, `neondb_owner`. You need two forms:
   - **Pooled:** the host contains `-pooler`. Every service uses this one.
   - **Direct:** no `-pooler`. Either form works for setup; the tooling turns prepared statements off.

## 2. Tooling (from the repo root)
```bash
python3.12 -m venv .venv-db && . .venv-db/bin/activate
pip install -r db/requirements.txt            # psycopg, pypdf, httpx; no torch
export DATABASE_URL='postgresql://neondb_owner:<pw>@ep-xxxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require'
```
`sslmode=require` is added automatically for `*.neon.tech` hosts.

## 3. Schema, users, and data
```bash
python db/setup.py migrate          # pgvector extension, all tables/functions, group roles + grants (idempotent)

export SPOT_APP_PASSWORD="$(openssl rand -hex 24)"  SPOT_MCP_PASSWORD="$(openssl rand -hex 24)"
python scripts/db_login_roles.py    # creates spot_app_user / spot_mcp_user in their groups (idempotent)
# Save both passwords in a password manager now; they're needed in step 5.

python db/setup.py seed             # kit tables, raw.*, events baseline, 58 seeded flags (non-destructive)
```
If Neon refuses `CREATE ROLE`, do this instead, then re-run `migrate` and `db_login_roles.py`:
1. In the console (**Roles**), create `spot_app` and `spot_mcp_reader`.
2. Also create the two users, `spot_app_user` and `spot_mcp_user`.

## 4. Embeddings and the knowledge graph (needs an embedding service)
Ingest calls an OpenAI-compatible `/v1/embeddings` endpoint. Use whichever you have:
- **The AMD cluster's embed service** (preferred; fast). It must be reachable from where you run this.
- **Local CPU fallback**, running in another terminal:
  - with Docker: `docker compose --profile models up embed` (serves on `localhost:8081`; the first run downloads about 1.2 GB);
  - without Docker: `python amd/embed/server.py` in that service's venv (`amd/embed/README.md`).

```bash
export EMBED_URL=https://<amd-embed-url>      # or http://localhost:8081
export EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B EMBED_DIM=1024
python amd/embed/ingest.py          # kit events, incidents, policy rules, Spot manual PDFs (SPOTDATA/) -> documents
python db/graph_build.py            # kg_nodes/kg_edges; add --no-embed if no embedder is reachable (nodes stay pending)
```
**If you can't reach an embedding service yet:**
- Skip both commands and run them later. They're idempotent.
- Until then, MCP vector search finds nothing, and the backend's `/search` uses full-text instead.

## 5. Verify
```bash
python db/setup.py status                 # row counts: kit tables filled, events 500, flags 58, documents > 0
python db/smoke_test.py                   # 3 RAG queries through both search functions (needs EMBED_URL)
python db/smoke_test.py --roles           # spot_mcp_reader can read/insert but not update/delete
```
Then build the two service URLs. They use the **pooled** host and the passwords from step 3:
```
backend  DATABASE_URL=postgresql://spot_app_user:<SPOT_APP_PASSWORD>@ep-xxxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require
mcp      DATABASE_URL=postgresql://spot_mcp_user:<SPOT_MCP_PASSWORD>@ep-xxxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require
```
Check each one by logging in with it:
```bash
psql "<backend url>" -c 'select count(*) from flags'    # 58
psql "<mcp url>" -c 'select count(*) from documents'    # > 0
```

## 6. Demo safety
- **Before judging,** create a branch named `demo-snapshot` (Branches → Create, from `main`). If the demo data gets messed up, restore `main` from it, or point the services at the branch. A branch has a **different host**, so changing the services means updating `DATABASE_URL` in Vercel and redeploying.
- **To clear the demo between judges,** call the backend's `POST /api/v1/ops/reset-demo` with `{"confirm": true}` and the `X-Admin-Token` header. It clears events, flags, assessments, acks, and demo docs, and keeps the kit and the logs. Don't use `setup.py reset` for this.
- **Expect a short delay on the first request after the database idles.** The free plan suspends idle compute, so the first request takes a moment to wake it. The backend's pool reconnects on its own.

## Alternative: one command with Docker
With Docker on your machine, the compose `db-init` job does steps 3–5 in one go (`docs/RUNNING.md` "Mode 3"):
1. Set these in `.env`:
   - `COMPOSE_PROFILES=neon,models`
   - `DATABASE_URL` = the owner URL
   - `SPOT_APP_PASSWORD`, `SPOT_MCP_PASSWORD`
2. Run `docker compose up db-init`.

## Re-running later
Every command here is idempotent. After a schema change lands (a new `db/migrations/0NN_*.sql`), run `python db/setup.py migrate` with the owner URL **before** deploying the code that needs it. On Vercel, stepped replay and reset-demo return 503 until migration `010` is applied.
