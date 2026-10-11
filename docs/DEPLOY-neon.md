# Deploy: Neon (Postgres + pgvector)

How to set up the production database. Written for the agent doing the deploy. Do this **before** `docs/DEPLOY-vercel.md`, because the backend and the MCP server fail their health checks without it.

The schema, seed, and role details live in `db/README.md`. This file is the ordered runbook.

## What you end up with
- One Neon project, **AWS us-east-2**, Postgres 17, database `neondb`, owner `neondb_owner`.
- Schema from `db/migrations/001–015` (two files share 013: `013_chat_mission`, `013_live`):
  - `public`: the kit tables, the analytics tables (`mission_kpis`, `risk_scores`, `insights`), events, flags,
    assessments, acknowledgements, predictions, RAG `documents`, `kg_nodes` / `kg_edges`, `replay_runs`, chat and
    live-run tables.
  - `raw`: the data verbatim.
  - `app`: logs.
- Two login users, each with only the access its service needs:

  | User | Group role | Used by | Access |
  |---|---|---|---|
  | `spot_app_user` | `spot_app` | backend (Vercel) | read/write `public`; append-only `raw` and `app` |
  | `spot_mcp_user` | `spot_mcp_reader` | MCP server (Vercel); System 1 call logging (AMD) | read `public`; insert into `app` |

- **The baseline** (what judges see before anyone replays):

  | What | Count |
  |---|---|
  | kit: locations / assets / missions / actions / readings / anomalies | 15 / 20 / 27 / 270 / 220 / 40 |
  | analytics: `mission_kpis` / `risk_scores` / `insights` (filled by `seed` from the kit) | 27 / 189 / 5 |
  | `events` (all of `replay.ndjson`, E00001–E00500) / seeded `flags` (P1 12, P2 46) | 500 / 58 |
  | `documents`, all embedded with `Qwen/Qwen3-Embedding-0.6B` (event 297, incident 98, manual 85, policy 23) | 503 |
  | knowledge graph `kg_nodes` (all embedded) / `kg_edges` | 574 / about 1,968 |
  | `raw.spot_event_log` / `raw.kit_files` / `raw.kit_rows` / `raw.inspection_media` | 500 / 6 / 592 / 6 |
  | assessments, acknowledgements, `system1_predictions`, replay and live runs | 0 (filled by use) |

## Rules
- **Never commit a connection string or password.** They go in your shell, `.env` (git-ignored), or the Vercel and AMD env settings.
- **Don't reuse the dev passwords** (`spot_app_dev`, `spot_mcp_dev`). Generate new ones with `openssl rand -hex 24`.
- **Create a Neon branch before anything destructive** (`setup.py reset --yes`, `seed --force-reseed`).
- **Never use `seed --force-reseed`** except for the baseline restore below, on a database whose `events` and
  `flags` are empty. On live data it overwrites events' payloads and flags' reasons, scores and escalation.
- **Embeddings must use the same model and size** as the MCP server at query time: `Qwen/Qwen3-Embedding-0.6B`, `EMBED_DIM=1024`.
- **Every migration runs again on every deploy.** The GitHub `neon-migrate` job runs `setup.py migrate` before each
  Vercel deploy, and `migrate` re-applies every `db/migrations/*.sql` file (there is no ledger). So:
  - To drop a table, remove its `CREATE` (and its `reset.sql` line) in the same commit. Otherwise it comes back,
    empty.
  - A `REVOKE` must live in a migration numbered after `007_roles.sql`, because 007 re-grants on all tables.
  - A new migration must be idempotent and must warn instead of failing (it runs against production unattended).
  - Data fixes (deletes, fills) are one-off owner scripts or `setup.py seed` steps, not migrations, unless they are
    deterministic upserts.

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
# A user that already exists keeps its password; SPOT_ROTATE_PASSWORDS=1 changes it (then update Vercel and the
# AMD box). The dev defaults (spot_app_dev / spot_mcp_dev) are refused on any non-local host.

