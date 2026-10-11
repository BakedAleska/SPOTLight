# Live simulation plan (draft, 2026-10-10)

**Goal:** in front of the judges, an operator watches SPOT walk a route on the kit's floor plan. At each stop the robot
"captures" a photo, its log goes through Laya live, and a ping lands on the map: **red = needs human attention**,
**green = all good**. Both colours carry the photo and Qwen's live explanation in a feed under the map, like a SOC
dashboard. Everything downstream of the robot's log is computed live and written to Neon while the judges watch.

Status (2026-10-11): built on branch `live-testing` as specified in `docs/LIVE-DEMO-PROMPT.md`: migration `013_live.sql`
(011/012 were taken by chat), `/api/v1/live/*`, web `/live`, `scripts/live_runner.py`, runbook `docs/LIVE-RUNBOOK.md`.
Difference from §2: the runner posts to the Vercel backend (no backend on the laptop) and the orchestrator is not
used (the runner calls vision and System 2 directly, so green stops need no flag). Decisions: `research/decisions.md`.

## 1. What is live and what is not (say this to judges)
| Piece | Source | Live? |
|---|---|---|
| Route order, stop timing | the five routes in §4, re-timed to "now" | scripted |
| Robot log at each stop (status, battery, reading, anomaly) | the kit row for that action (`actions/readings/anomalies.csv`), sent as a new event | kit data, not invented |
| Photo at each stop | fixed set: 6 kit sample images + MeasureBench gauge photos, picked by action type | **simulated capture**, labelled on screen |
| Laya verdict (escalate / priority) | System 1 on the AMD GPU | live |
| Rules verdict | `policy.py` validator, in parallel | live |
| Red / green | the combined decision (`TRIAGE_MODE=parallel`) | live |
| Vision readings on the photo | vision service (OCR + code readers) | live |
| Qwen explanation | Qwen3.8-27B Q8 + mmproj on the AMD box | live |
| Rows in Neon (events, flags, captures, embeddings) | written as each stop finishes | live |

Rules kept from the handoff: photos are labelled as simulation, they never judge the asset (decision "demo images
never judge the asset"), and a template fallback is shown as a template, never as Qwen.

## 2. The blocker: Vercel cannot reach the AMD box
The box is reachable only over SSH from the laptop. Vercel functions cannot open that tunnel. So the **laptop is the
bridge**:

```
AMD box (GPU)                 Presenter laptop                          Cloud
llama-server :8080 (Qwen)  ──ssh -L──►  live runner (new script)
Laya :8082                 ──ssh -L──►   ├─ backend ingest (Laya + rules) ──► Neon ◄── Vercel backend ◄── Vercel web
vision :8084               ──ssh -L──►   ├─ vision → System 2 (Qwen)     ──► Neon        (judges watch this)
embed :8081                ──ssh -L──►   └─ embed summary                ──► Neon pgvector
```
- The laptop runs the backend + System 2 + orchestrator against Neon (`COMPOSE_PROFILES=neon`, `docs/RUNNING.md`
  modes 2 and 3) with the four SSH tunnels.
- The Vercel site needs no access to the box: it reads Neon through the Vercel backend, so pings appear within one
  poll (2 s).
- **Start button:** the operator presses "Start route R2" on the Vercel site, which writes a `live_runs` row
  (`pending`) to Neon. The laptop runner polls Neon, claims it and starts walking. No inbound connection to the
  laptop is needed.

**Box status right now:** `ssh spot-amd` is refused (`connection refused` on the saved IP/port). It has probably been
relaunched, which wipes `/root`. Rebuild per `docs/HANDOFF-2026-10-11.md` (repo bundle, ROCm wheel, llama.cpp
`23b0202` from `local-artifacts/box-rebuild/`). Fallback without the box: Laya on CPU in Docker on the laptop
(2–3 s per event, fine), Qwen unavailable, so explanations show as **template**.

## 3. What to build
| # | Piece | Where | Size |
|---|---|---|---|
| 1 | Restore the box: llama-server (Qwen 27B Q8 + mmproj), Laya native venv, vision, embed | box | 1–2 h |
| 2 | Migration `011`: `live_runs` (run_id, route_id, status, current_stop, timestamps) and `live_captures` (run_id, stop, location, asset, event_id, flag_id, verdict, laya, rules, photo path + sha256, vision, qwen, source, timings) | `db/` | 30 min |
| 3 | Ingest accepts `details.reading` / `details.anomaly` / `details.source_action_id` for actions the kit doesn't know, so every live run uses fresh action ids (today `act-A0194` would update the seeded flag instead of creating a new one) | `backend/app/services/reference.py` + contract | 45 min |
| 4 | Routes: `GET /live/runs`, `GET /live/captures?run_id&after` (public), `POST /live/runs` (admin token, presenter console) | backend | 45 min |
| 5 | Runner `scripts/live_runner.py`: claim run → MISSION_STARTED → per stop: ACTION_STARTED, travel wait, ACTION_COMPLETED/FAILED through ingest (Laya + rules → flag) → photo to vision → System 2 with image → `live_captures` row → embed summary into `documents` → MISSION_COMPLETED | laptop | 2 h |
| 6 | Orchestrator: `flag_id` optional when `context` is given, so green stops (no flag) use the same vision → System 2 path as red ones | `amd/orchestrator` + contract | 30 min |
| 7 | Web `/live`: reuse `PathPreview`'s map; pings coloured by `live_captures.verdict`; SPOT marker at the latest stop; under the map a newest-first card feed (photo, LIVE LAYA badge, rules fired, vision readings, Qwen answer / reasoning / action, "needs human verification" + Acknowledge on red) | `web/` | 2–3 h |
| 8 | Dry-run all 40 stops through Laya; swap any stop whose live verdict disagrees with the plan (or keep it as a "Laya missed, rules caught it" story) | laptop | 30 min |

Pace: travel 4–6 s, Laya < 1 s on GPU, vision 1–3 s, Qwen 11–25 s with an image. Qwen runs in the background while
the robot walks on, so a card shows "Qwen analysing…" and then fills in. One 8-stop route ≈ 2–2.5 min.
Items 2–7 merge to `main`; CI deploys them to Vercel automatically (`deploy-vercel.yml`).

## 4. Five routes (8 stops, A → H)
Built from the kit with the project's own triage policy (`amd/system1/triage/policy.py`, the rulebook Laya was
fine-tuned on). Red = policy P1/P2; green = P4 inspection with a GOOD reading. Battery never rises along a route.
Every route has a different set of red stories (12 alerts across 40 stops). Laya's live verdict is checked in step 8.

