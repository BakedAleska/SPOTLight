# Hugging Face thermal/IR datasets for benchmarking Qwen3.8-27B (2026-10-11)

Goal: find HF thermal images that let us score the untuned Qwen3.8-27B VLM (llama.cpp, W7900D) automatically. This follows up the survey in `tech-spot-vision-cv-2026-10-10.md` §6, which said "no labelled thermal on HF". **Verdict: half true.** HF does hold labelled thermal images of real industrial equipment (motor fault/normal) and labelled solar modules. It holds **no** industrial thermal set with numeric temperature labels at benchmark scale.

## 1. Scope

- **HF Hub name search** (`hf_fs search`, `hub_repo_search`): thermal, infrared, IR, thermography, thermogram, FLIR, radiometric, LWIR, thermal camera/image/inspection/anomaly/fault/benchmark/VQA/QA/caption, hotspot, overheating, substation, insulator, transformer, motor, bearing, induction/electric motor, switchgear, electrical panel, power equipment, transmission line, PV/solar hotspot, colorbar, temperature map, RGBT, 红外.
- **HF tag filters** (API `?filter=`): thermal (46 repos), infrared (50), thermal-imaging (11), radiometric (5), thermography, flir, hotspot, rgb-thermal, rgbt, thermal-infrared, predictive-maintenance, industrial-inspection, anomaly-detection. Every listed repo was screened by name/card.
- **HF papers** with linked datasets: IF-Bench (2512.09663), WildFireVQA (2604.20190), IRGPT (2507.14449, no HF data).
- **Web:** `site:huggingface.co` searches (this surfaced `Ryanflash/herislab-ca-training-data`, which name search misses). Also ThermEval and RGB-Th-Bench release pages.
- **Verification:** cards, the datasets-server (splits/rows/schema/preview), file trees and byte sizes. I viewed 6 sample images and downloaded two 0.3–0.7 MB TIFFs to read their headers. Nothing large was downloaded.
- **Skipped:** `nealgoogs/thermal-fault-detection-dataset` and the Mendeley motor/transformer sets (already logged). Also skipped person/vehicle/maritime/drone IR detection sets beyond a name check, and Kaggle/Zenodo except to confirm where off-HF benchmarks live.

## 2. Candidates

