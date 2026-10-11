# SPOT context MCP server (`amd/mcp`)

This server is where System 2 gets its context. System 2 is the ~9B LLM on the AMD box that explains each confirmed flag to operators. The server reads Neon Postgres and never writes to it:

- **RAG over pgvector** for text: past incidents, asset history, operator notes, Spot manual passages, policy rules and past explanations.
- **Graph RAG** over a knowledge graph in plain Postgres (`kg_nodes` / `kg_edges`, contract §7): which failures cluster at which waypoint, which action types fail more in which environment, which manual passages cover a failure type.
- **Plain SQL** for numbers: success rates, battery risk and mission health.

The binding contract is `docs/system2-rag-contract.md`. It defines the tables, `match_documents` / `match_documents_hybrid`, the graph functions (§7), the embedding API and the `app.*` logs.

Built on the official MCP Python SDK **2.x** (`mcp.server.mcpserver.MCPServer`). In SDK 2.x, `FastMCP` was renamed to `MCPServer`, and `mcp.server.fastmcp` no longer exists.

## Tools

The model should call `get_flag_context` first. That one call is usually enough. The other tools are for follow-ups, when the packet's `gaps` say something is missing.

| Tool | What it's for |
|---|---|
| `get_flag_context(flag_id)` | **Tier 1.** One packet (≤ ~1,500 tokens) holding:<br>• the flag and the rules that fired<br>• the event: the action with its reading vs the safe band, trust, anomaly, asset and waypoint, plus the validator's priority, category and suggested action. For a mission flag, the event is the mission instead.<br>• System 1 predictions<br>• derived facts `F1..Fn`<br>• `graph`: ≤ ~300 tokens of knowledge-graph triples around the flag (statistics and manual links first)<br>• similar incidents, asset history, manual passages and policy rules<br>• `allowed_actions`, `citation_ids`, `insufficient_context` + `missing`, `gaps`<br>• **the question, last** |
| `search_similar_incidents(flag_id?, text?, asset_id?, location_id?, k=3)` | Past incidents like this one (hybrid search). The flag's own docs are excluded. |
| `search_asset_history(asset_id, query?, k=5)` | Events, incidents and notes for one asset. With no query, no similarity cutoff is applied. |
| `search_spot_manual(query, k=3)` | Spot User Guide passages (vector search). |
| `search_operator_notes(query, asset_id?, k=5)` | Notes operators wrote on earlier flags. |
| `search_past_explanations(query, k=3)` | Earlier System 2 outputs, for consistency. |
| `search_policy(query, k=3)` | The triage rules (`policy:<rule>` docs). |
| `graph_neighborhood(entity, hops=2, k=20)` | Graph facts around one thing (asset, waypoint, environment, action/failure/anomaly type, rule, topic, manual page). `entity` is a node_id (`location:WP09`) or a name/id (`WP09`, `Turbine Hall South`), resolved by `match_kg_nodes_text`, then by vector. `hops` 1–3, `k` ≤ 30. |
| `search_graph(query, k=5, hops=1)` | Entry nodes by name/id **and** meaning (merged, deduplicated), then their facts within `hops`. `k` ≤ 10, `hops` 1–3. |
| `get_flag(flag_id)` | Flag row, reasons, operator acks/notes and any previous explanation. |
| `get_system1_forecast(action_id)` | Laya's pre-action `will_fail` / `failure_mode` / `anomaly_expected` vs the real outcome, plus its triage answers vs the validator. |
| `get_action(action_id)` | Action, reading with its safe band (from `policy.py`) and trust, anomalies, asset, waypoint and validator verdict. |
| `get_mission_summary(mission_id)` | Reported status vs true health. Says whether "COMPLETED" hides failures. |
| `get_reliability_stats(action_type?, location_id?, configuration_version?, battery_band?, group_by?)` | Success rate with n, a 95% CI and a small-sample warning (n < 20). |
| `get_battery_risk(battery_pct, configuration_version?)` | Policy status (≤15 / ≤25 / ≤35) and the kit's success rate in that battery band vs the other bands. |
| `list_allowed_actions(category)` | The fixed recommended-action enum for `equipment`, `data_quality`, `navigation`, `robot_health`, `mission`, `none` or `all`. |
| `get_raw_event_log(mission_id, around_ts?, limit=20)` | Verbatim SPOT events from `raw.spot_event_log`, so the model can quote the robot. |