**R1 · East side, two alarms early** (WP05 → WP10 → WP09 → WP07 → WP08 → WP06 → WP04 → WP01)
| Stop | WP | Action | Asset | Type | Batt | Log | Verdict |
|---|---|---|---|---|---|---|---|
| A | WP05 | A0202 | PANEL-203 | visual | 91 | score 79 | green |
| B | WP10 | A0233 | PUMP-205 | acoustic | 82 | FAILED sensor timeout, ABNORMAL_VIBRATION HIGH | **red P2** |
| C | WP09 | A0245 | PANEL-212 | gauge | 82 | 99.0 psi | green |
| D | WP07 | A0194 | HX-204 | thermal | 80 | 41.4 °C looks normal, but ABNORMAL_TEMPERATURE **CRITICAL** | **red P1** |
| E | WP08 | A0250 | HX-201 | visual | 72 | score 96 | green |
| F | WP06 | A0042 | HX-217 | thermal | 66 | 64.6 °C | green |
| G | WP04 | A0094 | COMP-214 | gauge | 51 | 93.8 psi | green |
| H | WP01 | A0036 | PANEL-215 | thermal | 49 | 74.9 °C | green |

**R2 · West wing** (WP01 → WP02 → WP03 → WP04 → WP06 → WP05 → WP07 → WP10)
| Stop | WP | Action | Asset | Type | Batt | Log | Verdict |
|---|---|---|---|---|---|---|---|
| A | WP01 | A0217 | PANEL-215 | acoustic | 73 | 68.4 dB | green |
| B | WP02 | A0237 | HX-216 | thermal | 66 | 62.5 °C | green |
| C | WP03 | A0270 | HX-208 | gauge | 61 | 125.5 psi | green |
| D | WP04 | A0044 | COMP-214 | acoustic | 55 | 79.7 dB | green |
| E | WP06 | A0133 | HX-217 (CRITICAL) | gauge | 54 | FAILED, GAUGE_OUT_OF_RANGE HIGH | **red P1** |
| F | WP05 | A0074 | PANEL-203 | visual | 50 | score 95 | green |
| G | WP07 | A0147 | HX-213 | acoustic | 42 | FAILED signal noise, ABNORMAL_VIBRATION HIGH | **red P1** |
| H | WP10 | A0096 | PUMP-205 | visual | 39 | score 94 | green |

