# System 2 "Analyst": explaining escalated flags

System 1 (Laya + the deterministic validator, `amd/system1`) decides **what** needs attention. System 2 explains **why it
matters and what to do**: it takes one escalated flag plus a short context packet and returns Orbit-style JSON
(question / answer / reasoning / recommended action). It never decides what gets flagged, and every claim must cite a
row in the packet.

Plan and evidence: `research/tech-system2-context-distill-2026-10-10.md` (recommendations R1–R7).
The authoritative HTTP contract is [docs/backend-services-contract.md](../../docs/backend-services-contract.md)
(`2026-10-10.backend.2`). Backend owns retrieval through MCP/SQL fallback and supplies `context`.
The HTTP path does not build its own kit/Neon packet. Phase 1 `analyst/contract.py`, `schema/`, and
`fixtures/*.json` remain legacy offline artifacts, including inputs for the serving feasibility smoke.

## Current backend HTTP transport

`POST /v1/assess` takes strict positive integer `flag_id`, object `flag`, and object `context`, with optional
`request_id`, `event`, and `reasons`. The flat response contains `evidence[{claim,ids}]`, `answer`, `question`,
`recommended_action`, `confidence` (`low | medium | high`), `insufficient_context`, `flag_id`, `model`,
`latency_ms`, `tokens_per_sec`, and `path`. Actions use uppercase MCP vocabulary and packet membership;
`MANUAL_REVIEW` is the documented safe extension. Citations resolve to actual supplied content.
Live mode (Phase 3): with `SYSTEM2_LLAMA_URL` and `SYSTEM2_MODEL` set, `/v1/assess` runs the checked pipeline
(`analyst/live.py`: at most two llama-server attempts in 15 s, then the checked template) and reports `model`,
`latency_ms`, `tokens_per_sec` and `path` from that run; unset, it serves the stub (`model=stub`). Checked
`llm`/`retry` explanations are cached in `SYSTEM2_CACHE_DIR` (default `amd/system2/cache`, gitignored) under
`(flag_id, packet_hash, model, prompt_ver)`, re-checked before serving, and marked by `X-System2-Cache`;
`scripts/precompute.py` fills the same cache. Runbook: [docs/RUNNING.md](../../docs/RUNNING.md).

The published response schema enforces empty evidence, low confidence, `MANUAL_REVIEW`, and
the insufficient path when context is insufficient; complete assessments require evidence and the stub path.
Citation resolution and membership of the recommended action in the supplied packet remain runtime checks,
because the static response schema has no access to that request's content or allowed actions.

Six current fixture pairs are `fixtures/backend/equipment.json`, `fixtures/backend/robot_health.json`,
`fixtures/backend/navigation.json`, `fixtures/backend/data_quality.json`, `fixtures/backend/mission.json`,
and `fixtures/backend/insufficient_context.json`. Use their `request` objects for HTTP calls.

Native/compose System 2 port is **8083**; llama-server remains 8080. `SYSTEM2_API_KEY` is the primary secret;
`AMD_API_KEY` is the compatibility alias. See [RUNNING.md](../../docs/RUNNING.md) for current startup and compose.
Authorization via `X-API-Key` precedes body parsing; `/health` is open. Native binds loopback; remote access uses
`ssh -L 8083:127.0.0.1:8083 <user>@<amd-box>`. Compose callers use `http://system2:8083`.

## Multimodal inputs (contract `2026-10-10.backend.2`)

Optional and additive; a request without them gets byte-identical prompts and packet hashes
(`tests/golden/prompts_pre_multimodal.json`). `context.vision[]` rows (computed upstream by `amd/vision`) become
citable `V#` evidence; LOW-trust rows lose `value`/`delta`/`agrees`; a HIGH-trust `linked` row with `agrees=false`
removes `MONITOR`/`NO_ACTION` from the allowed actions. `pairing: "demo"` rows (a kit sample image, not this asset)
lose `agrees`/`delta`/`logged_*` and never restrict actions. An optional `flags` list (uppercase reader caveats, at
most 8) is kept; `AMBIGUOUS_NEEDLE`/`DEFAULT_SWEEP` force LOW trust and `CLIPPED` adds `bound: "lower"`.
`context.ocr[]` lines are capped, untrusted, never-citable text.
Top-level `image_b64` (JPEG/PNG, at most 2 MiB decoded, signature-checked, else 422) is context only: it is sent
image-first to `SYSTEM2_VISION_MODEL` (default `SYSTEM2_MODEL`); if the server rejects images (no `--mmproj`),
the same attempt is re-sent text-only and the response header `X-System2-Image` says `text_only`. Image requests
get `SYSTEM2_IMAGE_DEADLINE_S` (default 25 s, text requests keep 15 s); the image call keeps 8 s back and, if it
times out, the same attempt is re-sent text-only (`X-System2-Image: text_only`). A gateway auth error (LiteLLM's
400 "No connected db." for a wrong key) is never mistaken for an image rejection.
`SYSTEM2_LLAMA_API_KEY` is sent as a Bearer token (for the LiteLLM gateway). Exact prompt format for the
fine-tune: [PROMPT_FORMAT.md](PROMPT_FORMAT.md); render it with `python scripts/prompt_example.py`. Stack wiring:
[INTEGRATION-multimodal.md](INTEGRATION-multimodal.md). Tests: `tests/test_multimodal.py`.

