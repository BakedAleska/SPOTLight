# db: Neon Postgres + pgvector (schema, seeding, search functions, logs)

Implements `docs/system2-rag-contract.md` §2, §3, §6 and §7 (Graph RAG). Everything runs unchanged against Neon or a local Postgres:
only `DATABASE_URL` (and optionally `LOGS_DATABASE_URL`) changes. Embedding/ingestion is in `amd/embed/`.

## Files
| Path | What |
|---|---|
| `migrations/001_core.sql` | Core tables from the original draft (`git show 19dff30:web/db/schema.sql`), `IF NOT EXISTS`. Added to `flags`: `triage_event_id` (unique; `act-A0001` / `mis-M001`), `priority` (P1–P4), `category`. `anomaly_briefs` was removed on 2026-10-11 (014 drops it). |
| `migrations/002_system1_predictions.sql` | Laya's per-event answers. |
| `migrations/003_documents.sql` | `CREATE EXTENSION vector`, the `documents` RAG table. `vector(EMBED_DIM)` (`halfvec` above 2,000 dims), GIN on `content_tsv`, btree on the filter columns. **Exact search, no ANN index** by default. |
| `migrations/004_search_functions.sql` | `match_documents` and `match_documents_hybrid` (RRF; returns `score`, `similarity`, `text_match`; exact ids in the query rank first, contract §3). |
| `migrations/005_raw_and_app_logs.sql` | `raw.*` (verbatim SPOT data) and `app.*` (model calls, MCP tool calls, requests, job runs). Runs on `LOGS_DATABASE_URL`. |
| `migrations/006_log_helpers.sql` | `raw.get_mission_event_log(mission, source)` / view `raw.mission_event_log`; `app.get_model_call_stats(interval)` / view `app.model_call_stats` (p50/p95 latency and tokens/s per system+model+device); `app.get_mcp_tool_stats(interval)`. |
| `migrations/007_roles.sql` | NOLOGIN group roles `spot_mcp_reader` and `spot_app` + grants (guarded; warns instead of failing). |
| `migrations/008_spot_event_log_request_id.sql` | `raw.spot_event_log.request_id`. |
| `migrations/009_graph.sql` | Graph RAG: `kg_nodes` (pgvector + pg_trgm GIN on label/key), `kg_edges`, `match_kg_nodes`, `match_kg_nodes_text`, `graph_neighborhood`. Grants: `spot_mcp_reader` SELECT + EXECUTE; `spot_app` read/write (SELECT + EXECUTE here, INSERT/UPDATE in `013_live.sql`, because live runs write `kg_*`; DELETE, used by `reset-demo`, from 007's grant on every `public` table). |
| `migrations/010_replay_runs.sql` | Stepped replay state for Vercel (`replay_runs`: event ids, cursor, counts, lease; at most one `running`). CRUD for `spot_app`, SELECT for `spot_mcp_reader`. |
| `migrations/011_chat.sql` | AI Assistance chat history (`docs/chat-contract.md` §1): `chat_conversations`, `chat_messages` (seq per conversation, jsonb sources/citations/grounding/usage, `error_code`), sidebar + trigram search indexes. CRUD for `spot_app`; **nothing** for `spot_mcp_reader`. |
| `migrations/012_chat_logs.sql` | (logs) `app.model_calls.system` CHECK also allows `chat` (replaces the inline 005 CHECK idempotently). |
| `migrations/013_chat_mission.sql` | Mission chats (`docs/chat-contract.md` §9, `chat.3`): `chat_conversations.mission_id` → `missions` (`ON DELETE SET NULL`, named FK added if missing; dangling ids detached first) + index `(mission_id, updated_at DESC)`. Grants unchanged. |
| `migrations/013_live.sql` | Live simulation (backend contract "Live simulation"): `live_runs` (at most one pending/running), `live_captures` (one per stop). CRUD for `spot_app`, plus INSERT/UPDATE on `kg_nodes` / `kg_edges`; SELECT for `spot_mcp_reader`. Sorted after `013_chat_mission`; the two are independent. |
| `migrations/014_schema_hygiene.sql` | 2026-10-11 data audit; additive. Indexes on `flags(event_id)`, `flags(mission_id)` and `live_captures(flag_id)`. `assessments.source` / `fallback_reason`. Corrected comments. `DROP TABLE IF EXISTS anomaly_briefs`. Enum CHECKs (flag status/severity/priority, assessment state, ack action, prediction kind, `actions.failure_type` set iff FAILED), added `NOT VALID` and then validated; a violating row only warns, so a deploy never fails on it. |
| `migrations/015_kit_comments.sql` | The kit's data dictionary (`03_DATA_DICTIONARY/data_dictionary.csv`) as comments on the 56 kit columns, plus a "synthetic data" comment on each kit table. Metadata only. |
| `graph_build.py` | Builds the knowledge graph from the core tables + `documents` (see "Knowledge graph" below). |
| `tests/test_graph.py` | Graph build + functions on a throwaway DB (`make test-graph`). |
| `tests/test_chat.py` | Chat migrations, grants and every `ChatRepo` method on a throwaway DB (`make test-chat-db`). |
| `migrations/optional/hnsw.sql` | Optional HNSW index (`migrate --hnsw`). |
| `reset.sql`, `reset_logs.sql` | Drops (only via `setup.py reset --yes`). |
| `setup.py` | `migrate`, `seed`, `reset --yes`, `status` (row counts, including the analytics, replay/live run tables, documents per kind and graph per kind). |
| `policy_rules.py` | `policy.py` as named rules: flag reasons + the `policy:<rule>` RAG docs. Asserted against `policy.py` on every kit event at seed time. |
| `spotdb.py` | Connections (Neon TLS, pooler-safe), `job_run()` logging, paths. |
| `smoke_test.py` | Embeds the 3 test queries, calls both search functions and (if built) the graph functions; `--roles` checks `spot_mcp_reader`. |
| `local_pgvector.sh` | Local `pgvector/pgvector:pg17` container `spot-pgvector` on port 5433. |
| `Dockerfile`, `job.sh` | One-shot db job container (migrate/seed/ingest/graph/refresh/status/smoke; `HEALTHCHECK NONE`). `refresh` = ingest -> graph (`--no-embed` if ingest failed; plus `DB_GRAPH_ARGS`) -> status. CPU-only, no torch. |

Migrations are idempotent and all re-applied on every `migrate`. `EMBED_DIM` (default 1024) is substituted into
003/004; if it no longer matches the existing column, `migrate` stops and tells you to use `--recreate-documents`.
Production runs `migrate` before every deploy, so a dropped table needs its `CREATE` removed too, a `REVOKE` must
come after `007_roles.sql`, and data fixes belong in a one-off owner script or a `seed` step
(`docs/DEPLOY-neon.md` "Rules").

## Seed (`python db/setup.py seed`, idempotent and non-destructive)
- **Non-destructive by default (audit M5).** db-init runs `seed` on every `docker compose up`, so every write is
  insert-if-missing (`ON CONFLICT DO NOTHING`): existing kit rows, events and flags are never updated. The demo
  baseline (`events` from replay.ndjson + the seeded flags) loads only on a database's first seed (a finished `seed`
  job in `app.job_runs` that counted flags marks it); later runs leave events and flags alone, so live flags,
  acknowledged flags and a reset demo stay as they are.
