# Hugging Face navigation datasets for benchmarking Qwen3.8-27B (2026-10-11)

Goal: find HF images that let us score the untuned Qwen3.8-27B (llama.cpp, W7900D) on the operator's navigation questions automatically. **Verdict:** HF has one strong legged-robot navigation benchmark (NaviTrace). It has no Spot/GraphNav data, no floor-plan route set with gold answers, and no "is this scene localizable?" labels. Degraded visibility and map reading need proxies.

## 1. Scope

- **HF name search** (`hf_fs search`): traversability, obstacle, robot/indoor/egocentric navigation, legged robot, quadruped, anymal, spot robot, boston dynamics, graphnav, stairs, door, wet floor, spill, hazard, industrial inspection, power plant, floor plan, cubicasa, map reading, MapBench, MapEval, TopViewRS, maze, cognitive map, low light, ExDark, LOL, RESIDE/SOTS, dehaze, smoke, motion blur, degraded, Q-Bench, KADID. Named benchmarks: OpenEQA, SpatialRGPT, What'sUp, CV-Bench, BLINK, EmbSpatial, VSI-Bench, RoboVQA, ERQA, SpatialEval.
- **Verification:** cards, file trees and sizes, the datasets-server (`/rows`, `/filter`, `/statistics`), and duckdb column-pruned queries on remote parquet. I viewed 2 NaviTrace, 2 construction and 2 LOLv2 images. Nothing large was downloaded. I also read the NaviTrace paper (arXiv 2510.26909) for its metric.
- **Skipped:** driving sets, Kaggle/Zenodo originals, and video benchmarks beyond a card check.

## 2. Candidates

