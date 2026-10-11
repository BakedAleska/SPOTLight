# System 1 service (Laya inference), port 8082

Loads one Laya checkpoint (base `convaiinnovations/laya` or our fine-tune) and answers the typed questions in
`triage/questions.py` for canonical SPOT events. Binding contract: `docs/backend-services-contract.md`
("System 1 service"). Callers send structured events, never text: the service renders each one with
`triage/render.py` `render_json`, the exact rendering the model was trained on.

| Endpoint | Returns |
|---|---|
| `POST /v1/predict` | answers for 1–64 events, all pairs batched together (one forward pass on GPU), temperature-calibrated |
| `GET /v1/questions` | `{"questions": QUESTIONS, "question_sets": QUESTION_SETS}` |
| `GET /v1/model` | checkpoint, `fine_tuned`, temperatures, `training_summary`, device, amp, `questions_match` |
| `GET /health` | `{"ok": true, "model_loaded": true, "device": "cpu", "logging": false}` (`logging`: `LOGS_DATABASE_URL` is set) |

```http
POST /v1/predict
{"request_id": "r-123",
 "items": [{"id": "act-A0051", "kind": "action", "event": {...canonical event...}, "questions": ["escalate"]}]}
```
- `kind` is `pre_action | action | mission`; `event` is the dict built by `triage/events.py`
  (`pre_action_event`, `action_event`, `mission_event`). Unknown keys are ignored. If `event.kind` is present,
  it must equal `kind`.
- `questions` is optional: a subset of `QUESTION_SETS[kind]`, answered in the order given.
- Response: `{"request_id", "model", "fine_tuned", "device", "latency_ms", "items": [{"id", "kind",
  "answers": {qid: {"answer", "probs", "confidence"}}}]}`, with items in request order.