**Citation ids.** Every item carries an `id` the model can cite:

- **Row ids:** `flag:12`, `action:A0136`, `reading:R0002`, `anomaly:AN002`, `asset:HX-204`, `location:WP07`, `mission:M014`, `note:<ack_id>`, `explanation:<id>`, `s1:<prediction_id>`, `log:<log_id>`, `stat:<filters>`.
- **Packet-local facts:** `F1`..`Fn`.
- **RAG docs:** the `doc_id`s (`incident:AN007`, `manual:p42-1`, `policy:battery_low`...).
- **Graph facts:** `{"id", "fact", "text"?, "ev"?}`. The `id` is one endpoint's `node_id` (`failure_type:LOCALIZATION_FAILURE`, `environment:HIGH_NOISE`, `topic:docking`), or its doc_id for document-backed nodes (`manual:p42-3`, `note:5`). `ev` is the edge's evidence id when it has one. Every id resolves to a `kg_nodes` row or a `documents` row.

`citation_ids` lists them all, so the client can build the grammar's `ids` enum.

**Graph RAG, how it's built.**
- **Seeds:** for a flag they are its action, asset, waypoint, failure/anomaly type and up to 4 triggered rules (`<kind>:<key>`). For a mission flag, they are the mission's top failure types and its worst anomaly.
- **Walks:** two 2-hop `graph_neighborhood` walks run in parallel, one for the structure and one for the linked manual pages and notes. Running them separately means manual chunks can't crowd stats out of the node cap.
- **Facts:** they are the stored `kg_edges` among the reached nodes (one parameterized SELECT). This catches edges between two seeds, which the traversal's one-row-per-node output can't show.
- **Ranking:** statistics (`common_at`, `fails_more_in`) come first, then manual links, topics and notes, then other structure. Facts touching this flag's own nodes come first within each tier.
- **Dedup and merge:** FK facts that the flag/event sections already state are dropped. Facts sharing an endpoint are merged (`Docking —mentions→ battery, docking`).
- **Budget:** the result is cut to `MCP_GRAPH_TOKEN_BUDGET` (300). When the packet is over 1,500 tokens, RAG tails are trimmed first, then the graph tail (never below 3 facts). flag, event, facts and question are never trimmed.
- **Degradation:** on a database without migration 009, the packet's `gaps` say `graph unavailable` and the graph tools return `graph_unavailable: true`; neither errors. With the embedding service down, the packet's graph section still works (it needs no embedding) and `search_graph` falls back to name/id matches.

**Safety.**
- Every parameter is typed and range-checked: the JSON schema checks first, then the service checks again.
- The SQL is parameterized only. `group_by` and filters map through a whitelist.
- Result sizes are capped.
- Every data query runs in `SET TRANSACTION READ ONLY` with a 5 s statement timeout.
- Matches below `MCP_MIN_SIMILARITY` are dropped. If nothing is left, the tool returns `no_relevant_results: true` and "Do not cite anything from this search".
- A hybrid row that matched an exact id (e.g. `PANEL-212`) is kept even when its similarity is low.

**Logging.**
- Every tool call writes one row to `app.mcp_tool_calls`: tool, args, result_count, result_bytes, top_similarity, latency_ms, ok, error, request_id, flag_id.
- Every embedding call writes one row to `app.model_calls` (`system='embed'`).
- Logs go over a separate connection to `LOGS_DATABASE_URL` and are fire-and-forget: a logging failure only produces a warning.
- `request_id` comes from the tool call's `_meta.request_id` or the `X-Request-Id` header.
- `SELECT * FROM app.get_mcp_tool_stats()` gives p50/p95 latency and mean top similarity, which is how you tune the cutoff.

## Run locally

```bash
cd amd/mcp
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt pytest anyio
cp .env.example .env            # fill in DATABASE_URL etc.
set -a && . ./.env && set +a

python -m spot_mcp                       # stdio (for MCP clients that spawn the server)
python -m spot_mcp --http --port 8090    # streamable HTTP at http://127.0.0.1:8090/mcp, health at /healthz

python -m pytest -q                      # unit tests (fake DB + fake embedder); integration tests skip without env
python client_example.py --list-tools    # the tools as OpenAI-style JSON
```

For a local database, use the other agent's `db/setup.py migrate && db/setup.py seed`, run against any pgvector Postgres (for example `docker run -p 5432:5432 -e POSTGRES_PASSWORD=pw pgvector/pgvector:pg17`). Then run `amd/embed`'s ingest to fill `documents`.

