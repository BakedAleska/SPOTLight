# Backend + System 1 service contract

Companion to `docs/system2-rag-contract.md` (Neon tables, embedding API, MCP). **Change this file first, then code.**

## Services and ports (one container each; see CLAUDE.md "Engineering rules")
| Service | Folder | Port | Owner |
|---|---|---|---|
| Backend API (FastAPI) | `backend/` | 8000 | backend agent |
| System 1: Laya inference | `amd/system1/service/` | 8082 | System 1 service agent |
| Embedding service | `amd/embed/` | 8081 | pgvector agent |
| MCP server | `amd/mcp/` | 8090 | MCP agent |
| System 2 (LLM explainer, `/v1/assess`, contract `2026-10-10.backend.2`) | `amd/system2/` | 8083 | teammate |
| Vision / OCR (code readers, `/v1/vision`, `/v1/ocr`; requires `VISION_API_KEY`; contract `docs/vision-contract.md`) | `amd/vision/` | 8084 | vision agent |
| Orchestrator (LangGraph: assess + ask graphs; contract `docs/orchestrator-contract.md`) | `amd/orchestrator/` | 8085 | orchestrator agent |
| Model gateway (LiteLLM: `system1` / `system2` / `system2-vision` / `embed`) | `amd/gateway/` | 4000 | gateway agent |
| llama-server (System 2's model; the gateway's `LLAMA_URL` upstream, never in compose) | n/a | 8080 (tunnel 18080) | teammate |
| Web console (Next.js dev server, `node:24` image; reads the backend via `BACKEND_URL`) | `web/` | 3000 | web agent |
| Postgres + pgvector (local stand-in for Neon) | `db/` | 5433 | pgvector agent |

Inside docker-compose, services reach each other by service name (`http://system1:8082`), never `localhost`.

## System 1 service (Laya), port 8082
Loads one Laya checkpoint and answers the typed questions in `amd/system1/triage/questions.py` for canonical events
(the dicts built by `amd/system1/triage/events.py`: `pre_action_event`, `action_event`, `mission_event`). It renders
each event with `amd/system1/triage/render.py` `render_json`, the deterministic rendering the model was trained on, so
callers send structured events, not text.

```http
POST /v1/predict
{"request_id": "r-123",
 "items": [{"id": "act-A0123", "kind": "action", "event": { ...canonical event dict... }}]}
→ 200
{"model": "<checkpoint id>", "fine_tuned": true, "device": "cuda:0", "latency_ms": 41,
 "items": [{"id": "act-A0123", "kind": "action",
            "answers": {"escalate": {"answer": "A", "probs": {"A": 0.83, "B": 0.17}, "confidence": 0.83},
                        "priority": {"answer": "P2", "probs": {"P1": 0.1, "P2": 0.7, "P3": 0.15, "P4": 0.05}, "confidence": 0.7},
                        "...": {}}}]}
```
- `kind` ∈ `pre_action | action | mission`. Questions come from `QUESTION_SETS[kind]`. An optional `questions: [...]` subset is allowed.
- Probabilities are temperature-calibrated with the checkpoint's own temperatures (fitted during fine-tuning).
- Batch up to 64 items per call; 422 on a malformed event, naming the field.

```http
GET /v1/questions   → {"questions": QUESTIONS, "question_sets": QUESTION_SETS}
GET /v1/model       → {"checkpoint", "fine_tuned", "temperatures", "training_summary"?, "device"}
GET /health         → {"ok": true, "model_loaded": true}
```

| Env var | Meaning | Default |
|---|---|---|
| `LAYA_CHECKPOINT` | HF repo id or local dir (a fine-tuned `best/` mounted at `/models/laya`) | `convaiinnovations/laya` (compose: `Jyler/KnightHacks-Laya-Model`) |
| `LAYA_CHECKPOINT_SUBDIR` | folder inside the HF repo holding the checkpoint; only `<subdir>/*` is downloaded (into `HF_HOME`, cached across restarts). Ignored for a local dir | unset (compose: `run4_B_lora_lr4/best`) |
| `LAYA_FALLBACK_CHECKPOINT` | loaded when the primary checkpoint can't be downloaded or loaded (e.g. a private repo with no `HF_TOKEN`); logged as a warning, and `/v1/model` then reports the fallback with `fine_tuned: false`. Empty = fail instead | `convaiinnovations/laya` |
| `HF_TOKEN` | read token for a private checkpoint repo | unset |
| `LAYA_DEVICE` | `auto` / `cpu` / `cuda` | `auto` |
| `SPOT_AMP` | `1` = fp16 autocast on GPU, `0` = fp32 | `1` |
| `LOGS_DATABASE_URL` | optional: log every call to `app.model_calls` (system=`system1`) | unset (no logging) |
| `PORT` | | `8082` |

Image: `TORCH_VARIANT` build arg (`cpu` | `rocm-gfx1151` | `rocm-gfx1201`), same as the embedding image. The build context
is the repo root, so the image can COPY `amd/system1/triage/`. Laya is pinned to `laya==0.4.1`.

## Backend API (FastAPI), port 8000
The route tree agreed with the user (versioned under `/api/v1`):
`events` (POST ingest, batch, GET feed) · `replay` (start/stop/status, + step in stepped mode) · `flags` (list, get, context, explain, ack) ·
`missions` (list, get, timeline, debrief) · `system1` (predict proxy, forecast) · `assets` · `locations` ·
`insights` (overview, reliability) · `search` (RAG) · `ops` (model-call stats, jobs, reset-demo) · `ask` (orchestrator proxy) ·
`chat` (SPOTbot: conversations CRUD, models, suggestions, missions picker and mission chats (`chat.3`), SSE messages; `docs/chat-contract.md`) · `/health`.