python db/setup.py seed             # kit tables, raw.*, analytics, events baseline, 58 seeded flags (non-destructive)
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
export EMBED_URL=https://<amd-embed-url>      # or http://localhost:8081, or http://localhost:18081 (SSH bridge)
export EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B EMBED_DIM=1024
export EMBED_CLIENT_BATCH=16                  # the default; the SSH bridge drops longer requests
# leave EMBED_QUERY_PREFIX unset (the Qwen3 default, the same as the backend and the MCP server)
python amd/embed/ingest.py          # kit events, incidents, policy rules, Spot manual PDFs (SPOTDATA/) -> documents
python db/graph_build.py            # kg_nodes/kg_edges; add --no-embed if no embedder is reachable (nodes stay pending)
```
Both check the embedder's `/health` first and exit before writing anything if its model or dimension differs from
`EMBED_MODEL` / `EMBED_DIM`. Each batch is retried on transport errors and HTTP 429/502/503/504, and a re-run embeds
only documents whose text or model changed (0 on an unchanged corpus). `db/job.sh refresh` runs ingest, then the
graph build (with `--no-embed` if ingest failed), then `status`.
**If you can't reach an embedding service yet:**
- Skip both commands and run them later. They're idempotent.
- Until then, MCP vector search finds nothing, and the backend's `/search` uses full-text instead.

## 5. Verify
```bash
python db/setup.py status                 # row counts: the baseline table above (events 500, flags 58, documents 503, ...)
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
psql "<mcp url>" -c 'select count(*) from documents'    # 503
```

## 6. Refresh order
The whole chain, in this order (each step is idempotent):
```
migrate -> seed -> ingest -> graph_build -> status -> smoke
python db/setup.py migrate && python db/setup.py seed && python amd/embed/ingest.py && python db/graph_build.py \
  && python db/setup.py status && python db/smoke_test.py && python db/smoke_test.py --roles