| Dataset | Category | Images / splits | Task + labels | Real/sim, setting | Licence | Gated | Download | Fit |
|---|---|---|---|---|---|---|---|---|
| [leggedrobotics/navitrace](https://huggingface.co/datasets/leggedrobotics/navitrace) | traversability / route-in-scene | 502 val (GT public) + 500 test (GT hidden; [leaderboard](https://huggingface.co/spaces/leggedrobotics/navitrace_leaderboard)) | Instruction + embodiment → 2D trace. Labels: per-embodiment traces, `embodiments` list, 7 categories (val: Geometric Terrain 367, Stationary Obstacle 138, Visibility 71…), lighting/weather metadata, Mask2Former masks | Real first-person; mostly outdoor Swiss (455/502 daylight, 25 indoor, 22 night/low-light); 86 val frames from GrandTour (ANYmal); ~18 val rows warehouse/basement/industrial | CC BY 4.0 | No | val 171 MB parquet (13 shards) | **High.** Only real benchmark built for legged-robot navigation. Wheeled robot excluded in 226/502 val (a stairs/curb/terrain barrier label). The paper scores Qwen3-VL-235B |
| [manojkarnekar/construction-traversability-dataset](https://huggingface.co/datasets/manojkarnekar/construction-traversability-dataset) | traversability | 415 train + 91 val, 640×480 PNG | 28-class pixel masks (debris, material_pile, concrete_blocks, pit, puddle, wet_mud, rebar, pipes…). **No blocked label**: derive it | Real ground-robot OAK-D camera at low height; construction sites (both viewed frames outdoor) | CC BY-NC 4.0 | No | ~306 MB images + 3 MB masks | **Med.** Robot-height view with debris/puddles. Derived label needs a hand check |
| [nyu-visionx/CV-Bench](https://huggingface.co/datasets/nyu-visionx/CV-Bench) (3D) | spatial VQA | 1,200 (Depth 600, Distance 600) | 2-choice (A/B) depth/distance, bbox column | SUN RGB-D 400 (real indoor), Hypersim 400 (photoreal sim), nuScenes 400 | Apache-2.0 | No | `test_3d.parquet` 220 MB | **Med.** Obstacle-distance proxy; homes and offices |
| [FlagEval/EmbSpatial-Bench](https://huggingface.co/datasets/FlagEval/EmbSpatial-Bench) | spatial VQA | 3,640 MCQ | 6 egocentric relations incl. close/far (~200 per relation per source) | AI2-THOR 1,239 (sim), MP3D 1,201, ScanNet 1,200; homes | CC BY 4.0 ([original](https://huggingface.co/datasets/Phineas476/EmbSpatial-Bench)) | No | 409 MB single parquet | **Med-low.** Easy accuracy metric, household scenes |
| [BLINK-Benchmark/BLINK](https://huggingface.co/datasets/BLINK-Benchmark/BLINK) | spatial VQA | Relative_Depth 124 val, Spatial_Relation 143 val (test answers hidden) | MCQ; Relative_Depth is 2-choice over circled points | General photos | Apache-2.0 | No | ~5–7 MB per config | **Low-med.** Tiny sanity check |
| [FlagEval/ERQA](https://huggingface.co/datasets/FlagEval/ERQA) | spatial VQA | 400 MCQ | Spatial 84, Action 72, Trajectory 66, State 55, Task 38, Multi-view 37, Pointing 34; some multi-image | Real-world robotics context, manipulation-heavy | not on mirror card | No | 78 MB | **Low-med** |
| [MilaWang/SpatialEval](https://huggingface.co/datasets/MilaWang/SpatialEval) (vqa, Maze-Nav) | map / route | 1,500 Maze-Nav MCQ | Count turns / directions along a marked path from S to E | Synthetic 1168² maze renders | not stated | No | vqa shard 0 = 76 MB (818 Maze-Nav rows); all vqa 712 MB | **Med.** Only scoreable route-reading set found |
| [shuoxing/MapBench_VQA](https://huggingface.co/datasets/shuoxing/MapBench_VQA) | map / route | 96 maps, 1,592 start→destination queries | `gpt_answer` route text, **no gold route** | Real maps (zoo 25, museum 12, park 12, campus 11, mall 4…) | not stated | No | 48 MB | **Low.** Can't be scored automatically |
| [Voxel51/FloorPlanCAD](https://huggingface.co/datasets/Voxel51/FloorPlanCAD) | floor plan | 5,308 CAD crops | 30 symbol classes (doors, windows, stair…) as FiftyOne detections | Real CAD, residential and commercial | header CC BY-SA 4.0, body CC BY-NC 4.0 | No | 388 MB | **Low.** Symbol counting only; no routes |
| [okhater/lolv2-real](https://huggingface.co/datasets/okhater/lolv2-real) | degraded visibility | 689 train + 100 test low/normal pairs (600×400) | Paired: Input (dark) vs GT (normal) | Real; sampled scene indoor | Apache-2.0 (repackager) | No | 529 MB; `Test/` ≈ 75 MB | **Med-low.** Clean binary label, but a luminance threshold also solves it |
| [desonglll/reside-SOTS](https://huggingface.co/datasets/desonglll/reside-SOTS) | degraded visibility | 1,000 hazy / 542 clear | Hazy vs clear | SOTS synthetic haze | no card | No | one 435 MB zip | **Low-med.** INDOOR_DUSTY proxy |
| [q-future/Q-Bench-HF](https://huggingface.co/datasets/q-future/Q-Bench-HF) (dev) | degraded visibility | 1,495 MCQ with answers (test hidden) | Low-level perception: yes/no 550, what 452, how 493. Keyword hits: ~250 lighting, ~440 clarity/blur | General (non-robot) images | not stated | No | dev 1.08 GB (3 shards) | **Med-low.** Established and scoreable |
| [Ayanzadeh93/Obstacle_detection_blv](https://huggingface.co/datasets/Ayanzadeh93/Obstacle_detection_blv) | traversability | 320 | Free-text directive + clock-face obstacles + danger score 0–1. Regex: 146 stop/blocked, 57 clear, 11 partly, 106 other | Real; previewed rows indoor | none | No | 98 MB | **Low-med.** Labels look model-generated; no card |
| [dronefreak/ExDark](https://huggingface.co/datasets/dronefreak/ExDark) | degraded visibility | 7,344 (5,874/736/734), 640² | 12-class YOLO boxes; the original's lighting-type labels **were dropped** | Real low-light | BSD-3 + authors' non-commercial request | No | not checked | **Low** |

**Contamination:** CV-Bench, BLINK, EmbSpatial, ERQA, SpatialEval and Q-Bench have shipped answers on HF since 2024–25 and are standard VLM evals. Assume Qwen3.8 has seen them. NaviTrace's val GT has been public since Oct 2025; its hidden-GT test split is the contamination-safe number.

## 3. Rejected

- **Wrong input type:**
  - [What'sUp](https://huggingface.co/datasets/amitakamath2/whatsup_vlms): tabletop photos.
  - OpenEQA ([ellisbrown](https://huggingface.co/datasets/ellisbrown/OpenEQA) is text only): episode video with LLM-judged answers.
  - [VSI-Bench](https://huggingface.co/datasets/nyu-visionx/VSI-Bench) and [RoboVQA](https://huggingface.co/datasets/Tianli/robovqa): video.
  - [GraphNav](https://huggingface.co/datasets/graphnav-anonymous/GraphNav): closed-loop, multi-image Unity mazes.
- [**SpatialRGPT-Bench**](https://huggingface.co/datasets/a8cheng/SpatialRGPT-Bench): needs region masks, 918 MB, built on the same Omni3D data as CV-Bench.
- [**TopViewRS**](https://huggingface.co/datasets/chengzu/topviewrs): partial release; images sit in a 13.5 GB zip.
- [**MapEval-Visual**](https://huggingface.co/datasets/MapEval/MapEval-Visual): Google-Maps POI trivia.
- [**FloorplanQA-Layouts**](https://huggingface.co/datasets/OldDelorean/FloorplanQA-Layouts): JSON only; QA not released.
- **CubiCasa5k mirrors:** [v1nz](https://huggingface.co/datasets/v1nz/cubicasa5k-yolo) has wall/door/window polygons only (CC BY-NC); [Claudio9701](https://huggingface.co/datasets/Claudio9701/cubicasa5k) is 2 text rows; [Barath](https://huggingface.co/datasets/Barath/cubicasa) is empty.
- **Unlabelled robot logs:**
  - [Voxel51 Construction-Site-Traversability](https://huggingface.co/datasets/Voxel51/Construction-Site-Traversability): MCAP, no frame labels.
  - [GrandTour](https://huggingface.co/datasets/leggedrobotics/grand_tour_dataset): raw ANYmal logs.
  - [WildOS](https://huggingface.co/datasets/leggedrobotics/wildos): outdoor, 3.2 GB.
  - [grand-tour-stairs](https://huggingface.co/datasets/xavier-robotoAI/grand-tour-stairs): video, no card.
  - [kozhukovv anymal_nav](https://huggingface.co/datasets/kozhukovv/anymal_nav_dataset_hard_upd_sampler_navmesh): sim, 19.9 GB.
  - [vladkobli/graphnav-vlm](https://huggingface.co/datasets/vladkobli/graphnav-vlm): no card.
- **Outdoor street:** [ROD obstacles](https://huggingface.co/datasets/Abtinzandi/Obstacle-Detection-Dataset-YOLO) and [SANPO-walkable](https://huggingface.co/datasets/blind-assist/SANPO-walkable).
- [**Obstacle_on_Reflective_Ground**](https://huggingface.co/datasets/Fengxue93/Obstacle_on_Reflective_Ground): one 7.2 GB zip, empty card.
- **Off-topic or unusable:**
  - [realm-bench-ObstacleDet](https://huggingface.co/datasets/realm-bench/realm-bench-ObstacleDet): CFD fields.
  - [Simaihub](https://huggingface.co/datasets/Simaihub/Simaihub_Mobile_Robot_Navigation_Training_Dataset_sample): 3 rows.
  - [KADID mirror](https://huggingface.co/datasets/myzhao1999/kadid10k): image column only.
  - Other ExDark mirrors ([ShantyCam](https://huggingface.co/datasets/ShantyCam/exdark), [SatwikKambham](https://huggingface.co/datasets/SatwikKambham/ex-dark), [vsbgdhdt](https://huggingface.co/datasets/vsbgdhdt/ExDark)): no lighting labels either.
- **Spot / Boston Dynamics / GraphNav:** nothing on HF.

## 4. Recommendation

**Pick 1 — NaviTrace val → OBSTACLE_DETECTION / PATH_BLOCKED (502 images).**
- **Task A, a cheap F1 test.** Prompt with the image + `task`:
  ```json
  {"path_status": "clear|blocked", "blocking_feature": "stairs|curb|door|obstacle|terrain|none", "wheeled_robot_ok": true|false, "confidence": 0.0-1.0}
  ```
  Score F1 on `wheeled_robot_ok` against `"Wheeled Robot" in embodiments` (226 negatives / 276 positives). "Legged Robot" is feasible in 501/502, so that label can't be tested.
- **Task B, the native task.** Use embodiment "Legged Robot" and ask for `{"trace": [[x, y], ...]}`.
  - Score with the paper's metric: DTW + endpoint error + semantic penalty, scaled so the centre-line baseline = 0 and GT = 100. The repo's `src/run_evaluation.ipynb` is the template.
  - Report per category plus the ~18 industrial/indoor rows and the 22 night/low-light rows.
  - Check on 5 samples whether Qwen answers in pixels or 0–1000 coordinates. `--image-max-tokens 1024` downsizes these 1,024–2,275 px images, so rescale before scoring.
- **Fetch:**
  ```bash
  HF_ENDPOINT=https://hf-mirror.com hf download leggedrobotics/navitrace --repo-type dataset --include "data/validation-*" --local-dir ~/bench/navitrace
  ```
  - Write to `$HOME`, not `/tmp`.
  - Read the parquet with column pruning. `datasets` expands the masks to ~6 GB, and pyarrow 19 failed on these files ("Repetition level histogram size mismatch"); duckdb worked.
- **Runner-up for obstacles:** the construction set. Mark an image `blocked` when obstacle classes cover ≥ X % of a bottom-centre corridor, and flag puddle/wet_mud/pit as `surface_hazard` (a spill proxy). Score F1 and obstacle-class accuracy, and hand-check 50 labels first. Fetch with `--include "images/*" "masks/*"` (~310 MB).

**Pick 2 — LOLv2-real test + SOTS → LOCALIZATION_FAILURE / LOW_LIGHT / INDOOR_DUSTY (~200–400 images).**
- Prompt:
  ```json
  {"visibility": "good|degraded|unusable", "cause": "dark|haze|blur|none", "can_localize": true|false, "confidence": 0.0-1.0}
  ```
- Labels come from the pairs (Input/hazy = degraded). Score F1 on degraded, plus pairwise accuracy (the degraded image must rank worse than its clean twin).
- **Always run a code baseline next to it** (mean luminance, RMS contrast, Laplacian variance). If code wins, keep this check in code, per the vision-service decision.
- Optional published reference: the Q-Bench dev lighting/clarity subset (~600 MCQ).
- **Fetch:**
  - `--include "Test/*"` from okhater/lolv2-real
  - SOTS.zip from desonglll/reside-SOTS; unzip outside `/tmp`.

**Pick 3 — SpatialEval Maze-Nav → map/route reading (818 MCQ).**
- Prompt: the row's own `text` (which already lists the options), answering with the option letter only. Metric: accuracy; chance is 25%.
- **Fetch:** `--include "vqa/test-00000-of-00002.parquet"` (76 MB), then filter `id LIKE 'mazenav%'`.
- **Gap:** HF has no floor plan with waypoint pins and gold routes. For "which route reaches WP07?", render synthetic plant floor plans from the kit's waypoint legend with exact shortest-path truth, using the generator pattern in tech-spot-vision-cv §6.

## 5. Open questions / next searches

- The kit's 6 LOCALIZATION_FAILUREs are in **HIGH_NOISE** areas, not LOW_LIGHT. If HIGH_NOISE means acoustic or RF noise, no image benchmark tests that cause. Confirm the meaning before claiming the vision model explains it.
- No HF dataset labels "featureless / repetitive scene". Off-HF candidates: SubT-MRS and Hilti SLAM, both unchecked.
- NaviTrace's per-model scores (Fig. 4) weren't readable as numbers. Check the leaderboard before quoting Qwen3-VL.
- Whether the AMD box can reach the datasets-server is untested. List rows on the laptop and fetch files via hf-mirror.com.
- The ERQA, SpatialEval and Q-Bench licences aren't on their cards; check GitHub before publishing.
- LOLv2 GT files 00690 and 00691 have identical byte sizes. Dedupe by GT hash before counting pairs.
