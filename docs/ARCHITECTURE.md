# Architecture: SPOTlight (SPOT Operator Console)

> **SPOTlight** turns SPOT robot mission data into a clear, trustworthy operator view, live and after the mission.
> This file is the system map as built (updated 2026-10-10). Diagram: [SPOTlight Architecture (FigJam)](https://www.figma.com/board/ZZNkIAX3bo5TLE7cSrfAUx); source `docs/architecture.mmd`. How to run it: `docs/RUNNING.md`. Decisions and their reasons: `research/decisions.md`.

## 1. The one-sentence pitch
**"SPOT says 26 of 27 missions were green. We show the 21 that weren't, explain why, and tell the operator what to do next."**
(Data: `research/spot-data-insights-2026-10-09.md` #1. A one-line opener followed by a live demo is how winners pitch: `winners-playbook-judging`.)

## 2. Where things run

Two machines. The whole stack is one `docker-compose.yml` with profiles. The AMD Radeon Cloud VMs have no root and can't run Docker, so the GPU models run natively there in a venv and are reached over SSH tunnels.

```
┌──────────────────── Laptop: Docker Compose (profile `hybrid`) + `npm run dev` ────────────────────┐
│  web :3000 (Next.js)           backend :8000 (FastAPI)          orchestrator :8085 (LangGraph)    │
│  db :5433 Postgres 17+pgvector (or Neon, profile `neon`)        db-init (one-shot: migrate→seed)  │
│  mcp :8090 (read-only tools)   system2 :8083 (FastAPI)          gateway :4000 (LiteLLM proxy)     │
│  vision :8084 (OCR + code readers, CPU, no model)                                                 │
└───────────────┬──────────────────────────────────────────────────────────┬────────────────────────┘
                │ SYSTEM1_URL, EMBED_URL (SSH tunnels)                      │ LLAMA_URL (tunnel 18080→8080)
┌───────────────▼─── AMD Radeon 8060S, gfx1151, ROCm (native venv) ────────▼────────────────────────┐
│  system1 :8082  Laya 421M (fine-tuned)    embed :8081  Qwen3-Embedding-0.6B                       │
│  llama-server :8080  Qwen3.5-9B Q4_K_M GGUF, llama.cpp HIP build (+ mmproj for images)            │
└───────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Other profiles: `local` runs everything in Docker on CPU (for testing: Laya takes about 3–8 s per event), and `neon` swaps the `db` container for Neon. Full list in `docker-compose.yml` and `docs/RUNNING.md`.

## 3. Services

| Service | Port | What it does | Model | Code |
|---|---|---|---|---|
| **web** | 3000 | Operator console: the Overview page (hidden-failures headline, stat tiles, priority queue, battery cliff) and **SPOTbot** (`/assistant`), a ChatGPT-style chat with stored history. Reads the backend over HTTP (`BACKEND_URL`), with no database client of its own; falls back to the kit CSVs with a visible banner when the backend is unreachable. | none | `web/` |
| **backend** | 8000 | Dashboard API under `/api/v1`: event ingest, replay, flags, explain, ack, missions, insights, forecast, search, ask, **chat**, ops. | Gemini (chat only, via `GEMINI_API_KEY`) | `backend/` |
| **db** + **db-init** | 5433 | Postgres 17 + pgvector, or Neon. `db-init` migrates, creates login roles, seeds the kit and embeds the RAG corpus. | none | `db/` |
| **system1** | 8082 | Laya triage: `POST /v1/predict` answers typed questions per event with calibrated probabilities. | Laya (`convaiinnovations/laya`, ModernBERT-large) | `amd/system1/` |
| **embed** | 8081 | OpenAI-compatible `/v1/embeddings` for the RAG. | Qwen3-Embedding-0.6B (1024-d) | `amd/embed/` |
| **mcp** | 8090 | Read-only MCP context server (`get_flag_context`, mission and battery stats, raw log, search) over SQL + pgvector. | none (calls embed) | `amd/mcp/` |
| **system2** | 8083 | "Analyst": `POST /v1/assess` turns a flag + context packet into an Orbit-style explanation. A packet-grounded stub by default; live when `SYSTEM2_LLAMA_URL` is set. At most 2 attempts in 15 s (25 s with an image), then a template. | Qwen3.5-9B via the gateway | `amd/system2/` |
| **gateway** | 4000 | LiteLLM proxy: one OpenAI-compatible endpoint and key for every model (`system2`, `system2-vision`, `system1`, `embed`). No retries or fallback model, so a failure surfaces as System 2's checked template instead of a silent model swap. | none | `amd/gateway/` |
| **vision** | 8084 | `POST /v1/vision`, `/v1/ocr`. Code readers for thermal colorbars, dial gauges, spectrograms and Orbit Key Results turn an image into `V#` readings and an annotated JPEG. Code computes every number, because VLMs score 25–30% on gauges and thermal (`research/tech-spot-vision-cv-2026-10-10.md`). | none (RapidOCR) | `amd/vision/` |
| **orchestrator** | 8085 | LangGraph, with a per-node `trace` the UI can show. `GET /v1/graph` renders the graph as mermaid. Endpoints are `/v1/assess` and `/v1/ask` (§4). The backend uses it only when `ORCHESTRATOR_URL` is set. | none | `amd/orchestrator/` |

## 4. How a request flows

1. **Replay and ingest (every event).** `POST /api/v1/replay/start` streams the kit's `replay.ndjson` (or a client POSTs `/api/v1/events`). Each event goes through 7 steps under one `request_id`:
   1. Log the raw event to `raw.spot_event_log`.
   2. Normalize it into `events`.
   3. Build the canonical event.
   4. Call System 1 (Laya).
   5. Store the answers in `system1_predictions`.
   6. Run the rules validator (`amd/system1/triage/policy.py`).
   7. Upsert or clear the flag.
2. **Triage (System 1 = Laya + rules).** `TRIAGE_MODE` decides how the two combine:
   - `serial` (default): Laya flags and the rules confirm.
   - `parallel`: either one can escalate, and the rules' priority is a floor Laya can raise but not lower.
   - If Laya is down, both modes fall back to rules only (`laya_unavailable`).

   Both verdicts are stored in `flags.reasons`. The rules come first because they're explainable, and NextEra's brief asks for explainability. Running rules in parallel with Laya catches Laya's misses (`research/tech-cascade-alarm-mgmt-2026-10-10.md`).
3. **Explain (on demand, System 2).** `POST /api/v1/flags/{id}/explain` takes one of two paths, and the result is cached in `assessments`:
   ```
   backend /flags/{id}/explain ──(ORCHESTRATOR_URL set)──▶ orchestrator /v1/assess
     │                                                       load_context (MCP) → system1 (gateway → Laya)
     │                                                       → route → vision → system2 → finalize   (40 s)
     └──(unset or down)──▶ MCP get_flag_context ──▶ system2 /v1/assess ──▶ template if it fails
   ```
   - **What System 2 receives:** `{flag_id, request_id, flag, event, reasons, context}`, where `context` is the MCP packet. It can also take `context.vision[]` (`V#` rows), `context.ocr[]` and `image_b64`. System 2 never retrieves context itself; tool loops would break its ≤15 s p95.
   - **What it returns:** cited `evidence[{claim, ids}]`, plus `answer`, `question`, `recommended_action` (a fixed enum), `confidence` (`high|medium|low`), `insufficient_context`, `model`, `latency_ms`, `tokens_per_sec` and `path`. That matches NextEra's Orbit format.
   - **Checks:** a deterministic post-check rejects any number not in the cited evidence. The whole call is bounded by `REQUEST_DEADLINE_S` (45 s).
4. **Forecast (Laya Track A).** `GET /api/v1/system1/forecast` predicts `will_fail`, `failure_mode` and `anomaly_expected` before an action runs. It uses stored predictions first, then live Laya. It always shows the empirical baseline for that waypoint and action type, plus the actual outcome.
5. **Ask and search.**
   - `POST /api/v1/ask` sends a free-text question to the orchestrator's `/v1/ask`. There, the System 2 model may call up to 4 read-only MCP tools within 30 s.
   - `GET /api/v1/search` runs hybrid pgvector + full-text search over the kit, the Spot manual, incidents, policy and operator notes.
6. **Act.** `POST /api/v1/flags/{id}/ack` handles acknowledge, resolve, escalate and note. Each note is embedded as a RAG document, so later explanations can cite it.
7. **Chat (SPOTbot, `docs/chat-contract.md`).** `POST /api/v1/chat/conversations/{id}/messages` streams SSE. The backend retrieves first (hybrid or full-text `documents`, knowledge-graph entry nodes and their 2-hop neighbourhood, SQL fleet stats and digests of the missions, assets, waypoints and flags named in the question), then makes **one** Gemini call (free tier: 5 requests/min/model). Fast or Thinking mode and the model are the operator's choice. After the stream, a deterministic check verifies every `[id]` citation and every number against the sources and stores a grounding badge. Chats live in `chat_conversations` / `chat_messages` and are never retrieved as evidence.
8. **Live patrol (`/live`, added 2026-10-11).** An operator starts one of five 8-stop routes; the live runner
   (`scripts/live_runner.py`, on the presenter laptop, the only machine that reaches the AMD box) walks it. Per stop
   the backend logs the kit row of that action as a fresh live event, the runner calls Laya, and the backend runs the
   rules beside it (`parallel`) for a red or green ping; then the runner sends the stop's simulated photo to vision
   and System 2 (Qwen, with the image) while SPOT walks on, and the backend stores the explanation (or a labelled
   template), its embedding and the live knowledge-graph rows (`props.source = 'live'`). Tables `live_runs`,
   `live_captures` (`013`). Runbook: `docs/LIVE-RUNBOOK.md`.

## 5. Data (Postgres; schema in `db/migrations/001–013`)
- **`public`:**
  - Kit tables: `missions`, `actions`, `readings`, `anomalies`, `locations`, `assets`.
  - Pipeline tables: `events`, `flags`, `assessments`, `acknowledgements`, `system1_predictions`.
  - RAG: `documents`, with an embedding column and the `match_documents` / `match_documents_hybrid` functions.
  - Graph RAG: `kg_nodes` / `kg_edges` with `match_kg_nodes`, `match_kg_nodes_text` and `graph_neighborhood` (`009`).
  - Chat: `chat_conversations`, `chat_messages` (`011`; `spot_app` only, the MCP reader has no access).
  - Live patrol: `live_runs`, `live_captures` (`013`).
- **`raw`:** the SPOT data verbatim and append-only (`spot_event_log`, `kit_files`, `kit_rows`, `inspection_media`).
- **`app`:** logs (`model_calls`, `mcp_tool_calls`, `request_log`, `job_runs`). `GET /api/v1/ops/model-calls` turns them into p50/p95 latency and tokens/s per model, which are the AMD numbers.
- **Roles** (`007_roles.sql`): the backend writes as `spot_app`. The MCP server and System 1's logging use `spot_mcp_reader`, which can read `public` and append to `app`.

## 6. Contracts and runbooks (change the contract first)
| File | Covers |
|---|---|
| `docs/backend-services-contract.md` | Ports, the System 1 and System 2 HTTP contracts (`2026-10-10.backend.2`), backend env vars |
| `docs/system2-rag-contract.md` | Embeddings, pgvector, MCP tools, graph-RAG proposal (§7) |
| `docs/orchestrator-contract.md` | Orchestrator `/v1/assess` and `/v1/ask` |
| `docs/vision-contract.md` | Vision `/v1/vision` and `/v1/ocr` |
| `docs/chat-contract.md` | SPOTbot chat: tables, `ChatRepo`, retrieval and grounding rules, chat API + SSE events, UI requirements |
| `docs/RUNNING.md` | Compose profiles, the AMD hybrid setup, live System 2, timeouts and keys |
| `amd/system2/schema/`, `amd/system2/fixtures/` | System 2 JSON Schemas and request/response examples |
| `amd/*/README.md`, `backend/README.md`, `db/README.md` | Per-service details |

## 7. What each sponsor sees
| Prize | Where it's load-bearing |
|---|---|
| **NextEra** (core) | Answers the literal prompt: locations, timestamps, readings and anomaly flags, live (replay) and after the mission (missions, debrief, insights), explained for non-experts. |
| **AMD** | Laya fine-tuned on the Radeon 8060S. Qwen3.5-9B served by llama.cpp's HIP build on gfx1151 (35.6 tok/s, p95 7.9 s). `ops/model-calls` gives measured latency and tokens/s. AMD winners showed measured gains (`winners-taikai-hackerearth-enterprise`, `winners-lablab-ai`). |
| **Hugging Face** | Three open HF models doing real work: Laya (triage), Qwen3-Embedding (RAG), Qwen3.5-9B (explanations). Plan: publish the Laya fine-tune and its dataset card on the Hub. |
| **Neon** | One database holds the operational tables, pgvector RAG and the audit logs. It runs on Neon with the `neon` profile; the default `.env` is still `local`, so switch it for the demo. |
| **Figma** | UI and UX rules: status never shown by color alone (color + icon + word), keyboard navigation, WCAG AA contrast, dark operator theme with a light toggle (`winners-design-creative-categories`). |
| **MLH Gemini API** | SPOTbot answers through the Gemini API (Fast/Thinking, model picker), grounded in our pgvector RAG + knowledge graph with a citation and number check on every answer. |
| **Best AI** | Gates first, model second: rules ‖ Laya decide what gets AI attention, and System 2 explains with checked citations ("LLM proposes, gates decide", `winners-lablab-ai`). |

## 8. Known gaps (as of 2026-10-10)
- **The web app shows only the Overview page.** It now reads the backend (`/insights/overview`, `/flags`, `/missions`, `/assets`, `/locations`; `web/src/lib/dashboard.ts`), but the other nav items are still placeholders and nothing in the UI calls `/flags/{id}/explain`, `/ack` or `/replay/start` yet — the explain-and-act half of the core loop is API-only. `web/src/lib/analysis.ts` survives as the offline fallback (CSV triage in-process), shown behind a banner that says the numbers aren't live.
- **No System 2 `/v1/debrief`.** The backend's `GET /missions/{id}/debrief` calls it, but `amd/system2` has no such route, so the debrief always comes from a template.
- **Laya fine-tune:** the best run so far is full FT run2 (0.805 real, 0.846 counterfactual, vs LoRA's 0.733 / 0.602). The LoRA sweep (`scripts/sweep_lora.sh`) has no results in the repo yet. Checkpoints live on the AMD box, not in git.
- **Graph RAG** is built (`009_graph.sql`, `db/graph_build.py`: 578 nodes, 1,978 edges locally) and used by MCP and the chat. Node embeddings need the embed service; without it the chat uses the graph's text match only.
- **Chat:** Gemini free tier (5 requests/min/model and a daily cap), so heavy demo use can hit `rate_limited`; the UI offers another model. Gemma 4 is opt-in (`CHAT_MODELS`): on 2026-10-10 it was too slow and returned 503s.
- **Qwen vision fine-tune:** a teammate is working on it (`docs/TEAMMATE-qwen-vision-finetune.md`); no result yet.

If time runs short, **cut depth, not the core loop**: replay → triage → explain → act.