## Docker

```bash
docker build -t spot-mcp amd/mcp
docker run -p 8090:8090 --env-file amd/mcp/.env spot-mcp           # HTTP on :8090, HEALTHCHECK hits /healthz
docker run -i --rm --env-file amd/mcp/.env spot-mcp python -m spot_mcp --stdio   # stdio instead
```

- **Image:** python:3.12-slim, about 295 MB, runs as non-root user `mcp`, no torch.
- **Config:** comes only from env vars: `DATABASE_URL`, `LOGS_DATABASE_URL`, `EMBED_URL`, `EMBED_MODEL`, `EMBED_DIM`, `EMBED_QUERY_PREFIX`, `MCP_MIN_SIMILARITY`, `MCP_AUTH_TOKEN`...
- **`localhost` inside a container means the container itself.** Point `EMBED_URL` and `DATABASE_URL` at compose service names (e.g. `http://embed:8081`) or at `host.docker.internal`. Neon URLs work as-is.
- **Policy file:** the image only has `amd/mcp`, so it uses the vendored `spot_mcp/vendor/policy.py`. A unit test fails if that copy drifts from `amd/system1/triage/policy.py`. To refresh it: `cp amd/system1/triage/{policy,questions}.py amd/mcp/spot_mcp/vendor/`.

## Point it at Neon

1. Create a login role in the `spot_mcp_reader` group (`db/migrations/007_roles.sql`). That gives it SELECT on `public`, EXECUTE on the match functions and INSERT on `app.*`.
2. Use that role's connection string as `DATABASE_URL`. The pooled `-pooler` endpoint is fine: the server issues no session-level SETs and uses no prepared statements.
3. Leave `LOGS_DATABASE_URL` unset unless logs move to another database.
4. For `get_raw_event_log`, the role also needs `USAGE ON SCHEMA raw` and `SELECT ON raw.spot_event_log`. This is requested in contract §6.

## Deploy on Vercel

The same HTTP app as `python -m spot_mcp --http`, run as one Vercel Function. `asgi.py` exposes the top-level `app` (via `create_app()` in `spot_mcp/server.py`, the factory `main()` uses too), so auth, the open `/healthz` and the stateless JSON `/mcp` behave the same. Vercel's Python runtime runs the ASGI lifespan, which starts the SDK's session manager. Without it every `/mcp` request answers 500.

- **Project:** import the repo with **Root Directory `amd/mcp`**. The framework preset is "Python", detected from `requirements.txt`. No build command is needed.
- **Files:** `vercel.json` sets region `cle1`, `maxDuration` 60 s and `excludeFiles`. `.python-version` pins 3.12, the same as the Docker image. `.vercelignore` keeps any `.env`, keys, venvs, tests and `client_example.py` out of CLI uploads (a denylist, so it cannot drop needed files whichever directory Vercel anchors it to).
- **Region:** `cle1` (Cleveland) sits next to Neon's `aws-us-east-2`. If the Neon project moves, change `regions` to match. Put the backend project in the same region.
- **Env vars** (Project → Settings → Environment Variables; nothing is committed):

| Var | Value |
|---|---|
| `DATABASE_URL` | Neon **pooled** URL (`ep-…-pooler…`) as `spot_mcp_user`. Pooling is safe: every query is one transaction with `SET TRANSACTION READ ONLY` and a transaction-local `statement_timeout`, and prepared statements are off. |
| `MCP_AUTH_TOKEN` | **Required.** `asgi.py` refuses to start without it, because the URL is public. Callers send `Authorization: Bearer <token>`. |
| `EMBED_URL` | the AMD cluster's public HTTPS embedding endpoint (OpenAI-compatible `/v1/embeddings`) |
| `EMBED_MODEL` / `EMBED_DIM` | `Qwen/Qwen3-Embedding-0.6B` / `1024`. These must match what was ingested. |
| `EMBED_TIMEOUT_S` / `MCP_STATEMENT_TIMEOUT_MS` | defaults `10` / `5000`. Keep them well under `maxDuration`. |
| optional | `MCP_POOL_MAX` (default 4 per instance), `MCP_LOG_CALLS`, `LOGS_DATABASE_URL`, `MCP_MIN_SIMILARITY`, `LOG_LEVEL` |