- Errors: **422** names the bad field, e.g.
  `{"error": "items.1.event.asset.criticality: Input should be 'LOW', 'MEDIUM', 'HIGH' or 'CRITICAL'", "detail": [...]}`
  (`detail` is FastAPI's standard list with `loc`). More than 64 items gives a 422 on `items`. **500** means inference failed.

## Configuration (env)
| Var | Default | |
|---|---|---|
| `LAYA_CHECKPOINT` | `convaiinnovations/laya` | HF repo id or local checkpoint dir (`/models/laya` in Docker) |
| `LAYA_DEVICE` | `auto` | `auto` / `cpu` / `cuda` (ROCm also shows up as `cuda`) |
| `SPOT_AMP` | `1` | fp16 autocast on GPU; `0` = fp32 (use it if ROCm GEMMs fail) |
| `LOGS_DATABASE_URL` | unset | when set, every predict call is logged to `app.model_calls` (system=`system1`). Required on the AMD box (the `spot_mcp_user` pooled DSN, single-quoted in `/root/spot.env`; `deploy/amd-native/README.md` "System 1 call logging"). Unset: startup logs a WARNING and `/health` says `logging: false` |
| `PORT` / `HOST` | `8082` / `0.0.0.0` | |
| `LAYA_MAX_BATCH` | auto | (event, question) pairs per forward pass. Auto = 256 on GPU (any 64-event request is one pass), 16 on CPU (one 200-pair pass OOM-killed an 8 GB Docker VM; CPU throughput doesn't improve with batch size) |
| `LAYA_WARMUP` | `1` | run one tiny prediction at startup so the first real call isn't cold |
| `HF_HOME` | `/models/hf` in the image | HF cache; base weights (~842 MB) download here on first start |

Logging is fire-and-forget (a background thread with a bounded queue). A missing table or an unreachable database
only logs a warning; it never slows or fails a request. Each row stores `latency_ms`, `device`, `ok`/`error`,
`action_id` (single-event calls) and `metadata` = `{items, decisions, kinds, ids, forward_ms, fine_tuned}`.

## Docker: CPU (laptop)
The build context is the **repo root**, so the image can COPY `amd/system1/triage/`. `Dockerfile.dockerignore`
(a symlink to `.dockerignore`) allowlists only `triage/` and `service/`, so the context is ~100 kB.
```bash
docker build -f amd/system1/service/Dockerfile --build-arg TORCH_VARIANT=cpu -t spot-system1 .
# Reuse the host HF cache (no re-download), or use a named volume: -v hf-cache:/models/hf
docker run -d --name system1 -p 8082:8082 -v ~/.cache/huggingface:/models/hf spot-system1
curl localhost:8082/health
cd amd/system1 && python -m service.smoke            # 1 pre_action + 1 action + 1 mission from the kit
```
On Docker Desktop for Mac the container runs on CPU (no Metal in Linux containers). For MPS speed on a Mac, use
the native venv below; `LAYA_DEVICE=auto` picks `mps`.

## Docker: ROCm Linux host
```bash
docker build -f amd/system1/service/Dockerfile --build-arg TORCH_VARIANT=rocm-gfx1151 -t spot-system1:rocm .   # or rocm-gfx1201
docker run -d --name system1 -p 8082:8082 \
  --device=/dev/kfd --device=/dev/dri --group-add video --security-opt seccomp=unconfined \
  -v hf-cache:/models/hf spot-system1:rocm
curl localhost:8082/v1/model        # "device": "cuda:0" means the GPU is in use
```
If fp16 GEMMs fail on a new ROCm target, add `-e SPOT_AMP=0`. If the GPU resets, add
`-e TORCH_BLAS_PREFER_HIPBLASLT=0`. The build fails on purpose if `pip install laya` replaces the ROCm torch.

## Native venv (AMD Jupyter VM: no root, no Docker)
```bash
cd SpotTheDOT/amd/system1
python3 -m venv .venv && . .venv/bin/activate
pip install --index-url https://repo.amd.com/rocm/whl-multi-arch/ "torch[device-gfx1151]==2.12.0+rocm7.14.1"
#   gfx1201: pip install --index-url https://repo.amd.com/rocm/whl-multi-arch/ \
#              "torch==2.12.0+rocm7.14.1" "amd-torch-device-gfx1201==2.12.0+rocm7.14.1"
pip install -r service/requirements.txt
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"   # still +rocm, True
export TORCH_COMPILE_DISABLE=1 LAYA_CHECKPOINT=checkpoints/run2/best PORT=8082
nohup python -m service > service.log 2>&1 &
curl localhost:8082/health
```
The service must run from `amd/system1/` (it imports `triage`). The VM's port is usually only reachable through
the Jupyter proxy or an SSH tunnel: `ssh -L 8082:localhost:8082 <vm>`.

## Pointing at a fine-tuned checkpoint
A checkpoint is a directory with `rl_agent_config.json`, `model.safetensors`, `encoder/`, `tokenizer/` and
`questions.json` (written by `scripts/finetune.py` to `checkpoints/<run>/best/`). Its temperatures are read from
`rl_agent_config.json` and applied to every answer.
```bash
# Local dir, mounted read-only:
docker run -d -p 8082:8082 -v ~/.cache/huggingface:/models/hf \
  -v "$PWD/amd/system1/checkpoints/run2/best:/models/laya:ro" -e LAYA_CHECKPOINT=/models/laya spot-system1
# Hugging Face repo (pushed with finetune.py --push-to-hub); add -e HF_TOKEN=... if it's private:
docker run -d -p 8082:8082 -v hf-cache:/models/hf -e LAYA_CHECKPOINT=<user>/laya-spot-triage spot-system1
# Native:
LAYA_CHECKPOINT=checkpoints/run2/best python -m service
```
Check with `curl localhost:8082/v1/model`: `fine_tuned: true`, the fitted temperatures, `training_summary`, and
`questions_match: true` (the checkpoint's `questions.json` equals `triage/questions.py`).

## Measured (2026-10-10, MacBook arm64, Docker Desktop VM: 10 CPUs, 7.6 GB)
| Setup | Batch | Server `latency_ms` |
|---|---|---|
| Docker CPU image, base Laya | 3 events (pre_action + action + mission) = 10 decisions | ~8.2 s median (7.6–9.3 s) |
| Docker CPU image | 64 events = 222 decisions | 190 s, peak 3.5 GB RSS |
| Native macOS venv, CPU | 10 decisions | ~3.3 s |

CPU costs ~0.3–0.9 s per decision (ModernBERT-large, ~400 tokens each), so CPU is for development and tests
only. For the live demo, run on the AMD GPU (ROCm image or the native venv). Image: 1.55 GB on disk (CPU torch);
startup with a warm HF cache takes ~7 s (load 4.8 s + warmup 2.1 s).

## docker-compose snippet (for the root `docker-compose.yml`)
```yaml
  system1:
    build: {context: ., dockerfile: amd/system1/service/Dockerfile, args: {TORCH_VARIANT: cpu}}
    ports: ["8082:8082"]
    environment:
      LAYA_CHECKPOINT: ${LAYA_CHECKPOINT:-convaiinnovations/laya}
      LOGS_DATABASE_URL: ${LOGS_DATABASE_URL:-}
    volumes: ["hf-cache:/models/hf"]      # + "./amd/system1/checkpoints/run2/best:/models/laya:ro" for the fine-tune
# volumes: {hf-cache: {}}
```
The backend reaches it at `http://system1:8082`. The healthcheck allows a 10-minute start period for the first download.

## Tests
```bash
cd amd/system1 && pip install -r service/requirements-dev.txt
python -m pytest service/tests -q                          # 20 tests, ~20 s; loads real base Laya on CPU
SYSTEM1_SKIP_MODEL=1 python -m pytest service/tests -q     # validation and shape only (fake engine, ~4 s)
LAYA_CHECKPOINT=checkpoints/smoke/best python -m pytest service/tests/test_parity.py -q
```
- `test_api.py`: 422s name the field, contract shape, question subsets, the 64-item limit, one engine call per
  request, logging failure still gives a 200, and validation preserves `render_json` output for every kit event.
- `test_parity.py`: rebuilds holdout-mission M006 events from the kit, checks that their rendering equals the
  benchmark's `real:*:json` states, then checks that the service's answers equal `LayaRunner.predict` on those
  benchmark rows (argmax, and probabilities within 1e-4), and that the temperatures are applied.

## Files
`app.py` (routes, validation) · `engine.py` (LayaRunner wrapper, batching, model info) · `schemas.py` (event and
request models) · `calllog.py` (`app.model_calls`) · `config.py` (env) · `smoke.py` (live smoke test) ·
`__main__.py` (`python -m service`).
