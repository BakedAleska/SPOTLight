# Live demo runbook (presenter laptop = Tyler's)

The judges watch https://spotlight-web-mu.vercel.app/live. The page reads Neon through the Vercel backend. Only the
**live runner** on this laptop can reach the AMD box, so it is the bridge: it claims the run an operator starts on the
page, calls Laya / vision / Qwen / the embedder through SSH tunnels, and posts every result to the Vercel backend.
How it works: `docs/backend-services-contract.md` "Live simulation". Never commit IPs, ports or keys.

**No runner? Use the mock.** The route picker defaults to "Mission 1 · mock patrol": Start route replays kit mission
M001 in the browser (the seeded flags mark the stops that need attention; their explanations are SPOTbot's stored
answers). It needs only the web app and the backend, no runner, no tunnel, no model calls, and writes nothing.

## 1. Box: the five services (in tmux on the box: `ssh spot-amd`)
| tmux | Port | What | Check on the box |
|---|---|---|---|
| `llama` | 8080 | llama-server, Qwen3.8-27B Q8_0 + mmproj (`docs/AMD-RUNS-2026-10-10.md` §4) | `curl -s 127.0.0.1:8080/health` |
| `s2` | 8083 | System 2 `scripts/serve.py`, live against llama-server (§6 of the same file) | `curl -s 127.0.0.1:8083/health` says `live` |
| `vision` | 8084 | vision service (`amd/vision`) | `curl -s 127.0.0.1:8084/health` |
| `laya` | 8082 | Laya, `cd amd/system1 && python -m service` (`amd/system1/service/README.md`) | `curl -s 127.0.0.1:8082/v1/model` (note `fine_tuned`, `device`) |
| `embed` | 8081 | Qwen3-Embedding-0.6B, `python amd/embed/server.py` | `curl -s 127.0.0.1:8081/health` |

Keys live on the box only (`/root/tmp/*.key`). The runner needs the System 2 key (`SYSTEM2_API_KEY`) and the vision
key (`VISION_API_KEY`). Report what `laya /v1/model` says: if `fine_tuned` is false, say "base Laya" on stage.

## 2. Laptop: settings (environment only; nothing in the repo)
```bash
export BACKEND_URL=https://spotlight-backend-tau.vercel.app/api/v1
export ADMIN_TOKEN='<the backend ADMIN_TOKEN from Vercel>'
export SYSTEM2_API_KEY="$(ssh spot-amd cat /root/tmp/system2.key)"   # use the real file names on the box
export VISION_API_KEY="$(ssh spot-amd cat /root/tmp/vision.key)"
```
The web project on Vercel needs `BACKEND_ADMIN_TOKEN` set to the same token, or the Start button answers 503.

Optional (defaults are right for the demo; Docker: set `LIVE_SYSTEM2_STREAM` / `LIVE_DRAFT_INTERVAL_S` in `.env`):
| Env | Default | What |
|---|---|---|
| `SYSTEM2_STREAM` | `1` | `1`: Qwen's answer is streamed (`/v1/assess/stream`) and its drafts show on `/live`. `0`: plain `/v1/assess`, no drafts |
| `LIVE_DRAFT_INTERVAL_S` | `0.5` | at most one draft post to the backend per interval (newest draft wins) |
| `SYSTEM2_TIMEOUT_S` | `60` | the whole System 2 call, stream included; after it the stop gets the TEMPLATE card |