- **`seed --force-reseed`** is the explicit destructive path: upserts the kit, events and seeded flags over existing
  rows (flag `status`/acks kept; `reasons`, `score`, `escalated` overwritten) and reloads the baseline. db-init never
  uses it. Use it only to restore the baseline when `events` and `flags` are empty: after `setup.py reset --yes`
  (which keeps `app.job_runs`) or after `POST /ops/reset-demo` (`docs/DEPLOY-neon.md` §6 "Baseline restore"; the
  2026-10-11 cleanup restored it in its own transaction, see "Data cleanup 2026-10-11" there). Then run
  `ingest --kinds incident` (a full `ingest` when `documents` is empty, as after `reset --yes`) and `graph_build`.
- `public`: the 6 kit CSVs (upserts), `events` from `replay.ndjson` (500 rows; payload = the event's details + the
  action's reading/anomaly + the same `enriched` block the runtime ingest adds for action events; skip with
  `--no-events` if the live replay should insert them), and **58 flags**: every kit action/mission event the policy
  escalates (P1 = 12, P2 = 46 of 297 triaged events). `flags.reasons` = `[{rule, detail, weight, priority}]`; `rule`
  is the `policy:<rule>` doc. `--force-reseed` keeps `flag_id` and `status`. **The 58-flag seed is the production
  baseline.** A full replay yields 51, because `replay.ndjson` stops inside M023 (contract §2).