- **Callers:** the backend and the orchestrator set `MCP_URL=https://<project>.vercel.app/mcp` and the same `MCP_AUTH_TOKEN`. Use the production domain. Preview URLs sit behind Vercel's Deployment Protection by default, so server-to-server calls to them get a login page.
- **Logging rows:** the `app.*` rows are fire-and-forget. After a response, Vercel may pause an idle instance before an insert finishes, so a few rows can be late or lost. Tool calls are not affected.

Verify:

```bash
curl -s https://<project>.vercel.app/healthz                    # {"ok":true,"server":"spot-context",...}, no token needed
curl -s https://<project>.vercel.app/mcp -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python -c 'import json,sys; print(len(json.load(sys.stdin)["result"]["tools"]), "tools")'   # 17 tools
```

A missing or wrong token gets `401 {"error":"unauthorized"}`. To try it locally the way Vercel runs it: `DATABASE_URL=... MCP_AUTH_TOKEN=x uvicorn asgi:app --port 8090`.

## On the AMD VM, next to llama-server

```bash
# 1. System 2 model with tool calling (--jinja is required for tools)
llama-server -m Qwen3.5-9B-Q4_K_M.gguf --jinja -c 16384 --port 8080 -ngl 99
# 2. Embeddings (amd/embed), e.g. llama-server --embedding on :8081 with Qwen3-Embedding-0.6B
# 3. This server
docker run -d --name spot-mcp -p 127.0.0.1:8090:8090 --env-file amd/mcp/.env \
  -e EMBED_URL=http://host.docker.internal:8081 --add-host=host.docker.internal:host-gateway spot-mcp
# 4. The worker loop (model -> MCP -> model), against the real model
MCP_URL=http://localhost:8090/mcp LLM_URL=http://localhost:8080 python amd/mcp/client_example.py --flag-id 12 --prefetch
```

`client_example.py` is how the model "uses" MCP:

1. List the MCP tools and convert them to OpenAI `tools` JSON. Titles are dropped and `Optional[X]` is flattened to `X` for llama.cpp's grammar converter.
2. Loop: model → `tool_calls` → MCP (`_meta.request_id` set) → result → model.
3. Do a final no-tools turn with a `json_schema` `response_format`. Its evidence `ids` enum is built from the returned citation ids and its `recommended_action` enum from `allowed_actions`.
4. Run a post-check (ids, action, numbers).

Options:
- `--prefetch` calls `get_flag_context` before the first LLM turn, which saves a round trip.
- `enable_thinking=false` is sent via `chat_template_kwargs`.

If the port is exposed beyond localhost, set `MCP_AUTH_TOKEN`. Clients then send `Authorization: Bearer <token>`; `/healthz` stays open.

## Register with an MCP client

```bash
# Claude Code, HTTP
claude mcp add --transport http spot-context http://localhost:8090/mcp --header "Authorization: Bearer $MCP_AUTH_TOKEN"
# Claude Code, stdio
claude mcp add spot-context -e PYTHONPATH=$PWD/amd/mcp -e DATABASE_URL=$DATABASE_URL -e EMBED_URL=$EMBED_URL -- python -m spot_mcp
```

For Claude Desktop, Cursor or other JSON-config clients:

```json
{"mcpServers": {"spot-context": {"command": "python", "args": ["-m", "spot_mcp"],
  "env": {"PYTHONPATH": "/abs/path/amd/mcp", "DATABASE_URL": "...", "EMBED_URL": "http://localhost:8081"}}}}
```

## Tuning

- **`MCP_MIN_SIMILARITY`** (default 0.40): Qwen3-Embedding similarities run on a different scale from older models. Check `mean_top_similarity` in `app.get_mcp_tool_stats()` and run a few junk queries, then set the cutoff between them.
- **`MCP_PACKET_TOKEN_BUDGET`** (default 1500): when the packet is too big, the lowest-value retrieved items are dropped first, then snippets are shortened.
- **`MCP_GRAPH_TOKEN_BUDGET`** (default 300): the packet's `graph` section.
- **`MCP_GRAPH_MIN_SIMILARITY`** (default: `MCP_MIN_SIMILARITY`) is the cutoff for vector entry nodes in the graph tools. **`MCP_GRAPH_TEXT_MIN_SCORE`** (default 0.5) is the cutoff for `match_kg_nodes_text`. An exact name/id scores 1.0; incidental trigram hits on real data score ~0.33–0.37.
