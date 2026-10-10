# Smoke after the application fixes (2026-10-10)

**Label: untuned model after application fixes — regression smoke, not the comparison baseline.** Same Q8_0
deployment as `results/archive/untuned-q8-original-2026-10-10/` (model, llama.cpp build and flags, ROCm, hardware
unchanged; see that archive's `box/env/`). Each run is 4 cases × 2 calls, all over HTTP with
`SYSTEM2_CACHE_DIR=off`; every call's `X-System2-Cache` header says `off`, so each is a genuine generation.

| Run | Code on the box | Prompt | What changed vs. the previous run |
|---|---|---|---|
| `run6-after-fixes-smoke` | `0c15585` + `scripts/photo_smoke.py` (sha256 `9a5e94ad…`, committed unchanged as `b3a5d85`) | `analyst-prompt-2026-10-10.4` | short reviewed few-shot answers; `MIN_IMAGE_CALL_S` 4 → 9 s; recorder + cache-off |
| `run7-after-fixes-smoke` | `db3fb1d` (clean) | `analyst-prompt-2026-10-10.5` | no few-shot about the flag's own kit event |

Cases (`scripts/photo_smoke.py`): `gauge_photo` and `thermal_photo` are the kit photos through the vision service
(`amd/vision`, default settings, version 0.1.0) with `pairing: demo`, built like the orchestrator with
`DEMO_IMAGES=1`; `gauge_disagree` is the multimodal fixture (1x1 PNG placeholder, linked `V1` row); `text_equipment`
is the text-only backend fixture. `thermal_photo` uses held-out case `heldout-A0118` as a smoke input only. The two
kit photos are regression cases already debugged in the original archive, not fresh test evidence.

## Results
| Case | Run 6 (prompt .4) | Run 7 (prompt .5) |
|---|---|---|
| gauge_photo | 2/2 `retry`, final text-only (`X-System2-Image: text_only`), 20.2 / 18.3 s | 2/2 `llm`, image sent, 11.1 / 8.2 s |
| thermal_photo | 2/2 `llm`, image sent, 9.8 / 8.6 s | 2/2 `llm`, image sent, 11.2 / 8.6 s |
| gauge_disagree | 2/2 `llm`, image sent, cites `V1`, 11.6 / 10.6 s | 2/2 `llm`, image sent, cites `V1`, 12.9 / 10.2 s |
| text_equipment | 2/2 `llm`, 10.9 / 7.9 s | 2/2 `llm`, 10.7 / 8.0 s |

- Thermal (template fallback at 25.0 s in the original archive): now a complete 174-char answer on the first
  attempt in both runs.
- Run 6 gauge: attempt 1 (image) said "on COMP-214", an asset no cited row names; `ungrounded_entity`, correctly
  rejected. The name came from the `kit-data_quality` few-shot, which is the same kit event (A0030). Attempt 2 went
  text-only directly with the remaining ~13 s (no doomed image call: the 9 s floor) and was accepted in 8.9 s. The
  run-7 fix (`select_examples` skips the flag's own event) removed the leak.
- "Image sent" (`used`) means the accepted answer came from a request that carried the image. It does **not**
  mean the photo supported the recommendation: no photo answer cites a `V#` row (demo rows must not judge the asset),
  and the gauge reader's row is LOW trust (1.0 psi, confidence 0.3, `AMBIGUOUS_NEEDLE`).
- Run 7 gauge answer contains a model typo ("reinsect now"); the checker does not check spelling.
- Grounding pass = mechanical consistency with cited rows, not semantic correctness.

## Files per run
`run-info.txt` (commit, script hash, log line range, start time), `console.txt` (stdout), `results.jsonl` (one line
per call: headers, wall time, response, vision summary), `system2-records.jsonl` (recorder: raw text of every
generator call, attempts with checker errors, deadline, prompt version, packet and image hashes),
`*.request.json` (exact System 2 request bodies), `*.vision.json` + `*.annotated.jpg` (vision responses),
`system2-server.log`, `llama-server.segment.log` (this run's lines of the llama-server log), `SHA256SUMS`
(hashed on the box, verified after transfer). The vision service's own result cache was on (default) and had
analysed these exact photos during the original archive's run 5, so every vision call here returned in 2-9 ms from
that cache (same readings as run 5); the vision step was not re-measured in these runs.
