# System 2 RAG contract: Neon pgvector + embedding service + MCP server

Two parts are built in parallel and must meet here. **Change this file first, then the code.**

| Part | Folder | Owner |
|---|---|---|
| Neon schema, pgvector, search functions, embedding service, ingestion, `raw` + `app` log schemas | `db/` and `amd/embed/` | pgvector agent |
| MCP server (tools the System 2 model calls) | `amd/mcp/` | MCP agent |

## 1. Embedding API (OpenAI-compatible)
Every component talks to the embedding model through the same HTTP contract, so the model and the server can be
swapped by changing configuration only. `llama-server --embedding`, Hugging Face TEI and Infinity all expose this API.

```http
POST {EMBED_URL}/v1/embeddings
{"model": "<EMBED_MODEL>", "input": ["text 1", "text 2"]}
→ {"data": [{"index": 0, "embedding": [..floats..]}, ...], "model": "...", "usage": {...}}
```

| Env var | Meaning | Default |
|---|---|---|
| `EMBED_URL` | base URL of the embedding service | `http://localhost:8081` |
| `EMBED_MODEL` | model id sent in requests | `Qwen/Qwen3-Embedding-0.6B` (Apache-2.0; fallback `Snowflake/snowflake-arctic-embed-l-v2.0`, also 1024-d). See `research/tech-embedding-model-amd-2026-10-10.md` |
| `EMBED_DIM` | vector dimension; must equal the `documents.embedding` column | `1024` |
| `EMBED_QUERY_PREFIX` | text prepended to search queries | Qwen3: `Instruct: Given an operator question about a SPOT robot inspection, retrieve relevant inspection events, anomaly notes, operator notes, and Spot manual passages\nQuery:` (a real newline). Arctic fallback: `query: ` |
| `EMBED_DOC_PREFIX` | text prepended to documents at ingestion | empty (both models take no document prefix) |
| `EMBED_CLIENT_BATCH` | texts per request from `amd/embed/client.py` (ingest, graph build, smoke test) | `16` (the SSH bridge to the AMD box drops long requests) |
| `DATABASE_URL` | Neon (or local) Postgres connection string | none |
| `LOGS_DATABASE_URL` | where `raw.*` and `app.*` live | `DATABASE_URL` |

Clients normalize nothing themselves: the search functions use cosine distance.
**One query prefix everywhere (2026-10-11).** The backend, the MCP server and `amd/embed/client.py` (the smoke test's queries;
ingest and the graph build embed documents, which take no prefix) all use the Qwen3 default above when `EMBED_QUERY_PREFIX` is unset, and no prefix when it is set but empty. The prefix is
joined to the query with no separator (`...\nQuery:` + query, the Qwen3 model card format). Leave it unset in every
deployment. Before writing, ingest and the graph build (when it has node summaries to embed; not with `--no-embed`)
check the server's `/health`: if its `model` differs from
`EMBED_MODEL` or its `dim` differs from `EMBED_DIM`, they exit before any write, so one `embed_model` label never
holds vectors from two models.

## 2. Tables
- **Core relational tables** are the draft at `git show 19dff30:web/db/schema.sql`: `locations, assets, missions,
  actions, readings, anomalies, events, flags, assessments, acknowledgements, risk_scores, mission_kpis,
  insights`. Keep those table and column names; adding columns or tables is fine. (`anomaly_briefs` had no reader
  or writer; it was a Databricks leftover and was dropped on 2026-10-11 by `014_schema_hygiene.sql`.)
  Added by the pgvector agent: `flags.triage_event_id` (unique, System 1 event id `act-A0001` / `mis-M001`),
  `flags.priority` (P1–P4), `flags.category`. Seeded flags' `reasons[].rule` names a `policy:<rule>` doc.
  Added 2026-10-11 (014): `assessments.source` and `assessments.fallback_reason` (`docs/backend-services-contract.md`
  "Stored explanations").
  Databricks was dropped, so `db/setup.py seed` (`seed_analytics`) fills `risk_scores`, `mission_kpis` and
  `insights` from the kit. `risk_scores` holds the action failure rate per waypoint, action type, battery band and
  configuration (189 rows). `mission_kpis` has one row per kit mission (27). `insights` holds the 5 headline metrics
  of the kit's `validation_results.json`, each recomputed in SQL (seed prints a warning on a mismatch). The step is
  idempotent and runs on every `seed`, including after the demo baseline has already loaded.