```
- **After any replay or `reset-demo`,** run `python amd/embed/ingest.py --kinds incident && python db/graph_build.py`.
  Flag incident documents (`incident:flag-<flag_id>`) and graph flag nodes are keyed by `flag_id`, and both
  operations replace the flags. Without the refresh, similar-incident search sees only the 40 anomaly incidents and
  the graph has no flag nodes.
- **A replay covers missions M001–M023 only.** `replay.ndjson` ends at E00500, inside M023. So a full replay with
  `reset: true` ends with **51** flags, not the seed's 58: the seed also flags 7 events the replay never sends
  (`act-A0233`, `act-A0247`, `act-A0251`, `act-A0265`, `mis-M024`, `mis-M025`, `mis-M027`). Do not invent events to
  close the gap.
- **Baseline restore** (the only use of `--force-reseed`). Use it after `reset-demo` or `setup.py reset --yes`, when
  `events` and `flags` are empty, to get the 58-flag baseline back:
  `python db/setup.py seed --force-reseed`, then `ingest --kinds incident` (a full `ingest` when `documents` is
  empty, e.g. after `setup.py reset --yes`, which drops `documents` and the graph too), `graph_build` and `status`.
  Plain `seed` does not reload the baseline, because the first finished `seed` job in `app.job_runs` marks it as
  loaded. It does refill the analytics tables on every run. After `reset-demo` the restored flags take new ids from
  the sequence (not 1–58); after `reset --yes` (new tables) or the 2026-10-11 cleanup (sequence reset) they are 1–58.

## 7. Demo safety
- **Before judging,** create a branch named `demo-snapshot` (Branches → Create, from `main`). If the demo data gets messed up, restore `main` from it, or point the services at the branch. A branch has a **different host**, so changing the services means updating `DATABASE_URL` in Vercel and redeploying.
- **To clear the demo between judges,** call the backend's `POST /api/v1/ops/reset-demo` with `{"confirm": true}` and the `X-Admin-Token` header. It clears events, flags, assessments, acks, demo docs and the graph's non-live flag/note/explanation nodes, and keeps the kit and the logs. Then follow "Refresh order" above. Don't use `setup.py reset` for this.
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

## Current production and automation

The deployed project is **SPOTlight** (`summer-sound-57299654`), main branch
`br-wispy-hat-b54morim`, Postgres 17 in AWS us-east-2. The older SpotTheDog
project is not the production database for these three services.

Production holds the seed baseline from "What you end up with" (58 flags, 500 events,
503 documents, 574 graph nodes), restored by the 2026-10-11 cleanup below. Replay
run 1 (2026-10-10, 51 flags) was a deploy check made while System 1 was
unreachable: all of its flags carried `laya_unavailable`, so it was removed. The
`demo-snapshot` branch `br-still-dawn-b55ga6ag` preserves the pre-replay seed
baseline (without documents or graph).

The GitHub `neon-migrate` job now runs migrations and status before Vercel deploys.
It uses the `NEON_DATABASE_URL` secret from environment `NEON_API_KEY`: direct
owner connection, database neondb, role neondb_owner. It does not seed, reset,
rotate service passwords, create test branches, or call the Neon management API.
See [the Actions guide](DEPLOY-github-actions.md) for exact configuration and
[the deployment report](DEPLOY-status-2026-10-10.md) for evidence.

### Data cleanup 2026-10-11

Run by the owner (`neondb_owner`) after the data audit, in this order. The full
audit and plan stay outside the repo.
1. **Backup.** A `pg_dump` of the database is taken first and kept off-repo
   (never committed). The `demo-snapshot` branch and Neon point-in-time restore
   are the other rollbacks.
2. **Migrate.** `004` (exact-id ranking), `014_schema_hygiene`
   (indexes, `assessments.source` / `fallback_reason`, CHECKs, drop of the
   unused `anomaly_briefs`) and `015_kit_comments` (the kit data dictionary
   as column comments).
3. **Remove test rows and restore the baseline in one guarded transaction.**
   An off-repo owner script (not `seed --force-reseed`) locks the affected
   tables, deletes, re-seeds and checks post-conditions before `COMMIT`; it
   aborts unless every count matches the audit:
   - Replay run 1: its 51 flags (ids 59–109), its `replay_runs` row, its
     `app.job_runs` row, and its 500 duplicate `raw.spot_event_log` rows
     (`source = 'replay'`, byte-identical to the seed's `replay.ndjson` rows,
     which are kept). The 500 `events` rows it had re-ingested were not
     deleted: they were overwritten in place with the seed payload (step 4).
   - Interaction tests: 6 template assessments and 3 button-test
     acknowledgements (all on replay flags), 2 test chat conversations and
     their 4 messages, and 1 test live run that was never claimed.
   - Logs: 80 `app.model_calls` rows and 7 `app.mcp_tool_calls` rows of dev
     and test calls (21 chat rows held the Gemini API key in their error
     text, so that key must be rotated) and 59,848 `app.request_log` rows
     (mostly dashboard polls).
   - Sequences reset, so the restored flags are ids 1–58 again.

   The seed's `app.job_runs` row is kept, along with the kit, `raw.*`
   and every other table.
4. **Restore the baseline (same transaction).** `seed_events` with the force
   flag upserts the 500 events in place (the seed payload with its
   `enriched` block and no `triage`), then `seed_flags` (58 flags, ids 1–58,
   P1 12 / P2 46), `seed_analytics` (`mission_kpis` 27, `risk_scores` 189,
   `insights` 5) and a media `UPDATE`: `seed` only inserts missing `raw.*`
   rows, so the existing 6 `raw.inspection_media` rows get the new
   `metadata` hints (`action_type`, `inspection_hint`, `subject_hint`;
   `db/setup.py` `MEDIA_HINTS`) there (`action_id`, `mission_id`,
   `captured_at` stay NULL). After the commit: `ingest` (503 documents) and
   `graph_build` (574 nodes, 1,968 edges), `status`, and `smoke_test.py`
   with `--roles`.

Expected `status` afterwards: the baseline table in "What you end up with",
plus `app.job_runs` = the seed rows and one ingest and one graph row.

After the code deploy: set `EMBED_URL` / `EMBED_MODEL` / `EMBED_DIM` on both
Vercel projects (`DEPLOY-vercel.md`), add `LOGS_DATABASE_URL` on the AMD box
(`deploy/amd-native/README.md` "System 1 call logging"), truncate
`app.request_log` once more (rows the old middleware wrote between the cleanup
and the deploy), and rotate the Gemini key (set the new one with `printf %s`).

### Migration initialization troubleshooting

An installed pgvector extension can still be unloaded in a fresh database
session. A function containing `SET hnsw.iterative_scan` then fails permission
validation even for the owner. The migration renderer now executes
`SELECT '[0]'::vector` when pgvector >= 0.8 is installed, registering the setting
before defining search functions. The fix preserves the function-level setting;
no privilege escalation or password change is needed. Keep this initialization
in the same connection as the migrations; a separate psql process cannot fix it.
