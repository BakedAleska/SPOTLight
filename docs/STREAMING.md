# Streaming Qwen's drafts

Qwen3.8-27B needs 8-15 s for an answer. Instead of an empty box, the flag dialog and the `/live` cards show the
model's text while it is written, labelled **unverified draft**, and swap in the checked answer when it lands.
Contract (shapes, errors, every rule): `docs/backend-services-contract.md` "Streaming Qwen drafts".

## What streams where
```
llama-server (stream: true) ──▶ System 2 POST /v1/assess/stream   (SSE: start · draft ≤ 1 per 100 ms · final | error)
   ├─▶ backend POST /flags/{id}/explain/stream   (SSE: start · stage · draft · done | error; keep-alive 15 s)
   │     └─▶ Next GET /api/flags/[id]/explanation/stream  (piped unbuffered) ──▶ flag dialog (FlagDetails)
   └─▶ live runner (scripts/live_runner.py, reads the SSE, keeps the newest draft)
         └─▶ backend POST /live/runs/{id}/stops/{i}/draft  (≤ 1 per LIVE_DRAFT_INTERVAL_S = 0.5 s)
               └─▶ Neon live_captures.qwen {draft: true, ...} ──▶ /live polls the run every 750 ms
```
- **System 2** (`amd/system2/analyst/{api,generation,live,drafts}.py`): runs the unchanged pipeline with
  llama-server streaming; `drafts.py` turns the partial JSON into `{evidence[{claim, ids}], answer,
  recommended_action, confidence}` snapshots. At most 8 streams at once (`503` JSON above that).
- **Backend** (`backend/app/services/{system2,flags,live}.py`): relays drafts for flag explanations; falls back to
  `/v1/assess` (no drafts) when System 2 answers the stream with 404/405 (remembered for 5 minutes) or a busy 503.
- **Web**: `lib/sse-proxy.ts` pipes the body as-is; `lib/explanation-stream.ts` reads it and falls back to the JSON
  route on any failure; `components/QwenDraft.tsx` types the snapshot out. `/live` polls fast only while the run is
  live and a card is still pending.
- **Live runner**: streams by default (`SYSTEM2_STREAM=1`; compose sets it from `LIVE_SYSTEM2_STREAM`); a side thread
  posts the newest draft so a slow Vercel call never slows the reader, and no draft is posted after `/analysis`.

## Why drafts are unverified, and what replaces them
A draft is the model's JSON before any check: a claim may cite a wrong id, a number may be unsupported, the answer
may stop mid-word. System 2 checks only the finished answer (schema, citations, numeric faithfulness, allowed
actions), retries or falls back to its template, and sends that as `final`. The backend's `done` is exactly what
`POST /explain` returns; on `/live`, `/analysis` overwrites the draft. Only the checked result is stored, cached,
indexed, counted or embedded. A rejected answer simply gives way to the labelled TEMPLATE. When a run ends with a
stop still pending (its `/analysis` failed), `finish`/`cancel` mark it `qwen_source: error` and the card says the
draft was discarded.

## Measured (2026-10-11, W7900D, Qwen3.8-27B Q8_0)
| Path | First draft / text | Final | Drafts |
|---|---|---|---|
| System 2 direct, equipment text request | 5.6 s | 13.4 s | 47-48, ~133 ms apart |
| System 2 direct, image request | 3.7 s | 11.8 s | 47-48, ~133 ms apart |
| Backend relay via a local tunnel (`/flags/{id}/explain/stream`) | 7.25 s | `done` 15.1 s | 48, median gap 132 ms |
| Public Cloudflare URL Vercel uses (System 2 stream) | 2.7 s | 10.2 s (path `llm`) | 47, ~130 ms apart |

Prompt processing comes before the first draft, so it varies with the packet. The backend's `done.explanation`
equalled `/explain` for the same flag; the runner's drafts were accepted by the real `/draft` handler and SQL.

## Deploy
1. **System 2 on the box** (files changed: `api.py`, `generation.py`, `live.py`, new `drafts.py`):
   ```bash
   cd /root/SpotTheDOT/amd/system2/analyst
   mkdir -p /root/backup-s2 && cp api.py generation.py live.py /root/backup-s2/      # rollback copies
   # copy the four files from this repo's amd/system2/analyst/ into this directory (scp/rsync over `ssh spot-amd`)
   supervisorctl -c /root/supervisord.conf restart system2
   curl -s localhost:<system2 port>/health                                          # status ok, model_status up
   ```
   Rollback: copy the three backups back, delete `drafts.py`, restart. An old build only means "no drafts": the
   backend and the runner fall back to `/v1/assess` on 404/405.
2. **Backend and web on Vercel**: redeploy both projects (no new env vars). The backend also carries the live-packet
   fix (no `gaps`), without which every `/live` card is a TEMPLATE with no drafts.
3. **Live runner**: restart `python scripts/live_runner.py --tunnel spot-amd` (or rebuild the `live-runner` image)
   on the new script; `--check` prints a `stream OK <n> drafts` line.

## Verify (no secrets on the command line history: export them first)
```bash
# System 2 directly: start, ~47 draft events, then final
jq -c .request amd/system2/fixtures/backend/equipment.json | curl -sN -X POST "$SYSTEM2_URL/v1/assess/stream" \
  -H "X-API-Key: $SYSTEM2_API_KEY" -H 'Content-Type: application/json' --data-binary @- | grep -c '^event: draft'
# Backend relay (refresh=true regenerates; a cached row sends `stage: cached` and no drafts)
curl -sN -X POST "$BACKEND_URL/flags/<flag_id>/explain/stream?refresh=true" | grep '^event:' | uniq -c
# Web route (refresh=false, the dialog's own call)
curl -sN "$WEB_URL/api/flags/<flag_id>/explanation/stream" | head -20
# Live runner: one streamed System 2 call, nothing posted
python scripts/live_runner.py --tunnel spot-amd --check
```
Expect `start`, `stage: context`, `stage: system2`, many `draft`, `stage: storing`, `done` from the backend.
Then open a flag on the dashboard, press **Load explanation**, and run a route on `/live`.

## Known limits
- **Neon flags may still answer `insufficient_context`** (no drafts, template) until the Neon ingest and the graph
  build have run: MCP's `get_flag_context` packet lists `gaps` for what it cannot find, and System 2 treats any gap as
  insufficient context. Live stops are not affected (their packet has no `gaps`).
- Cloudflare quick-tunnel URLs change on every restart: update `SYSTEM2_URL` on the Vercel backend and redeploy.
- `/ops/model-calls` counts `/v1/assess/stream` under `/v1/assess` (one System 2 latency); raw `app.model_calls` rows
  keep the real endpoint. A stream cut by the client or the deadline logs `ok=false, error="cancelled"`.
- Drafts need the orchestrator off: with `ORCHESTRATOR_URL` set the backend asks the orchestrator, which does not
  stream, so the dialog shows stages and then the answer.
- Each System 2 stream holds a worker thread until the model finishes (a closed dialog does not cancel it; the answer
  is cached, so a retry is instant). More than 8 at once get `503`; the backend and the runner then ask the plain
  `/v1/assess` (no drafts).
