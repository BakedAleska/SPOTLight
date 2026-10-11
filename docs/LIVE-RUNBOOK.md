# Live demo runbook (presenter laptop = Tyler's)

The judges watch https://spotlight-web-mu.vercel.app/live. The page reads Neon through the Vercel backend. Only the
**live runner** on this laptop can reach the AMD box, so it is the bridge: it claims the run an operator starts on the
page, calls Laya / vision / Qwen / the embedder through SSH tunnels, and posts every result to the Vercel backend.
How it works: `docs/backend-services-contract.md` "Live simulation". Never commit IPs, ports or keys.

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

## 3. Smoke check (one real request each; llama.cpp can keep `/health` OK while every answer fails)
```bash
python scripts/live_runner.py --tunnel spot-amd --check
```
Every line must say `OK`. `system2 TEMPLATE` means Qwen is not answering: restart llama-server (§6), then re-check.

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
Then press **Start route** on `/live`. The runner logs each stop (verdict, Laya, rules) and "Qwen answered" or
"TEMPLATE (why)". One route takes about 2 to 2.5 minutes; Qwen cards fill in a few seconds after each ping.
Pace: `--travel-s 5 --work-s 3` (defaults). Docker instead: `COMPOSE_PROFILES=live docker compose up live-runner`
(open the tunnels on the host first, ports 18081-18084).

## 6. If something breaks
| Symptom | Fix |
|---|---|
| Page says "waiting for the robot runner" | runner not running or wrong `BACKEND_URL`/`ADMIN_TOKEN`; restart step 5 |
| A run is stuck "running" | press **Cancel** on `/live` (or it unblocks itself after 10 min) |
| Every card says TEMPLATE | on the box: `tmux kill-session -t llama`, start it again (AMD-RUNS §4), wait for READY, re-run `--check` |
| Laya UNAVAILABLE in the log | `tmux attach -t laya` on the box; the rules still decide, cards say so |
| `ssh spot-amd` refused | the box was relaunched: rebuild per `docs/HANDOFF-2026-10-11.md`; the demo still runs rules-only + template |
| Venue Wi-Fi blocks SSH | phone hotspot |

## What to say (honest)
- The robot's log at each stop is the challenge kit's row for that action, re-sent live under a new id; photos are
  simulated captures (kit sample images) and never decide red or green.
- Laya, the rules, vision, Qwen 27B on the AMD GPU, the Neon writes, the embeddings and the knowledge-graph rows all
  happen during the run. A template card is labelled TEMPLATE, never passed off as Qwen.
