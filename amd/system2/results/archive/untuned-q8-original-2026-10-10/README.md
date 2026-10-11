# Original untuned deployment results (2026-10-10), including known failures

**Label: original untuned deployment.** Qwen3.8-27B, no fine-tune, Q8_0, on the SSH W7900D. This archive is
the record of what the deployment did *before* any application fixes for the comparison. It includes the known
failures. **Do not overwrite or edit it**; later runs go in new directories.

Archived off the disposable box on 2026-10-10. Every file under `box/` was hashed on the box (`box/SHA256SUMS`)
and verified after transfer (47/47 OK). `images/` and `run-1-phase4/` carry their own `SHA256SUMS`.

## Two kinds of reproduction
1. **Rebuild from recorded outputs** (`report.py --rebuild DIR`): recomputes every metric from `cases.jsonl` and
   the checked-in held-out dataset, with no model involved. It must reproduce the saved metrics exactly; it did
   (`run-1-phase4/rebuild-check.txt`: `rebuild: identical`, on the box at edae3db, against files whose hashes match
   this archive).
2. **Rerun live inference**: same model, flags and code, new generations. Wording, retries and timing can differ
   (temperature is 0, but timings always vary and llama.cpp does not guarantee identical text across builds,
   batch or cache states). A rerun is a new measurement, never a substitute for these records.

## Runs (kept separate: they used different code revisions and cache settings)
| # | What | When (UTC) | Repo revision on the box | Cache | Records |
|---|---|---|---|---|---|
| 1 | Phase 4 held-out report, `report.py --runner both` (38 flags + 6 dev) | 20:21-20:29 | `df63f64` clean | none (report.py calls llama-server directly) | `run-1-phase4/` (= `results/report/amd-qwen38-27b-q8/`), `box/scripts/step5.sh`, `box/logs/step5.log` |
| 2 | Step 6 smoke, original: text fixture + `gauge_disagree.json` (1x1 PNG placeholder) | 20:29-20:31 | `df63f64` clean | `/root/s2cache` (fresh) + precompute into `/root/s2cache-fresh` | `box/scripts/step6.sh`, `box/logs/step6.log`, `box/logs/system2.log`, `box/caches/s2cache-step6-original/`, `box/caches/s2cache-step6-precompute/` |
| 3 | Multimodal diagnosis, in-process (raw attempt text) | 20:33-20:47 | `df63f64` clean | n/a (in-process) | `console-captures/02-*` |
| 4 | Step 6 smoke after the cited-ID grounding fix | 20:50-20:51 | `df63f64` + uncommitted fix identical to `edae3db` | `/root/s2cache-fix` (fresh) | `console-captures/03-*`, `box/logs/system2-fix.log`, `box/caches/s2cache-step6-after-fix/`, `box/smoke-real/last-step6-*` |
| 5 | Real kit photos (gauge, thermal) through the vision service + System 2, `pairing: demo` | 21:04-21:10 | `edae3db` clean + `box/scripts/realimg.py` | default `amd/system2/cache` (ON; one gauge call was a hit, then cleared per run) | `console-captures/04-*`, `05-*`, `box/smoke-real/realimg-*-body.json`, `images/`, `box/logs/vision.log`, `box/logs/system2-real.log` |

`box/logs/llama-server.log` covers all five runs (per-request prompt/eval timings, image-chunk token counts,
cancellations).

## Known failures recorded here (not fixed in these runs)
- Run 1: p95 15003 ms against the 15 s budget (`p95 within the 15 s live budget | no`); 2/38 template fallbacks,
  2/40 attempts timed out. The user accepted this latency for the project (`research/decisions.md`); the report's
  own verdict against 15 s stands.
- Run 2: the multimodal fixture fell back to the template both times: first answer rejected for namespaced IDs
  in claim text, retry text-only and out of time.
- Run 5, thermal: template fallback both times at 25.0 s; attempt 1's `answer` stopped at exactly 240 characters
  mid-value (`anomaly_type=1`) and was rejected; the attempt-2 image call started with ~5 s left and was cancelled,
  then the text-only resend timed out.
- Run 5, gauge: the code gauge reader returned `V1` = 1.0 psi, confidence 0.3, LOW (`AMBIGUOUS_NEEDLE`,
  `LABEL_OUTLIER`), so no usable image reading; System 2 answered from logged rows (model path, image sent).
- No answer in run 5 cited a `V#` row; demo-paired rows must not judge the asset, so this is by design.