## Streaming drafts (`POST /v1/assess/stream`, added 2026-10-11)

Same body, `X-API-Key` and validation as `/v1/assess` (401/422 stay plain JSON before the stream, and so does a
`503 {"detail": ...}` when `STREAM_MAX_CONCURRENT` = 8 streams already run in the process). The reply is
`text/event-stream`: `start` `{flag_id, request_id, model, mode}`, then `draft` `{call, tokens, fields}` at most once
per 100 ms per model call while Qwen generates, then `final` `{response, cache, image}`, where `response` is exactly
the `/v1/assess` body and `cache`/`image` are its `X-System2-Cache`/`X-System2-Image` values (null when that header
is absent), or `error` `{detail}`. A `: keep-alive` comment is sent after 10 s without an event. Stub mode, cache
hits and insufficient packets send only `start` and `final`. Contract: "Streaming Qwen drafts" in
[backend-services-contract.md](../../docs/backend-services-contract.md).

A draft is never an answer. `fields` (`analyst/drafts.py`) is the model's partial JSON before any check:
`{evidence: [{claim, ids}], answer, recommended_action, confidence}` with only the keys whose value has started. The
last string may stop mid-word, and `ids` holds complete IDs only. Each draft is a full snapshot. `call` increases on
the pipeline retry and on an image request's text-only resend. The first draft of a call is sent at once (it may be
`fields: {}`); a snapshot equal to the last one sent is skipped.

How it works: `LiveAssessor.assess_image(body, on_draft=...)` runs the unchanged `run_pipeline` inside
`LlamaGenerator.drafts(cb)`, a per-thread switch that sends each llama-server call with `stream: true` +
`stream_options.include_usage` and an otherwise identical body. The SSE chunks are folded into a synthetic chat
completion and judged by the same `parse_envelope` (same deadline watchdog, status handling, size caps, `finish_reason`,
usage and timings), so the checked result, cache and recorder are those of a plain call. Outside the switch the
request is byte-identical to before. If llama-server ignores `stream: true` and answers a plain completion (Content-Type
not `text/event-stream`), it is read as one under the plain size cap, with no drafts. The endpoint runs the assessment
in a worker thread, so a client that disconnects does not cancel it; the answer is still cached. Tests:
`tests/test_stream.py`, `tests/test_drafts.py`.
Measured on the W7900D (Qwen3.8-27B Q8_0; prompt processing comes before the first draft): a text request
(`fixtures/backend/equipment.json`) gave its first draft at 3.9-5.6 s and `final` at 11.7-13.4 s, an image request
3.7 s / 11.8 s, 47-48 drafts about 133 ms apart, and the final response equals `/v1/assess` apart from latency.
Through the public tunnel Vercel uses: first text 2.7 s, final 10.2 s. Deploy and verify: [STREAMING.md](../../docs/STREAMING.md).

## Phase 2 offline generation and evaluation

Everything here is offline: no new service, port, package or env var. Code lives in `analyst/` and is driven by
`scripts/`; each script puts `amd/system2` on `sys.path` and imports `analyst.*` (for example
`from analyst.evaluation import EvalConfig, ModelRunner, TemplateRunner, run_evaluation`). Commands run from
`amd/system2`:

```bash
uv run --no-project --with-requirements requirements.txt python scripts/save_packets.py --seed --output data/dev_packets.json
uv run --no-project --with-requirements requirements.txt python scripts/save_packets.py --input captures.json --output data/approved_packets.json
uv run --no-project --with-requirements requirements.txt python scripts/evaluate.py --runner template --out-dir results/eval/template
uv run --no-project --with-requirements requirements.txt python scripts/evaluate.py --runner both --model-url http://127.0.0.1:8080 --model qwen3.5-9b-q4km --label amd-gfx1151-rocm-qwen35-9b-q4km --out-dir results/eval/qwen35-9b
```