**R3 · Two floors, starts at the Inspection Dock** (WP15 → WP12 → WP11 → WP13 → WP14 → stairs → WP07 → WP05 → WP01)
| Stop | WP | Action | Asset | Type | Batt | Log | Verdict |
|---|---|---|---|---|---|---|---|
| A | WP15 | A0203 | VALVE-207 | acoustic | 88 | FAILED, reading unreliable (robot's sensor, not the valve) | **red P2** |
| B | WP12 | A0262 | VALVE-207 | acoustic | 86 | 70.5 dB | green |
| C | WP11 | A0193 | HX-210 | thermal | 81 | 43.8 °C | green |
| D | WP13 | A0223 | HX-213 | thermal | 81 | 53.6 °C | green |
| E | WP14 | A0206 | GAUGE-209 | gauge | 78 | 110.1 psi | green |
| F | WP07 | A0236 | HX-204 | visual | 70 | score 99 | green |
| G | WP05 | A0062 | HX-202 (CRITICAL) | visual | 68 | score 71, just under the 72 band | **red P2** |
| H | WP01 | A0082 | PANEL-215 | acoustic | 60 | 70.7 dB | green |

**R4 · Process loop, three alerts** (WP01 → WP10 → WP09 → WP08 → WP03 → WP02 → WP05 → WP06)
| Stop | WP | Action | Asset | Type | Batt | Log | Verdict |
|---|---|---|---|---|---|---|---|
| A | WP01 | A0217 | PANEL-215 | acoustic | 73 | 68.4 dB | green |
| B | WP10 | A0002 | PUMP-205 | visual | 69 | score 85 | green |
| C | WP09 | A0132 | PANEL-212 | gauge | 59 | FAILED, GAUGE_OUT_OF_RANGE HIGH | **red P1** |
| D | WP08 | A0144 | GAUGE-209 | visual | 57 | score 80 | green |
| E | WP03 | A0145 | HX-208 | acoustic | 52 | 82.4 dB, over the 82 dB band | **red P2** |
| F | WP02 | A0124 | HX-213 | visual | 52 | score 98 | green |
| G | WP05 | A0074 | PANEL-203 | visual | 50 | score 95 | green |
| H | WP06 | A0045 | HX-217 (CRITICAL) | gauge | 49 | 74.3 psi, under the 76 psi band | **red P2** |

**R5 · Battery cliff** (WP01 → WP04 → WP06 → WP05 → WP07 → WP08 → WP09 → WP03)
| Stop | WP | Action | Asset | Type | Batt | Log | Verdict |
|---|---|---|---|---|---|---|---|
| A | WP01 | A0217 | PANEL-215 | acoustic | 73 | 68.4 dB | green |
| B | WP04 | A0219 | COMP-214 | thermal | 70 | 77.3 °C | green |
| C | WP06 | A0013 | HX-217 (CRITICAL) | acoustic | 64 | 85.0 dB, over band | **red P2** |
| D | WP05 | A0143 | HX-202 | visual | 62 | score 86 | green |
| E | WP07 | A0064 | PANEL-211 | visual | 57 | score 85 | green |
| F | WP08 | A0144 | GAUGE-209 | visual | 57 | score 80 | green |
| G | WP09 | A0160 | PANEL-212 | visual | 19 | FAILED, VISUAL_DEFECT HIGH at 19 % battery | **red P1** |
| H | WP03 | A0100 | HX-208 | gauge | 14 | 150.6 psi at 14 % battery | **red P2** |

R5 drops from 57 % to 19 % in one hop (the kit's battery cliff, insight #2); say so if asked.
Photo per type: visual → `visual_pump_motor_01` / `valve_detail_01` / `pipe_gallery_01`, thermal →
`thermal_pump_bearing_01`, acoustic → `acoustic_valve_01`, gauge → `gauge_pressure_01` or a MeasureBench photo
(credited).

## 5. Demo script (3 min)
1. Open `/live`, pick **R2** and press Start (≈ 2 min run). Narrate the green stops briefly.
2. At stop E the map pings red. Read the card: Laya's verdict, the rule that fired, the photo and Qwen's explanation.
   Press Acknowledge.
3. While it finishes, switch to the history of earlier runs (R1, R4, R5) to show other alert types. These are real
   earlier live runs, timestamped.

## 6. Risks
- Box down or relaunched (it is now). Rebuild early; CPU Laya + template is the fallback.
- llama.cpp "empty grammar stack" bug: `/health` stays OK while every answer falls back to the template. Before the
  demo, check one real request, not just `/health`, and keep a restart command ready.
- Venue Wi-Fi blocking SSH: keep a phone hotspot ready.
- Laya disagreeing with the plan on a stop: step 8 catches it before the demo.