## 3. Smoke check (one real request each; llama.cpp can keep `/health` OK while every answer fails)
```bash
python scripts/live_runner.py --tunnel spot-amd --check
```
Every line must say `OK`. `system2 TEMPLATE` means Qwen is not answering: restart llama-server (§6), then re-check.
The last line is the stream: `stream OK 48 drafts, first after 2.8s` (Qwen's drafts reach the runner). `cache=hit`
with 0 drafts is fine (the stored answer was served, nothing was generated). `stream OFF ... 404` means the box's
System 2 is an older build without `/v1/assess/stream`: answers still work, the Qwen card just fills in at the end
(restart `s2` on the new code for drafts). The stream line never changes the exit code.

## 4. Before the demo: Laya dry run on all 40 stops (no writes)
```bash
python scripts/live_runner.py --tunnel spot-amd --dry-run | tee laya-dryrun.txt
```
It prints, per stop: the plan, the rules, Laya's escalate answer and p, and the live verdict (rules ‖ Laya). A note
means Laya disagrees with the plan. Keep the file; it is the evidence for "Laya missed, rules caught it" if asked.

## 5. Run it
```bash
python scripts/live_runner.py --tunnel spot-amd            # leave it running; Ctrl-C to stop
```
Then press **Start route** on `/live`. The runner logs each stop (verdict, Laya, rules), `Qwen streamed N drafts,
first after X s; M posted`, then "Qwen answered" or "TEMPLATE (why)". One route takes about 2 to 2.5 minutes.

What the judges see in the Qwen card of a stop: a few seconds after the red/green ping, Qwen's text appears and grows
as the model writes it, labelled **"Qwen is writing · unverified draft"**; when System 2 has checked the answer, the
draft is replaced by the checked explanation (or by the labelled TEMPLATE card if the checks rejected it). Only the
checked result is stored, counted and embedded; a draft never is.

How the drafts travel: the runner reads System 2's event stream and keeps only the newest draft; a side thread posts
it to the backend (`POST /live/runs/{id}/stops/{i}/draft`) at most every `LIVE_DRAFT_INTERVAL_S`, so a slow Vercel
call never slows the stream. When System 2's final answer arrives, the runner drops any unsent draft, waits for the
post in flight, then posts `/analysis`, so a draft never lands after the checked answer. A failed draft post is
logged once and never fails the stop. Hops, measured latencies and deploy steps: `docs/STREAMING.md`.
Pace: `--travel-s 5 --work-s 3` (defaults). Docker instead: `COMPOSE_PROFILES=live docker compose up live-runner`
(open the tunnels on the host first, ports 18081-18084).

## 6. If something breaks
| Symptom | Fix |
|---|---|
| Page says "waiting for the robot runner" | runner not running or wrong `BACKEND_URL`/`ADMIN_TOKEN`; restart step 5 |
| A run is stuck "running" | press **Cancel** on `/live` (or it unblocks itself after 10 min) |
| Every card says TEMPLATE | `--check` says `system2 TEMPLATE`: on the box, `tmux kill-session -t llama`, start it again (AMD-RUNS §4), wait for READY, re-run `--check`. If `--check` is OK but cards still say TEMPLATE (`insufficient_context`, 0 drafts in the log), the Vercel backend predates the live-packet fix (it sent a `gaps` note, which makes System 2 skip the model): redeploy the backend |
| Laya UNAVAILABLE in the log | `tmux attach -t laya` on the box; the rules still decide, cards say so |
| `System 2 has no /v1/assess/stream (404)` in the log | the box's System 2 predates streaming: restart `s2` on the new code; until then cards fill in at the end (no drafts) |
| `draft post failed (404 ...)` in the log | the Vercel backend has no `/draft` endpoint yet: redeploy it; answers still arrive via `/analysis` |
| `draft post failed (409 ...)` in the log | the run was cancelled or finished while Qwen wrote: drafts stop for that stop; nothing to fix |
| A card shows "Qwen did not finish; its unverified draft was discarded" | that stop's `/analysis` post failed and the run ended, so `finish`/`cancel` marked it `error`; check the runner log for the analysis error |
| Drafts misbehave or flood the backend | `SYSTEM2_STREAM=0` (plain `/v1/assess`, exactly the pre-streaming behaviour), or raise `LIVE_DRAFT_INTERVAL_S` |
| `ssh spot-amd` refused | the box was relaunched: rebuild per `docs/HANDOFF-2026-10-11.md`; the demo still runs rules-only + template |
| Venue Wi-Fi blocks SSH | phone hotspot |

## What to say (honest)
- The robot's log at each stop is the challenge kit's row for that action, re-sent live under a new id; photos are
  simulated captures (kit sample images) and never decide red or green.
- Laya, the rules, vision, Qwen 27B on the AMD GPU, the Neon writes, the embeddings and the knowledge-graph rows all
  happen during the run. A template card is labelled TEMPLATE, never passed off as Qwen.
- The text you watch Qwen write is an unverified draft. The checks run on the finished answer, which replaces the
  draft; only that checked answer is stored.
