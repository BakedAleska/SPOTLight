# amd/embed: embedding service + RAG ingestion (System 2 context)

The RAG half of `docs/system2-rag-contract.md`. `server.py` serves embeddings over the contract's OpenAI-compatible
API, `client.py` calls it, and `ingest.py` builds the six corpora into Neon's `documents` table (pgvector).
The schema, search functions and seeding live in `db/` (see `db/README.md`).

| File | What it does |
|---|---|
| `server.py` | FastAPI: `POST /v1/embeddings`, `GET /health`, `GET /v1/models`. sentence-transformers on PyTorch (ROCm on the AMD box, MPS/CPU locally). Model from `EMBED_MODEL`; loads in the background, `/health` is 503 until ready. Responses carry a `timing` block (ms, tokens/s, device) for the AMD latency slide. |
| `client.py` | `embed(texts, kind="query"\|"doc")`: adds `EMBED_QUERY_PREFIX` / `EMBED_DOC_PREFIX` (an unset query prefix means the Qwen3 instruction the backend and MCP use; set but empty means none), batches (`EMBED_CLIENT_BATCH`, default 16), retries each batch (5 attempts, 1/2/4/8 s backoff on transport errors and HTTP 429/502/503/504), and checks one vector of `EMBED_DIM` values per input. `EMBED_MODEL` defaults to `Qwen/Qwen3-Embedding-0.6B`; `check_health()` stops before any write when the server reports another model or dimension; `wait_ready()` waits up to `EMBED_WAIT_S` (900) and fails at once on an unresolvable host. Works against llama-server/TEI too. |
| `ingest.py` | Builds `event` (297), `incident` (98), `policy` (23), `manual` (85) docs; `operator_note` and `explanation` come from `acknowledgements` / `assessments` at runtime. Stable `doc_id`s: re-runs only embed new or changed docs (no transaction is open while embedding; the prune keeps `metadata.source = 'live'` docs). Incident docs list policy rules only; manual titles read `Spot User Guide p.<printed> (PDF p.<page>): <heading>`. Logs to `app.job_runs` and every embedding batch to `app.model_calls` (endpoint `/v1/embeddings`). |
| `Dockerfile` | The embedding service image (`TORCH_VARIANT=cpu\|rocm-gfx1151\|rocm-gfx1201`). |

**Model** (research/tech-embedding-model-amd-2026-10-10.md): `Qwen/Qwen3-Embedding-0.6B`, 1024 dims, Apache-2.0,
query instruction `Instruct: Given an operator question about a SPOT robot inspection, retrieve relevant inspection
events, anomaly notes, operator notes, and Spot manual passages\nQuery:`, no document prefix. Fallback:
`Snowflake/snowflake-arctic-embed-l-v2.0` (1024 dims, query prefix `query: `). The model is pure config
(`EMBED_MODEL`, `EMBED_DIM`, prefixes, see `.env.example`). Embed the corpus and the queries with the **same
backend**; after changing the model run `python amd/embed/ingest.py --force`.

## Run locally (Mac: MPS or CPU)
```bash
cd SpotTheDOT                                   # repo root
python3 -m venv amd/embed/.venv && . amd/embed/.venv/bin/activate
pip install torch                               # CPU/MPS wheel from PyPI
pip install -r amd/embed/requirements.txt -r db/requirements.txt
cp amd/embed/.env.example amd/embed/.env && set -a && . amd/embed/.env && set +a

./db/local_pgvector.sh                          # Docker pgvector on :5433, prints DATABASE_URL
python amd/embed/server.py &                    # :8081; first start downloads ~1.2 GB into ~/.cache/huggingface
python db/setup.py migrate && python db/setup.py seed
python amd/embed/ingest.py                      # ~1 min on an M-series Mac (MPS)
python db/smoke_test.py --roles                 # 3 test queries + role permissions
```

## Run on the AMD VM (Jupyter, no root, so no Docker: run natively)
```bash
git clone https://github.com/JylerTackson/SpotTheDOT.git && cd SpotTheDOT
python3 -m venv amd/embed/.venv && . amd/embed/.venv/bin/activate
# 1) AMD's PyTorch FIRST (amd/system1/README.md). gfx1151 (Strix Halo):
pip install --index-url https://repo.amd.com/rocm/whl-multi-arch/ "torch[device-gfx1151]==2.12.0+rocm7.14.1"
#    gfx1201 (R9700) instead:
# pip install --index-url https://repo.amd.com/rocm/whl-multi-arch/ "torch==2.12.0+rocm7.14.1" "amd-torch-device-gfx1201==2.12.0+rocm7.14.1"
python -c "import torch; print(torch.__version__, torch.version.hip, torch.cuda.is_available())"   # +rocm, a hip version, True
# 2) the rest. requirements.txt has no torch, so pip keeps the ROCm wheel. Re-check afterwards.
pip install -r amd/embed/requirements.txt -r db/requirements.txt
python -c "import torch; print(torch.__version__)"                                                # still +rocm?
# 3) serve (bf16 on the GPU automatically; don't enable flash_attention_2 on gfx1151)
export EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B EMBED_DIM=1024 HF_HOME=$HOME/hf-cache
nohup python amd/embed/server.py > embed.log 2>&1 &
curl -s localhost:8081/health                  # "device": "cuda", "hip": "...", "gpu": "AMD Radeon ..."
# 4) ingest into Neon from the same box (or from anywhere that can reach EMBED_URL)
set -a && . amd/embed/.env && set +a           # DATABASE_URL = the Neon URL
python db/setup.py migrate && python db/setup.py seed && python amd/embed/ingest.py
```
Fallback server (same API): `llama-server -m Qwen3-Embedding-0.6B-Q8_0.gguf --embedding --pooling last -ub 8192 --port 8081`
(see the research file). If you switch backends, re-embed with `ingest.py --force`.

## Docker
```bash
# CPU (any machine)
docker build --build-arg TORCH_VARIANT=cpu -t spot-embed amd/embed && docker run -p 8081:8081 -v hf-cache:/models spot-embed
# ROCm on a Linux host with an AMD GPU and Docker (NOT the AMD Jupyter VMs: no root, no Docker; run natively there)
docker build --build-arg TORCH_VARIANT=rocm-gfx1151 -t spot-embed:rocm amd/embed      # or rocm-gfx1201
docker run --device=/dev/kfd --device=/dev/dri --group-add video -p 8081:8081 -v hf-cache:/models spot-embed:rocm
# Optional: bake the model into the image (bigger image, no first-start download)
docker build --build-arg TORCH_VARIANT=cpu --build-arg BAKE_MODEL=1 -t spot-embed:baked amd/embed
```
Config via the contract env vars (`-e EMBED_MODEL=... -e EMBED_DIM=...`). The model downloads on first start into
the `/models` volume (`HF_HOME`); the healthcheck allows 10 minutes for that. Tested 2026-10-10: CPU image 1.87 GB,
first start 317 s (download), healthy, 1024-d vectors identical to the native MPS server (cosine 1.0000). CPU in
Docker Desktop is slow (~2.3 s per single query vs ~60 ms on native MPS); use it for wiring, not for demos.
