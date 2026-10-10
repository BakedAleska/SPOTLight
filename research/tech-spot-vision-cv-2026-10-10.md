# Reading SPOT inspection images (thermal, acoustic, gauge, path) and feeding them to the models (2026-10-10)

## 1. Scope
The plan under study: a CV step reads the kit's inspection images and passes accurate readings, plus the images, to System 1 (Laya) and System 2 (Qwen VLM).

Ten parallel research passes, several with small local prototypes (scratchpad only):
1. thermal
2. acoustic
3. gauges
4. robot path and maps
5. which VLM to run
6. OCR, charts and screenshots
7. fusion into Laya
8. fusion into the System 2 VLM
9. synthetic data and evaluation
10. how Boston Dynamics' Orbit does it, an image router, and architecture

Skipped:
- training audio anomaly models (no audio in the kit)
- fine-tuning a VLM for gauges (GRPO on synthetic dials took Qwen2.5-VL-7B only from 14.6% to 19.7% on real images)
- early fusion of image embeddings into Laya

## 2. What the kit actually contains
- **No CSV row links to an image.** The 6 `sample_inspection_images/` are about 240 px, synthetic, and illustrative. The 18 Orbit screenshots are UI captures.
  - Any image-vs-log cross-check is a demo pairing by action type.
  - Scored evaluation needs generated images (§6).
- **What the readings can be checked against:** readings.csv has 40 TEMPERATURE_C rows (39.8–83.5), 37 SOUND_DB rows (52.9–85.0) and 30 PRESSURE_PSI rows (68.6–150.6). 24 of the 107 are QUESTIONABLE.
- **Action types:**

  | Action type | Count |
  |---|---|
  | THERMAL_INSPECTION | 49 |
  | ACOUSTIC_INSPECTION | 43 |
  | GAUGE_READING | 38 |
  | VISUAL_INSPECTION | 68 |
  | NAVIGATION (no image) | 72 |

- **The gauge sample is AI-generated and unreadable.**
  - It prints "290" where 250 should be.
  - Its two-ended needle points at both 0 and about 200 psi; neither falls in the logged psi range.
  - A good reader should return **low confidence** on it. That's a demo point, not a bug.
- **The thermal sample saturates its own scale.** The bearing hotspot is about 99.8 °C at the 100 °C top of the bar, so treat it as clipped (≥100). The motor body is about 40 °C, so ΔT is about 58 °C, which is "critical" under every scheme below.
- **The acoustic sample is a 0–20 kHz spectrogram with no colorbar.**
  - It gives relative band energy only, never absolute dB.
  - Real leak ultrasound is about 20–80 kHz, so a leak call from this image is weak.
  - Prototype result: the 0–2 kHz band is strongest, with a peak around 1.3 kHz and broadband bursts about every 2 s.
- **The Orbit screenshots carry the real numbers as text:**
  - Acoustic "Key Results": SPL At Source, SNR Value and SNR Threshold. **Status = SNR > threshold**, not SPL: 167/200 is NOMINAL; 230, 781 and others are ANOMALY.
  - Thermal panes show Region Max/Min °F and **no colorbar**, so colour inversion only applies to the kit samples.
- **Only 1 of 18 screenshots shows a map** (11.35.29, a Site View GraphNav map over CAD). It has no WP labels and no mission trail.
- **`locations.csv` x/y coordinates don't match the pins on `fictional_floor_plan.png`.** For example, WP06 is listed top-left but its pin is top-centre.
  - `web/src/lib/kit.ts` loads the coordinates but nothing draws them on the PNG yet.
  - If we draw routes, use a hand-placed pin table instead.