- **Analytics (`seed_analytics`, every seed, idempotent):** `mission_kpis` (27 missions: reported status vs true
  health, failed/total actions, HIGH/CRITICAL anomalies, battery drop), `risk_scores` (189 rows: action failure rate
  per waypoint, action type, battery band and configuration) and `insights` (5 headline metrics from the kit's
  `validation_results.json`, each recomputed in SQL; a mismatch prints a warning). They run even when the demo
  baseline is already loaded. Rows change only when the kit does.
- `raw`: every CSV row verbatim (`raw.kit_files` with sha256 + `raw.kit_rows`), every replay line verbatim
  (`raw.spot_event_log`, `UNIQUE (source, sha256)`), and the 6 sample inspection images (`raw.inspection_media`,
  repo-relative `uri`, sha256, bytes, `metadata.action_type` and plain-word hints; they are synthetic, reused across
  stops and not linked to a kit action, so `action_id`, `mission_id` and `captured_at` stay NULL).
- The seed and ingest jobs log themselves to `app.job_runs`. The first finished `seed` job that counted flags marks
  the baseline as loaded; keep that row.
- The seed functions write through the caller's connection and do not commit by themselves, so an owner script can
  restore the baseline inside one transaction.

## NULLs that are data (do not "fill" them)
| Column | NULL means |
|---|---|
| `events.action_id` / `location_id` / `asset_id` | a mission-level event (`MISSION_STARTED`, `MISSION_COMPLETED`: 45 of 500) |
| `flags.action_id` / `location_id` / `asset_id` | a mission flag (`category = 'mission'`) |
| `flags.event_id` | the flag's event is not in `replay.ndjson` (7 seeded flags: M024–M027 and actions after A0228) |
| `events.mission_id` | a live event (live missions are not kit missions; the live ids are in `payload.live`) |
| `actions.failure_type` | the action succeeded (014 CHECK: set if and only if `status = 'FAILED'`; the kit has only SUCCESS and FAILED) |
| `assessments.fallback_reason` | a System 2 answer (`DONE`) |
| `system1_predictions` (empty table) | no replay has run with System 1 reachable since the baseline was loaded |

`events` and `flags` entity ids have no foreign keys on purpose: live events use ids such as `LIVE-<run>-<stop>`.
`actions` / `readings` / `anomalies.location_id` is where the inspection happened; `assets.location_id` is the
asset's nominal home (they differ for 61 of 270 actions in the kit itself).

## Neon
Neon supports pgvector (`CREATE EXTENSION vector`; Neon ships 0.8.0 on PG14–17, which has `hnsw.iterative_scan`):
https://neon.com/docs/extensions/pgvector and https://neon.com/docs/extensions/pg-extensions.
```bash
export DATABASE_URL='postgresql://neondb_owner:<pw>@ep-xxxx.us-east-2.aws.neon.tech/neondb?sslmode=require'
python db/setup.py migrate && python db/setup.py seed && python amd/embed/ingest.py && python db/graph_build.py \
  && python db/setup.py status            # the order of docs/DEPLOY-neon.md §6 (then smoke_test.py)
```
- `sslmode=require` is added automatically for `*.neon.tech` URLs. Pooled (`-pooler`) URLs work (no server-side
  prepared statements). Run migrations as the owner role.
- **Roles.** The owner (`neondb_owner`) can create the NOLOGIN roles in SQL, so `migrate` does it. If that's not
  permitted, create `spot_mcp_reader` and `spot_app` in the Neon console (Roles), then re-run `migrate` to apply
  grants. Give services a login role: `CREATE ROLE mcp LOGIN PASSWORD '...' IN ROLE spot_mcp_reader;` (or create it
  in the console and `GRANT spot_mcp_reader TO mcp;`), and use that role in the MCP server's `DATABASE_URL`.
  - `spot_mcp_reader`: SELECT on `public.*`, EXECUTE on the match functions, SELECT on `raw.spot_event_log`,
    `raw.mission_event_log`, `raw.get_mission_event_log()`; INSERT on `app.*` (plus SELECT on `call_id`, so
    `INSERT ... RETURNING call_id` works). No UPDATE/DELETE anywhere.
  - `spot_app`: read/write `public.*`; SELECT + INSERT on `raw.*` and `app.*` (append-only); UPDATE on `app.job_runs`.