**Saved packets.** `--seed` rebuilds `data/dev_packets.json` byte for byte from the kit CSVs. `--input` approves
offline MCP captures through the same validator and writes atomically. Approval rules: the split is `train`, and
neither the case mission nor any source row belongs to a fold-2 holdout mission (M006, M009, M012, M013, M017,
M018, M019, M020, M023, matching System 1's default fold). Every dynamic evidence item is the exact projection of
a checked-in kit row, with its row hash and an availability time no later than the case `as_of`. Static passages
are exact excerpts of pinned files. Requests may not contain labels, answers, prior outputs or unverified
aggregates. The manifest's `content_hash` binds the whole set. An optional independent `reference`
`{expected_action, source}` sits outside the request and never reaches a prompt. A source of `validator`, `policy`
or `system1` is rejected.

**Development numbers only.** The six cases are synthetic-kit packets from training missions. The few-shots come
from the same pool: the first three in `EXAMPLE_ORDER` that share no evidence with the current packet, so a case
is never its own example. Fewer than three such examples counts as a failed attempt and falls back to the
template. Every result is a training-pool development number, and `scope.headline_eligible` is always `false`.
Phase 4 owns held-out headline numbers, confidence intervals, human labels and any entailment judge. Phase 3
owns live serving and the model bakeoff.

**Token budget.** `max_tokens` defaults to 512 (350 until 2026-10-11). The SRV-00 smoke reply used 439 output
tokens, but that was the uncapped Phase 1 prompt. The Phase 2 prompt uses compact few-shots and schema caps (at
most 3 claims of 160 characters, and a 240-character answer). On the M001 replay (2026-10-11) Qwen3.8-27B
pretty-printed a valid reply past 350 tokens, so the cap was raised. A reply that hits the limit
(`finish_reason=length`) counts as truncated: it is retried, then falls back to the template, and is never accepted.

**Fake HTTP versus a measured run.** `tests/test_evaluation.py` runs the same CLI against a fake loopback
llama-server, so its numbers are regression checks of the harness, not measurements. A measurement is a run
against llama-server on the AMD box (README "Serving smoke test"; from a laptop, first run
`ssh -L 8080:127.0.0.1:8080 <user>@<amd-box>`) with a `--label` that names the hardware, backend and model. No
measured Phase 2 run has been recorded yet.

**Artifacts.** The output directory (`--out-dir`, default `results/eval/<runner>`) holds three files:
- `cases.jsonl`: one record per runner and case.
- `report.md`: a summary table.
- `metrics.json`: written last, so it exists only for a completed run.

`metrics.json` has these sections:
- `identity`: dataset content and file hashes, selected case IDs, packet-set hash, runners, config (seed, limit,
  deadline, timeout, max_tokens, credential-free model URL, model ID), prompt version and hashes, code revision
  and dirty flag, and `identity_hash`. Identical inputs give an identical identity. Measured timings vary.
- `scope`, `measurement`: the label, whether the endpoint is loopback, and start and finish times.
- `runners`: the per-runner aggregates described below.
- `comparison`: the case IDs and packet hashes common to all runners.

Exit codes: 0 means the run completed, including runs where the model failed (failures are counted and fall back
to the checked template). 1 means the dataset was rejected: unreadable, empty, missing its manifest, changed,
leaking, holdout, future evidence or a bad reference. Rejection happens before any model call or output write.
2 means a usage error. 3 means the run failed after the dataset was accepted (runner setup, such as few-shot
loading, or an error mid-run); stderr names the stage and the error class only, and no output is written.
`--limit N --seed S` takes a seeded smoke sample.

**Paths.** `llm` means the first attempt was accepted, and `retry` means the second was. `fallback` means both
attempts failed, or the deadline ran out, and the checked template was used. It is labelled `model=template` and
never counts as model success. `insufficient_context` means either the packet itself lacks evidence (guarded,
with no generator call) or no template claim survived the checker. The template runner is the baseline. It uses
the same normalization and checker as the pipeline's fallback, so its paths are `fallback` or
`insufficient_context` and it makes no attempts.

**Metric semantics.**
- Every rate is `{value, count, denominator}`. An empty denominator gives `value: null` with `count: 0`, never
  NaN.
- Failed, timed-out and fallback cases stay in their denominators.
- `first_attempt` covers the raw first model reply, over packets with enough evidence. `final` covers the output
  after retry or fallback, over the same packets. Guarded insufficient packets are reported separately.
- Schema, citation, numeric and name checks are cumulative and fail closed. A check passes only for a parsed,
  schema-valid output that has no errors of that class. These are mechanical consistency checks against the
  cited packet content, not entailment or correctness.
- `action.agreement_with_validator` compares the output with the validator's `suggested_action`. It is not action
  quality. `action.action_quality` exists only for cases with an independent reference; otherwise it is null with
  count 0.
- Latency is per case and includes retry and fallback. It is reported in milliseconds with nearest-rank
  quantiles (rank = ceil(q·n)).
- Token counts are summed over attempts.

## Historical Phase 1 build order (offline)
1. **Context packet builder** (R1–R2): `flag → packet` JSON under ~1,500 tokens, key facts first, every row with a short
   ID (`F#` derived facts, `S#` asset, `W#` waypoint, `I#` similar incidents). Reads the kit CSVs
   (`amd/system1/data/kit/`) for now; swaps to Neon views later.
2. **Output schema** (R3): `evidence[{claim, ids}]` → `answer` → `question` → `recommended_action` (enum) →
   `confidence` → `insufficient_context`. Grammar-enforced; the `ids` enum is built per packet.
3. **Prompt**: system prompt + 3 few-shot packet→JSON examples, thinking off, `max_tokens` 512.
4. **Post-check** (R4): every number and asset/waypoint name in the output must appear in the rows it cites; retry
   once, then fall back to a templated explanation. Two references need no row: the flag's own subject (its
   asset, waypoint and mission IDs and the asset/waypoint names, plus "flag <id>"), and the short form of a cited
   ID (`mission:M001` → M001, `flag:14` → "flag 14"). Numbers always need a cited row.