| Dataset | Images / splits | Labels (numeric °C?) | Real/synth | Licence | Gated | Download | Fit for Qwen |
|---|---|---|---|---|---|---|---|
| [Ryanflash/herislab-ca-training-data](https://huggingface.co/datasets/Ryanflash/herislab-ca-training-data) | `electric_motor`: 196 normal (168 train + 28 test) + 173 fault PNG, 320×240. `induction_motor`: 25 normal + 344 fault BMP, 320×240 (HF mirror of Mendeley m4sbt8hbvk). Plus 7,836 + 1,075 PV TIFFs (normal only) | Folder labels normal/fault. The fault type isn't recorded in the flat folders. **No °C, no colorbar** | Real | MIT (repackager). Induction upstream CC BY 4.0. `electric_motor` upstream not identified | No | electric_motor 8.5 MB; induction 85 MB; whole repo ≈3.4 GB (so use `--include`) | **High** for fault/normal: the only labelled thermal of real plant equipment on HF, and it looks like Orbit panes (ironbow, no bar). Caveats: "normal" = a cold, idle motor (brightness shortcut); faults are near-duplicate frames from one sequence |
| [STRDrones/FLIR](https://huggingface.co/datasets/STRDrones/FLIR) (+ [STRDrones/DJI](https://huggingface.co/datasets/STRDrones/DJI)) | 9 radiometric JPEGs (FLIR E40, T640, B60, AX8, Edge Pro, generic; DJI XT/XT2/XTR) + ~30 DJI R-JPEGs (H20T/H30T/M30T/M3T/M4T…), each with an exiftool JSON | **Yes:** raw 16-bit + Planck constants embedded, so per-pixel °C via exiftool/flirimageextractor (FLIR) or the DJI Thermal SDK (DJI). FLIR screens burn in a colorbar plus max/min/spot readouts | Real (handheld/fixed industrial + drone) | MIT | No | FLIR ≈6.6 MB; DJI ≈40 MB | **Med:** best numeric ground truth on HF and the closest look to Orbit's "Region Max/Min", but only 9 FLIR images. Smoke test, not a benchmark |
| [tester202405/InfraredSolarModules](https://huggingface.co/datasets/tester202405/InfraredSolarModules) | 16,000 / 4,000 parquet; 24×40 px | 12 classes: Hot-Spot (251), Hot-Spot-Multi (247), Cell, Cracking, Diode, Offline-Module, Soiling, Vegetation… No-Anomaly (10,000). No °C | Real (Raptor Maps aerial IR) | MIT ([upstream](https://github.com/RaptorMaps/InfraredSolarModules)) | No | 6.1 MB | **Med-low:** trivial to fetch, real fault labels, but tiny images (upscale them) and solar, not plant equipment. Widely used in CV papers |
| [casiatao/IF-Bench](https://huggingface.co/datasets/casiatao/IF-Bench) | 499 IR images, 680 MCQs over 10 dimensions incl. "thermal feature understanding/reasoning"; QA JSON is on [GitHub](https://github.com/casiatao/IF-Bench) | MCQ answers. No °C | Real (23 public IR sets: surveillance, driving, aerial) | Apache-2.0 | No | < 1K images; total size not checked | **Low-med:** general IR scenes, but the paper reports Qwen2.5-VL/Qwen3-VL scores, so we get a published comparison point |
| [seyeddd/Solar-PV-Clean-Hotspot-Images](https://huggingface.co/datasets/seyeddd/Solar-PV-Clean-Hotspot-Images) | 4.2K, 500–1080 px | clean / single_hotspot | Real (solar) | **None stated** | No | 1.1 GB parquet | **Low-med:** binary hotspot at a usable resolution. No licence; solar |
| [menasi11/colorbar-range-dataset](https://huggingface.co/datasets/menasi11/colorbar-range-dataset) | 80 colorbar-only images | Numeric range per colour band | Synthetic | MIT | No | 0.9 MB | **Low:** probes only the "read the colorbar" sub-skill |

Two follow-up checks on the Ryanflash PV TIFFs: `pv_thermal_inspection` is 16-bit 640×512 senseFly thermoMAP. A sample's DN range is 11,004–14,259; DN/100−100 would give 10–42.6 °C, but that scale is **unverified**. `pv_om_inspection` is 16-bit LZW of unknown scale.

## 3. Rejected (don't re-check)

- [mobiiin/WildFire_VQA](https://huggingface.co/datasets/mobiiin/WildFire_VQA): HF holds only the QA JSON (with min/max/mean °C per frame from FLAME 3 Celsius TIFFs). The images aren't on HF (Kaggle/FLAME 3), and the scenes are aerial wildfire.
- [Dhi-Technologies/thermal-perception-benchmark](https://huggingface.co/datasets/Dhi-Technologies/thermal-perception-benchmark): gated (manual), card unreadable.
- [toqi/GasGround](https://huggingface.co/datasets/toqi/GasGround): optical-gas-imaging VQA (methane/CO₂), gated.
- [mani-ramanagopal/subtle-heat-flows](https://huggingface.co/datasets/mani-ramanagopal/subtle-heat-flows): gated, raw microbolometer video.
- [nnuochen/RGBT-Scenes-raw2temp-inferno-p0-98-filtered](https://huggingface.co/datasets/nnuochen/RGBT-Scenes-raw2temp-inferno-p0-98-filtered): the manifest lists 10 scenes (incl. RotaryKiln), but the upload has only Ebike renders plus RotaryKiln COLMAP files and no `raw_images`. Licence unknown.
- [dejanb/beograd](https://huggingface.co/datasets/dejanb/beograd): 825 16-bit H20T TIFFs. The card says the values are **uncalibrated counts, not °C**. Landmine field. Same for [dejanb/colombia](https://huggingface.co/datasets/dejanb/colombia).
- [Francel12/nave-hidalgo-dataset](https://huggingface.co/datasets/Francel12/nave-hidalgo-dataset): 3,215 white-hot M4T R-JPEGs of a factory roof. Unlabelled, needs the DJI SDK, 5.8 GB, licence "other".
- [Manishsahu53/Solar-Panel-Thermal-Drone-UAV-Images](https://huggingface.co/datasets/Manishsahu53/Solar-Panel-Thermal-Drone-UAV-Images) and its copies (Clausson, seyeddd, bensidhomh, Hlimaaa, yifeiyang51): unlabelled DJI solar, 19 GB of zips.
- [darkone01/synthetic-cooling-coil-thermal-sequences-v1](https://huggingface.co/datasets/darkone01/synthetic-cooling-coil-thermal-sequences-v1): 64×64 rasters whose RGB channels are 3 time-steps of linearly encoded °C, built for a forecasting task. Not a thermal picture.
- [ZoneTwelve/Thermal-Heatmap-Source-Localization](https://huggingface.co/datasets/ZoneTwelve/Thermal-Heatmap-Source-Localization): text matrices, not images.
- [ThreeGold11602/infrared_benchmark](https://huggingface.co/datasets/ThreeGold11602/infrared_benchmark) (+ `infrared-instruct-12k`, `infrared-pretrain-500k`): LLVIP/FLIR person-counting QA, 2 GB zip.
- [dtang318/thermal_wire_perception](https://huggingface.co/datasets/dtang318/thermal_wire_perception): wire detection/depth, 9.6 GB.
- [AIagent0/power_thermal](https://huggingface.co/datasets/AIagent0/power_thermal): phone-battery chat logs.
- Too small, unlabelled or broken: Treza12/Thermal (33), Drone20/thermal_images (14), reyrg/thermal-camera_v3 (546 image/mask pairs, licence unknown), emtake-ai/thermal_classification (empty), seyeddd/Solar_PV_single_multi_hotspots (empty), cyrusaddy/thermal_datset (4 GB zip), Skieriya/thermal7 (CSV), yiranranranra/thermal (LSOTB-TIR tracking, 22 GB), whynot0128/infrared_dataset (viewer broken).
- People/vehicles/animals/maritime/anti-UAV/wildfire/driving IR, no equipment: Voxel51/Thermal-Person-Detector, Kiuyha/hit-uav, Francesco/LibreYOLO thermal-dogs/cheetah (RF100), FLIR_aligned/FLIR-IISR/LLVIP/KAIST/M3FD/NIICU, hanchong maritime, anti-UAV sets, Dharini24/AVID, quailrobot, HotSight, vanrakshak, IRVAL, casiatao/InfMix (pretraining), GerardDMG/USEFUL, Voxel51/SEW-Multimodal-AMR, TartanRGBT.
- Agriculture thermal: Project-AgML pomegranate (982, 3-class, CC BY 4.0, 741 MB), lettuce, okra, rice.
- Named "thermal" but simulation/tabular: heat3d, SISSAmathLab, hotspot-chiplet, ClarusC64, Taylor658, QSBench, gemant BMS, xpertsystems/oil039 (CC BY-NC), Voxel51/BoilingBench.
- [sriom1/electrical-panels-dataset](https://huggingface.co/datasets/sriom1/electrical-panels-dataset): scraped RGB with YOLOE auto-labels.

## 4. Recommendation

1. **Motor fault call, the main benchmark:** `Ryanflash/herislab-ca-training-data` `electric_motor`.
   - **Slice:** all 196 normals plus 173 faults (≈370 images, 8.5 MB), sampled at a stride so near-duplicate frames don't dominate.
   - **Second split:** the `induction_motor` mirror (25 normal / 344 fault, Mendeley). Report fault recall only, because of the imbalance.
   - **Prompt:** "Thermal image of an electric motor; no colour scale. Is it overheating? Reply only JSON `{"status":"normal|overheating","hot_component":"<text>","confidence":0-1}`."
   - **Metric:** macro-F1 / balanced accuracy, plus JSON-parse rate.
   - **Caveat:** label it a floor test, because normals are visibly cold.
2. **Numeric smoke test:** `STRDrones/FLIR` (9 images).
   - **Ground truth:** decode raw → °C with exiftool + Planck (`flirimageextractor`).
   - **Prompt:** "Report `{"max_c":…, "min_c":…, "hotspot_xy":[x,y]}`."
   - **Variants:** run each image as-is (OCR of burnt-in readouts, which is Orbit-like) and with the text boxes masked but the colorbar kept (colour reading).
   - **Metric:** max-°C MAE per image, plus hotspot hit within 5 % of image width.
   - **Use:** demo-grade evidence only; n is tiny. Keep the synthetic ironbow renders (§6 of the vision file) as the scored max-°C MAE benchmark.
3. **Optional breadth:** `InfraredSolarModules` test split.
   - **Classes:** collapse to 3 (hot-spot = Hot-Spot + Hot-Spot-Multi; other-anomaly; no-anomaly).
   - **Slice:** 600 balanced images, upscaled 10× with nearest-neighbour.
   - **Metric:** macro-F1.

**Fetch on the box** (hf-mirror returned 308 → huggingface.co from a US laptop, so run this on the box, whose egress is a China-region IP; never write to `/tmp`):
```bash
export HTTPS_PROXY=http://notebook-egress-proxy.amd-oneclick-lablab.svc.cluster.local:3128
export HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 HF_HOME=/root/hf
hf download Ryanflash/herislab-ca-training-data --repo-type dataset \
  --include "train/normal/electric_motor/*" "test/*/electric_motor/*" "test/*/induction_motor/*" "train/normal/induction_motor/*" \
  --local-dir /root/data/thermal/herislab
hf download STRDrones/FLIR --repo-type dataset --local-dir /root/data/thermal/strdrones-flir
hf download tester202405/InfraredSolarModules --repo-type dataset --include "data/test-*" --local-dir /root/data/thermal/ism
```

## 5. Open questions / next searches

- **Upstream of `electric_motor` (no_fault/fault PNGs):** not found on Mendeley or Kaggle. Find it to confirm its licence and capture conditions.
- **Not on HF, but closer to our task:**
  - [RGB-Th-Bench](https://arxiv.org/abs/2503.19654): 29 FLIR ONE RGB-T pairs of industrial and residential inspection scenes, 1,624 yes/no questions incl. colour-scale reading, warmest area and anomaly. On [Google Drive](https://drive.google.com/file/d/1aNaKYpI0lirhDUm34eq6PChA0pmjoXUD/view?usp=sharing); dataset licence unstated. Small enough to rsync.
  - [ThermEval](https://arxiv.org/abs/2602.14989): about 55K thermal VQA pairs with per-pixel °C, but human-centric; reported to be on Kaggle (not verified).
- **senseFly TIFF scale:** confirm the DN→°C scale. If it's DN/100−100, these 1,075 real PV frames become a numeric max-°C set; render them with our own colorbar.
- **sxiong/Power-equipment-image-dataset:** has a "high-voltage conductor joint overheating" box class. The [repo](https://huggingface.co/datasets/sxiong/Power-equipment-image-dataset) doesn't say whether those images are IR; the 530 MB zip is unopened.
- **Gated sets worth an access request if time allows:** Dhi-Technologies/thermal-perception-benchmark, toqi/GasGround.