- Demo safety: make a Neon branch before `reset --yes`.

## Local
```bash
./db/local_pgvector.sh                      # prints: export DATABASE_URL=postgresql://postgres:postgres@localhost:5433/spot
python db/setup.py migrate && python db/setup.py seed && python db/setup.py status
./db/local_pgvector.sh stop | wipe
```

## Docker job (build context = repo root)
```bash
docker build -f db/Dockerfile -t spot-db .
docker run --rm -e DATABASE_URL=postgresql://postgres:postgres@host.docker.internal:5433/spot \
  -e EMBED_URL=http://host.docker.internal:8081 --env-file amd/embed/.env spot-db all   # migrate + seed + ingest + graph + status
docker run --rm -e DATABASE_URL=... spot-db migrate        # or seed | ingest | graph | refresh | status | smoke | reset --yes
```

## Search
```sql
SELECT * FROM match_documents('[...]'::vector, ARRAY['incident'], filter_asset => 'HX-204', match_count => 5);
SELECT doc_id, score, similarity, text_match FROM match_documents_hybrid('[...]'::vector, 'HX-204');
```
`text_match` is true when the row contains every query term (so `HX-204` matches only HX-204 rows, not other heat
exchangers), or every id-like token of a longer question (`PANEL-211`, `A0061`, `AN014`, `WP05`, `M014`); such rows
also rank first (contract §3). Exact search over ~500 docs takes about 1–10 ms. If it ever gets slow: `python db/setup.py migrate --hnsw`
(the functions already set `hnsw.iterative_scan = strict_order`, so filtered queries still return `match_count` rows).

## Knowledge graph (contract §7, `python db/graph_build.py` / `db/job.sh graph`)
Plain SQL on Neon (no Apache AGE): `kg_nodes` + `kg_edges`, pgvector on nodes, `WITH RECURSIVE` traversal. db-init runs
it after `ingest` (`db/job.sh refresh`); run it again after every replay or `reset-demo`, after `ingest --kinds
incident`. Flag nodes use only fired policy rules, and nodes with `props.source = 'live'` are never overwritten.
Idempotent: upserts nodes (unchanged rows keep `updated_at`), deletes nodes it built that no longer
exist, and rewrites every edge with `props.source` in fk/stat/extract/curated in one transaction.
- **Nodes** (574 on the seed baseline, all embedded): the 15 kinds of the contract. Summaries are short facts
  computed from the data, e.g.
  `Turbine Hall South (WP09): mechanical zone, floor 1, HIGH_NOISE environment; assets PANEL-211, PANEL-212; 22
  actions, 64% success, 8 failed (3 reading unreliable, 2 sensor timeout, ...)`. A manual node's summary is the first
  ~200 chars of its chunk plus its page.
- **Edges** (about 1,968): `fk` from the rows (weight 1/n of the dst's in-edges of that rel); `stat` in SQL
  (`common_at`: failure type at a location / anomaly type on an asset, n ≥ 2, weight = rate; `fails_more_in`: action
  type in an environment with ≥ 3 actions and success below its overall rate, weight = the drop); `curated`
  (`about`, `relates_to`: the maps at the top of `graph_build.py`, asserted against policy.py's enums and
  db/policy_rules.py's rules); `extract` (`mentions`: asset/WP ids, location/asset names and a synonym dictionary over
  manual chunks, anomaly descriptions, operator notes and explanations; weight = hit count).
- **Embeddings**: document-backed nodes copy `documents.embedding`; the ~70 others embed their summary over HTTP, only
  when it changed (`props.embedded_hash`). `--no-embed` skips the HTTP calls; an unreachable embedder only leaves
  those nodes pending; when one batch fails, the vectors of the batches that succeeded are kept. Logged to
  `app.job_runs` (job `graph`) and `app.model_calls`.
- Flags: `--dry-run` (counts only), `--no-embed`, `--force-embed`. ~1 s without embedding, ~20 s on CPU the first time.
```sql
SELECT node_id, score FROM match_kg_nodes_text('HX-204');                      -- asset:HX-204 1.0, then its actions 0.99
SELECT hop, array_to_string(path, ' '), weight
FROM graph_neighborhood(ARRAY['action:A0061'], 2, kind_filter => ARRAY['manual','topic','failure_type']);
--  1  action:A0061 -failed_with-> failure_type:LOCALIZATION_FAILURE                              0.167
--  2  action:A0061 -failed_with-> failure_type:LOCALIZATION_FAILURE <-mentions- manual:p52-1      0.5
```
`weight` in `graph_neighborhood` is the path's strength (product of edge weights). Full behaviour (NULLs, score
scales, ordering): contract §7 "Implemented".