- **NULLs that are data, not gaps:** mission-level `events` (`MISSION_STARTED` / `MISSION_COMPLETED`) and mission
  flags (`category = 'mission'`) have NULL `action_id` / `location_id` / `asset_id`. `actions.failure_type` is set
  if and only if `status = 'FAILED'` (014 CHECK; the kit has only SUCCESS and FAILED, so NULL means SUCCESS). The 7 seeded flags whose event is not in `replay.ndjson` have NULL
  `flags.event_id` (the file stops inside M023, at E00500, so M024–M027 and the actions after A0228 never replay).
  Live events have NULL `mission_id`: `events.mission_id` references `missions`, and live missions are not kit
  missions. The action, location and asset ids of `events` and `flags` have no foreign keys, so live ids
  (`LIVE-<run>-<stop>`) can be stored there; do not add any.
- **Where an inspection happened:** `actions` / `readings` / `anomalies.location_id` is the waypoint where the
  inspection took place. `assets.location_id` is the asset's nominal home. They differ for 61 of 270 actions in the
  kit itself (e.g. PANEL-218 lives at WP12 and is inspected at WP15). Flag titles and RAG text use the action's
  location.
- **Added: `system1_predictions`**: Laya's answers per event, one row per (event, question).
  ```sql
  CREATE TABLE system1_predictions (
    prediction_id bigserial PRIMARY KEY,
    event_kind  text NOT NULL,          -- pre_action | action | mission
    action_id   text, mission_id text,
    question    text NOT NULL,          -- will_fail, failure_mode, anomaly_expected, escalate, priority, category, reading_trust, mission_misleading
    answer      text NOT NULL,          -- the option key, e.g. 'A', 'P2', 'navigation'
    probs       jsonb NOT NULL,         -- {"A": 0.71, "B": 0.29}
    confidence  real,
    model       text NOT NULL,          -- e.g. 'convaiinnovations/laya' or a fine-tuned checkpoint name
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_kind, action_id, mission_id, question, model)
  );
  ```
- **Added: `documents`** (the RAG store):
  ```sql
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE TABLE documents (
    doc_id       text PRIMARY KEY,        -- stable citation id: 'event:act-A0123', 'incident:AN012', 'manual:p42-3', 'policy:battery_low'
    kind         text NOT NULL CHECK (kind IN ('event','incident','operator_note','explanation','manual','policy')),
    source_id    text,                    -- id of the source row (action_id, anomaly_id, flag_id, ack_id, page...)
    mission_id   text, action_id text, asset_id text, location_id text,
    action_type  text, status text, anomaly_type text, severity text,
    ts           timestamp,
    title        text,
    content      text NOT NULL,           -- exactly the text that was embedded (without EMBED_DOC_PREFIX)
    metadata     jsonb NOT NULL DEFAULT '{}',
    content_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(title,'') || ' ' || content)) STORED,
    embedding    vector(EMBED_DIM),       -- dimension substituted at migration time
    embed_model  text,
    created_at   timestamptz NOT NULL DEFAULT now()
  );
  -- indexes: GIN (content_tsv), btree (kind, asset_id), (kind, location_id), (kind, mission_id), (embed_model)
  -- vector search is EXACT by default (a few thousand rows: exact is fast and never drops filtered results).
  -- Optional HNSW (embedding vector_cosine_ops) only if EXPLAIN shows it's slow; then the match functions must
  -- SET LOCAL hnsw.iterative_scan = strict_order so metadata filters still return k rows (pgvector >= 0.8; Neon ships 0.8.x).
  ```

## 3. Search functions (pgvector agent implements; MCP server calls)
```sql
-- Vector search with optional metadata filters. similarity = 1 - cosine distance.
match_documents(query_embedding vector, kinds text[] DEFAULT NULL, filter_asset text DEFAULT NULL,
                filter_location text DEFAULT NULL, filter_mission text DEFAULT NULL,
                match_count int DEFAULT 5, min_similarity real DEFAULT 0.0)
  RETURNS TABLE (doc_id text, kind text, title text, content text, metadata jsonb, asset_id text,
                 location_id text, mission_id text, action_id text, ts timestamp, similarity real)

-- Hybrid: vector + Postgres full-text (catches exact IDs like 'HX-204'), merged by reciprocal rank fusion.
match_documents_hybrid(query_embedding vector, query_text text, kinds text[] DEFAULT NULL,
                       filter_asset text DEFAULT NULL, filter_location text DEFAULT NULL,
                       filter_mission text DEFAULT NULL, match_count int DEFAULT 5, rrf_k int DEFAULT 60)
  RETURNS TABLE (doc_id text, kind text, title text, content text, metadata jsonb, asset_id text,
                 location_id text, mission_id text, action_id text, ts timestamp, score real,
                 similarity real, text_match boolean)   -- ADDED 2026-10-10 by MCP agent, see note
```
NULL filters mean "no filter". Results are ordered best first.

