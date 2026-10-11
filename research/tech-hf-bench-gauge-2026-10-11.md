# HF gauge datasets for a held-out Qwen benchmark (2026-10-11)

## 1. Scope
- **Hub search** (`hf_fs search`, `hub_repo_search`, collections, papers). Terms:
  - gauges: analog gauge, pressure gauge, gauge reading, gauge(s), dial, dial gauge, dial reading
  - meters: meter, meter reading, analog meter, pointer, pointer meter, needle, water/gas/electricity/industrial meter
  - instruments: manometer, barometer, thermometer, speedometer, tachometer, volt/am/multimeter, instrument, measurement reading, clock reading
  - benchmarks and sites: MeasureBench, DialBench, InSituMeasure, Pointer-10K, MME-Industry, InspecSafe, substation, SCADA, inspection robot
  - Chinese: 压力表, 指针式仪表, 仪表读数
- **How I verified:**
  - datasets-server `/size`, `/splits` and `/first-rows`, the HF API (storage, gating, licence) and file listings
  - opened sample images for the goodcoffee, UTM and RPM-10K sets
  - for RPM-10K, HTTP range reads of the zip's central directory and `labels/test.json` (about 1 MB plus 7 sample images; nothing bulk-downloaded)
- **Web:** arXiv ([DialBench](https://arxiv.org/abs/2511.21982), [InSituMeasure](https://arxiv.org/abs/2609.04014), [Under pressure](https://arxiv.org/abs/2404.08785), [Leon-Alcazar WACV'24](https://arxiv.org/abs/2308.14583), [VLM gauge study 2608.17723](https://arxiv.org/abs/2608.17723)), GitHub, the Kaggle API and Zenodo.
- **Skipped (already logged):** MeasureBench, the 72-photo Wikimedia set, `monica-sekoyan/...PressureGauge-DiverseSynthetic`, `moondream/synthetic-gauges-v6` and `Mileeena/synthetic-analog-gauges`. Roboflow Universe sets that aren't mirrored on HF are out of scope.

## 2. Candidates

| Dataset | Images / splits | Labels | Real? | Licence | Gated | Size | Overlap MB/Wiki | Fit |
|---|---|---|---|---|---|---|---|---|
| **RPM-10K (DialBench)**, `RPM-10K.zip` in [BommadiUday/Gauge-Meter](https://huggingface.co/datasets/BommadiUday/Gauge-Meter); upstream [Event-AHU/DialBench](https://github.com/Event-AHU/DialBench) | 10,730 = 8,730 train / 2,000 test (verified from `labels/*.json`) | `reading` (string), `range` (= full scale), `meter_type`, `environment_conditions`. No unit field, but the main types print **mA** on the dial. 61 test rows are multi-pointer text. | **Real.** Test = 1,806 cropped video frames of 6 substation surge-arrester leakage-current meters (`biao1–6`, e.g. JCQ-3A 0–10 mA) + 194 web images (`others`). | Upstream "TBD"; the mirror has no card | No | Zip 3.19 GB; test JPGs 604 MB (median 91 KB); each file is range-readable | `biao`: none expected. `others`: web-sourced, not checked | **High.** Real utility-plant needle dials with full scale and condition tags (tilt, blur, low light, glare). Caveats: mA not psi; few physical meters. |
| **Grassme/AAU pressure-gauge frames**: videos `man1–5,7.mov` and `out2/3/5.mov` in [BommadiUday/Gauge-Meter](https://huggingface.co/datasets/BommadiUday/Gauge-Meter); labels in [masked-token/dataset-splits-analog-dial-gauge-reading](https://huggingface.co/datasets/masked-token/dataset-splits-analog-dial-gauge-reading) (`k_p_*`); upstream [Kaggle juliusgrassme](https://www.kaggle.com/datasets/juliusgrassme/pressure-gauge-reader-data) | 93 labelled frames (57 train / 36 test) from about 10 videos | `actual_val`, `min_val`, `max_val`; no unit | **Real** pressure dials | Upstream CC BY-SA 4.0; the mirror has none | No | Videos are 68–923 MB each; only labelled frames are needed | None | **Medium.** The only real *pressure* set. It has a published Qwen2.5-VL-7B baseline (25 % range-normalised error with range given, [2608.17723](https://arxiv.org/abs/2608.17723)). But it's tiny, frames must be re-extracted, and `man6.mov` isn't mirrored. |
| [claytonsds/UTM_Testing_Dataset](https://huggingface.co/datasets/claytonsds/UTM_Testing_Dataset) | 904 test | Liquid-column height in **cm** (folder name, ±0.1 cm); 150 cm tape | **Real** lab U-tube manometers | CC BY-SA 4.0 | No | 28.6 MB | None | **Medium (secondary).** Real pressure instrument and a trivial fetch, but it's a linear tape and meniscus read. Score in cm: % of the 150 cm full scale is too lenient. |
| [goodcoffee/Meter_Reading](https://huggingface.co/datasets/goodcoffee/Meter_Reading) | 1,500 PNGs (1,200 / 302 rows; 1,000 base + 500 `_ds6`) | Value, range min/max, needle/face boxes; COCO JSON with keypoints. No unit field ("bar" printed in the sample). | Synthetic (Endava Blender renders on photo backgrounds) | Card says Apache-2.0, but [upstream Kaggle](https://www.kaggle.com/datasets/endava/synthetic-data-for-precision-gauge-reading) is **CC BY-NC-SA 4.0** | No | 2.4 GB | None | **Medium-low.** Synthetic secondary. All 126 `k_s_test` files are here, with a Qwen2.5-VL baseline (21 % error). Public since 2024, so contamination risk. |
| [monica-sekoyan/MeasureBenchAmmeter-DiverseSynthetic](https://huggingface.co/datasets/monica-sekoyan/MeasureBenchAmmeter-DiverseSynthetic) | 1,000 / 50 / 100 | `primary_value` + `primary_unit`, needle angle, exact readings | Synthetic | Not stated | No | 90 MB | Same generator family as MeasureBench | **Low-medium.** Exact truth with unit, but not independent of MeasureBench. |
| *Digital/counter, secondary only:* [henrik-dra/energy-meter](https://huggingface.co/datasets/henrik-dra/energy-meter) | 138 / 30 | Register string (e.g. `0003972.2`) | Real | Not stated | No | 361 MB | None | Low for dials; OCR check only |
| *Same:* [Praekelt/ElectricityMeterReadings1o4](https://huggingface.co/datasets/Praekelt/ElectricityMeterReadings1o4) | 165 | Reading string | Real | Not stated | No | 44 MB | None | Low for dials; OCR check only |

## 3. Rejected
- [Synanthropic/reading-analog-gauge](https://huggingface.co/datasets/Synanthropic/reading-analog-gauge) (and its copy [hibahammouda/…](https://huggingface.co/datasets/hibahammouda/reading-analog-gauge)): synthetic keypoints encoded in filenames; no value or scale; 2.6 GB of zips (MIT).
- [KhoaUIT/Synthetic-Data-for-Precision-Gauge-Reading](https://huggingface.co/datasets/KhoaUIT/Synthetic-Data-for-Precision-Gauge-Reading): 500 Endava renders with value only (no range or unit) and no licence (upstream is NC). The goodcoffee set covers it.
- [Paco4365483/pressure_gauge](https://huggingface.co/datasets/Paco4365483/pressure_gauge) and [preassure_gauge](https://huggingface.co/datasets/Paco4365483/preassure_gauge): 9 images each. [Chaymaa/meter_reading](https://huggingface.co/datasets/Chaymaa/meter_reading): 6 rows. [dumb-ai/gauge_test_29_rows_split](https://huggingface.co/datasets/dumb-ai/gauge_test_29_rows_split): 29 rows, boxes only. [kutay1907/scadaphotodataset](https://huggingface.co/datasets/kutay1907/scadaphotodataset): 42 rows.
- [Francesco/gauge-u2lwv](https://huggingface.co/datasets/Francesco/gauge-u2lwv) and [LibreYOLO/gauge-u2lwv](https://huggingface.co/datasets/LibreYOLO/gauge-u2lwv): RF100, 235 real photos, boxes only (gauge, numbers).
- [ybli/yolo-analog-meter-recognition](https://huggingface.co/datasets/ybli/yolo-analog-meter-recognition): empty repo; data sits on an external netdisk; boxes only.
- [hcltech-robotics/gauge_data_industrial_env](https://huggingface.co/datasets/hcltech-robotics/gauge_data_industrial_env): 9.6 GB + 41 GB zips with no card. [Cian911/meter-captures](https://huggingface.co/datasets/Cian911/meter-captures): one 3.7 GB zip with no card.
- [moondream/1M-synthetic-gauges](https://huggingface.co/datasets/moondream/1M-synthetic-gauges), v2–v5 and [dokidoki1744/synthetic-gauges-v3](https://huggingface.co/datasets/dokidoki1744/synthetic-gauges-v3) (33.5 GB): same synthetic family as the logged v6.
- [kagabo/raw_meter_dataset](https://huggingface.co/datasets/kagabo/raw_meter_dataset) and [UniDataPro/water-meters](https://huggingface.co/datasets/UniDataPro/water-meters) (60-row sample, CC BY-NC-ND): water-meter digit OCR. The [utilitimetersai](https://huggingface.co/utilitimetersai) sets are gated.
- [AvaXiao/clockreading-time](https://huggingface.co/datasets/AvaXiao/clockreading-time) and [moondream/analog-clock-benchmark](https://huggingface.co/datasets/moondream/analog-clock-benchmark): clocks. [Ajax102/MME-Industry](https://huggingface.co/datasets/Ajax102/MME-Industry): multiple choice. [Tetrabot2026/InspecSafe-V1](https://huggingface.co/datasets/Tetrabot2026/InspecSafe-V1): no reading labels. [BommadiUday/shuyansy-Gauge-Meter](https://huggingface.co/datasets/BommadiUday/shuyansy-Gauge-Meter) and [phathuynh70497/tachometer](https://huggingface.co/datasets/phathuynh70497/tachometer): empty.
- [Rekin226/aquascope-gauges](https://huggingface.co/datasets/Rekin226/aquascope-gauges), the SOTAagi2030 "gauge" sets and [CashlessConsumer/chennai-rain-gauges](https://huggingface.co/datasets/CashlessConsumer/chennai-rain-gauges): hydrology tables, not images.
- [Zenodo industrial gauge videos](https://zenodo.org/records/20624895) ([code](https://github.com/aMa2210/VLM_gauge)): not on HF; dial indicator and calipers, not pressure; 3 GB; CC BY 4.0.

## 4. Recommendation
**Picks, in order:**
1. **RPM-10K test, `biao1–6` only.** This is the clean real held-out set: real video frames of utility substation meters, none sourced from MeasureBench or Wikimedia.
   - Slice: 600 images, 100 per type, stratified by `environment_conditions`, fixed seed. Drop the 61 multi-pointer rows.
   - Unit = mA. Confirm by eye on 5 images per type.
   - Keep `others` (194, web-sourced) as a separate slice, and only after pHash dedup against MeasureBench-real and the 72 Wikimedia photos. Add its units by hand.
   - Pre-register the slice like the Wikimedia set.
2. **Grassme pressure frames.** Use all 93, since we don't train on them. It's the only real pressure-dial set. Report it separately because n is small.
3. **UTM_Testing_Dataset.** Take a 300-image seeded sample as a cheap real secondary.

**Prompt** (keep it identical to the MeasureBench dev run for comparability):
> "Read the main needle of this instrument. Ignore digital counters. Reply with JSON only: `{"value": number, "unit": "unit as printed", "scale_min": number, "scale_max": number, "confidence": 0-1}`."

Run two conditions: **blind**, and **range-given** (append "The dial spans {min}–{max}."), which matches 2608.17723. Keep the current settings (`--image-max-tokens 1024`, temperature 0).

**Scoring:**
- Error = |value − truth| / (max − min).
  - RPM: use `range`. Its formula is |y−y*|/Range, so a min of 0 is assumed; check this on non-zero-origin dials.
  - Grassme: use `max_val − min_val`.
  - UTM: report error in cm, plus the share within ±0.5 cm.
- **Primary metric:** share within ±2 % FS, with a 95 % bootstrap CI.
- Also report:
  - ±1 % FS, which is DialBench Acc_ε. Qwen2-VL-7B scored 45.8 % and Qwen2.5-VL-7B 25.6 %.
  - MAE and median of % FS
  - unit accuracy after normalising strings
  - scale-max accuracy, which separates scale misreads from needle misreads
  - parse failures, counted as wrong
- **Calibration:**
  - ECE over 10 bins
  - AUROC of confidence for predicting "within 2 %"
  - accuracy at 80 % coverage
- Break all results down by environment tag.

**Fetching through hf-mirror** (the box has no HF access and a 4 GB `/tmp`):
- **RPM-10K:**
  - Range-read `https://hf-mirror.com/datasets/BommadiUday/Gauge-Meter/resolve/main/RPM-10K.zip` (3,185,292,936 bytes; standard, non-zip64 EOCD).
  - Pull the 1.2 MB central directory and `RPM-10K/labels/test.json`, then only the sampled JPGs (600 images ≈ 100–200 MB).
  - Write the files outside `/tmp`.
  - If the mirror ignores Range, run the same script on the laptop and rsync.
- **UTM:** `HF_ENDPOINT=https://hf-mirror.com hf download claytonsds/UTM_Testing_Dataset --repo-type dataset --local-dir ~/data/utm` (28.6 MB).
- **Grassme:**
  - On the laptop, download only the `.mov` files you need. Get `man6` from Kaggle.
  - Extract frames with `ffmpeg -ss MM:SS -frames:v 1`, then rsync the ~100 JPGs.
  - Spot-check 5 frames against `actual_val` first. The labels name `*cropclipscale*` clips, so their timestamps may not match the raw videos.
- **Check the mirror from the box first.** From a US laptop, hf-mirror.com returns `308 → huggingface.co`.

**Contamination:** RPM-10K has been public since April 2026 (possible but unlikely). The Endava synthetic set has been public since 2024 (higher risk). The Grassme videos are old, but their frame labels date from Aug 2026. UTM was published in Jan 2026.

## 5. Open questions / next searches
- **InSituMeasure** ([2609.04014](https://arxiv.org/abs/2609.04014)) would be ideal: 2,922 real industrial scenes including pressure and SF6 dials, value+unit and confidence diagnosis. I found no public release. The arXiv HTML returned 404 and the PDF was too large to fetch.
- **Leon-Alcazar WACV'24** (4,813 real landmark images): the [repo](https://github.com/fuankarion/automatic-gauge-reading) has only a README.
- **RPM-10K:**
  - The dataset licence is "TBD", so use it internally only and don't redistribute images.
  - I haven't verified how `range` is defined for dials that don't start at 0.
  - The `others` overlap with MeasureBench is unchecked.
  - The BommadiUday mirror is unofficial. Its counts match the paper (10,730 / 2,000), but checksums are unverified.
- **Grassme:** the unit (bar?) and the frame/clip alignment are unverified.
- **Unknown contents:** `shuyansy-Gauge-Meter.zip` (85 MB) inside BommadiUday/Gauge-Meter, the hcltech and Cian911 zips, and the gated utilitimetersai sets.
- **Next:** ask the InSituMeasure authors about a release; dedup RPM-10K `others` against MeasureBench.