5. **Test set + eval** (R6): held-out flags; schema validity, citation validity, number consistency, latency.
6. **Serve** on the AMD box behind `/v1/assess`, and precompute explanations for every flag (R7).

Prompt first. Fine-tuning is a gated stretch goal (R5), only if the pipeline works end to end.

## Legacy offline contract
All models live in `analyst/contract.py` and reject unknown fields (an extra field gets a 422). Types are strict, so
the server accepts exactly what the exported JSON Schema allows: `"yes"` is not a boolean, `"5"` is not an integer,
and a number is not a timestamp.

Timestamps are **naive** ISO 8601 strings in kit site time, with no offset and no `Z`, e.g. `"2026-09-01T18:27:54"`
(optionally with up to 6 fractional-second digits). This is the only form the kit CSVs use. A timestamp with an offset
or `Z` gets a 422, so send JavaScript dates as local site time, not `toISOString()`. The schema states this as a
`pattern` instead of `format: date-time`, because RFC 3339 date-time requires an offset.

| Model | What it is |
|---|---|
| `Flag` | One escalated System 1 flag: ids, `kind` (action or mission), `category`, `priority`, `escalate`, `reading_trust`, the validator `reasons`, optional `scores`. |
| `ContextPacket` | The cited context System 2 builds for the flag: `event`, `facts`, `asset`, `waypoint`, `incidents` rows, `allowed_actions`, `missing` sections, and the `question`. |
| `Explanation` | The answer: cited `evidence`, `answer`, `question`, `recommended_action`, `confidence`, `insufficient_context`. |
| `Meta` | How the answer was made: `model`, `prompt_ver`, `path`, `latency_ms`, `tokens_per_sec`, `packet_hash`. |
| `AssessRequest` | The request body of `POST /v1/assess`: just `{"flag": {...}}`. System 2 builds the packet itself. |
| `AssessResponse` | The response: `contract_version`, `flag_id`, `explanation`, the `packet` it is grounded in, and `meta`. |

Packet row IDs are a prefix letter plus a number (`E1`, `F3`, `I2`): `E` event, `F` derived facts, `S` asset,
`W` waypoint, `I` similar incidents. Every evidence item cites one or more of these IDs, and the contract rejects a
citation to a row that is not in the packet.

`Explanation` fields always come in this order: `evidence`, `answer`, `question`, `recommended_action`, `confidence`,
`insufficient_context`. The order is part of the contract (the model writes evidence before its answer), so never
reorder fields. `confidence` is `low`, `med` or `high`. `meta.path` is `first_pass`, `retry`, `fallback` or
`insufficient_context`.

