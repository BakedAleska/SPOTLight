# HF datasets to benchmark Qwen3.8-27B on detection, grounding, hallucination and scene VQA (2026-10-11)

## 1. Scope
- **Searched** the HF Hub (MCP `hf_fs search`).
  - Standard: POPE/RePOPE, AMBER, MMHal, HallusionBench, RefCOCO/+/g, CountBench, PixMo-Count, MMStar, RealWorldQA, MMBench, SEED, CV-Bench, ODinW, COCO, LVIS, Objects365, OpenImages.
  - Industrial: valve, pipe, pump, substation, electrical panel, power equipment, insulator, PPE, hard hat, fire extinguisher, leak, spill, industrial VQA, MMAD, MME-Industry, IndustryEQA, RF100/RF100-VL, wind turbine, robot inspection.
- **Verified without downloading:** datasets-server `/info /size /rows /statistics`, the HF tree API, and range reads of zip central directories. All recommended files return 200 on hf-mirror.com.
- **Model docs:** the [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B), [Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B) and [Qwen3.5-27B](https://huggingface.co/Qwen/Qwen3.5-27B) cards, the [Qwen3-VL report](https://arxiv.org/abs/2511.21631), the [grounding cookbook](https://github.com/QwenLM/Qwen3-VL/blob/main/cookbooks/2d_grounding.ipynb) and [RF100-VL](https://arxiv.org/abs/2505.20612).
- **Skipped:** thermal, gauge and acoustic sets (other agents), the defect sets in `tech-spot-vision-cv` §6, video and gated repos.

## 2. What Qwen emits (scoring contract)
- **Boxes:** `[{"bbox_2d": [x1, y1, x2, y2], "label": "..."}]`, often inside a ```json fence. Points use `point_2d`.
  - Coordinates are **relative 0–1000**. The Qwen3-VL report says: "Different from Qwen2.5-VL, we adopt a normalized coordinate system scaled to the range [0, 1000]".
  - Pixel = `v / 1000 × original W or H`, so llama.cpp's resize doesn't matter.
  - The Qwen3.5/3.8 cards don't restate this; probe first (§6).
- **Official eval prompts** (Qwen3-VL report, App. B.5):
  - RefCOCO: `Locate every object that matches the description "{ref}" in the image. Report bbox coordinates in JSON format.`
  - ODinW-13: `Locate every instance that belongs to the following categories: "{names}". Report bbox coordinates in JSON format.` Scored as mAP with confidence fixed at 1.0.
  - CountBench is scored as multiple choice.
- **Limit:** more than 40–50 instances can cause an endless generation loop (cookbook), so cap `max_tokens`.
- **Thinking is on by default.** Send `"chat_template_kwargs": {"enable_thinking": false}` and `temperature: 0`.
- **Contamination:** Qwen3-VL's grounding data includes COCO, Objects365, OpenImages and RefCOCO/+/g. COCO-based scores are **deployment sanity checks**, not a test of generalisation.

## 3. Candidates

| Dataset | Tier | Images / splits | Task + labels | Licence | Gated | Download | Published Qwen 27B | Fit |
|---|---|---|---|---|---|---|---|---|
| [MM-Hallu/RePOPE](https://huggingface.co/datasets/MM-Hallu/RePOPE) | std | 8,185 Qs, 500 COCO val2014 images (adversarial 2,684); yes 3,539 / no 4,646 | Object-presence yes/no, relabelled POPE | MIT | no | 255 MB (9 parquet) | none | **High**: the hallucination metric itself. Images contaminated |
| [lmms-lab-encoder/HallusionBench](https://huggingface.co/datasets/lmms-lab-encoder/HallusionBench) | std | 951 image Qs + 178 text-only | yes/no on charts, tables, illusions | not stated | no | 147 MB | 70.0 (3.5) | Low–med: charts/illusions, not scenes |
| [lmms-lab-encoder/RefCOCO](https://huggingface.co/datasets/lmms-lab-encoder/RefCOCO) | std | val 8,811 / test 5,000 / testA 1,975 / testB 1,810 refs | REC: expression list + COCO xywh pixel box | not stated | no | testB 133 MB | RefCOCO avg 90.9 (3.5), 92.5 (3.6) | **High** as deployment check. testB = non-person objects. In training |
| [vikhyatk/CountBenchQA](https://huggingface.co/datasets/vikhyatk/CountBenchQA) | std | 491 test | Count question, integer 2–10 | not stated | no | 23 MB | 97.8 (3.5/3.6, MC protocol) | Med: cheap sanity check, saturated |
| [Lin-Chen/MMStar](https://huggingface.co/datasets/Lin-Chen/MMStar) | std | 1,500 val | General MCQ | not stated | no | 42 MB | 81.0 (3.5), 81.4 (3.6) | Med: cheap sanity check, not plant scenes |
| [xai-org/RealworldQA](https://huggingface.co/datasets/xai-org/RealworldQA) | std | 765 test | Short answer / MCQ, real scenes | CC BY-ND 4.0 | no | 678 MB (2 parquet) | **85.9 (3.8)**, 84.1 (3.6) | **High**: the only number on the Qwen3.8 card |
| [nyu-visionx/CV-Bench](https://huggingface.co/datasets/nyu-visionx/CV-Bench) | std | 2D 1,438 (count, relation) / 3D 1,200 | MCQ on COCO/ADE20K | Apache-2.0 | no | 185 MB (2D) | none | Med |
| [kcz358/ODinW-13](https://huggingface.co/datasets/kcz358/ODinW-13) | std | 4,938 test (3,422 PascalVOC) | xyxy pixel boxes | not stated | no | 214 MB | 41.1 mAP (3.5) | Low–med: VOC-heavy; protocol-sensitive (RF100-VL paper) |
| [AndrzejDD/15-class-Substation-Equipment](https://huggingface.co/datasets/AndrzejDD/15-class-Substation-Equipment) | ind | train 5,023 / val 601 / test 1,452 (1,033 unique stems); 16–45 boxes per image | YOLO xywh, 15 classes: open/closed disconnect switches, breaker, insulators, arrester, recloser, muffle, power/current/potential transformers… | Apache-2.0 (HF); [original](https://www.mdpi.com/2306-5729/8/7/118) CC BY 4.0 | no | test.zip 1.8 GB (~1.2 MB per image) | none | **High**: real substation, switch state, low contamination |
| [sxiong/Power-equipment-image-dataset](https://huggingface.co/datasets/sxiong/Power-equipment-image-dataset) (substation zip) | ind | 752 images, 7,640 polygons | VIA polygons, 18 types (capacitor, GIS, bushing, switch, PT, CT, breaker, arrester, insulator…) | MIT | no | 31 MB | none | Med–high: tiny; polygons → boxes; low-res |
| [Ajax102/MME-Industry](https://huggingface.co/datasets/Ajax102/MME-Industry) | ind | 1,050 MCQ, 50 × 21 sectors (Power, Petrochemical, Chemical, Machinery…) | A–D + E "no corresponding features" | Apache-2.0 | no | images.zip 444 MB + 0.5 MB JSON | Qwen2-VL-72B 75.04 % EN (paper) | Med–high: industrial VQA; E correct only once |
| [RF100-VL Industrial](https://huggingface.co/datasets/LibreYOLO/rf100-vl) ([parquet](https://huggingface.co/datasets/probicheaux/rf100-vl)) | ind | 22 sets; plant-relevant: `infraredimageofpowerequipment` (737 IR images, 9 substation classes, 31 MB), `cable-damage` | COCO boxes | MIT per set | no | per-set tars | Qwen2.5-VL-72B: 5.6 / 7.8 mAP overall | Med: OOD by design; IR overlaps thermal agent |
| [lg227210/valve-detection-dataset](https://huggingface.co/datasets/lg227210/valve-detection-dataset) | ind | 9,038 (7,685 / 1,353) gas-valve wells | YOLO boxes: gate/globe/ball/other valve | CC BY-NC 4.0 | no | val tar 267 MB | none | Med–low: YOLOv8 pseudo-labels from 30 hand-checked seeds |

## 4. Rejected
- [lmms-lab-encoder/POPE](https://huggingface.co/datasets/lmms-lab-encoder/POPE): superseded by RePOPE's label fixes.
- [detection-datasets/coco](https://huggingface.co/datasets/detection-datasets/coco) (val 4,952, 807 MB): mAP without confidence scores is weak; in training.
- [allenai/pixmo-count](https://huggingface.co/datasets/allenai/pixmo-count): image URLs only.
- No images: [jxu124/objects365](https://huggingface.co/datasets/jxu124/objects365) (annotations only), [winvoker/lvis](https://huggingface.co/datasets/winvoker/lvis) (loader script).
- Manually gated: OpenImages / ODinW-35 / COCO under [xmu-xiaoma666-dataset](https://huggingface.co/xmu-xiaoma666-dataset).
- AMBER: no HF copy found.
- [MMHal-Bench](https://huggingface.co/datasets/Shengcao1006/MMHal-Bench): GPT-4-judged.
- [MMBench](https://huggingface.co/datasets/lmms-lab-encoder/MMBench): v1.0 vs Qwen's v1.1; needs CircularEval.
- [SEED-Bench](https://huggingface.co/datasets/lmms-lab-encoder/SEED-Bench): 27 GB.
- [jiang-cc/MMAD](https://huggingface.co/datasets/jiang-cc/MMAD): MVTec/VisA factory parts, CC BY-NC-SA.
- [IndustryEQA](https://huggingface.co/datasets/IndustryEQA/IndustryEQA): synthetic warehouse videos.
- Worker PPE, not equipment: [construction-safety-gsnvb](https://huggingface.co/datasets/Francesco/construction-safety-gsnvb), [hard-hat-detection](https://huggingface.co/datasets/keremberke/hard-hat-detection).
- [cables-nl42k](https://huggingface.co/datasets/Francesco/cables-nl42k): telecom racks with whole-image boxes.
- [gauge-u2lwv](https://huggingface.co/datasets/Francesco/gauge-u2lwv): gauge agent's scope.
- [sriom1/electrical-panels-dataset](https://huggingface.co/datasets/sriom1/electrical-panels-dataset): text rows, auto-labelled.
- [neurograce/SubstationDataset](https://huggingface.co/datasets/neurograce/SubstationDataset): satellite imagery, ~70 GB.
- [jcccqw/Substation_elements_detection.v2i](https://huggingface.co/datasets/jcccqw/Substation_elements_detection.v2i): empty repo.
- [aialliance/substation](https://huggingface.co/datasets/aialliance/substation): stats only.
- [Verso-Industries/oil-gas-leakage](https://huggingface.co/datasets/Verso-Industries/oil-gas-leakage): tabular.
- [jjldo21/IndustrialDetectionStaticCameras](https://huggingface.co/datasets/jjldo21/IndustrialDetectionStaticCameras): near-duplicate video frames.

## 5. Recommended suite
**Common settings:**
- One image per request, with `--parallel 1`.
- Thinking off, `temperature: 0`.
- `max_tokens`: 16 for yes/no and MCQ, 512 for boxes.
- Downscale the longest side to ≤1280 px client-side, but keep the original W×H for box conversion.
- Strip ```json fences. Unparseable output counts as a miss.
- Time 20 items before running the full slices.
- Report 95 % bootstrap CIs, resampling by image.

**Fetch on the box, never from the laptop** (laptop→box upload is ~16 KB/s):
```
export HTTPS_PROXY=… HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1
huggingface-cli download --repo-type dataset OWNER/NAME FILE --local-dir /root/bench/NAME
# or: curl --http1.1 -L -O https://hf-mirror.com/datasets/OWNER/NAME/resolve/main/FILE
```
Use `/root`, not `/tmp` (only 4 GB).

1. **Hallucination: RePOPE adversarial.** All 9 parquet (255 MB).
   - Slice: `category == "adversarial"`, 100 seeded image_ids, about 540 Qs.
   - Prompt: `{question} Answer yes or no only.`
   - Metrics: accuracy, precision, recall, F1, and the **yes-ratio and false-yes rate** (the hallucination number).
   - No published Qwen3.x POPE score (the Qwen3-VL report has no POPE).
2. **Grounding sanity: RefCOCO testB.** `data/testB-00000-of-00001.parquet` (133 MB).
   - Slice: 300 seeded refs, using the first expression of each.
   - Prompt: the official RefCOCO prompt in §2.
   - Metric: Acc@IoU ≥ 0.5 of the first box (GT xywh → xyxy).
   - Expect about 90 (avg 90.9 / 92.5). Below about 80 points to a coordinate-convention or mmproj fault.
3. **Industrial: Substation-15 test.** `test.zip` (1.8 GB). Dedupe by stem, then sample 150 seeded images.
   - **(a) Industrial POPE.** Per image, ask about 3 present and 3 absent classes (900 Qs). Draw the absent ones first from confusable twins: open↔closed switch, current↔potential transformer, glass↔porcelain insulator.
     - Prompt: `Is there a {class} in this substation photo? Answer yes or no.`
     - Metrics: accuracy, F1, false-yes rate.
   - **(b) Per-class grounding and counting.** For each present class, prompt `Locate every instance that belongs to the following category: "{class}". Report bbox coordinates in JSON format.`
     - Metrics: precision, recall and F1 at IoU 0.5 with greedy matching; count exact-match and MAE.
     - Use single-class prompts: RF100-VL found Qwen does better with them, and these images sit near the 40–50-instance loop limit.
   - Spot-check 30 false positives by eye in case the labels missed instances.
4. **Scene VQA: RealWorldQA.** Both parquet files (678 MB).
   - Slice: 300 seeded Qs; prompts as given.
   - Metric: normalized exact match (letter or single word).
   - Reference: 85.9 for Qwen3.8-27B. n = 300 gives about ±4 pp.

**Optional add-ons:**
- CountBenchQA, all 491: `{question} Answer with a number only.`, exact match (ref 97.8, MC protocol).
- MME-Industry, about 200 MCQ from the Power, Petrochemical, Chemical and Machinery sectors; accuracy, logging E picks.
- The 31 MB sxiong substation set as a second industrial check.

## 6. Open questions / next searches
- **Coordinate probe first:** box one COCO object and score IoU under both the 0–1000 and pixel readings.
- Unstated on the cards: whether Qwen3.8's RealWorldQA score used thinking, and which splits make up the RefCOCO "avg".
- **Substation provenance:** the HF copy has 7,076 images vs 1,660 in Gomes et al., and the `_NN` suffixes look like crops. Its README is licence-only. The sxiong set's source is also undocumented.
- AMBER: try GitHub (`junyangwang0410/AMBER`).
- MME-Industry: do the Power-sector images show plant interiors? I saw one sample only.
- Hand RF100-VL `infraredimageofpowerequipment` to the thermal agent.
- hf-mirror was checked from the laptop, not from the box.