## Configuration
- **Model:** `unsloth/Qwen3.8-27B-GGUF` at revision `4ca720788d1e01f1bff70c033e0d0028fd02e502`,
  `Qwen3.8-27B-Q8_0.gguf` (29,047,086,048 B, sha256 `a680f44a…e348`) + `mmproj-F16.gguf` (927,607,488 B,
  sha256 `cbb841a9…4e43e`), downloaded via hf-mirror.com and hash-checked against huggingface.co's API
  (`box/env/model-files.sha256`, `box/logs/dl.log`). Original checkpoint (GGUF metadata,
  `box/env/gguf-metadata.txt`): `Qwen/Qwen3.8-27B`, architecture `qwen35`, vision projector `qwen3vl_merger`.
  Binaries are not in Git; re-download with `box/scripts/dl-model.sh` and verify the hashes.
- **llama.cpp:** commit `23b0202a189c44a54625aadcb37a946dd1d6278d`, HIP build for gfx1100
  (`box/env/llama-cpp.txt`, `box/scripts/build-llama.sh`, `box/logs/build.log`); launch flags in
  `box/env/llama-server-cmdline.txt` (`-ngl all -fa on -c 16384 -np 1 -rea off --image-max-tokens 1024 --metrics`).
- **ROCm:** pip `rocm` 10.0.0 in `/opt/venv`, plus `rocm-sdk-devel` 10.0.0rc4 (`--no-deps`, `rocm-sdk init`) for the
  HIP CMake files; HIP 7.15.26333; `LD_LIBRARY_PATH` = devel `lib` (`box/env/rocm.txt`,
  `box/env/rocm-devel-wheel.sha256`). Setup steps: `docs/AMD-RUNS-2026-10-10.md` ("Run log") and `box/scripts/env.sh`.
- **Python deps:** `box/env/pip-freeze-{opt-venv,s2env,visenv}.txt`.
- **Hardware:** `box/env/host.txt`, `box/env/amd-smi-static.txt` (Radeon Pro W7900D, gfx1100, 48 GB; container
  16 CPUs / 55 GiB).
- **Prompt and few-shots (run 1):** `analyst-prompt-2026-10-10.3`, prompt sha256
  `1a20523c5eaf3eaa64bfb1b66b709b77f0c5b5d8a4f2ef17e884b2af6e1132dc`, examples `kit-equipment`, `kit-navigation`,
  `kit-data_quality` (recorded per case in `cases.jsonl`); source `amd/system2/analyst/prompts.py` at `df63f64`.
  Runs 2-5 use the same prompts.py (unchanged in `edae3db`).
- **Generation:** temperature 0, thinking off, max_tokens 350, JSON-schema response format (answer <= 240 chars,
  claim <= 160 chars, <= 3 evidence items). Deadlines: text 15 s, image 25 s (`TEXT_RESERVE_S` 8 s,
  `MIN_IMAGE_CALL_S` 4 s). No seed is sent.
- **Validator:** `amd/system2/analyst/grounding.py` at `df63f64` (runs 1-3) and with the `_mask_cited` fix (runs 4-5,
  = `edae3db`). Contract `2026-10-10.backend.2`.
- **Vision service (run 5):** `amd/vision` at `edae3db`, `scripts/serve.py` with default settings, version `0.1.0`;
  orchestrator row building with `VISION_TRUST_MIN` 0.5, `OCR_TOP_N` 8 (defaults).
- **Datasets:** held-out `data/heldout_packets.json` content hash `588700df763b…`, file sha256 `4e0be101…`, 38
  case IDs and packet-set hash in `run-1-phase4/metrics.json` → `identity`. Run 5 thermal used held-out case
  `heldout-A0118` as a smoke input only (never training or few-shot); gauge used kit fixture
  `fixtures/backend/data_quality.json` (flag 14, action A0030).

## Not captured (do not reconstruct)
- Raw model text for run 1's attempts: `report.py` records statuses, errors, tokens and timings, not the text.
- Raw text of run 2's HTTP attempts and of runs 4-5's HTTP attempts: `serve.py` does not log it. Raw text exists
  only for the in-process diagnostic calls in `console-captures/02-*` and `05-*`.
- Full vision-service JSON responses (run 5): only the derived rows/OCR/alert/router printed by the script, and
  the rows, OCR and annotated image embedded in `box/smoke-real/realimg-*-body.json`, were kept.
- The cache entry from run 5's first gauge generation (deleted when the cache was cleared between runs).
- Per-run `pip freeze` at the exact time of each run: captured once, after run 5 (no packages changed in between
  except `gguf`, installed into `s2env` while archiving, and `pytest`, installed before run 4).
- GPU clocks/power during runs.