## 3. Core principle: code makes the numbers, models read labels and explain
- **VLMs can't read the measurement itself:**
  - MeasureBench: best model (Gemini-2.5-Pro) 30.2% over all 26 real instrument types and 31.5% on real dials, though units are right more than 90% of the time; Qwen3-VL-8B gets 15.3%, and prior special-purpose gauge readers scored lower still (8.5%, or failed on all). Scoring is interval matching ([2510.26865](https://arxiv.org/abs/2510.26865), HTML Tables 2 and 7).
  - InSituMeasure: best of 24 models 25.7% ([2609.04014](https://arxiv.org/abs/2609.04014)).
  - ThermEval: 25 VLMs "consistently fail at temperature-grounded reasoning" ([2602.14989](https://arxiv.org/abs/2602.14989)).
  - MapEval routing: 50% vs 85% for humans ([2501.00316](https://arxiv.org/pdf/2501.00316)).
- **Supplying correct extracted text plus the image helps:** VQAv2 rises from 85.5% to 92.8% ([2503.02199](https://arxiv.org/abs/2503.02199)), and DePlot's two-step method beats the fine-tuned state of the art by 24% ([2212.10505](https://arxiv.org/abs/2212.10505)).
- **But VLMs follow wrong text.** With corrupted text, LLaVA-NeXT falls from 79.5% to 28.7% (same paper), and "focus on the image" prompts barely help.
- **So:**
  - Code computes every value and every value-vs-log comparison (`delta`, `agrees`).
  - Only readings above a confidence floor are passed on.
  - The VLM never arbitrates between the tool and the log.

## 4. Per-modality readers
| Modality | Method (recommended) | Expected accuracy | Effort |
|---|---|---|---|
| **Gauge** | Hough circle for the centre. Needle = argmax of ray darkness from the centre; peak/2nd-peak ratio = ambiguity. Calibrate value = a·θ + b by RANSAC over OCR'd scale labels; fall back to per-asset θmin/θmax config, then a default 270° sweep. VLM reads only unit/min/max. | ~1–2% FS when OCR works ([ETH 2404.08785](https://arxiv.org/html/2404.08785v1)); ~2–5% FS estimate for our geometry | 5–7 h |
| **Thermal** | Find the colorbar; LUT = median of the bar's centre columns, ends trimmed. OCR tick labels → linear fit (residual 0.22 °C on the sample). Map scene pixels to the nearest LUT colour with a distance cut-off, 5×5 smoothing. Hotspot = largest blob within max−3 °C. ΔT vs reference region. Flag `clipped`. VLM only names the hot component. | ±1–2 °C typical, ~±5 °C worst case, poor at the flat yellow/white end | 3–4 h |
| **Acoustic, Orbit frame** | OCR Key Results → `spl_db`, `snr`, `snr_threshold`; rule `anomaly = snr > threshold` | ≈100% on clean screenshots (tested) | 2–3 h |
| **Acoustic, spectrogram** | OCR axes → calibrate. Jet-LUT inversion with off-table pixels masked. Band energy per kHz, peak kHz, high-frequency ratio, burst times from >5 kHz energy peaks. Relative only; `absolute_db_available=false`. | Good for shape, no absolute dB | 3–4 h |
| **Path/map** | **No CV.** Build the route from `actions.csv` waypoint order (ground truth). Draw it as an SVG over the floor plan using hand-placed pins. | Exact | 3–4 h |

Not worth doing:
- Audio models on a spectrogram image (AST expects 16 kHz audio with 128 mel bins; going back from a picture is lossy and out of distribution).
- Thermal models on HF (only person/dog detectors exist).
- Map-trail skeletonization (6–10 h, brittle, no labels).

**Severity mappings** (for Laya and System 2):
- **Thermal, NETA MTS ΔT between similar components:** 1–3 °C possible, 4–15 °C probable, >15 °C major. These bands are from secondary sources; verify before quoting.
- **Thermal, grease bearings:** alarm 90–110 °C, trip 95–120 °C ([TECO](https://www.tecowestinghouse.com/Stock_Motor_PDF/RTD_Alarm_Trip_Settings.pdf)).
- **Acoustic, SNR/threshold:** <1 none, 1–1.5 low, 1.5–3 medium, ≥3 high. This is our own proposal.

## 5. Shared pieces
- **OCR: RapidOCR on onnxruntime CPU** (Apache-2.0, [repo](https://github.com/RapidAI/RapidOCR)). Tested locally on all kit images:
  - It read every spectrogram tick, all colorbar labels, the gauge labels (including "290") and the Orbit Key Results.
  - Common misreads are minus signs from tick marks and 0↔8 / 5↔S swaps; `parse_num` plus RANSAC absorbs them.
  - ORT dropped its ROCm provider in 1.23, so stay on CPU, which is fast enough (0.2–1.2 s).
  - Florence-2-large (MIT, 0.77B) is the GPU alternative.
- **`textaxes` module:**
  - `ocr(img) -> [Word(text, quad, conf)]`, which upscales 3× when height < 300 px
  - `parse_num`
  - `find_ticks`
  - `calibrate_axis(ticks, scale=linear|log|sqrt)` via RANSAC, with `min_samples=2` and failure below 3 inliers
  - `kv_rows` to pair Orbit "label … value" rows
  - Orbit gauges can use square-root scales.
- **VLM: the Qwen3.5-9B already on the box.**
  - Add `--mmproj mmproj-F16.gguf` (918 MB, from [unsloth/Qwen3.5-9B-GGUF](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF)), so no second model.
  - Benchmarks: OCRBench 89.2, CharXiv 73.0, RefCOCO 89.7.
  - Image cost: a 240 px image is ~64 tokens; a full screenshot is ~5.8k tokens, so cap with `--image-max-tokens 1024`.
  - Backup: Qwen3-VL-8B-Instruct.
  - **gfx1151 risks:**
    - Cross-request content leak with mmproj ([#28056](https://github.com/ggml-org/llama.cpp/issues/28056); fix in unmerged PR #28058; use its repro as the acceptance test).
    - Corrupted output with `n_ubatch<n_batch` or `--parallel≥2` ([#27579](https://github.com/ggml-org/llama.cpp/issues/27579)).
    - Encoder crash at ≥2048 px ([#28608](https://github.com/ggml-org/llama.cpp/issues/28608); downscale to ≤1280).
    - Possible flash-attention corruption of image embeddings ([#29629](https://github.com/ggml-org/llama.cpp/issues/29629)).
    - Whether image + `json_schema` work in one request is untested; the fallback is two calls.
- **Router:**
  1. `action_type` from the event (free and certain).
  2. Colour heuristic for event-less uploads: ≥81% high-saturation pixels means thermal or spectrogram.
  3. SigLIP2 zero-shot ([google/siglip2-base-patch16-224](https://hf.co/google/siglip2-base-patch16-224)).
  4. VLM as a last resort.

  Bonus: an "image/event mismatch" flag when SigLIP disagrees with `action_type`.

## 6. Data and evaluation
- **Render one image per reading from its CSV value**, so the ground truth is exact:
  - gauge: matplotlib, needle at the logged psi
  - thermal: Gaussian hotspot at the logged °C, ironbow 25–100 °C with a colorbar
  - spectrogram: synthesized audio (hum, broadband hiss for leaks, periodic impulses for bearing faults), rendered 0–20 kHz in jet
  - All renders get domain randomization ([Tobin 2017](https://arxiv.org/abs/1703.06907)): ~240 px, JPEG quality 40–90, blur, ±8° rotation, glare.
  - Seed = `sha1(reading_id)`. Images go to `amd/vision/data/synth/` with a `manifest.jsonl`.
  - MeasureBench's Blender generator is the photoreal tier.
- **Real checks:**

  | Name | Modality | License | URL |
  |---|---|---|---|
  | MeasureBench (1,272 real) | gauges | CC BY-SA 4.0 | https://huggingface.co/datasets/FlagEval/MeasureBench |
  | Induction-motor thermal | thermal | CC BY 4.0 | https://data.mendeley.com/datasets/m4sbt8hbvk/2 |
  | Dry-type transformer thermal | thermal | CC BY 4.0 | https://data.mendeley.com/datasets/8mg8mkc7k5 |
  | MIMII valve/pump | audio | CC BY-SA 4.0 | https://zenodo.org/records/3384388 |

- **More Hugging Face candidates** (folded in from the 2026-10-10 dataset survey; sizes and labels checked via the HF datasets-server, nothing downloaded):

  | Dataset | Modality | Size / labels | Licence | Use |
  |---|---|---|---|---|
  | [monica-sekoyan/MeasureBenchPressureGauge-DiverseSynthetic](https://huggingface.co/datasets/monica-sekoyan/MeasureBenchPressureGauge-DiverseSynthetic) | pressure gauges (MeasureBench renderers) | 1,000 / 50 / 100; exact psi, needle angle, scale-zero angle | not stated | Tune and test the gauge reader against exact truth; private use only |
  | [moondream/synthetic-gauges-v6](https://huggingface.co/datasets/moondream/synthetic-gauges-v6) | synthetic dials | 262,144 (~20 GB); JSON value + tick/label pixel positions | not stated | Stream a slice if a learned needle/tick detector is ever needed |
  | [LibreYOLO/corrosion-bi3q3](https://huggingface.co/datasets/LibreYOLO/corrosion-bi3q3) | real photos | 840 / 304 / 105; boxes: corrosion, crack, slippage (Roboflow 100) | CC BY 4.0 | VISUAL_DEFECT: VLM yes/no eval or a small detector |
  | [lg227210/gas-valve-anomaly-dataset](https://huggingface.co/datasets/lg227210/gas-valve-anomaly-dataset) | real gas-valve wells | 1,301 images, 3,071 boxes: water, corrosion_rust, seepage, crack, coating damage, fog (VLM-made, human-verified) | MIT | Closest real plant-equipment defects |
  | [khoaliamle/Corrosion_Rust](https://huggingface.co/datasets/khoaliamle/Corrosion_Rust) | photos | 750; rust / no rust | MIT | Simplest binary check |
  | [HTill/dcase2025_task2_dev](https://huggingface.co/datasets/HTill/dcase2025_task2_dev) | machine audio | 5,000 / 1,000 / 700; machine type incl. **valve**, normal/anomaly | MIT | Acoustic, only if audio ever replaces the spectrogram image |
  | [stdt1/mimii_pump_datasets](https://huggingface.co/datasets/stdt1/mimii_pump_datasets) | pump audio as mel spectrograms | 8,830 / 1,893 / 1,892; normal/abnormal, SNR | not stated (MIMII itself is CC BY-SA 4.0) | Same |

  Not useful: HF thermal sets are unlabelled solar panels or "rgb vs thermal" folders (`nealgoogs/thermal-fault-detection-dataset`), so the Mendeley sets above stay the thermal check. `Voxel51/mvtec-ad` is factory parts under CC BY-NC-SA. `Mileeena/synthetic-analog-gauges` (CC BY 4.0, COCO boxes) is gated. MeasureBench's real split has 118 pressure gauges and its synthetic test split 120.
- **Per-reader metrics:**
  - gauge: MAE as % of full scale, and the share within ±2% FS
  - thermal: max-°C MAE, and hotspot localization error in px
  - acoustic: macro-F1 over normal/leak/impulsive
  - all readers: fallback rate and latency
- **End-to-end caveat.** Today's `reading_trust` label is fully determined by quality, status and range (`triage/policy.py`), so CV fields can't improve the existing rows.
  - Build a separate `vision` segment instead: render half the images at the logged value and half at a perturbed value (±15–40% FS, or stuck).
  - Compare Laya text-only vs Laya + vision.
  - Report the lift with mission-bootstrap CIs. Text-only should sit at chance on the perturbed half.
  - Keep `benchmark.jsonl` and `train.jsonl` byte-identical; vision rows go in their own files, behind a `--vision N` flag.

## 7. Feeding the models
**System 1 (Laya, text-only; `DecisionModel.forward` takes `input_ids` only).**
1. **Rule floor first, no retraining.** A confident CV value that disagrees with the log beyond tolerance makes the reading untrusted. Rules only raise priority, as in the shield pattern in `tech-cascade-alarm-mgmt`.
   - `CV_TOL`: 4 °C / 6 psi / 10 visual points.
2. **Then text early-fusion.** Add an optional `vision` block that keeps the raw value but puts the comparisons into words. Encoders handle magnitudes poorly ([Wallace 2019](https://arxiv.org/abs/1909.07940), [NumBERT](https://arxiv.org/abs/2010.05345)). Example:
   ```json
   "vision": {"source": "thermal camera", "value": 84.0, "confidence": "high",
              "band": "above safe band (max 78)", "vs_logged": "disagrees: camera 12.7 C higher"}
   ```
   - Keep it to 5 keys: action states peak at 251 of the 320-token budget, and this block makes 309.
   - Use lowercase words; UPPER_SNAKE enums split into many tokens.
   - **`service/schemas.py` uses `extra="ignore"`, so a `vision` field is silently dropped today.** Add a `Vision` model.
3. **Training rows:**

   | Share | Simulated CV output | Label |
   |---|---|---|
   | 60% | agrees with the log | trusted |
   | 15% | confident disagreement | untrusted |
   | 15% | low-confidence and wrong | trusted (teaches not to over-trust) |
   | 10% | unreadable | unchanged |

   - Pair each row with its no-vision twin via `meta.pair` (missing-modality dropout comes free).
   - Fine-tune from the best checkpoint for 2–3 epochs.
   - Acoustic and path data aren't per-reading cross-checks, so they go to System 2 only.

**System 2 (VLM).**
- Each tool reading becomes a citable evidence row `V#`, for example: `tool, value, unit, confidence, trust, logged_id, logged_value, delta, agrees, pairing=demo`.
  - The image itself is never citable evidence, so the existing claim-local numeric checks in `grounding.py` work unchanged.
  - Add `kHz` to its units.
- Draw the needle line, hotspot box and peak marker on the image, labelled with their V# IDs (Set-of-Mark: [2310.11441](https://arxiv.org/abs/2310.11441); Visual Sketchpad +8.6%: [2406.09403](https://arxiv.org/abs/2406.09403)).
- Message order: image first, then the packet, with the question last.
- Prompt rule: "Do not read numbers off the image. If agrees=false, cite both IDs and prefer MANUAL_READING / CHECK_SENSOR_PAYLOAD."
- `/v1/assess` gains `context.vision[]` and an optional `image_b64`. The change is additive; bump the contract to `2026-10-10.backend.2`.

## 8. Architecture and output shape
- **Separate `amd/vision/` service on :8084** rather than adding to System 2: OpenCV/OCR dependencies differ from llama.cpp, and System 2's contract stays stable.
  - It calls llama-server for VLM label reading.
  - Sync, with a timeout; cache keyed on `sha256(image) + versions`.
  - Write `docs/vision-contract.md` first; add a compose service, ports table and RUNNING.md entry (CLAUDE.md rules); run in a venv on the AMD VM.
  - **Contract resolved:** separate vision service on :8084 (`docs/vision-contract.md`). The System 2 pass had sketched `/v1/vision` on System 2's port (8083); the request/response shape moved to the vision service.
- **Mirror Orbit's data model**, which NextEra's Spot operators will recognise:
  - output `dataCaptures[].keyResults[{name, units, value}]` plus `alert{title, severity 0–4, source}`
  - plus our own fields: `logged_value`, `delta`, `agrees`, `router`, `annotated_image`
  - Webhook types are `ACTION_COMPLETED` and `ACTION_COMPLETED_WITH_ALERT` ([Orbit API](https://dev.bostondynamics.com/docs/concepts/orbit/orbit_api), [anomaly JSON](https://github.com/boston-dynamics/spot-sdk/blob/master/docs/concepts/orbit/about_orbit.md)).
  - Pitch line: the service could run as a [Network Compute Bridge](https://dev.bostondynamics.com/docs/concepts/network_compute_bridge) worker.
- **Production note for the pitch:**
  - Real Spot gives radiometric thermal data (CAM+IR, 640×512 uint16 decikelvin; ±5 °C; [docs](https://dev.bostondynamics.com/docs/concepts/data_acquisition_thermal_raw)).
  - Real routes come as data: GraphNav waypoints and edges, and Orbit's `get_run_statistics`.
  - Orbit's AIVI reads gauges with Gemini Robotics-ER 1.6, which scores 93% on instrument reading ([DeepMind](https://deepmind.google/blog/gemini-robotics-er-1-6)).
  - So pixel reading in our build stands in for the kit's JPEGs, not for production.
- **HF prize angle:**
  - Publish the synthetic inspection-image dataset with ground truth (check NextEra's terms before including any kit images).
  - A Gradio Space ("upload an inspection image → Orbit-style key results + annotated image").
  - List every Hub model used (Qwen3.5, SigLIP2, Laya, RapidOCR/PP-OCR).

## 9. Suggested build order (~20 h)
1. `textaxes` OCR + calibration (1–2 h)
2. Orbit Key Results parser (2 h)
3. Thermal reader (3–4 h)
4. Gauge reader (5–7 h)
5. Synthetic renderer + vision segment (4–6 h)
6. Laya rule floor + `vision` block (3 h), then fine-tune (1 GPU h)
7. System 2 `V#` rows + annotated image (3 h)
8. CSV route overlay in the console (3–4 h)
9. Spectrogram features (3 h), only if time remains

## 10. Open questions
- Whether llama.cpp handles image + `json_schema` together with the Qwen3.5 mmproj, and whether PR #28058 merges cleanly into our build.
- Vision-encoder, RapidOCR and Florence latency on gfx1151 (all timings so far are from a Mac or are estimates).
- The exact definition of Orbit's "SNR Value", and the JSON behind its thermal Region results.
- The verbatim NETA MTS Table 100.18 and the ISO 18434-1 severity tables.
- Whether the kit images may be redistributed on HF.
- The Synanthropic gauge dataset license.