**Hybrid trailing columns (requested by the MCP agent, additive).** RRF `score` is rank-based, so it can't tell a weak
match from a strong one. Please append `similarity real` (1 - cosine distance for that row, the same value
`match_documents` would return) and `text_match boolean` (true if the row matched the full-text query). The MCP server
keeps a hybrid row only if `similarity >= MCP_MIN_SIMILARITY` or `text_match` is true. Until these columns exist, the MCP
server falls back to a second `match_documents` call plus an exact-ID check, so nothing breaks either way.
**Implemented (pgvector agent, 2026-10-10):** `text_match` is true when the row contains **every** query term
(`plainto_tsquery('english', query_text)`), so `HX-204` (indexed as `hx` + `-204`) is true only on HX-204 rows, not on
other heat exchangers. The lexical side of the RRF still ORs the terms (all-terms rows rank first). `similarity` is
NULL only if the row has no embedding. Vector search is exact (no ANN index) unless `migrate --hnsw` is run.
**Exact ids inside a question (CHANGED 2026-10-11, vector-store audit; same signature and columns).** Id-like tokens
in `query_text` (`HX-204`, `PANEL-211`, `A0061`, `AN014`, `WP05`, `M014`; pattern `[A-Za-z]{1,6}-[0-9]{2,4}`,
`A[0-9]{4}`, `AN[0-9]{3}`, `WP[0-9]{2}`, `M[0-9]{3}`) are ANDed into one id query. On the lexical side, rows that
contain every one of those ids rank first. They also get an extra `1/rrf_k` in the fused `score`. `text_match` is
true when the row contains every query term **or** every id in the query. So "What anomalies were found on PANEL-211
last week?" returns the PANEL-211 rows with `text_match` true and ranks them above semantic neighbours such as
PANEL-212. A query with no id-like token ranks exactly as before.

## 4. Corpora (`documents.kind`)
| kind | Source | doc_id pattern |
|---|---|---|
| `event` | every kit action and mission event, as prose (reuse `amd/system1/triage/render.py` `render_prose`). The title names the action id, so `A0061` is searchable | `event:act-<action_id>` / `event:mis-<mission_id>` (the System 1 triage event id, = `flags.triage_event_id`; not `events.event_id`, which is the replay id `E00001`). Live: `event:act-LIVE-<run>-<stop>` |
| `incident` | anomalies plus System 1 flags with outcome. Flag incidents render only the fired policy rules (never the `laya_verdict` / `rules_verdict` / `laya_unavailable` rows); `metadata.laya_status` is `ok`, `unavailable` or `none` | `incident:<anomaly_id>` / `incident:flag-<flag_id>` |
| `operator_note` | `acknowledgements` rows with a non-blank note | `note:<ack_id>` |
| `explanation` | System 2 outputs (`assessments` with `state = 'DONE'`; template fallbacks, `state = 'TEMPLATE'` / `model = 'template'`, are excluded: audit M10) | `explanation:<assessment_id>` (live green stops: `explanation:live-<capture_id>`) |
| `manual` | chunks of the Spot User Guide PDFs in `SPOTDATA/SPOT_KnightHacks_Challenge_Kit Part Two/`. The title cites the printed page and the PDF page; `metadata.printed_page` holds the printed one | `manual:p<PDF page>-<chunk>` |
| `policy` | one doc per triage rule in `amd/system1/triage/policy.py` | `policy:<rule_name>` |

Baseline corpus (2026-10-11): 503 documents, all embedded (event 297, incident 98 = 40 anomalies + 58 flags,
manual 85, policy 23). `incident:flag-<flag_id>` docs are keyed by `flag_id`, and a replay or `reset-demo` creates
new flag ids, so after either run `python amd/embed/ingest.py --kinds incident` and then `python db/graph_build.py`.
Otherwise similar-incident search sees only the 40 anomaly incidents. The ingest prune (docs of a processed kind
that the build no longer produces) never deletes live documents (`metadata.source = 'live'`).

## 5. MCP tools (MCP agent implements; see the tool list agreed in chat)
Tier 1 `get_flag_context`. RAG tools: `search_similar_incidents`, `search_asset_history`, `search_spot_manual`,
`search_operator_notes`, `search_past_explanations`, `search_policy`. SQL tools: `get_flag`,
`get_system1_forecast`, `get_action`, `get_mission_summary`, `get_reliability_stats`, `get_battery_risk`,
`list_allowed_actions`, `get_raw_event_log` (verbatim `raw.spot_event_log` rows, id `log:<log_id>`). All read-only. No raw SQL from the model. Every returned row carries its `doc_id`
or a short stable id the model can cite.