`recommended_action` must be one of the packet's `allowed_actions`, which come from this table. Only
`manual_review` is guaranteed: the contract rejects a packet whose `allowed_actions` lacks it. `monitor` is listed
for every category, but a packet may leave it out. `mission_health` flags allow every action. When
`insufficient_context` is true, the action is always `manual_review`, `evidence` is empty and `confidence` is `low`.

| Action | Meaning | Allowed for |
|---|---|---|
| `dispatch_technician` | Send a person to the asset now. | equipment, mission_health |
| `schedule_maintenance` | Book a planned work order; not urgent. | equipment, data_quality, mission_health |
| `reinspect_next_mission` | Add the waypoint to the next SPOT route. | all categories |
| `verify_reading_manually` | Take the reading by hand; the robot's value is not trusted. | data_quality, mission_health |
| `recharge_robot` | Dock and charge SPOT before further work. | robot_health, mission_health |
| `check_robot_comms` | Inspect SPOT's radio/network link. | robot_health, mission_health |
| `clear_route_obstruction` | Remove whatever blocks the path. | navigation, mission_health |
| `relocalize_robot` | Re-run localization / fiducial check. | navigation, mission_health |
| `monitor` | No action now; watch the trend. | all categories |
| `manual_review` | A human must look; System 2 cannot tell. | all categories |

Any change to the wire format bumps `CONTRACT_VERSION` in `analyst/contract.py`, and the JSON Schemas are then
re-exported with `python scripts/export_schema.py`.

## Legacy offline fixtures
Each file in `fixtures/` holds a `request` and the `response` System 2 should give. Every one is built from real kit
data from training missions only, and every number in its explanation appears in its packet.

| File | Kit event | Category | `meta.path` it shows |
|---|---|---|---|
| `fixtures/equipment.json` | `act-A0009` (mission M001): failed visual inspection of Heat Exchanger 08 | equipment | `first_pass` |
| `fixtures/robot_health.json` | `act-A0010` (M001): inspection run at 15% battery | robot_health | `retry` |
| `fixtures/navigation.json` | `act-A0037` (M004): localization issue at WP06 | navigation | `first_pass` |
| `fixtures/data_quality.json` | `act-A0030` (M003): sensor timeout, questionable reading | data_quality | `fallback` (templated explanation) |
| `fixtures/mission_health.json` | `mis-M001`: mission reported COMPLETED with 2 of 10 actions failed | mission_health | `first_pass` |
| `fixtures/insufficient_context.json` | `act-A9999`: an action the data source cannot resolve | data_quality | `insufficient_context` |

The stub answers each request with the matching fixture's response, stamped with the caller's flag and
`meta.model = "stub"` (so stub output can never pass for model output). It keeps the fixture's `meta.path`, so web can
render every UI state. A request whose `flag_id` matches a fixture gets that fixture; otherwise it gets its category's
fixture.

## Historical Phase 1 stub commands (superseded)
The commands below document the original offline fixture stub on 8000. For current HTTP startup, use
`SYSTEM2_API_KEY=your-shared-secret uv run --no-project --with-requirements requirements.txt python scripts/serve.py --port 8083`
from `amd/system2`, and send a `fixtures/backend/` request. The flag-only request and nested response below
describe Phase 1 history, not the current service.
Setup on the AMD box, from `amd/system2`:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Start it, from `amd/system2` (one command). The key must be at least 16 printable ASCII characters with no leading or
trailing whitespace (watch for a trailing space or tab in a `.env` line), and it is never logged. The server compares
keys in constant time (`hmac.compare_digest`).

```bash
AMD_API_KEY=your-shared-secret uv run --no-project --with-requirements requirements.txt python scripts/serve.py
```

With the venv instead of uv: `AMD_API_KEY=your-shared-secret .venv/bin/python scripts/serve.py`. Flags: `--port`
(default 8000), `--host` (default `127.0.0.1`), `--fixtures`, `--log-level`. Without a valid key the script exits with
code 2 before binding any port.

Call it:

