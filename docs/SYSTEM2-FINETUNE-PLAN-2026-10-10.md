# System 2 fine-tune: what can be trained, on what, and what needs a decision (2026-10-10)

Facts below come from the code at `db3fb1d` and measurements on the SSH W7900D box; open items are marked.
Run records: `amd/system2/results/archive/untuned-q8-original-2026-10-10/` (original untuned deployment),
`amd/system2/results/smoke/after-fixes-2026-10-10/` (after the application fixes),
`amd/system2/results/eval/measurebench-gauge-reader-2026-10-10/` (gauge reader on real photos).

## 1. The checkpoint behind the deployed GGUF
- Deployed: `unsloth/Qwen3.8-27B-GGUF` @ `4ca7207…`, `Qwen3.8-27B-Q8_0.gguf` + `mmproj-F16.gguf` (Unsloth quantization).
- GGUF metadata names the base as `Qwen/Qwen3.8-27B` (`general.base_model.0.repo_url`), architecture `qwen35`, vision
  projector `qwen3vl_merger`. **It does not record which base revision Unsloth converted.**
- Base downloaded to the box (`/root/models/qwen38-27b-base`): `Qwen/Qwen3.8-27B` @ `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`
  (current main), `Qwen3_5ForConditionalGeneration` (image-text-to-text), 27,781,427,952 parameters, Apache-2.0.
  All 19 LFS files verified against huggingface.co's published sha256 (fetched from the laptop; the box can only
  reach hf-mirror.com).
- The Q8_0 GGUF is a quantized inference artifact, not a training input. Training starts from the safetensors base;
  the result returns to serving as a LoRA GGUF (`convert_lora_to_gguf.py`, then `llama-server --lora`) or merged and
  re-quantized to Q8_0 (keeps "same quantization strategy" for the comparison). Whether `convert_lora_to_gguf.py`
  handles `qwen35` linear-attention modules is **unverified**.

## 2. How images reach Qwen and what the validator accepts (from the code)
| Question | Answer |
|---|---|
| Does Qwen get the raw image, rows, or both? | Both. The orchestrator sends the vision service's **annotated** image as `image_b64` plus `context.vision` V# rows and `context.ocr` lines (`orchestrator/assess.py`, `system2` node). |
| Who computes vision values and confidence? | The vision service (`amd/vision`): code readers (Hough/RANSAC dial, colour-bar LUT), no model. |
| Who computes trust? | The orchestrator (`vision_rows`): HIGH iff confidence >= `VISION_TRUST_MIN` (0.5) and no `AMBIGUOUS_NEEDLE`/`DEFAULT_SWEEP`. System 2 drops `value`/`delta`/`agrees` from LOW rows (`packet.py`). |
| Can Qwen make direct visual claims? | No numeric ones. Every claim must cite packet IDs and every number, ID, date or entity in it must appear in the cited rows (`grounding.py`). The image is never an evidence ID ("context only; never evidence", `VISION_RULES`). A number read off the photo with no row is `ungrounded_number` and the answer is rejected. Word-only remarks about the photo are not checked against anything. |
| When does a photo support an accepted recommendation? | Only through a cited `V#` row: the vision service must produce a reading, it must be HIGH trust, the image must be `pairing: linked` (the operator's own photo of this asset; `demo` rows never judge the asset), and Qwen must cite the `V#` ID with matching numbers. |
| If the gauge reader's row is LOW or missing? | Qwen has **no permitted route** to use the photo's reading. Correct behaviour is to rely on logged rows and, where the reading matters, ask for a manual reading or a new capture. |

**Measured:** on 118 real MeasureBench pressure-gauge photos the reader gave HIGH trust 7 times, and 5 of those 7
were wrong; 89 LOW, 22 no value. So with real photos the "photo supports the answer" path almost never opens, and
when it does it is usually wrong.

## 3. Training objectives
| # | Objective | Trainable by fine-tuning Qwen? | Accepted by the application today? |
|---|---|---|---|
| 1 | Concise, complete, cited explanations and rule-consistent actions from supplied records (incl. V# rows, demo vs linked, LOW rows, conflicting evidence, requests for recapture/manual review) | Yes (LoRA on the language layers, vision tower frozen) | **Yes**: this is exactly what the checker and schema accept. |
| 2 | Qwen reading instruments from the image | In principle | **No**: image-derived numbers are rejected unless the evidence contract changes (e.g. a separate, never-authoritative "model reading" field). Not recommended for this deadline. |
| 3 | Better extraction/confidence in the vision service | **No**: it is code, not Qwen. Fine-tuning Qwen does not change `confidence` or `trust`. | n/a (separate work: the gauge reader, deferred) |

**Recommendation: objective 1 only.** The test must then show improvement on behaviour the app accepts: fewer
rejected attempts/fallbacks, correct actions under explicit scenario rules, appropriate recapture/manual-review
requests when V# rows are LOW, and no unsupported claims.

## 4. Data
**MeasureBench** (`FlagEval/MeasureBench` @ `93c00167…`, CC BY-SA 4.0): `real_world` 1,272 items, 26 instrument
types; 84 % are YouTube video frames. Relevant slice: `pressure_gauge`, 118 photos, all dials, 114 source groups
(102 YouTube videos), labels are human reading intervals with units (psi, MPa, bar, kPa, kgf/cm², 39 dual-scale).
- Supports: real gauge photos for the simulated replay, and an evaluator answer key for readings and units.
- Does **not** support: maintenance recommendations, operating limits, or **any thermal imaging** (its
  "thermometer" items are liquid/dial thermometers, not IR images). No thermal claims can rest on it.
- 118 photos cannot fill 200-400 train + 30-50 val + 30-50 test from gauges alone once grouped by source; and for
  objective 1 the photo is not what is learned, the packet behaviour is. Scenario packets per photo (several
  operating-limit situations, linked vs demo, LOW vs HIGH rows) are needed, all grouped by source photo/video.
- Licence: CC BY-SA 4.0 is share-alike; anything published that derives from it (manifests with its labels,
  training targets) must be CC BY-SA. Per-image rights of YouTube frames are not stated by the dataset.

## 5. Compute: the decision that blocks training
- bf16 weights alone are ~55.6 GB; the W7900D has 48 GB, and the serving llama-server already holds ~30 GB. **bf16
  LoRA on this box is impossible.** The container also has a 55 GiB RAM limit (no CPU offload headroom).
- 4-bit QLoRA (~16-18 GB weights) might fit with llama-server stopped, but: Unsloth advises against QLoRA for the
  Qwen3.5 family (`docs/TEAMMATE-qwen-vision-finetune.md` §4), bitsandbytes 4-bit on ROCm gfx1100 with `qwen35`
  is **untested here**, and the box has no transformers/peft/trl installed yet.
- A larger AMD GPU (e.g. a 192 GB MI300X-class instance) would allow bf16 LoRA but is a new resource and a spending
  decision.

## 6. Deferred
- Gauge-reader accuracy on real photos (2/118 usable and correct) - separate vision work.
- Thermal: no labelled thermal dataset is in hand; the kit thermal photo is a regression case only.
- Operator UI states (photo used as evidence / uncertain / demo / not used / template fallback) - not started.
