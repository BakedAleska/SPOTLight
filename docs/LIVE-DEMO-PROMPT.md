# Prompt for a new Claude session: build the SPOTlight live demo

Paste everything below the line into a new Claude Code session opened in this repo.

---

You're working in the SpotTheDOT repo (SPOTlight, our NextEra SPOT challenge entry for Knight Hacks IX). The Devpost
deadline is **Sun Oct 11, 10:00 AM EDT**. Work on the branch `live-testing`. Follow `CLAUDE.md` (containerize,
contract-first, one root `docker-compose.yml`, decisions in `research/decisions.md`). Read these first:
`docs/LIVE-SIM-PLAN.md` (the plan and five routes), `docs/HANDOFF-2026-10-11.md`, `docs/ARCHITECTURE.md`,
`docs/RUNNING.md`, `docs/backend-services-contract.md`, `docs/orchestrator-contract.md`, and §7 of
`docs/system2-rag-contract.md` (knowledge graph).

## What I want judges to see
A real-time simulation of a SPOT robot inspecting the kit's two-floor manufacturing facility, shown on our live
Vercel site (https://spotlight-web-mu.vercel.app), like a SOC / cybersecurity live dashboard:

1. An operator opens the live page and starts a route. SPOT starts at stop A on the kit's floor plan
   (`web/public/kit/fictional_floor_plan.png`, pin anchors in `web/src/lib/path-data.ts`) and walks A → B → … → H,
   visiting every stop, and stops at the last one. That ends the run.
2. At each stop the robot "takes a photo" and its log for that stop goes through **Laya** (System 1) live:
   - Laya / the combined decision **does not flag it** → **green ping** on the map ("all good"). The photo and
     **Qwen's** output are still sent and shown.
   - Laya / the combined decision **flags it** → **red ping** ("needs attention"). The photo and Qwen's output are
     shown, marked **needs human verification**, with an Acknowledge action.
3. Each ping on the map has its details (from Qwen) in a feed **below the map**, newest first: photo, verdict, Laya's
   answer and confidence, the rules that fired, vision readings on the photo, and Qwen's question / answer /
   reasoning / recommended action, with timings.
4. The map and the dashboard update live while the robot moves. Everything is computed during the run: the logs and
   photo records land in **Neon** as the robot goes, the **embeddings** are computed live, **Qwen** runs live on the
   AMD server, and the **knowledge graph (`kg_nodes` / `kg_edges`) updates in real time** as the bot moves through
   the path (see below). We don't precompute or store any answers ahead of the run, and we don't make up any values.
5. There are **five routes** to pick from (in `docs/LIVE-SIM-PLAN.md` §4), each with 8 stops and at least two red
   stops, so we can show judges both a real problem being found and stops where everything is fine.

## What's real and what's simulated (keep this honest, and show it on screen)
- The robot's log at each stop (status, battery, reading, anomaly) is the **kit's row for that action**, re-sent as a
  new event with the current time. Values are not invented.
- Photos come from a fixed set (the 6 kit sample images in `web/public/kit/inspection/`, plus MeasureBench gauge
  photos with credits), picked by action type. No kit row links an image, so label them on screen as a
  **simulated capture**. Per our decision, demo photos never judge the asset: red/green comes from the log through
  Laya + the rules.
- Laya's verdict, the rules verdict, vision readings, Qwen's output, embeddings, KG updates and every Neon write
  happen live. If Qwen fails, the card must say **template**, never pass the template off as Qwen.
- Keep my reporting rules: development vs fresh results separate, no fine-tuning claims for System 2, and never put
  IPs, ports or keys in the repo.

## Current state (verified 2026-10-10/11)
- Vercel deploys `main` automatically after CI passes (`.github/workflows/deploy-vercel.yml`). Web, backend
  (https://spotlight-backend-tau.vercel.app) and MCP (https://spotlight-mcp.vercel.app) are live on Neon.
- The live backend's `/health`: database up; `system1` and `embed` down (they point at localhost on Vercel);
  `system2` and `orchestrator` unset. So today every flag is rules-only and explanations are the template.
- **Vercel cannot reach the AMD box.** It's reachable only over SSH (`ssh spot-amd`). **Tyler has the box connected
  on his laptop**, so his laptop is the bridge. From the other laptop, SSH is refused.
- Already built: `web/src/components/PathPreview.tsx` (map, SPOT marker, kit replay and a "Live feed" mode that polls
  `/api/path-feed` → backend `GET /events`), the backend's 7-step ingest (`backend/app/services/ingest.py`: raw log →
  events → canonical event → Laya → predictions → validator → flag), stepped replay, flags + `assessments`, and
  `db/graph_build.py` (a full KG rebuild, not incremental).
- Gotchas:
  - Flags upsert on `triage_event_id = "act-<action_id>"`, and `system1_predictions` is unique per
    (kind, action, mission, question, model). Reusing kit action ids would update the seeded rows instead of
    creating new ones, so each live run needs **fresh action ids** that carry the kit row's details, including the
    reading and anomaly (today those are looked up only by kit action id in `backend/app/services/reference.py`).
  - Ingest's in-memory caches don't survive across Vercel invocations.
  - `graph_build.py` deletes edges whose `props.source` is fk/stat/extract/curated and nodes it built that no longer
    exist. Live KG rows must use their own source (e.g. `props.source = 'live'`, `props.built_by = 'live'`) so a
    rebuild doesn't wipe them.

## Architecture to build
Everything except the box connection lives on Vercel. Tyler runs one command.

- **Vercel / Neon (merged to `main`):**
  1. Migration `011`: `live_runs` (run_id, route_id, status pending/running/done/error, current_stop, timestamps)
     and `live_captures` (one row per stop: run, stop letter, location, asset, event_id, flag_id, verdict red/green,
     Laya answers, rules verdict, photo path + sha256, vision readings, Qwen output with model/source/latency,
     timings).
  2. Backend endpoints:
     - `POST /api/v1/live/runs` (admin token; operator's Start button) queues a run.
     - `GET /live/runs` and `GET /live/captures?run_id&after` (public, for polling).
     - Admin endpoints the runner calls: claim a run; submit a stop (event + Laya answers), where the backend runs
       the rules validator, combines the verdicts and writes `raw.spot_event_log`, `events`, `system1_predictions`,
       `flags`; then submit the stop's vision + Qwen result and embedding, which writes `live_captures`,
       `assessments`, `documents`, and the **KG upserts**.
     - Contract docs change first.
  3. Web `/live` page:
     - Reuse the `PathPreview` map, with red/green pings from `live_captures` and SPOT moving between stops.
     - A route picker and Start button.
     - The SOC-style card feed under the map.
     - A small live KG panel (nodes and edges added this run).
     - History of earlier runs.
     - Poll every 2 s.
  4. The five routes as a data file (`docs/LIVE-SIM-PLAN.md` §4 has the action ids).
- **AMD box:** llama-server with Qwen3.8-27B Q8 + mmproj, Laya (`amd/system1/service`, native venv), the vision
  service, System 2 (`amd/system2` serve), and the embedder (Qwen3-Embedding-0.6B). Rebuild steps are in the
  handoff. Check one real Qwen request, not just `/health` (known llama.cpp "empty grammar stack" bug).
- **Tyler's laptop:** `scripts/live_runner.py` plus one wrapper script that opens the SSH tunnels. Its only settings
  are `BACKEND_URL` and `ADMIN_TOKEN`. It:
  - polls for a queued run and claims it;
  - for each stop, sends ACTION_STARTED, waits the travel time, sends ACTION_COMPLETED/FAILED, calls Laya through
    the tunnel and submits the stop;
  - sends the photo to vision, then System 2/Qwen with the image, embeds the summary and submits the result. Qwen
    runs in the background while the robot walks on, so cards show "Qwen analysing…" and then fill in;
  - ends with MISSION_COMPLETED.

  It never holds Neon credentials.

## Knowledge graph must update in real time
As each stop is processed, upsert into `kg_nodes` / `kg_edges` (contract §7 relations), tagged `source: 'live'`:
- a `mission` node for the live run;
- an `action` node per stop (`part_of` mission, `at` location, `inspects` asset, `of_type` action_type,
  `failed_with` failure_type when failed);
- an `anomaly` node (`raised_on` asset, `during` action, `of_type` anomaly_type) when the stop has one;
- a `flag` node on red (`flags` action, `triggered` policy_rule per fired rule);
- an `explanation` node for Qwen's answer (`explains` flag, document-backed by the live `documents` row and its
  embedding).

Embed node summaries live (reuse `documents.embedding` for document-backed nodes, as `graph_build.py` does). Show it
on the page so judges can watch the graph grow during the run, and make sure System 2 / MCP retrieval
(`get_flag_context`, `/v1/ask`) can see the new nodes right away. Update §7 of `docs/system2-rag-contract.md` for the
`live` source.

## Done means
- From the live Vercel site, an operator starts any of the five routes. With the box and Tyler's runner up, SPOT walks
  all 8 stops; green and red pings appear in order. Every card has a photo (labelled simulated) and a real Qwen output
  (or a clearly labelled template). Red cards say needs human verification.
- Neon gains, per run: events, Laya predictions, flags for the red stops, captures, assessments, documents with
  embeddings, and live KG nodes/edges. Re-running the same route creates new rows and doesn't overwrite earlier runs.
- Dry-run all 40 stops through Laya before the demo and report any stop where Laya disagrees with the planned verdict.
- Tests and CI pass. `docker-compose.yml`, `docs/RUNNING.md` and the contracts are updated in the same change.
- Show me the diff before merging to `main`, because merging deploys to the live site.
- Write a one-page runbook for Tyler: tunnels, env, start the runner, smoke check, restart commands.