**Packet completeness: `missing` and `gaps` block, `notes` inform (ADDED 2026-10-11).** System 2's rule is in
`docs/backend-services-contract.md` ("Authoritative System 2 HTTP contract"): a non-empty `missing` or `gaps` list,
or `insufficient_context: true`, makes it answer `insufficient_context` / `MANUAL_REVIEW` without calling the model.
`notes` is not a consumed key, so System 2 ignores it. Both packet builders (MCP `get_flag_context` and the
backend's SQL fallback packet) sort their messages this way:
- **`missing`** (+ `insufficient_context: true`): core context is absent, e.g. the flag's action or mission row,
  or the asset record.
- **`gaps`** (blocking): a lookup failed or was skipped, so the packet may be wrong or incomplete. Examples:
  `<x> lookup failed (...)`, `<x> search failed (...)`, `retrieval skipped: <embedding error>`, `graph unavailable
  (...)`, `graph lookup failed (...)`, `packet is over the N-token budget even after trimming`, and in the SQL packet
  `some SQL lookups failed: ...`, `some SQL lookups did not finish within the request deadline`, `action X not found`.
- **`notes`** (informational, never blocking): the lookup worked and simply found nothing, or a degraded mode was
  used on purpose. Examples: `no relevant <section> above similarity 0.40`, `no knowledge-graph facts linked to this
  flag`, `category unknown; all actions allowed`, `trimmed to the N-token packet budget: asset_history 2->1, graph
  6->4` (MCP: retrieved items the packet budget left out). In the backend's SQL packet, the first note says why it is SQL-only
  (`MCP unavailable (...)` or `deadline: MCP did not answer ...`, then `SQL-only packet, no RAG retrieval`).
  Before 2026-10-11 these went into `gaps`, so an empty search turned a complete packet into `insufficient_context`.

**Other packet rules (2026-10-11).**
- `flag.reasons` (up to 8), the exact `policy:<rule>` documents, the policy query text and the graph seeds use only
  the fired policy rules. The verdict rows (`laya_verdict`, `rules_verdict`, `laya_unavailable`) go under a separate
  compact `flag.verdicts` key and are never treated as rules. (The backend's SQL fallback packet has no
  `flag.verdicts`: its `flag.reasons` is the first 8 `flags.reasons` rows, verdicts included, and only its `facts`
  skip the verdict rows. A `laya_unavailable` row carries the fixed detail in both packets.)
- Mission packets: `event.mission.worst_anomaly` carries `asset_name` and `asset_criticality`. The orchestrator
  needs them to build the canonical mission event for System 1.
- Asset history and prior flags are bounded to robot time before this flag's event. They exclude the flag's own
  anomaly and action, and keep only non-blank operator notes.
- Similar-incident and asset-history searches exclude the flag's own event document, `event:` +
  `flags.triage_event_id`.
- `get_raw_event_log` returns each `event_id` once. When the same event was logged under several sources (the seed's
  `replay.ndjson` import and a later streamed `replay`), the `replay.ndjson` row wins.
- Graph tools on a database whose `kg_nodes` is empty say `knowledge graph is empty (graph_build not run)`, not
  "no graph node matches".

## 6. Logs and raw SPOT data (added 2026-10-10)
Same Neon project and database, two extra Postgres schemas. `LOGS_DATABASE_URL` (defaults to `DATABASE_URL`) lets
logs move to a separate Neon database later without code changes. The existing tables stay in `public`.

**Roles:** the MCP server reads `public` (data + `documents`) read-only and may INSERT into `app.*` only.
It also needs `USAGE ON SCHEMA raw` + `SELECT ON raw.spot_event_log` for the `get_raw_event_log` tool (requested by the
MCP agent 2026-10-10; read-only, so still append-only for everyone else).
Implemented in `db/migrations/007_roles.sql` as NOLOGIN group roles `spot_mcp_reader` (also SELECT on
`raw.mission_event_log` / EXECUTE `raw.get_mission_event_log(mission_id, source)`, and SELECT on `call_id` of
`app.model_calls` / `app.mcp_tool_calls` so `INSERT ... RETURNING call_id` works) and `spot_app`. Helpers:
`app.get_model_call_stats(interval)` and `app.get_mcp_tool_stats(interval)` (p50/p95 latency, tokens/s).
Nothing ever UPDATEs or DELETEs `raw.*` (append-only).

### `raw`: SPOT data exactly as received (append-only, for audit, replay and re-derivation)
```sql
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE raw.spot_event_log (          -- every robot/replay event, verbatim
  log_id      bigserial PRIMARY KEY,
  source      text NOT NULL,               -- 'replay.ndjson' | 'live' | ...
  event_id    text, mission_id text, event_type text,
  event_ts    timestamp,                   -- time from the robot
  payload     jsonb NOT NULL,              -- the event exactly as received
  received_at timestamptz NOT NULL DEFAULT now(),
  sha256      text NOT NULL,               -- of the raw payload; UNIQUE (source, sha256) makes re-imports idempotent
  request_id  text,                        -- ADDED 2026-10-10 (backend agent): ties the event to app.* rows
  UNIQUE (source, sha256)
);
-- migration for existing DBs: ALTER TABLE raw.spot_event_log ADD COLUMN IF NOT EXISTS request_id text;
--                             CREATE INDEX IF NOT EXISTS spot_event_log_request ON raw.spot_event_log (request_id);
CREATE TABLE raw.kit_files (               -- provenance of every imported kit file
  file_id bigserial PRIMARY KEY, file_name text NOT NULL, sha256 text NOT NULL, row_count int,
  imported_at timestamptz NOT NULL DEFAULT now(), UNIQUE (file_name, sha256));
CREATE TABLE raw.kit_rows (                -- every CSV row verbatim, before typing/cleaning
  file_id bigint REFERENCES raw.kit_files, row_num int, data jsonb NOT NULL, PRIMARY KEY (file_id, row_num));
CREATE TABLE raw.inspection_media (        -- metadata + pointer for images/audio; blobs live in object storage or the repo
  media_id text PRIMARY KEY, action_id text, mission_id text, media_type text, uri text NOT NULL,
  sha256 text, bytes int, captured_at timestamp, metadata jsonb NOT NULL DEFAULT '{}');
-- indexes: (mission_id, event_ts), (event_type, event_ts), GIN (payload)
```

### `app`: application logs (append-only)
```sql
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE app.model_calls (             -- every System 1 / System 2 / embedding / chat call (the AMD latency evidence)
  call_id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), request_id text,
  system text NOT NULL,                    -- model_calls_system_check (012_chat_logs.sql replaces 005's inline CHECK):
                                           --   CHECK (system IN ('system1','system2','embed','chat'))
  model text, endpoint text,
  flag_id bigint, action_id text, input_tokens int, output_tokens int, latency_ms int, tokens_per_sec real,
  device text, ok boolean NOT NULL, error text, metadata jsonb NOT NULL DEFAULT '{}');
CREATE TABLE app.mcp_tool_calls (          -- audit of every MCP tool call System 2 makes
  call_id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), request_id text, flag_id bigint,
  tool text NOT NULL, args jsonb NOT NULL, result_count int, result_bytes int, top_similarity real,
  latency_ms int, ok boolean NOT NULL, error text);
CREATE TABLE app.request_log (             -- backend API writes, errors and slow requests (LOG_REQUESTS, backend contract)
  req_id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), request_id text, service text,
  route text, method text, status int, latency_ms int,
  error text);                             -- '<code>: <detail>' (<= 200 chars of detail) when status >= 400, else NULL
CREATE TABLE app.job_runs (                -- seed / ingest / replay / fine-tune sync jobs
  run_id bigserial PRIMARY KEY, job text NOT NULL, started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz, status text NOT NULL DEFAULT 'running', counts jsonb NOT NULL DEFAULT '{}',
  error text, metadata jsonb NOT NULL DEFAULT '{}');
-- indexes: ts DESC on each; (system, ts) on model_calls; (tool, ts) on mcp_tool_calls; (request_id) everywhere
```
`request_id` ties one flag's whole journey together across tables.

**Row conventions (2026-10-11).**
- `endpoint` is a path such as `/v1/embeddings`, never a URL with a host. The MCP server puts the host in
  `metadata.base_host`.
- No credential is ever written. The backend passes every `error` (model calls, requests, jobs) through
  `calllog.redact()` before capping it at 500 characters: API keys (`AIza...`, `AQ....`, `?key=`), `Authorization` /
  `x-goog-api-key` / `api_key` values, `Bearer` tokens and httpx's `Illegal header value b'...'` become `[redacted]`.
  These tables are served by `GET /ops/model-calls`.
- Embedding rows take `device` (`timing.gpu`, else `timing.device`), `tokens_per_sec` and `metadata.server_ms` from
  the embedding server's `timing` block when it sends one.
- The live runner has no database credentials, so the backend writes its rows with `metadata.via = 'live_runner'`:
  System 1 at `/result`, System 2 at `/analysis` (`/v1/assess`; vision time in `metadata.vision_ms`) and one `embed`
  row per `/embeddings` batch.
- Owner-run one-off scripts (`db/smoke_test.py`) roll back or delete their own probe rows.

## 7. Graph RAG (added 2026-10-10)
A knowledge graph in plain Neon Postgres: node/edge tables, pgvector on nodes, and multi-hop traversal with
`WITH RECURSIVE`. **Apache AGE (Cypher) is not available on Neon** (not in its extension list), so there's no
Cypher; everything is standard SQL. Retrieval: vector + text search find entry nodes → traverse 1–2 hops → return
connected facts with citation ids.

### Tables (migration `009_graph.sql`)
```sql
CREATE TABLE kg_nodes (
  node_id     text PRIMARY KEY,          -- '<kind>:<key>', e.g. 'asset:HX-204', 'location:WP09', 'failure_type:LOCALIZATION_FAILURE'
  kind        text NOT NULL CHECK (kind IN ('mission','action','asset','location','environment','action_type',
                'failure_type','anomaly_type','anomaly','flag','policy_rule','topic','manual','operator_note','explanation')),
  key         text NOT NULL,             -- the natural id (asset_id, WP id, enum value, doc id...)
  label       text NOT NULL,             -- human-readable name ("Heat Exchanger 04", "Turbine Hall South")
  summary     text NOT NULL,             -- one or two sentences of facts about the node; this is what gets embedded
  props       jsonb NOT NULL DEFAULT '{}',
  doc_id      text REFERENCES documents(doc_id) ON DELETE SET NULL,   -- document-backed nodes, see "Implemented" below
  embedding   vector(EMBED_DIM),
  embed_model text,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE kg_edges (
  src      text NOT NULL REFERENCES kg_nodes ON DELETE CASCADE,
  rel      text NOT NULL,                -- see the relation list below
  dst      text NOT NULL REFERENCES kg_nodes ON DELETE CASCADE,
  weight   real NOT NULL DEFAULT 1.0,    -- strength (counts/rates for statistical edges)
  props    jsonb NOT NULL DEFAULT '{}',  -- e.g. {"n": 6, "rate": 0.5}; 'source': 'fk' | 'stat' | 'extract' | 'curated'
  evidence text,                         -- a citable id (doc_id, anomaly_id, action_id) supporting the edge
  PRIMARY KEY (src, rel, dst)
);
-- indexes: kg_edges (dst), (rel); kg_nodes (kind), GIN trigram on (label) and (key) via pg_trgm
```
Directed edges, traversed in both directions.

`kg_edges.evidence` is NULL by design on structural and curated edges that no document backs: `located_at`,
`has_environment` and `relates_to` (`db/graph_build.py`; production 2026-10-11: 20 + 15 + 25 = 60 edges). Every other
relation carries evidence, and every non-NULL value resolves to a document or a `stat:` key.

### Relations
| rel | src → dst | source |
|---|---|---|
| `part_of` | action → mission | fk |
| `at` | action → location | fk |
| `inspects` | action → asset | fk |
| `of_type` | action → action_type; anomaly → anomaly_type | fk |
| `failed_with` | action → failure_type | fk (failed actions) |
| `located_at` | asset → location | fk |
| `has_environment` | location → environment | fk |
| `raised_on` | anomaly → asset | fk |
| `during` | anomaly → action | fk |
| `flags` | flag → action or mission | fk |
| `triggered` | flag → policy_rule | fk, from `flags.reasons` (policy rules only, not `laya_verdict`/`rules_verdict`/`laya_unavailable`) |
| `annotates` | operator_note → flag | fk (ADDED 2026-10-10, pgvector agent: a note was otherwise only linked by its mentions) |
| `explains` | explanation → flag | fk (ADDED 2026-10-10, pgvector agent) |
| `common_at` | failure_type → location; anomaly_type → asset | stat: weight = rate, props n/rate, only if n ≥ 2 |
| `fails_more_in` | action_type → environment | stat: success rate below the overall rate, props n/rate |
| `about` | policy_rule → failure_type / anomaly_type / topic | curated from the policy thresholds |
| `relates_to` | failure_type / anomaly_type / action_type → topic | curated map |
| `mentions` | manual / operator_note / explanation / anomaly → asset, location, failure_type, anomaly_type, action_type, topic | extract: regex ids (`HX-204`, `WP09`) + a synonym dictionary; weight = hit count |

Topics form a small curated vocabulary that links the Spot User Guide to the data, e.g. `topic:localization`,
`topic:battery`, `topic:docking`, `topic:communication`, `topic:obstacles`, `topic:thermal`, `topic:acoustic`,
`topic:gauge`, `topic:visual`, `topic:sensors`, `topic:safety`. Every manual chunk becomes a `manual` node
(`doc_id` = its document).

### Functions (migration `009_graph.sql`)
```sql
-- Entry points by vector similarity (nodes with embeddings).
match_kg_nodes(query_embedding vector, kinds text[] DEFAULT NULL, match_count int DEFAULT 5, min_similarity real DEFAULT 0.0)
  RETURNS TABLE (node_id text, kind text, label text, summary text, doc_id text, similarity real)

-- Entry points by name/id text (trigram + exact key match; catches 'HX-204', 'WP09', 'Turbine Hall').
match_kg_nodes_text(query_text text, kinds text[] DEFAULT NULL, match_count int DEFAULT 5)
  RETURNS TABLE (node_id text, kind text, label text, summary text, doc_id text, score real)

-- k-hop neighbourhood of seed nodes, both edge directions, cycle-free, best first
-- (hop ascending, then path weight descending; see "Implemented"). path is the readable chain from the seed.
graph_neighborhood(seed_ids text[], max_hops int DEFAULT 2, rel_filter text[] DEFAULT NULL,
                   kind_filter text[] DEFAULT NULL, max_nodes int DEFAULT 40)
  RETURNS TABLE (node_id text, kind text, label text, summary text, doc_id text, hop int,
                 via_rel text, via_node text, path text[], weight real, evidence text)
```
`max_hops` is capped at 3 inside the function. `spot_mcp_reader` gets SELECT on both tables and EXECUTE on the functions.

**Implemented (pgvector agent, 2026-10-10).** Exact behaviour the MCP server can rely on:
- **Node ids and documents.** `node_id = '<kind>:<key>'`. Document-backed nodes carry `doc_id` (cite it) and reuse that
  document's vector: mission `event:mis-M001`, action `event:act-A0001`, anomaly `incident:AN001`, flag
  `incident:flag-14`, policy_rule `policy:battery_low`, manual `manual:p52-1` (= its node_id), operator_note `note:3`,
  explanation `explanation:5` (`doc_id` is NULL until ingest has made the document). The other kinds (asset, location,
  environment, action_type, failure_type, anomaly_type, topic) embed their `summary`.
- **Edge weights** (one rate-like scale, bigger = stronger): `fk` = 1/n, n = the edges of that rel into the same dst
  (`props.of`; one of WP05's 43 actions weighs 1/43, an action's own flag 1/1); `stat` `common_at` = rate = n/of;
  `fails_more_in` = overall rate − rate in that environment (n ≥ 3 actions; props n, rate, overall, failures);
  `curated` = 1/n, n = that src's targets for the rel (a rule `about` all 8 failure types links each at 0.125);
  `extract` (`mentions`) = hit count (props hits, terms). Stat edges carry `props.examples` (ids) and `evidence` = the
  first example's doc id.
- **`match_kg_nodes`**: like `match_documents`: similarity = 1 − cosine distance; NULL `kinds` = all; NULL
  `min_similarity` = 0; nodes without a vector are skipped; best first.
- **`match_kg_nodes_text`**: `score` in [0.3, 1]: **1.0** = exact match of the query on node_id, key or label ignoring
  case and punctuation (`HX-204`, `hx204`, `asset:HX-204`, `WP09`, `localization failure`); otherwise the best pg_trgm
  score capped at **0.99**: similarity / word_similarity of the query against label and key (`Turbine Hall` → WP08 and
  WP09 at 0.99), or a label/alphanumeric key found inside a longer query (strict_word_similarity ≥ 0.75, so
  `why does HX-204 keep failing in the pipe gallery?` → asset:HX-204 and location:WP05). Below 0.3 is dropped. NULL or
  blank query → no rows. Action labels contain their asset and WP ids, so `HX-204` returns asset:HX-204 (1.0) and then
  actions on it (0.99): pass `kinds` to narrow.
- **`graph_neighborhood`**: seeds that don't exist are ignored; NULL/empty `seed_ids` → no rows. `max_hops` NULL → 2,
  clamped to 0..3 (0 → no rows). `rel_filter` restricts the edges walked. **`kind_filter` filters the returned rows
  only**; the walk passes through every kind (so `kind_filter => '{manual,topic}'` from an action still reaches manual
  chunks via its failure type). `max_nodes` (NULL → 40) applies after `kind_filter`. Seeds are **not** returned as hop 0,
  but a seed reached from another seed is (hop ≥ 1), so the edge between two seeds still shows. One row per node: its
  shortest, then strongest path. `via_rel`/`via_node`/`evidence` = the last edge walked (`via_node` is the previous node
  on the path; the stored edge may point either way). **`weight` = the path's strength**, the product of the edge
  weights along it (hop 1: the edge's own weight), so a path through a hub ranks below one through a specific fact.
  `path` alternates node ids and steps, with the direction: `{action:A0061, -failed_with->,
  failure_type:LOCALIZATION_FAILURE, <-mentions-, manual:p52-1}` (`-rel->` walks src→dst, `<-rel-` dst→src);
  `array_to_string(path, ' ')` reads as a sentence. Order: hop ascending, weight descending, then semantic kinds
  (topic, manual, failure_type, anomaly_type, environment, action_type, policy_rule, location, asset, …) before
  instance kinds (anomaly, flag, mission, action), then node_id. Hop 1 always comes before hop 2, so a hub seed
  (a location has 10–43 actions) fills `max_nodes` with hop-1 actions: pass `kind_filter` to skip instance kinds.
- **Timing** (local Postgres, 577 nodes / 1,974 edges): `graph_neighborhood` 2 hops ≈ 2 ms (7 flag-context seeds ≈ 6 ms;
  worst case 4 hub seeds × 3 hops ≈ 90 ms); `match_kg_nodes_text` ≈ 12 ms (≈ 1 ms with `kinds`); `match_kg_nodes` ≈ 3 ms.

### Build (`db/graph_build.py`; run by `db/job.sh graph`, and by `db/job.sh refresh` after `ingest`, which `db-init` runs; with `--no-embed` if ingest failed)
Idempotent:
- upsert nodes;
- delete and re-insert the edges with `props->>'source' IN ('fk','stat','extract','curated')` (CHANGED 2026-10-10:
  `curated` added, so editing the curated maps takes effect; edges with any other source are never touched);
- delete nodes the build made (`props->>'built_by' = 'graph_build'`) that no longer exist (e.g. a deleted flag);
- embed node `summary` text via the embedding API (no document prefix), reusing `documents.embedding` for document-backed nodes.
  Only summaries whose text or `EMBED_MODEL` changed are re-embedded (`props.embedded_hash`); if the embedding
  service is down the graph is still written and those nodes are embedded on the next run (`--no-embed` skips it).
All of it is one transaction (readers see the old or the new graph), logged to `app.job_runs` (job `graph`, counts per
node kind and per rel) and each embedding batch to `app.model_calls`. ~1 s without embedding; ~20 s on CPU to embed the
~70 summary nodes the first time. Runtime nodes (new operator notes and explanations) are picked up on the next run.
**2026-10-11:**
- Flag summaries and `triggered` edges use only fired policy rules, never verdict rows.
- A node whose stored `props.source` is `live` is never overwritten by the build.
- Embedding runs batch by batch: if one batch fails, the vectors of the batches that succeeded are kept, and only the
  failed nodes stay pending.
- Baseline graph (seed + ingest): 574 nodes, all embedded (flag 58, manual 85, action 270, anomaly 40, mission 27,
  policy_rule 23, asset 20, location 15, topic 11, ...), about 1,968 edges.
- After a replay or `reset-demo`, run the build again (after `ingest --kinds incident`, §4). `reset-demo` deletes
  the non-live `flag`, `operator_note` and `explanation` nodes in the same transaction as the flags; their edges
  cascade. The rebuild links the new flags.

### Live rows (`props.source = 'live'`, ADDED 2026-10-11; `docs/backend-services-contract.md` "Live simulation")
The live simulation writes graph rows **as each stop is processed**, not through `graph_build.py`. Every live node has
`props.source = 'live'`, `props.built_by = 'live'`, `props.run_id`, `props.route_id`, `props.stop`; every live edge has
the same `source`, `run_id` and `stop`. The build never deletes them (its node delete is limited to
`built_by = 'graph_build'` and its edge rewrite to `fk | stat | extract | curated`).
- `mission:LIVE-<run>` (at claim) — the run.
- `action:LIVE-<run>-<stop>` (document-backed: `event:act-LIVE-<run>-<stop>`) `part_of` the mission, `at` its
  location, `inspects` its asset, `of_type` its action type, `failed_with` its failure type when it failed.
- `anomaly:LIVE-<run>-<stop>` when the stop's log has an anomaly: `of_type` anomaly type, `raised_on` asset, `during`
  the action.
- `flag:<flag_id>` on a red stop: `flags` the action, `triggered` each fired policy rule.
- `explanation:<assessment_id>` (red) or `explanation:live-<capture_id>` (green), document-backed, only for a real
  System 2 answer (never a template): `explains` the flag (red) or the action (green; an extension of the relation
  for live stops, which have no flag).
Edges to kit nodes that do not exist are skipped. Node summaries are embedded by the live runner through the
backend (`POST /live/runs/{id}/embeddings`); document-backed nodes reuse their document's vector, as in the build.
`graph_neighborhood` and `match_kg_nodes*` see them as soon as they are written (a kit flag's 2-hop neighbourhood
reaches live actions on the same asset). Known gap: MCP `get_flag_context` on a **live** flag has no `actions` row to
bundle (live actions are not kit actions), so its own packet reports the action as missing; the live console shows the
packet the backend built for that stop instead.

### MCP (`amd/mcp`)
- `get_flag_context` gains a bounded `graph` section (≤ ~300 tokens): the 1–2-hop neighbourhood of the flag's action, asset, location, failure_type/anomaly_type and triggered rules, with manual/topic links ranked first. Each fact is a readable triple (`WP09 —has_environment→ HIGH_NOISE`) cited by `node_id`/`evidence`. Every `id` in the section is unique and is not cited by another section: a lower-ranked fact citing an already-cited node is left out, because System 2 rejects a packet that repeats an id with different content (422 `conflicting content for duplicate id`). The whole packet stays ≤ 1,500 tokens.
- New tool `graph_neighborhood(entity, hops=2, k=20)`: `entity` is a node_id, or a name/id resolved with `match_kg_nodes_text` (falling back to vector).
- New tool `search_graph(query, k=5, hops=1)`: entry nodes by vector + text, then a 1-hop expansion.
- Both tools are logged in `app.mcp_tool_calls` like the others.