Ingest pipeline for `POST /api/v1/events`:
1. Append verbatim to `raw.spot_event_log`.
2. Normalize into `events`.
3. Build the canonical event.
4. Call System 1 `/v1/predict`.
5. Store the answers in `system1_predictions`.
6. Run the deterministic validator (`amd/system1/triage/policy.py`) according to `TRIAGE_MODE`:
   - `serial` (default; the team's design): Laya flags, then the validator confirms. Only flags Laya raises AND the rules agree with escalate.
   - `parallel` (the research recommendation, `research/tech-cascade-alarm-mgmt-2026-10-10.md`): both run, and either can escalate. Rules set a priority floor that Laya can raise but never lower, so a Laya miss on a CRITICAL asset is still caught.
   In both modes the final priority is consistent with the decision (audit M1, 2026-10-10): an escalated flag is
   always P1 or P2 (a P3/P4 priority is raised to the rules' priority if that is P1/P2, else to P2), and a
   non-escalated event is always P3 or P4 (a P1/P2 priority is capped at P3). Severity and score follow the final
   priority (P1 CRITICAL, P2 HIGH, P3 MEDIUM, P4 LOW).
   Store both verdicts on the flag (`reasons`) either way, so the UI can show "Laya: escalate (0.83) · rules: confirmed".
7. Write a `flags` row when escalated.

Every step logs to `app.*` with one `request_id`.

| Env var | Meaning | Default |
|---|---|---|
| `DATABASE_URL` / `LOGS_DATABASE_URL` | Neon (or local) | none / `DATABASE_URL` |
| `SYSTEM1_URL` | Laya service | `http://localhost:8082` |
| `EMBED_URL`, `EMBED_MODEL`, `EMBED_QUERY_PREFIX` | for `/search` and for embedding new operator notes | see RAG contract |
| `MCP_URL` | MCP server (streamable HTTP), for `/flags/{id}/context` | `http://localhost:8090/mcp` |
| `SYSTEM2_URL` | System 2 `/v1/assess`; if unset or down, `/explain` returns a templated explanation from the flag's reasons | unset |
| `KIT_DIR` | kit data for `/replay` | `amd/system1/data/kit` |
| `TRIAGE_MODE` | `serial` or `parallel` (see step 6) | `serial` |
| `SYSTEM1_TIMEOUT_S` / `EMBED_TIMEOUT_S` / `MCP_TIMEOUT_S` / `SYSTEM2_TIMEOUT_S` | per-call timeouts | `5` / `10` / `15` / `60` |
| `SEARCH_EMBED_TIMEOUT_S` | `/search` waits this long for the query embedding, then falls back to full text (CPU embedder ~2.3 s) | `4` |
| `MCP_AUTH_TOKEN` | sent as `Authorization: Bearer …` when the MCP server requires it (always on Vercel, `https://<mcp-project>.vercel.app/mcp`; `amd/mcp/README.md`) | unset |
| `SYSTEM2_API_KEY` | sent as `X-API-Key` to System 2 | unset |
| `EMBED_DIM`, `EMBED_DOC_PREFIX` | checked on every embedding / prepended to operator-note documents | `1024` / empty |
| `REPLAY_SPEED`, `REPLAY_MAX_GAP_S` | replay pace (robot seconds per wall second; cap per wait) | `60`, `2` |
| `BREAKER_COOLDOWN_S` | after a failed call, skip that service (instant fallback) this long | `10`; the effective cooldown is max(this, the service's timeout) (audit H1) |
| `SYSTEM1_INGEST_TIMEOUT_S` | per-event System 1 timeout on the ingest/replay path (fail fast to rules-only); `SYSTEM1_TIMEOUT_S` is for explicit batch calls (forecast, `/system1/predict`) | `SYSTEM1_TIMEOUT_S` |
| `TRIAGE_DIR` | folder holding the `triage` package | `amd/system1` |
| `ADMIN_TOKEN` | shared secret for the admin endpoints (header `X-Admin-Token`, see "Admin endpoints"); unset = those endpoints answer 503 | unset |
| `REQUEST_DEADLINE_S` | overall deadline for `/flags/{id}/context` and `/flags/{id}/explain`, covering every downstream call (see "Request deadline") | `45` |
| `ORCHESTRATOR_URL` | LangGraph orchestrator (`amd/orchestrator`, :8085). Set -> `/explain` asks it first and `/ask` proxies to it (see "Orchestrator path"); unset -> `/explain` behaves exactly as without it and `/ask` answers 503 | unset |
| `ORCHESTRATOR_API_KEY` | sent as `X-API-Key` to the orchestrator | unset |
| `ORCHESTRATOR_TIMEOUT_S` | per-call cap for orchestrator calls; `/explain` also caps it at the deadline minus the reserve (45 - 3 = 42) | `42` |
| `CORS_ORIGINS` | comma-separated browser origins allowed by CORS (the dashboard's origin; `*` is not the default) | `http://localhost:3000` |
| `DB_POOL_MAX`, `LOG_REQUESTS` | | `10`, `1` |
| `SERVERLESS` | `1` = no per-process assumptions (see "Serverless deployment"): pool `min_size` 0 + a connection check on checkout, pending log writes awaited before the response. Unset = on when Vercel's `VERCEL` env var is set | auto |
| `DB_POOL_MIN` | pool connections kept open | `1` (`0` when serverless) |
| `REPLAY_MODE` | `background` (asyncio task, Docker) or `stepped` (state in `replay_runs`, driven by `POST /replay/step`) | `background` (`stepped` when serverless) |
| `REPLAY_STEP_BUDGET_S` | default time budget of one `POST /replay/step` | `10` |
| `GEMINI_API_KEY` | Google AI Studio key for `/api/v1/chat` (header `x-goog-api-key` only; never logged or returned). Unset -> `/chat/models` `configured: false`, `POST .../messages` 503 `gemini_unset` | unset |
| `GEMINI_BASE_URL` | Gemini API root | `https://generativelanguage.googleapis.com/v1beta` |
| `CHAT_MODELS` | comma-separated model ids, in picker order (unknown ids work with a derived label). Gemma (`gemma-4-31b-it`) is opt-in | `gemini-3.8-flash,gemini-3.5-flash,gemini-3.5-flash-lite` |
| `CHAT_DEFAULT_MODEL` | default model (first of `CHAT_MODELS` if not listed there) | `gemini-3.8-flash` |
| `CHAT_TITLE_MODEL` | one `generateContent` call (6 s, `thinkingLevel: minimal`, 40 output tokens) for a 3-6 word title after the first answer; empty/`none` = auto titles only | `gemini-3.1-flash-lite` |
| `CHAT_TIMEOUT_S` | whole Gemini streaming call | `120` |
| `CHAT_RETRIEVAL_BUDGET_S` | retrieval deadline before the call (embed capped at min(`SEARCH_EMBED_TIMEOUT_S`, 60 %)) | `8` |
| `CHAT_HISTORY_MESSAGES` | stored messages sent as plain history turns (never as evidence) | `12` |

### Serverless deployment and stepped replay (ADDED 2026-10-10; additive)
The backend also runs as a Vercel Python function (Root Directory `backend/`, entry `backend/index.py`, config
`backend/vercel.json`; runbook: `backend/README.md` "Deploy on Vercel"). Instances there are not sticky and can be
frozen between requests, so no state that must survive may live only in process memory. Docker Compose is unchanged
(`background` replay).
- **Shared code.** Vercel bundles only the Root Directory, so the build command `python3 vercel_build.py` copies
  `amd/system1/triage/{__init__,events,kit,policy,questions,render}.py` and `amd/system1/data/kit/` into
  `backend/_shared/amd/system1/` (git-ignored, never committed). `TRIAGE_DIR`/`KIT_DIR` defaults use the repo copy
  when it exists (local, Docker, CI) and `_shared` otherwise, so the drift guard keeps testing the one canonical copy.
- **Stepped replay** (`REPLAY_MODE=stepped`). Same `POST /replay/start` body and `GET /replay/status` /
  `POST /replay/stop` shapes; the status adds `mode: "stepped"`, `cursor` and `next_due_in_s`. The run lives in table
  `replay_runs` (`db/migrations/010_replay_runs.sql`): the selected `event_ids` (fixed at start), `next_index`,
  `counts`, `last_event`, `state` (`running | stopped | finished | error`) and a lease. At most one run is `running`
  (partial unique index), so a second start is 409 whichever instance gets it.
- `POST /api/v1/replay/step {"run_id"?, "budget_s"? (0.5-60, default REPLAY_STEP_BUDGET_S), "max_events"? (1-500,
  default 100)}` (admin): takes the run's lease, ingests the events that are due, stops at the budget or at
  `max_events`, and returns the status plus `step: {"ingested", "elapsed_ms", "busy", "done"}`. An event is due when
  its paced offset (the background mode's gaps: robot time / `speed`, each capped at `max_gap_s`; `speed=0` = all
  due at once) is at most the time since `started_at`. `next_due_in_s` says when the next event is due. A step that
  finds the lease held by another step returns at once with `busy: true` (no error). With no running run it returns
  the last run's status with `done: true`. 409 `replay_mode` in `background` mode; 503 without a database.
- **Idempotent.** `next_index` and `counts` are written after every event, and only by the lease holder; a step
  killed mid-run loses at most the event in flight, which the next step re-ingests under the same request id
  `rp<run_id>-<event_id>` (raw log `ON CONFLICT DO NOTHING`, events/flags upserts). A lease expires after the step's
  budget + 30 s, so a frozen or killed instance never blocks the run for long. `reset-demo` and `stop` end the run.
- **Driving it.** The client calls start, then `step` in a loop (sleep `min(next_due_in_s, 2)` when it is > 0) until
  `state != "running"`. Today the driver is the `curl` loop in `backend/README.md` (the web app has no replay
  control); no process has to stay alive between calls, but the replay only advances while something calls `step`.
- **Per-instance state (documented, not shared):** circuit breakers, the System 1 `busy` guard, the column cache,
  `ops/model-calls` `routing.since_start` counters, `/health`'s `replay` (the last status this instance saw) and the
  ACTION_STARTED detail cache for actions the kit lacks. Each instance learns them on its own; none is needed for
  the stored data to be correct.

### Web console consumption (updated 2026-10-10)
The Next.js console (`web/`, :3000) opens no database connection and calls no model service.
The browser polls `/api/dashboard` every 10 seconds; `web/src/lib/backend.ts` makes server-side,
uncached reads of `/insights/overview`, `/flags?status=OPEN&limit=500`, `/assets`, `/locations`,
and `/insights/reliability?group_by=action_type` (the success-by-inspection-type chart). Backend flag order and scores are preserved.
Overview cards supply mission counts, hidden failures, battery success and matched-battery V2 gain.
Flag detail, explanation and action routes proxy to FastAPI, controlled by `FEATURE_FLAG_DETAILS`,
`FEATURE_FLAG_EXPLANATIONS` and `FEATURE_FLAG_ACTIONS`. These switches are not authentication.
`BACKEND_URL` includes `/api/v1` (Docker default `http://backend:8000/api/v1`, overridden by
`WEB_BACKEND_URL`); `EXPLAIN_TIMEOUT_MS` defaults to 50000. Neither reaches the browser.
The server renders the initial dashboard and `/api/dashboard` uses the same loader. Backend failure
returns explicitly labeled challenge-kit data (`source: "kit"`, `degraded` message), with details,
actions and explanations disabled so sample IDs never reach live flag routes. Successful backend
reads return `source: "backend"`; polling can recover automatically. `/api/health` checks frontend liveness.

### Admin endpoints (ADDED 2026-10-10, audit H3)
`/api/v1/ops/*`, `/api/v1/replay/*`, `POST /api/v1/events` and `POST /api/v1/events/batch` change or wipe state, so
they require the header `X-Admin-Token: <ADMIN_TOKEN>` (compared in constant time).
- `ADMIN_TOKEN` unset → `503 {"error": "admin_token_unset"}`: the endpoints stay closed, never open. There is no bypass.
- Header missing or wrong → `401 {"error": "unauthorized"}`.
- Everything else (`GET /events` feed, flags incl. ack/explain, missions, search, insights, `/health`) stays open.
- CORS allows only `CORS_ORIGINS` (default `http://localhost:3000`). A dashboard that drives the replay or the demo
  reset sends `X-Admin-Token` itself (the presenter enters it); it is not baked into a public bundle.

### Request deadline (ADDED 2026-10-10, audit M4)
`/flags/{id}/context` and `/flags/{id}/explain` each finish within `REQUEST_DEADLINE_S` (default 45 s), whatever the
per-call timeouts (`MCP_TIMEOUT_S` applies to each of 3 JSON-RPC posts; `SYSTEM2_TIMEOUT_S` to System 2).
- `/context`: if MCP has not answered in time, the SQL packet is returned (`source: "sql_fallback"`,
  `fallback_reason: "deadline: ..."`, first `gaps` entry says so). Its SQL lookups get the remaining time; any not
  finished are listed in `gaps`.
- `/explain`: the context step gets at most 40 % of the deadline (then the SQL packet), System 2 gets the rest minus a
  small reserve; on expiry the template is returned (`source: "template"`, `fallback_reason: "deadline: ..."`).
- If the flag row itself cannot be read in time: `504 {"error": "deadline_exceeded"}`. Never a hang.

### Stored explanations (ADDED 2026-10-10, audit M10)
`assessments.state`: `DONE` = a System 2 explanation (final; returned from cache unless `refresh=true`).
`TEMPLATE` = the templated fallback (`model = 'template'`), and also (ADDED 2026-10-10, audit A-8 / P-5 for this
case) a System 2 reply that is System 2's own checked template rather than a model answer: `path` `fallback` or
`insufficient_context`, or `model` `template`. Such a reply is returned with `source: "system2_template"` and a
`fallback_reason`, stored `TEMPLATE` and never indexed. (`path: stub` replies are still stored `DONE`.) A `TEMPLATE` row is not final: when `SYSTEM2_URL` is set,
the next `/explain` retries System 2 (and replaces it on success); when unset, the stored template is returned
(`cached: true`). Template rows are never indexed as `explanation` RAG docs (RAG contract §4).

### Orchestrator path (ADDED 2026-10-10; additive, opt-in with `ORCHESTRATOR_URL`)
When `ORCHESTRATOR_URL` is set, `/flags/{id}/explain` asks the orchestrator first (`docs/orchestrator-contract.md`)
and falls back to the existing path when it is down or has no context. Unset, nothing below applies and `/explain`
is unchanged.
- After the cache check, the backend calls `POST {ORCHESTRATOR_URL}/v1/assess {"flag_id", "request_id", "refresh"}`
  (+ `image_b64`, `image_kind` when the caller sent them) with `X-API-Key: ORCHESTRATOR_API_KEY`, within
  `min(ORCHESTRATOR_TIMEOUT_S, deadline - reserve)`. The orchestrator's own deadline is `ORCH_DEADLINE_S` = 40 s, so
  it answers before the backend's 45 s `REQUEST_DEADLINE_S` and leaves time to store the result.
- Its answer is used when `context_source != "none"` and `answer` is non-empty. The response is the usual
  `Explanation` plus `orchestrator: {route, route_reasons, trace, system1, vision, elapsed_ms}` (the graph trace
  and the `V#` vision rows for the UI). `source` is `system2` (stored `DONE`, indexed as an `explanation` RAG doc),
  `system2_template` (System 2 answered with its own checked template), `template` or `system1` (all three stored
  `TEMPLATE`: not final, never indexed).
- Otherwise (connection error, 5xx, timeout, breaker open, `context_source: "none"`, no answer) the existing
  MCP -> System 2 -> template path runs with the time left, and the response carries
  `orchestrator: {"error": "<why>"}`. A 4xx from the orchestrator (e.g. a 422 image) does not open its breaker.
- A stored `TEMPLATE` row is retried when either `SYSTEM2_URL` or `ORCHESTRATOR_URL` is set.
- `Explanation.source` ∈ `system2 | system2_template | template | system1` (`system1` = the orchestrator's short System 1 answer for a flag
  with no escalation signal). A cached `TEMPLATE` row reads back as `template`.
- Optional request body on `POST /flags/{id}/explain`: `{"image_b64": "<base64 or data: URL>", "image_kind":
  "thermal|gauge|spectrogram|acoustic_imager|orbit_screenshot|visual"}` (at most 12,000,000 characters; 422 if
  not base64). An image bypasses the cache (an operator photo is a new question) and is forwarded only to the
  orchestrator (`pairing: "linked"`, route reason `operator_image`). Without `ORCHESTRATOR_URL` it is ignored and the
  response carries `warning: "image ignored: ORCHESTRATOR_URL is not set"`.
- `POST /api/v1/ask {"question" (2-2000 chars), "flag_id"?, "max_steps"? (1-4)}` proxies to the orchestrator's
  `/v1/ask` (bounded read-only MCP tool loop) and returns its body unchanged. `citations` can be `[]` with
  `cited: false`; `retrieved` then lists every id the tools returned (show it as "consulted, not cited"). `503 {"error": "orchestrator_unset"}`
  when `ORCHESTRATOR_URL` is unset; `503 {"error": "orchestrator_unavailable"}` when it is down. Open like `/explain`.
- `GET /health` adds `orchestrator` (`unset | up | down | degraded`, plus `breaker`).
- `GET /health` `system2` adds `model_status` (`up | down | unknown | stub`, ADDED 2026-10-11) from System 2's
  `/health`; `system2.status` is `degraded` when `model_status` is `down` (System 2 answers, but only with its template).
- `app.model_calls`: orchestrator calls are logged as `system = 'system2'` (the table's CHECK allows
  system1/system2/embed) with `endpoint = 'orchestrator:/v1/assess'` (or `/v1/ask`) and `metadata.via =
  'orchestrator'`, `route`, `source`, `system2_path`.

### Live simulation (ADDED 2026-10-11; additive. Plan: `docs/LIVE-SIM-PLAN.md`, runbook: `docs/LIVE-RUNBOOK.md`)
An operator queues one of five routes on the web console (`/live`); the **live runner** (`scripts/live_runner.py`, on
the presenter laptop, the only machine that reaches the AMD box) claims it and walks its 8 stops. Vercel never talks to
the box: the runner calls Laya, vision, System 2 and the embedder through SSH tunnels and posts every result here.
Tables: `live_runs`, `live_captures` (`db/migrations/013_live.sql`). Routes: `backend/app/data/live_routes.json`
(each stop = a kit action, `source_action_id`, with the rules' `planned_verdict`).
- **What is real.** Each stop's robot log is the kit row of `source_action_id`, re-sent now as a new event under a
  fresh action id `LIVE-<run_id>-<stop>` (mission `LIVE-<run_id>`), so a re-run never updates an earlier run's flags or
  predictions. Values are never invented. The photo is a kit sample image picked by action type (`photo.label =
  "simulated capture"`); vision rows are `pairing: "demo"` and never judge the asset. Red/green is the combined
  decision of Laya and the rules in `TRIAGE_MODE=parallel` (either escalates; the rules' priority is a floor).
- **Public** (the console polls every 2 s): `GET /live/routes`; `GET /live/runs?limit` (newest first, with
  `red`/`green` counts); `GET /live/runs/{run_id}` (`{run, route, captures}`); `GET /live/captures?run_id&after`
  (captures updated after the cursor, plus a new `cursor`); `GET /live/kg?run_id` (live KG nodes/edges of the run and
  graph totals); `GET /live/preview?route_id` (every stop's canonical action event + rules verdict; no writes).
- **Admin** (`X-Admin-Token`): `POST /live/runs {route_id, requested_by}` queues a run (`409 live_conflict` while one
  is pending/running; a run with no progress for 10 min is marked `error` and stops blocking);
  `POST /live/runs/{id}/cancel`. The runner: `POST /live/claim {runner}` (oldest pending -> running, logs
  `MISSION_STARTED`; `{run: null}` when idle) -> per stop `i`:
  1. `POST /live/runs/{id}/stops/{i}/start` logs `ACTION_STARTED` (no Laya call) and creates the capture
     (`phase: arrived`, the map shows SPOT there). Returns `action_event`, the canonical action event the result will
     be triaged on; the runner sets its `timestamp` to the result time.
  2. The runner calls Laya `/v1/predict` with that event (kind `action`).
  3. `POST .../stops/{i}/result {timestamp, laya}`: `laya` is `{status: "ok", answers, model, latency_ms, device,
     fine_tuned}` or `{status: "unavailable", error}` (rules-only, `laya_unavailable`). The 7-step ingest runs with
     these answers instead of calling `SYSTEM1_URL` (`IngestPipeline.ingest(..., laya=...)`; the Laya call is logged to
     `app.model_calls` with `metadata.via = "live_runner"`). Writes `raw.spot_event_log` (source `live`), `events`
     (`mission_id` NULL: live missions are not kit missions; the live ids are in `payload.live`/`payload.mission`),
     `system1_predictions`, `flags` (red only), the capture (`phase: triaged`, verdict, Laya, rules, decision), the
     event document `event:act-LIVE-<run>-<stop>` and the live KG rows. Returns `system2_request` (the System 2
     `/v1/assess` body: a packet in the MCP `get_flag_context` shape, `packet_version: "1-live"`; `flag_id` is the
     flag for a red stop, the `capture_id` for a green one, which has no flag), the photo, and `embed`: texts to embed.
     The packet carries no `gaps`: System 2 treats any gap as insufficient context and answers its template without
     calling the model, so the live provenance note ("kit row ... re-sent now; the photo is a simulated capture") is
     the last ordinary, cited fact `F<n>`.
  4. The runner (in the background while SPOT walks on) sends the photo to vision, adds the `V#` rows and OCR to
     `context` and calls System 2 with `image_b64`; then `POST .../stops/{i}/analysis {photo{path, sha256, bytes},
     vision | vision_error, system2_request, system2 | system2_error, system2_ms, image_status, timings}`. A System 2
     answer on a model path is stored as `qwen_source: system2` (assessment `DONE` for a red stop, `explanation:<id>`
     document + KG node); otherwise the backend builds the checked template (`qwen_source: template`, assessment
     `TEMPLATE`, never indexed) and says why in `qwen.fallback_reason`. Returns `embed` for the explanation.
  5. `POST /live/runs/{id}/embeddings {model, items[{key, text, vector}]}`: `key` is `doc:<doc_id>` or
     `node:<node_id>`; a vector is stored only if the row's stored text equals `text` and it has `EMBED_DIM` values.
     Document-backed KG nodes take their document's vector.
  `POST /live/runs/{id}/finish {status: done|error, error}` logs `MISSION_COMPLETED` (stored, not triaged) and sets
  `counts` (`red`, `green`, `qwen`, `template`, `laya_live`, `stops`); it and `cancel` mark captures whose analysis
  never landed `qwen_source: "error"`. Errors: `404 live_not_found`, `409
  live_conflict` (e.g. a stop on a run that is not running, so a cancelled run stops the runner).
- **Ingest additions** (all additive): `details.source_action_id` makes an unknown action id inherit that kit row;
  `details.reading` / `details.anomaly` (kit rows) are used instead of looking them up by action id; `details.mission`
  is the mission record for a live event; `details.live` marks a live event.

### Backend details (ADDED 2026-10-10 by the backend agent; additive)
- **`flags.reasons`** is a list, verdicts first: `{"rule": "laya_verdict", "verdict", "confidence", "priority",
  "category", "model", "detail"}` (or `{"rule": "laya_unavailable", "detail"}` when System 1 is down), then
  `{"rule": "rules_verdict", "verdict", "priority", "category", "policy_version", "agreement", "triage_mode",
  "detail": "Laya: escalate (0.83) · rules: confirmed"}`, then one `{"rule", "detail", "weight", "priority"}` per
  fired policy rule. Policy rule names are exactly `db/policy_rules.py`'s (the `policy:<rule>` RAG doc ids);
  `backend/tests/test_validator.py` checks this on all 297 kit events. `agreement` ∈ `confirmed | rules_rejected |
  rules_only_escalate | both_routine | laya_unavailable`.
- Flags upsert on `flags.triage_event_id` (`act-<action_id>` / `mis-<mission_id>`) and also fill `priority` and
  `category`. A re-ingested event that no longer escalates keeps its row with `escalated = false`.
- `ACTION_STARTED` becomes a `pre_action` event: Laya's forecast (`will_fail`, …) is stored in
  `system1_predictions`, but it is never validated or flagged. `MISSION_STARTED` is stored, not triaged.
- `events.payload` = the event's `details` + the joined `reading`/`anomaly` (same shape as `db/setup.py seed`)
  + `enriched` (action/location/asset) + `triage` (the verdict summary, incl. `request_id` and `flag_id`).
- `raw.spot_event_log.sha256` = sha256 of the event serialized like a replay.ndjson line, so it matches the seed's
  line hashes. The streamed replay logs under `source = 'replay'` (the seed's file import is `'replay.ndjson'`).
- `request_id`: from `X-Request-ID` or generated (`r-<12 hex>`); batch events get `<id>-<nnn>`, replay events
  `rp<run>-<event_id>`. It is forwarded to System 1 (body + header), System 2, the embedder and MCP
  (`X-Request-ID` and `_meta.request_id`), and written to `app.model_calls`, `app.request_log`,
  `events.payload.triage.request_id` and `raw.spot_event_log.request_id` (**needs the additive column, RAG
  contract §6**; written only when it exists).
- **System 1 load and logging (integrator, 2026-10-10, audit H2/M2).** Laya serves one request at a time. While an explicit batch call (forecast, `/system1/predict`) is in flight, ingest does not queue behind it: the event gets the rules-only verdict with `system1.status = laya_unavailable`, error `busy: ...`. A live forecast asks Laya for at most 5 actions per request; the rest get the empirical baseline. On the AMD GPU a batch takes well under a second, so this only matters on CPU. `app.model_calls` holds **one row per successful System 1 call, written by the System 1 service** (the true model latency). The backend writes `system='system1'` rows only for failed calls (`ok=false`, `metadata.caller='backend'`).
- **Authoritative System 2 HTTP contract (2026-10-10.backend.1; current version `2026-10-10.backend.2` adds the
  optional multimodal inputs in "System 2 multimodal inputs" below, everything here still holds):** `POST /v1/assess` requires
  `flag_id` (strict positive integer; booleans rejected), `flag` (object), and `context` (object).
  Optional fields are `request_id` (string/null), `event` (object/null), and `reasons` (list of strings/null entries).
  Additive input keys are ignored; DB rows need no legacy timestamp/kind/mission_health fields.
  Consumed context fields: `question` string, `allowed_actions` code-to-description object or code list,
  `suggested_action` allowed code, `citation_ids` string list, `missing`/`gaps` string lists,
  `insufficient_context` boolean; evidence sections `facts`, `similar_incidents`, `asset_history`, `manual`,
  `policy`, `system1`, `graph` (knowledge-graph facts, `docs/system2-rag-contract.md` §7) object lists and
  recursive `event`/`mission` objects. IDs retain their supplied spelling.
  Evidence content comes from fact/text/content or substantive structured values, never IDs, titles or control metadata.
  Malformed consumed values, unknown actions and conflicting duplicate IDs return 422. Valid absent/unusable
  evidence, orphan citations, missing actions, nonempty missing/gaps or explicit insufficiency return 200 with
  empty evidence, low confidence, MANUAL_REVIEW and path insufficient_context, before generation.
  The flat response is `evidence[{claim,ids}]`, `answer`, `question`, `recommended_action`, `confidence`
  (high/medium/low), `insufficient_context`, integer `flag_id`, `model`, nonnegative `latency_ms`,
  nullable `tokens_per_sec`, and `path`. Every output object rejects extras. Response metadata (ADDED
  2026-10-10, additive widening under the same version marker): `model` is a nonblank string (default `stub`);
  `path` ∈ `stub | llm | retry | fallback | insufficient_context` (`llm` = first model attempt passed the checks,
  `retry` = second attempt after one retry with the check errors, `fallback` = the checked template after both
  attempts failed); `latency_ms` is a nonnegative integer and `tokens_per_sec` a finite nonnegative number or
  null (booleans, NaN and infinity are rejected). Path rules: `insufficient_context` requires
  insufficient_context true, empty evidence, low confidence and MANUAL_REVIEW; every other path requires
  insufficient_context false and at least one evidence item; `stub` requires model `stub`, `fallback` requires
  model `template`, and `llm`/`retry` name the serving model (never `stub` or `template`). Metadata stays flat:
  no nested attempt or debug objects. The live `/v1/assess` returns `stub`/`insufficient_context` when
  `SYSTEM2_LLAMA_URL` is unset, and `llm`/`retry`/`fallback`/`insufficient_context` from the checked pipeline when
  it is set (live mode, below).
  Ordinary actions follow MCP ALL_ACTIONS and packet membership; System 2 explicitly extends the vocabulary
  with MANUAL_REVIEW, appended internally without mutating caller context. Each citation resolves to actual
  supplied content. X-API-Key authorization runs before parsing (401 for missing/wrong keys).
  Legacy Phase 1 fixture models remain offline examples and do not define this HTTP contract.
- System 2 container env (ADDED 2026-10-10): `SYSTEM2_API_KEY` (required, X-API-Key secret), `SYSTEM2_HOST` /
  `SYSTEM2_PORT` (`0.0.0.0` / `8083` in the image), `SPOT_KIT_DIR` (`/app/kit`; compose mounts
  `amd/system1/data/kit` read-only there for the grounding entity catalog and saved-packet row checks),
  `SYSTEM2_LLAMA_URL` / `SYSTEM2_MODEL` / `SYSTEM2_CACHE_DIR` (live mode, below; empty = stub, no cache).
- **System 2 live mode (ADDED 2026-10-10, Phase 3; additive, contract version unchanged):**
  env `SYSTEM2_LLAMA_URL` (llama-server base URL, e.g. `http://127.0.0.1:8080`; unset or empty means stub),
  `SYSTEM2_MODEL` (required when the URL is set; sent to llama-server and part of the cache key),
  `SYSTEM2_CACHE_DIR` (explanation cache; default `amd/system2/cache` in a checkout, none in the image unless set to
  a writable mount). Live metadata: `model` = the serving model llama-server reports; `latency_ms` = checked-pipeline
  time for this request; `tokens_per_sec` = llama-server `timings.predicted_per_second` of the accepted attempt,
  null for `fallback`/`insufficient_context`. The generation budget is a 15 s deadline over at most two attempts
  (then the checked template, `fallback`). Response header `X-System2-Cache: hit | miss | off` in live mode only
  (absent in stub mode). On `hit` the body is the stored response unchanged, so its `model`/`latency_ms`/
  `tokens_per_sec` describe the generation that produced it; callers logging live speed skip hits. The cache key is
  `(flag_id, packet_hash, model, prompt_ver)` and only `llm`/`retry` results are stored; a stored entry is re-checked
  against the current packet before it is served. Live `GET /health` adds `"model"` and reports `"mode": "live"`.
- **Model probe (ADDED 2026-10-11; additive).** Live `GET /health` also reports whether the model behind System 2
  answers: `"model_status": "up" | "down" | "unknown"` (unknown until the first probe finishes), `"model_checked_s_ago"`
  (int or null), `"model_probe_ms"` (int or null) and, when down, `"model_error"` (a short kind such as `timeout`,
  `transport` or `http 401`; never a URL, host, port or key). A background thread sends a 1-token chat completion over
  the same URL, model and key as `/v1/assess` every `SYSTEM2_MODEL_PROBE_S` (default 30 s) with a
  `SYSTEM2_MODEL_PROBE_TIMEOUT_S` (default 15 s) timeout, so `/health` itself never waits on the model. A probe is
  skipped while an assessment is running, and an `llm`/`retry` answer counts as a successful probe. Stub mode is
  unchanged (no `model_*` keys).
- System 2 request the backend sends: `POST /v1/assess {"flag_id", "request_id", "flag", "event", "reasons":
  [detail strings], "context": <packet from GET /flags/{id}/context>}`; it accepts `reasoning` or R3's
  `evidence[{claim, ids}]`. Optional `POST /v1/debrief {"mission_id", "facts"}` → `{"summary",
  "recommendations"}`; without it, debriefs are templated.

### System 2 multimodal inputs (contract `2026-10-10.backend.2`, ADDED 2026-10-10; additive)
Every field below is optional. A request without them is a `backend.1` request and gets byte-identical prompts,
packet hashes and responses. The **response body is unchanged**: V# IDs simply appear in `evidence[].ids`.
Research basis: `research/tech-spot-vision-cv-2026-10-10.md` §3 and §7 (code makes the numbers; the model explains).
- **`context.vision`**: list (at most 16) of tool readings from the vision service (`docs/vision-contract.md`):
  `{"id": "V1", "tool", "kind", "quantity", "value", "unit", "confidence", "trust": "HIGH|LOW", "logged_id",
  "logged_value", "delta", "agrees", "pairing": "demo|linked"}`. `id` (`V` + 1–3 digits, first digit nonzero),
  `tool` (nonblank) and `trust` are required. `value`, `logged_value`, `delta` are finite numbers or null;
  `confidence` is a number in [0, 1] or null; `agrees` is a boolean or null; text fields are strings or null,
  stripped of control characters and capped at 64 characters. Unknown keys are ignored. `value`, `delta` and
  `agrees` are **computed by code upstream** (never by a model). A `trust: "LOW"` row carries no value: System 2
  drops `value`, `delta` and `agrees` from LOW rows before the prompt, so they can never be cited. Each row becomes
  an ordinary citable evidence row `V#` whose content is the canonical JSON of its retained fields, so the
  existing claim-local checks apply: a number in a claim must appear in a V# or R#/reading row that claim cites.
- **`context.vision` follow-up (ADDED 2026-10-10, still `backend.2`, additive; prompt `analyst-prompt-2026-10-10.3`):**
  optional `flags`: a list of uppercase reader caveats (`^[A-Z][A-Z_]{0,31}$`, no digits), de-duplicated and capped
  at 8; anything else is 422. `AMBIGUOUS_NEEDLE` or `DEFAULT_SWEEP` forces `trust: "LOW"`. `CLIPPED` keeps the trust
  but adds `bound: "lower"` (the value is the top of the scale; the true value is at least that). Optional `bound`:
  `lower | upper`.
- **Demo rows:** `pairing: "demo"` rows come from an illustrative kit image chosen by action type, not from this
  asset: System 2 drops their `agrees`, `delta`, `logged_id` and `logged_value`, and they never trigger the action
  restriction below.
- **`context.ocr`**: list of `{"text", "conf"}` lines read off the image (`conf` a number in [0, 1] or null).
  Untrusted context text, **never evidence and never citable**: System 2 keeps the first 30 nonblank lines,
  strips control and format characters, collapses whitespace, caps each line at 80 characters and all lines at
  1,200 characters. A number seen only in OCR text fails the grounding check unless a cited V#/R# row states it.
- **`context.system1`** (already consumed): Laya rows in the MCP shape `{"id", "when", "q", "answer", "meaning",
  "p"}`. Laya-native keys are also accepted and mapped: `question` → `q`, `probability` or `confidence` → `p`,
  and `probs[answer]` → `p` when `p` is absent. The prompt shows question, answer, meaning and probability.
- **`image_b64`** (top level, string or null): one JPEG or PNG, base64, optionally with a
  `data:image/jpeg;base64,` / `data:image/png;base64,` prefix (the prefix type must match the bytes). Decoded size
  at most 2 MiB (2,097,152 bytes); the first bytes must be the JPEG (`FF D8 FF`) or PNG (`89 50 4E 47 0D 0A 1A 0A`)
  signature. Anything else (bad base64, wrong signature, too large, non-string) is **422**. `null` or `""` means
  no image. Marks drawn on the image (needle line, hotspot box, peak marker) are labelled with their V# ID.
  **The image is context only and is never citable evidence.**
- **Actions:** when any HIGH-trust **`linked`** V# row has `agrees: false` (demo rows never do), `MONITOR` and `NO_ACTION` are removed from that
  request's allowed actions (the tool and the log disagree, so "all fine" is not an answer); if the validator's
  `suggested_action` was removed it becomes `MANUAL_READING`, else `CHECK_SENSOR_PAYLOAD`, when supplied, else
  unset. No new action codes. The prompt asks the model to prefer `MANUAL_READING` / `CHECK_SENSOR_PAYLOAD`, cite
  both the V# and the logged reading ID, and call a LOW-trust image reading unreliable.
- **Prompt and serving:** the multimodal system rules are appended only when `vision`, `ocr` or an image is
  present. With an image, the final user message is an OpenAI content array, image first, then the packet text
  with the question last (exact format: `amd/system2/PROMPT_FORMAT.md`), sent to `SYSTEM2_VISION_MODEL`
  (default `SYSTEM2_MODEL`) with the same `json_schema` response format. If the server rejects image input
  (e.g. llama-server without `--mmproj`), the same attempt is re-sent once text-only to `SYSTEM2_MODEL`; the
  request still succeeds. Response header **`X-System2-Image: used | text_only | not_used`**, present only when
  the request carried an image (`not_used` = stub mode, insufficient context or template fallback).
  `VISION_RULES` (prompt `.3`) also says demo rows are illustrative and never compared, `bound=lower` values are
  lower bounds ("at least"), and `RELATIVE_ONLY` values are relative levels. Requests with no vision fields still get
  the byte-identical pre-multimodal prompt (golden test unchanged).
- **Image deadline:** requests with `image_b64` use `SYSTEM2_IMAGE_DEADLINE_S` (default 25 s, must be in (0, 120])
  instead of 15 s; the image call keeps 8 s back and, if it times out, the same attempt is re-sent text-only
  (`X-System2-Image: text_only`, not cached). A gateway 400 "No connected db." (LiteLLM's answer to a wrong key) is an
  auth error, never an image rejection: no text-only resend.
- **Cache:** `packet_hash` also covers the kept OCR lines and the image's sha256 (only when present, so old hashes
  are unchanged); image requests key on the vision model; text-only answers to image requests are not cached.

### Streaming Qwen drafts (ADDED 2026-10-11; additive: every existing endpoint is unchanged)
Goal: the boxes Qwen fills (flag detail "Load explanation", the `/live` "Qwen explanation (System 2)" card) show the
model's text while it is generated, instead of staying empty for the 8-30 s a 27B answer takes. **A draft is never an
answer.** It is the model's partial JSON before any check; the checked result (`final` / `done` / `/analysis`) always
replaces it, and only that result is stored, cached, indexed, counted or embedded. A rejected draft simply gives way to
the checked template.

**Draft fields** (one shape at every hop): `fields = {evidence: [{claim, ids}], answer, recommended_action,
confidence}`, parsed from the incomplete JSON text generated so far, in GEN-01 order (evidence first, then answer,
question, recommended_action, confidence). Only keys whose value has started are present; the last string may stop
mid-word; `ids` holds only complete strings; every evidence item is always `{claim, ids}` (`claim: ""` / `ids: []`
until started); `question` and `insufficient_context` are never sent (they repeat the packet). Every draft is a **full
snapshot** (replace, never append), so a lost draft costs nothing and a retry simply starts a new snapshot. The first
draft of a call is sent at once and may be `fields: {}` (nothing readable yet); a later snapshot equal to the last one
sent is skipped. `call` (1-based) counts model calls inside one assessment: it increases on the pipeline's retry
and on an image request's text-only resend; a client shows the newest `call` only. `reasoning` (backend and live only)
is the evidence claims joined like `normalize_assessment` (`"<claim> [<ids>]"`, space-separated).

**System 2 `POST /v1/assess/stream`** (same body, `X-API-Key` and validation as `/v1/assess`). A 401 or 422 is a plain
JSON error before the stream starts, and so is `503 {"detail": ...}` when 8 streams (`STREAM_MAX_CONCURRENT`, one
worker thread each) are already running in the process; the caller retries or uses `/v1/assess`. Otherwise `200 text/event-stream` (`Cache-Control: no-cache, no-transform`,
`X-Accel-Buffering: no`), events `event: <name>` + `data: <one-line JSON>`:
- `start` `{flag_id, request_id, model, mode: "live" | "stub"}`, sent at once.
- `draft` `{call, tokens, fields}`: at most one per 100 ms per call while llama-server streams (`tokens` = content
  chunks received so far in this call).
- `final` `{response, cache: "hit" | "miss" | "off" | null, image: "used" | "text_only" | "not_used" | null}`, always the
  last event on success. `response` is exactly the body `/v1/assess` would return for the same request (same checks,
  cache, deadlines and recorder); `cache`/`image` are the values of its `X-System2-Cache` / `X-System2-Image` headers.
- `error` `{detail}`: an unexpected failure after the stream started; the last event.
- `: keep-alive` comment after 10 s without an event.
Stub mode, cache hits and `insufficient_context` packets send `start` then `final` (no drafts). Live generation asks
llama-server for `stream: true` + `stream_options.include_usage` with otherwise the same body; the streamed content is
concatenated and judged exactly like a non-streamed completion (`finish_reason`, usage, `timings` come from the
stream's chunks). If llama-server (or a proxy) ignores `stream: true` and answers a plain completion (a 2xx whose
Content-Type is not `text/event-stream`), it is read as one under the plain size cap, with no drafts. A client
disconnect does not cancel the assessment: it finishes and is cached as usual.

**Backend `POST /api/v1/flags/{flag_id}/explain/stream?refresh=false`** (no body; open like `/explain`). Flag-not-found
`404`, an unreachable database `503` and `504 deadline_exceeded` (the flag row not read in time) are JSON before the
stream. Otherwise `200 text/event-stream` (same headers), events:
`start` `{flag_id, request_id}` · `stage` `{stage: "cached" | "context" | "system2" | "storing"}` (0..n) · `draft`
`{call, fields, reasoning}` (0..n, relayed from System 2) · `done` `{explanation}` · or `error` `{error, detail}`; a
`: keep-alive` comment every 15 s. `done.explanation` is exactly the JSON `POST /flags/{id}/explain` returns for the
same call (same cache rules, `REQUEST_DEADLINE_S`, fallbacks, storage and indexing). Cached rows and the orchestrator
path (`ORCHESTRATOR_URL` set) send no drafts. If System 2 answers the stream with 404/405 (an older build), the backend
uses `/v1/assess` inside the same deadline, without drafts, and remembers it for 5 minutes per instance
(`NO_STREAM_TTL_S`; no breaker trip, no `model_calls` row). A `503` (System 2's stream slots are busy) also uses
`/v1/assess`, for that call only. A `200 application/json` reply on `/v1/assess/stream` is
accepted as the final response (no drafts). System 2's `error` event, a stream without `final`, a transport error or
another status count as a System 2 failure (breaker, template) exactly like `/v1/assess`; there is no second call. A
stream cancelled by the client or the deadline logs an `ok=false, error="cancelled"` `model_calls` row and does not
touch the breaker. `/ops/model-calls` stats count `/v1/assess/stream` calls under `/v1/assess` (rows keep the real
endpoint).

**Live runner -> backend `POST /api/v1/live/runs/{run_id}/stops/{stop_index}/draft`** (admin, `X-Admin-Token`), body
`{call, fields, tokens?}`. While the run is `running` and the capture's `qwen_source` is `pending` it sets
`capture.qwen = {draft: true, call, tokens, answer, evidence, recommended_action, confidence, reasoning, drafted_at}`
(every key, `null` / `[]` when not started) and `phase = "analysing"`, and returns `{updated: true}`; after
`/analysis` has stored the result it changes nothing and returns `{updated: false}`. Errors: `404 live_not_found` (no
such run or stop), `409 live_conflict` (the run is not `running`, e.g. cancelled or finished, or the stop is not
triaged yet), `413 live_too_large` (`fields` over 16 KB serialized), `422` (bad body). No schema change (the draft
lives in the existing `qwen` jsonb until `/analysis` overwrites it). `GET /live/runs/{id}` and `/live/captures` return
it as is, with `qwen_source: "pending"`. When the run ends (`finish` or `cancel`), captures still `pending` (their
`/analysis` never landed) become `qwen_source: "error"`, `phase: "error"`; a leftover draft stays in `qwen` and the
console labels it discarded.

**Live runner** (`scripts/live_runner.py`): calls `/v1/assess/stream` (env `SYSTEM2_STREAM`, default `1`, set in
compose from the host variable `LIVE_SYSTEM2_STREAM`; `0` = plain `/v1/assess`; a 404/405 or a busy 503 also falls
back to `/v1/assess` in the time left) and posts the newest draft at most every `LIVE_DRAFT_INTERVAL_S` (default `0.5`) from a
separate thread, so posting never slows reading. `SYSTEM2_TIMEOUT_S` bounds the whole call, stream included. A `200`
JSON reply on the stream endpoint is taken as the final response; an `error` event is a System 2 error (template,
no fallback call). Draft posts are best effort: a failure is logged once and never fails the stop, a 4xx stops drafts
for that stop, and a snapshot over 16 KB is skipped. The `/analysis` call is unchanged and still carries the final
response; no draft is posted after it.

**Web.** `GET /api/flags/[id]/explanation/stream` pipes the backend stream through unbuffered (behind
`FEATURE_FLAG_EXPLANATIONS`, browser abort cancels upstream). Flag detail renders drafts under "Qwen is writing ·
unverified draft" and swaps in the `done` explanation with its usual label; any stream failure falls back to
`GET /api/flags/[id]/explanation`. `EXPLAIN_TIMEOUT_MS` bounds only the wait for the backend's response headers, and
both explanation routes set `maxDuration = 60`. `/live` shows `capture.qwen` drafts in the Qwen box with the same label
while `qwen_source` is `pending`, and polls the selected run every 750 ms while the run is pending/running and any of
its captures is pending (2 s otherwise). Runbook and measured latencies: `docs/STREAMING.md`.