## AI Assistance chat (`docs/chat-contract.md` §1)
- **Storage.** `chat_conversations` (title, `title_source` default/auto/model/user, model, mode fast/thinking, pinned)
  and `chat_messages` (`seq` 1, 2, 3 ... per conversation, `UNIQUE (conversation_id, seq)`, `ON DELETE CASCADE`).
  Edit/regenerate delete the tail (`seq >= n`). Search is `ILIKE` on titles and message content (pg_trgm GIN).
  `chat.2`: `chat_messages.error_code` holds the §4 code of an error answer (011 adds it with
  `ADD COLUMN IF NOT EXISTS` to databases created earlier), and `list_conversations` skips conversations with no
  messages (also in `total`). `chat.3` (013): a conversation can be about one mission (`mission_id`, set at
  creation, never changed; a deleted mission sets it NULL). Conversation rows carry `mission_id` and
  `mission {mission_id, mission_name, mission_status}`; `list_conversations(mission_id=)` filters,
  `search_documents(mission_id=)` filters (hybrid `filter_mission` and full text), and `list_missions(q, limit)` feeds
  the mission picker (counts, `hidden_failure`, `chats`).
- **Access.** Only through `backend/app/db/chat.py` (`ChatRepo`): uuids as `str`, timestamps as `datetime`, jsonb as
  lists/dicts. `add_message` locks the conversation row and assigns `max(seq)+1` in one transaction. The same class
  holds the chat's read-only retrieval: `search_documents` (hybrid with a vector, else full text), `match_graph_nodes`
  (text + vector entry nodes), `graph_neighborhood`, `fleet_snapshot`, `entity_digests`, `suggestion_facts`
  (each ~5–30 ms locally).
- **Not evidence.** Chats are never embedded into `documents`, never graph nodes, and `spot_mcp_reader` gets no
  privilege on them (011 revokes what 007's "all tables" grant would give; it runs after 007 on every `migrate`).
  `db/reset.sql` drops both tables; `/ops/reset-demo` leaves them alone.

## Tests
```bash
make test-graph     # db/tests: extractor unit tests + build/functions on a throwaway DB spot_graphtest_<hex>
                    # in the compose Postgres (DB_PORT from .env); skips without Postgres. ~5 s.
GRAPH_TEST_EMBED=1 EMBED_URL=http://localhost:8081 make test-graph   # + the incremental-embedding check (~25 s CPU)
make test-chat-db   # chat: 011/012/013 (re-run), grants, every ChatRepo method on spot_chattest_<hex>; ~4 s
                    # (make test-graph runs all of db/tests, so it includes this file too)
make test-db        # seed is non-destructive (tests/test_seed_nondestructive.py)
# Retrieval regression set (db/tests/retrieval_testset.json: document queries + graph checks G01-G05). Read-only
# session, so it is safe on Neon; skips without DATABASE_URL + EMBED_URL or with an empty `documents`:
DATABASE_URL=... EMBED_URL=http://localhost:18081 python -m pytest -q db/tests/test_retrieval.py
make smoke          # RAG + graph queries and spot_mcp_reader permissions inside the stack
```

### Repeated migrations on Neon

The migration renderer initializes pgvector in each connection before adding
`SET hnsw.iterative_scan` to search functions. An existing extension does not
automatically load its configuration definitions in a fresh session; without
initialization, repeated migrations can fail with an insufficient-privilege error
even for `neondb_owner`. No extra role grants are required.
