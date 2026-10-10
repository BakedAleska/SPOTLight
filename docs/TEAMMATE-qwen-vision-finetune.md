# Teammate brief: fine-tuning the Qwen vision model on thermal and gauge data

**For:** the teammate collecting Hugging Face datasets and fine-tuning Qwen on our thermal and gauge data.
**Written:** Sat 2026-10-10. **Devpost deadline:** Sun 2026-10-11, 10:00 AM (code freeze 11:00).
**Read this first, then** `research/tech-spot-vision-cv-2026-10-10.md` (why the system is built this way) and
`research/decisions.md` (what's already decided). Don't commit to `main` without asking.

---

## 1. What we're building

The project is SPOTlight, an operator console for NextEra's SPOT robot. It replays SPOT missions, flags problems, and
explains each escalated flag in Orbit's style (question / answer / reasoning / recommended action). Each flag's
explanation cites the evidence it is based on.

```
 web console ──▶ backend (FastAPI :8000) ──▶ orchestrator (LangGraph :8085)
                                                 │
       ┌─────────────────────────────────────────┼──────────────────────────────────┐
       ▼                    ▼                    ▼                                  ▼
 MCP server :8090    System 1 "Laya" :8082   Vision/OCR :8084                System 2 analyst :8083
 (RAG over Neon      (ModernBERT triage:     (RapidOCR + code readers:        POST /v1/assess
  pgvector)           escalate/priority)      thermal °C, gauge value,        (grounded JSON)
                                              Orbit key results → V# rows,          │
                                              annotated image)                      ▼
                                                                      LiteLLM gateway :4000
                                                          aliases: system1 · system2 · system2-vision · embed
                                                                                    │
                                                                    system2-vision ─┘─▶ VISION_LLAMA_URL
                                                                    llama-server: Qwen GGUF + mmproj   ◀── YOUR MODEL
                                                                    (AMD Radeon 8060S, gfx1151, ROCm)
```

**Request flow for one escalated flag:**
1. The orchestrator pulls context from MCP (asset history, similar incidents, kit rows).
2. System 1 (Laya) classifies the flag: escalate or not, priority, and whether the reading can be trusted.
3. If it escalates, the vision service reads the image. Code computes the number (°C or psi), compares it with the
   logged value, and returns **V# rows** (`tool, value, unit, confidence, trust, logged_id, logged_value, delta,
   agrees, …`), OCR words, and an **annotated image** with the needle line, hotspot box and V# labels drawn on.
   `agrees` is `true`, `false`, or absent (no logged value or tolerance to compare). Readings with confidence below
   0.5 arrive as `"trust": "LOW"` rows; System 2 strips their `value`, `delta` and `agrees`, so they can never be
   stated as fact ([`docs/vision-contract.md`](vision-contract.md), "Feeding System 2").
4. System 2 calls the `system2-vision` alias with the image first, then the packet (evidence rows, V# rows, OCR,
   Laya output, allowed actions), with the question last. It returns schema-checked JSON. If that fails it retries
   once, then falls back to a template.

**Where your model sits:** behind the gateway alias `system2-vision`, served by `llama-server` as a **GGUF + mmproj**
pair on the AMD box. Plugging it in is one step: point `VISION_LLAMA_URL` at your llama-server.

**What "done" looks like for the demo:** a judge watches an escalated thermal or gauge flag. The explanation
cites the V# reading. When the camera and the log disagree, it says so and recommends a manual check, and it never
invents a number. Your model must beat the stock Qwen3.5-9B on our eval (§6), and both the dataset and the adapter
must be on the HF Hub with cards.

## 2. The core rule: code computes every number, the VLM explains

The research is clear that VLMs can't be trusted to read instruments:
- **MeasureBench:** the best model scores 30.2% over all real instruments (31.5% on dials; Qwen3-VL-8B 15.3%), even
  though units are right more than 90% of the time ([arXiv 2510.26865](https://arxiv.org/abs/2510.26865)).
- **InSituMeasure:** the best of 24 MLLMs reaches 25.7% joint value-unit accuracy
  ([arXiv 2609.04014](https://arxiv.org/abs/2609.04014)).
- **ThermEval:** across 25 VLMs, models "consistently fail at temperature-grounded reasoning"
  ([arXiv 2602.14989](https://arxiv.org/abs/2602.14989)).
- **VLMs follow wrong text:** given corrupted extracted text, LLaVA-NeXT drops from 79.5% to 28.7%
  ([arXiv 2503.02199](https://arxiv.org/abs/2503.02199)).

So the fine-tune's goals, **in priority order**, are:

| # | Goal | What good looks like |
|---|---|---|
| a | **Faithfully use the supplied evidence:** V# readings, OCR, Laya output and the annotated image | Cites V#/row IDs. Copies numbers exactly. When `agrees=false`, cites both the V# row and the logged row and recommends a manual check. Never writes a number that isn't in the packet. Says "unreadable" or picks the insufficient-context path when the evidence is weak. |
| b | **Read what's visible, qualitatively** | Names labels, units and scale min/max. Says which component is hot. Describes the needle position in words ("near the top of the scale"). Calls glare or occlusion "unreadable". |
| c | *Optional:* direct reading as an auxiliary task | Train and score it **separately**. The model's reading is never trusted over the code readers and never appears in the served output. |

**Also build a dataset in our exact prompt format:** (image + V# rows + OCR + Laya output + question → Orbit-style
JSON). Goal (a) is what wins the demo, and only this kind of example teaches it.

## 3. Data

### 3.1 Candidate datasets (each checked on 2026-10-10)

| Dataset | Modality | Licence (as checked) | Size / labels | Fit |
|---|---|---|---|---|
| [FlagEval/MeasureBench](https://huggingface.co/datasets/FlagEval/MeasureBench) | gauges, real + synthetic | **CC BY-SA 4.0** (card) | `real_world` 1.3K, `synthetic_test` 1.2K; question + evaluator | **Eval only.** Both splits are test sets. ShareAlike means a derived dataset must also be CC BY-SA. |
| [Synanthropic/reading-analog-gauge](https://huggingface.co/datasets/Synanthropic/reading-analog-gauge) | analog dials | **MIT** (`LICENSE` file in repo; no licence tag on the card) | 10K–100K images in `corners.zip` / `keypoint.zip` (~2.6 GB) | Good for goal (b)/(c): keypoints give exact needle geometry. Needs `make-dataset.py`. |
| [KhoaUIT/Synthetic-Data-for-Precision-Gauge-Reading](https://huggingface.co/datasets/KhoaUIT/Synthetic-Data-for-Precision-Gauge-Reading) | synthetic gauges | **None stated** (card says "More Information Needed") | 500 rows (question, image, answer) | Use privately as a sanity check only. **Don't redistribute** or derive a published set from it. |
| Roboflow Universe: [analog-meter-reading](https://universe.roboflow.com/workspace-yolo11/analog-meter-reading), [analog-gauge-reader](https://universe.roboflow.com/analog-guage-reader/analog-gauge-reader-xnvsn), [pressure_gauge_key_points](https://universe.roboflow.com/m-zaid/pressure_gauge_key_points) | gauges | CC BY 4.0 per search listings; **re-check each page** | ~120–210 images; boxes for center / needle_tip | Real photos for goal (b). Labels are boxes, not readings. |
| [Mendeley m4sbt8hbvk](https://data.mendeley.com/datasets/m4sbt8hbvk/2): induction-motor thermal | thermal | **CC BY 4.0** | 11 conditions (winding shorts, stuck rotor, fan failure). Image count and format aren't listed on the page | Goal (b): "which component is hot", fault vs healthy. |
| [Mendeley 8mg8mkc7k5](https://data.mendeley.com/datasets/8mg8mkc7k5): dry-type transformer thermal | thermal | **CC BY 4.0** | 255 images, 9 conditions, 45 segmentation masks | Goal (b). Masks give a hotspot region. |
| [RaptorMaps/InfraredSolarModules](https://github.com/RaptorMaps/InfraredSolarModules) | thermal (solar) | **MIT** (repo) | 20K images, **24×40 px**, 12 classes (Hot-Spot, Diode…) | Weak fit: tiny images and a different domain. Use only for a hotspot / no-hotspot auxiliary. |

None of these come with our V# rows. They teach perception. **The faithfulness behaviour comes from §3.2.**
More HF candidates (exact-psi synthetic gauges, corrosion and valve-defect sets, machine audio) are in the research
file §6, "More Hugging Face candidates".

### 3.2 How to build supervision (in order of value)

1. **Synthetic renders from `readings.csv`, which give exact ground truth.** Render one image per reading at its
   logged value. Gauges: matplotlib dial with the needle at the logged psi. Thermal: Gaussian hotspot at the logged
   °C, ironbow 25–100 °C, with a colorbar. Domain-randomize: ~240 px, JPEG quality 40–90, blur, ±8° rotation, glare.
   Seed each render with `sha1(reading_id)` and write to `amd/vision/data/synth/` with a `manifest.jsonl`
   (research file §6).
2. **Run our vision service (`amd/vision`, :8084, `POST /v1/vision` with `logged: {reading_id, measurement_type,
   value, unit}`; see [`docs/vision-contract.md`](vision-contract.md)) on every image.** That gives you V# rows,
   OCR words and `annotated_image_b64` **exactly as at inference**. Train on the *annotated* image, because that's
   what System 2 receives. Don't hand-write V# rows, because they'd drift from what the model sees in production.
   It's built: `docker compose --profile vision up -d vision`, or natively `cd amd/vision && .venv/bin/python
   scripts/serve.py` (`docs/RUNNING.md` "Vision / OCR on 8084"); `scripts/run_kit.py --kit <dir>` batch-runs a kit-layout folder offline.
   V# rows for System 2 are built from its response by the orchestrator (`vision_rows` in
   `amd/orchestrator/orchestrator/assess.py`); reuse that function so your rows match production.
3. **Train disagreement (and agreement) only on `pairing: "linked"` rows.** A `demo` row (a kit sample chosen by
   action type, not this asset) never carries `agrees`, `delta` or `logged_*`; its target calls the image
   illustrative and does not judge the asset (worked example: `amd/system2/fixtures/multimodal/gauge_demo.json`,
   PROMPT_FORMAT.md §6b). The golden disagreement example `gauge_disagree.json` now uses `pairing: "linked"`.
   Include some rows with reader `flags`: `["CLIPPED"]` + `bound: "lower"` (target says "at least <value>"),
   `RELATIVE_ONLY` spectrogram rows (relative levels, not absolute), and `AMBIGUOUS_NEEDLE` rows, which arrive as
   `trust: "LOW"` with no value.
4. **Deliberately perturbed pairs** teach `agrees=false`. Render half of the images at the logged value and half
   at a perturbed one (±15–40% of full scale, or a stuck needle). The target JSON must cite both the V# row and the
   logged row, say they disagree, and pick a manual-check action. Add **unreadable** cases (heavy glare,
   occlusion, low confidence), whose target is low confidence plus manual review.
5. **Targets.** Take System 2's checked template output for the same packet, then hand-edit a sample of those
   targets so they also describe the image (goal b). Every target must pass System 2's validator (§4).

### 3.3 Hygiene

- **Licences.** CC BY-SA (MeasureBench) is viral: anything derived from it that you publish must also be CC BY-SA.
  Data with no licence (KhoaUIT) must not be redistributed. Record each source's licence in the dataset card.
  Avoid NC data in anything published.
- **No leakage of held-out missions.** Hold out fold 2 of `amd/system1/data/manifest.json`, i.e. missions **M006,
  M009, M012, M013, M017, M018, M019, M020, M023** (same as System 2's `data/heldout_packets.json`). No training
  example may come from, or cite a row from, those missions. Split by mission, not by row.
- **The kit images are eval/demo only:** the 6 `sample_inspection_images/` and the Orbit screenshots in
  `SPOTDATA/…/01_REFERENCE/Approved_Orbit_Screenshots`. The kit says it is "synthetic and fictional" but grants
  no redistribution licence. **Ask NextEra before putting any kit image on the Hub.** Never train on them; they are
  our demo and eval set.

## 4. Training recipe

| Item | Choice | Source / note |
|---|---|---|
| Base model | **`Qwen/Qwen3.5-9B`**: Apache-2.0, `image-text-to-text`, arch `qwen3_5`, ~9.65B params | [Hub](https://huggingface.co/Qwen/Qwen3.5-9B) (verified) |
| Fallback | `Qwen/Qwen3-VL-8B-Instruct`: Apache-2.0, no thinking mode, official GGUF | [Hub](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct), [GGUF](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF). Switching base means re-running the System 2 baseline. |
| Method | **bf16 LoRA.** Unsloth says "It is not recommended to do QLoRA (4-bit) training on the Qwen3.5 models"; the 9B needs about 22 GB for bf16 LoRA | [Unsloth Qwen3.5 guide](https://unsloth.ai/docs/models/qwen3.5/fine-tune) |
| Framework | **Unsloth** (supports Qwen3.5 vision fine-tuning; lists gfx1151 as supported, install `unsloth[amd]` after ROCm torch) **or** TRL `SFTTrainer` + PEFT | [Unsloth AMD](https://unsloth.ai/docs/basics/amd), [TRL SFT](https://huggingface.co/docs/trl/main/en/sft_trainer). LLaMA-Factory support for Qwen3.5 vision is **unverified**; skip it. |
| Libraries | Qwen3.5 needs **transformers v5** ("older versions will not work") | Unsloth guide |
| Hardware | AMD "Fine Tuning on GPU" box (gfx1151, 48 GB env) with AMD's gfx1151 torch wheels; after every `pip install`, check that `torch.__version__` still says `+rocm` | `research/tech-amd-rocm-hf-serving-2026-10-09.md` §2.1/§3. HF credits or another GPU are fine for training. |
| Freeze | **Freeze the vision tower and projector**; LoRA on the language model only. In Unsloth: `finetune_vision_layers=False, finetune_language_layers=True`. In PEFT, check module names with `print(model)` (Qwen3.5 has linear-attention layers) | Keeping the vision tower frozen lets you **reuse the stock `mmproj-F16.gguf`** (§5) |
| Thinking | **Off.** Render training text with `enable_thinking=False` so it matches serving (`-rea off`). Ignore Unsloth's "keep 75% reasoning" advice; that's for keeping thinking, which we don't use. | Unsloth guide; System 2 sends `chat_template_kwargs.enable_thinking=false` |
| Image size | Downscale to ≤1280 px longest side (gfx1151 encoder crash ≥2048 px). Cap the processor's pixel budget at roughly **1024 image tokens** to match `--image-max-tokens 1024` (~1024·32·32 px if 16-px patches with 2×2 merge; **check the processor config**) | research file §5 |
| Starting hyper-params | r=16, alpha=16 (Unsloth example; "alpha ≥ r"), lr **1e-4** for adapters (TRL tip), warmup 10 steps, 1–2 epochs (or 100–300 steps), batch 1 × grad-accum 8, bf16, gradient checkpointing, `max_length=None` (TRL: truncation can cut image tokens), loss on completion/assistant only | Unsloth guide, TRL docs |
| Output | Must validate against System 2's packet-scoped schema: `GeneratedAssessment` / `response_schema()` in `amd/system2/analyst/generation.py`. Six fields in this order: `evidence[{claim≤160, ids}]` (≤3), `answer≤240`, `question` (copied verbatim), `recommended_action` (from the packet's `allowed_actions`), `confidence` `high\|medium\|low`, `insufficient_context` | Run `validate_generated()` on **every** target before training |

**Skip RL.** MeasureBench's GRPO on synthetic dials took Qwen2.5-VL-7B from only 14.6% to 19.7% on real images
(research file §1). That isn't worth a weekend.

## 5. Export and serving

Pick **one** of two paths:

**A. LoRA as a GGUF adapter (fastest, no re-quantizing):**
```bash
python llama.cpp/convert_lora_to_gguf.py ./lora-out --base Qwen/Qwen3.5-9B --outfile spot-vision-lora.gguf
llama-server -m ~/models/q35/Qwen3.5-9B-Q4_K_M.gguf --mmproj ~/models/q35/mmproj-F16.gguf \
  --lora spot-vision-lora.gguf -ngl all -fa on -lm none -c 16384 --jinja -rea off --parallel 1 \
  --image-max-tokens 1024 --host 127.0.0.1 --port 8088 --metrics
```
Whether `convert_lora_to_gguf.py` handles Qwen3.5's linear-attention modules is **unverified**. If it fails, use path B.

**B. Merge, then convert:**
```bash
# 1) merge: PeftModel.from_pretrained(base, "./lora-out").merge_and_unload().save_pretrained("./merged")  (+ processor)
python llama.cpp/convert_hf_to_gguf.py ./merged --outtype f16 --outfile spot-vision-f16.gguf
python llama.cpp/convert_hf_to_gguf.py ./merged --mmproj --outtype f16   # only if you trained the vision tower
llama-quantize spot-vision-f16.gguf spot-vision-Q4_K_M.gguf Q4_K_M
```
I checked llama.cpp master: `convert_hf_to_gguf.py --mmproj` exists ("An 'mmproj-' prefix will be added"), and
`Qwen3_5ForConditionalGeneration` is registered both as a text model (`conversion/qwen.py`) and as a vision
mmproj (`conversion/qwen3vl.py`). With a frozen vision tower, **reuse the stock**
[`unsloth/Qwen3.5-9B-GGUF`](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF) `mmproj-F16.gguf`. Unsloth's
`save_pretrained_gguf(..., quantization_method="q4_k_m")` also works, but its docs don't mention mmproj.

**Serve** on a spare port (e.g. 8088; check that it's free) and A/B it against the stock server on 8080. Use
`--parallel 1`. **Integration is one step:** set `VISION_LLAMA_URL=http://127.0.0.1:8088` for the gateway. Leave
everything else alone.

**Known gfx1151 llama.cpp issues** (research file §5):

| Issue | Effect | Mitigation |
|---|---|---|
| [#28056](https://github.com/ggml-org/llama.cpp/issues/28056) | Content leaks across requests with mmproj | Fix is in unmerged PR #28058. Use the issue's repro as an acceptance test. |
| [#27579](https://github.com/ggml-org/llama.cpp/issues/27579) | Corrupted output with `--parallel ≥2` or `n_ubatch<n_batch` | `--parallel 1` |
| [#28608](https://github.com/ggml-org/llama.cpp/issues/28608) | Encoder crash on images ≥2048 px | Downscale to ≤1280 px |
| [#29629](https://github.com/ggml-org/llama.cpp/issues/29629) | Flash attention may corrupt image embeddings | If outputs look wrong, retry with `-fa off` |

Whether an image and `json_schema` work together in one request is still untested. Test it on your server first.

## 6. Evaluation and acceptance gate

Training loss doesn't count. **Ship only if the adapter beats the stock Qwen3.5-9B on all three checks below**, per
the team decision: "LoRA is a gated stretch, shipped only if it beats the prompt baseline" (`research/decisions.md`).

1. **System 2 held-out harness (text path, no regressions).** The packets are `amd/system2/data/heldout_packets.json`
   (38 flags in 8 held-out missions), run with
   `python scripts/report.py --runner both --model-url http://127.0.0.1:8088 --model <id> --label amd-gfx1151-rocm-<name> --out-dir results/report/amd-<name>`
   (see `amd/system2/results/report/README.md`). Gates: first-attempt schema-valid, citation, numeric and name
   pass rates **≥ stock**, and **p95 ≤ 15 s**. The stock dev run was 5/5 valid with p95 7.9 s
   (`amd/system2/results/eval/README.md`).
2. **Vision-faithfulness set (new; you build it).** About 100 held-out-mission renders, half with correct V# rows
   and half with corrupted ones, plus about 10 unreadable cases. Metrics:
   - disagreement flagged when `agrees=false` (cites both IDs, manual-check action)
   - **invented-number rate**: any number in the output that isn't in the packet; **target 0**
   - correct "unreadable" / low-confidence rate
   - schema-valid rate

   Report 95% mission-bootstrap CIs (`research/tech-smalldata-finetune-eval-2026-10-10.md`).
3. **MeasureBench subset** (e.g. 200 `real_world` rows) for **label/unit/min-max reading only**. Score value
   reading separately (goal c), and never use it to gate shipping.

Record base vs. fine-tuned in one table (§7).

## 7. Deliverables and timeline (working back from Sun 10:00 AM)

| By | Deliverable |
|---|---|
| **Sat 18:00** | Source list and licences settled. Synthetic renders done (train missions only). Held-out set frozen. |
| **Sat 21:00** | Dataset v0 (≥300 packet→JSON examples, all passing `validate_generated`). Stock-model numbers on §6.2 and §6.3. |
| **Sun 03:00** | Training done (1–2 runs max). |
| **Sun 05:00** | GGUF or LoRA-GGUF served on :8088. #28056 repro passes. Smoke run through the gateway alias. |
| **Sun 06:30** | Eval table (base vs. fine-tuned, CIs). **Go/no-go**, posted to the team. |
| **Sun 08:00** | On HF: **dataset + card** (sources, licences, held-out missions, no kit images unless NextEra OKs it), **LoRA adapter + model card** (base, data, hyper-params, eval table, gfx1151/ROCm, limits: "does not read gauge values; code does"). GGUF + mmproj only if it ships. |
| **Sun 08:30** | A 3-sentence Devpost blurb naming the tools (Qwen3.5-9B, Unsloth/TRL + PEFT, ROCm on Radeon 8060S, llama.cpp GGUF, HF Hub), sent to whoever owns the Devpost page. |

**Who to ping:** the System 2 owner (branch `aatif/system2`) about the prompt format and schema, and the
gateway/orchestrator owner about switching `VISION_LLAMA_URL`. Post go/no-go in the team chat.

**Don't touch:**
- The System 2 contract (`amd/system2/analyst/contract.py`, `schema/`, `docs/backend-services-contract.md`)
- `generation.py` schemas
- Gateway config, except the `system2-vision` URL
- `docker-compose.yml`
- System 1 / Laya files

If something needs to change, ask the owner.

## 8. Interfaces cheat-sheet

| Service | Port | Notes |
|---|---|---|
| Backend (FastAPI) | 8000 | `backend/` |
| llama-server (stock Qwen3.5-9B Q4_K_M + mmproj) | 8080 | `-rea off`, `-np 1`; never in compose (laptop reaches it over `ssh -L 18080:127.0.0.1:8080`) |
| Embedding service | 8081 | `amd/embed/` |
| System 1 (Laya) | 8082 | `amd/system1/service/` |
| System 2 analyst | 8083 | `POST /v1/assess`, `X-API-Key`; contract `2026-10-10.backend.2` |
| Vision/OCR | 8084 | `amd/vision/`. `POST /v1/vision`, `POST /v1/ocr`; [contract](vision-contract.md) |
| Orchestrator | 8085 | `amd/orchestrator/`. `POST /v1/assess`, `POST /v1/ask`; [contract](orchestrator-contract.md) |
| MCP server | 8090 | `amd/mcp/` |
| LiteLLM gateway | 4000 | `amd/gateway/`. Aliases `system1`, `system2`, `system2-vision`, `embed` (+ hidden `qwen3.5-9b-q4km` = `system2`) |
| **Your candidate llama-server** | 8088 (suggested) | Check that it's free |

**Env vars** (as in `docker-compose.yml`, `.env.example` and `docs/RUNNING.md`):
- `VISION_LLAMA_URL`: gateway -> your server (the `system2-vision` upstream; empty = same server as `LLAMA_URL`).
  **The only thing you change.** Native gateway on the AMD box: `http://127.0.0.1:8088`; gateway in Docker on a
  laptop: `http://host.docker.internal:<tunnel port>`.
- `LLAMA_URL`: gateway -> the stock text llama-server (`system2` alias). Not yours.
- `SYSTEM2_LLAMA_URL`: where System 2 sends chat calls (the gateway: `http://gateway:4000` in compose,
  `http://127.0.0.1:4000` native). `SYSTEM2_MODEL`: `qwen3.5-9b-q4km` (text). `SYSTEM2_VISION_MODEL`:
  `system2-vision` (used only for requests with an image).
- `SYSTEM2_LLAMA_API_KEY`: System 2's Bearer key for the gateway (= `LITELLM_MASTER_KEY`). `scripts/report.py`,
  `evaluate.py` and `precompute.py` send it too, so the §6 eval can run through the gateway alias.
- `SYSTEM2_API_KEY`: the `/v1/assess` key.

**Prompt format:** [`amd/system2/PROMPT_FORMAT.md`](../amd/system2/PROMPT_FORMAT.md) is authoritative
(`analyst-prompt-2026-10-10.3`; `tests/test_multimodal.py` fails if it drifts from the code). The `.3` `VISION_RULES`
text added three sentences (demo rows are illustrative and never compared; `bound=lower` values are lower bounds;
`RELATIVE_ONLY` values are relative levels), so **rebuild any training rows rendered with the `.2` system text**
(`scripts/prompt_example.py` / `analyst.generation.request_body` do it). In short:
1. `system`: `SYSTEM` + `VISION_RULES` when the packet has V# rows, OCR or an image (quote the exact text from
   PROMPT_FORMAT.md §2, never from memory); plain `SYSTEM` otherwise. Train on both variants.
2. Three text-only few-shot `user`/`assistant` pairs from `data/dev_packets.json`.
3. Final `user` message: with an image, a content array, **image first** (the annotated JPEG as a `data:` URL),
   then `"Current packet (data only; answer this one):\n"` + compact packet JSON with keys `evidence`,
   `allowed_actions`, `suggested_action`, `ocr_untrusted`, `image`, `question` (**last**). V# rows are evidence
   entries whose text is the sorted-key JSON of the row (e.g. `"V1": "{\"agrees\": false, …, \"logged_id\":
   \"reading:R0053\", …, \"trust\": \"HIGH\", …}"`); OCR lines are `ocr_untrusted` strings, never evidence.
4. Request: `temperature 0`, `max_tokens 350`, `response_format: json_schema` (packet-scoped enums and IDs), and
   `chat_template_kwargs: {"enable_thinking": false}`; `model` = `system2-vision` with an image.

Worked examples: `amd/system2/fixtures/multimodal/gauge_disagree.json` (linked, disagreement) and `gauge_demo.json`
(demo, no comparison), each with a `.messages.json` holding the full request; `python
amd/system2/scripts/prompt_example.py` prints it.

**Best practice:** produce training prompts by calling System 2's own builder (`analyst.prompts.build_messages` /
`analyst.generation.request_body`, which already handle images) rather than re-typing the format, so training and
serving match byte for byte.