```bash
python3 -c "import json; print(json.dumps(json.load(open('fixtures/equipment.json'))['request']))" > /tmp/req.json
curl -s -X POST http://127.0.0.1:8000/v1/assess \
  -H "X-API-Key: $AMD_API_KEY" -H "Content-Type: application/json" --data @/tmp/req.json
```

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /health` | open | `{"status": "ok", "mode": "stub", "contract_version": ..., "fixtures": 6}` |
| `POST /v1/assess` | `X-API-Key` header | an `AssessResponse` |

There are no `/docs`, `/redoc` or `/openapi.json` routes: the committed `schema/*.json` files are the published
contract. Every path except `/health` needs the key (a keyless request to any other path gets 401).

Status codes for `POST /v1/assess`:
- **200**: a schema-valid `AssessResponse`.
- **401**: missing or wrong `X-API-Key`. The key is checked in middleware before the body is read, so a request
  without a valid key gets 401 even when its body is not valid JSON.
- **422**: malformed flag (wrong type, unknown field, broken kind/category rule).
- **501**: no fixture covers the flag's category.

The server binds `127.0.0.1` and never a public interface by default (it warns if you pass a non-loopback `--host`).
To reach it from a laptop, open an SSH tunnel to the box: `ssh -L 8000:127.0.0.1:8000 <user>@<amd-box>`, then call
`http://127.0.0.1:8000` locally.

## Tests and schemas
From `amd/system2`:

```bash
uv run --no-project --with-requirements requirements.txt python -m pytest -q
python scripts/export_schema.py            # regenerate schema/*.json after a contract change
python scripts/export_schema.py --check    # exit 1 if a committed schema no longer matches the contract
```

`schema/` holds the legacy offline generated JSON Schemas: `flag.schema.json`, `packet.schema.json`,
`assess_request.schema.json` and `assess_response.schema.json`. Do not edit them by hand.

## Serving smoke test (AMD box)
SRV-00 is already complete. This optional replay is not an execution prerequisite.
See [evidence index](results/smoke/README.md) for the recorded GPU result's provenance and limits.
Proves one model loads on the gfx1151 GPU and answers one JSON-schema-constrained request (Phase 1.1, SRV-00). The
Radeon Cloud Jupyter VMs have no root and no Docker, so llama.cpp is built in the home folder:

```bash
hf download unsloth/Qwen3.5-9B-GGUF Qwen3.5-9B-Q4_K_M.gguf --local-dir ~/models/q35
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp && cd ~/llama.cpp
HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" \
  cmake -S . -B build -DGGML_HIP=ON -DGPU_TARGETS=gfx1151 -DCMAKE_BUILD_TYPE=Release && cmake --build build -j "$(nproc)"
~/llama.cpp/build/bin/llama-server -m ~/models/q35/Qwen3.5-9B-Q4_K_M.gguf \
  -ngl all -fa on -lm none -c 16384 -rea off --host 127.0.0.1 --port 8080 --metrics
# from amd/system2, in another terminal:
python3 scripts/smoke_serve.py --backend rocm --runs 3 --max-tokens 1024
```

No ROCm on the machine: use the Vulkan prebuilt (`llama-<ver>-bin-ubuntu-vulkan-x64.tar.gz` from the llama.cpp
releases) and `--backend vulkan`. Older builds reject `-ngl all`, `-lm` or `-rea`; use `-ngl 999 --no-mmap`.

**Result (2026-10-10, Qwen3.5-9B Q4_K_M, ROCm build, Radeon 8060S):** 33/33 layers offloaded to `ROCm0`, 3/3 replies
valid under the original checker, 12.4 s per request for 1,450 prompt and 439 output tokens, generation 35.6 tok/s. Latency is generation-bound,
so the output length is the main lever: 439 tokens exceeds the planned ~350 cap (the reply would truncate), so Phase 2
caps the evidence list or raises `max_tokens` to ~512. The reported prompt tok/s reflects a warm prompt cache; rerun
with an unseen `--fixture` for cold prefill speed.
Cold prefill remains unmeasured; Vulkan was not benchmarked. The current checker validates the complete emitted
schema, including evidence shape/types/caps; original validation checked keys/order and citation/action/confidence
membership only. Current regression coverage is not a retrospective validation of absent raw GPU replies.

## Historical offline interface with System 1 (superseded on HTTP)
The `Flag` model in `analyst/contract.py` is now the agreed shape for System 1 to conform to: `escalate`, `priority`,
`category`, `reading_trust`, their probabilities in `scores`, and the validator rules that fired in `reasons`.
Action-level flags use `kind` "action" with one of the four action categories. Mission-level flags use `kind`
"mission" with `category` "mission_health". Until System 1 emits flags, the packet builder fakes them from the kit's
severity and the policy in `amd/system1/triage/policy.py` (with `source` "policy").
