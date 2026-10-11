# HF datasets for benchmarking Qwen on acoustic inspection images (2026-10-11)

## 1. Scope
- **Searched:** Hub name search (~80 terms: acoustic, sound camera, beamform, ultrasonic, leak, compressed air, gas, MIMII, ToyADMOS, DCASE, anomalous sound, machine fault, bearing, CWRU, Paderborn, MAFAULDA, valve, pump, compressor, fan, motor, mel/STFT spectrogram, audio-QA…); full-text card search (~45 phrases incl. "acoustic camera/imager", "beamforming", "anomalous sound detection", "dB SPL", SPOT/Orbit, Fluke ii900, FLIR Si124); HF papers. Verified with the datasets-server API (`/size`, `/info`, `/statistics`, `/rows`) and upstream licence pages. Downloaded 8 single clips (≤1 MB) to check sample rate and band energy; nothing else.
- **Skipped:** the logged sets (only re-judged); non-HF sources beyond licence checks; speech, music and bio spectrograms; vibration-only bearing sets.
- **Headline: no labelled acoustic-camera image dataset exists on HF.** There were zero card hits for acoustic camera/imager, sound camera, acoustic map, or vendor names; the closest is an unlabelled UAV mic array (§3).

## 2. Candidates

| Dataset | Items / splits | Modality | Labels | Real/synth | Licence | Gated | Download | Fit |
|---|---|---|---|---|---|---|---|---|
| [jiangab/RMIS `iica`](https://huggingface.co/datasets/jiangab/RMIS) (= IDMT-ISA-COMPRESSED-AIR) | 16,792 × 10 s, split `fd`; rows run tube → vent → ventlow leak (~5.6k each) | audio 48 kHz mono | `status` leak/noleak (2,719/2,880 in the tube part); leak type; noise (lab, hydr, hydr_low, work, work_low); mic; session; knob | real lab rig | CC BY-NC-ND 4.0 "for evaluation purpose" ([IDMT](https://www.idmt.fraunhofer.de/en/publications/isa-compressed-air.html), [Zenodo](https://zenodo.org/record/7551606)); repo MIT | no | 16.1 GB (33 × 489 MB shards) or 0.96 MB per clip via `/rows` | **High**: only real compressed-air leak set; 48 kHz covers 0–24 kHz. In lab noise a leak clip had 12.6% of its energy in 15–24 kHz vs 0.9% for no-leak. |
| [jiangab/RMIS `iiee`](https://huggingface.co/datasets/jiangab/RMIS) (= IDMT-ISA-ELECTRIC-ENGINE) | 2,378 × 3 s | audio 44.1 kHz | good 774 / broken 789 / heavyload 815; background (pure, atmo, talking, whitenoise) in the filename | real | CC BY-NC-ND 4.0 ([Zenodo](https://zenodo.org/record/7551261)) | no | 630 MB (2 shards) | **High-med**: small, 3 classes, ≥40 kHz. Caveat: each class comes from a different engine unit. |
| [jiangab/RMIS `mafaulda_sound`](https://huggingface.co/datasets/jiangab/RMIS) | 1,951 × 5 s | audio 50 kHz (Shure SM81 mic, rated 20 Hz–20 kHz) | 10 classes: normal 49, imbalance 333, horizontal/vertical misalignment, under/overhang bearing ball/cage/outer race | real | none stated ([source](https://www02.smt.ufrj.br/~offshore/mfs/page_01.html)) | no | 0.98 GB (2 shards) | **Med**: real bearing faults, but both clips checked had ~90% of their energy in a ~22 kHz tone (likely interference), and only 49 are normal. |
| [IoTDataAtelier/CANDAS](https://huggingface.co/datasets/IoTDataAtelier/CANDAS) | 21,211 WAVs (28 h); the viewer indexes 2,900 | audio 44.1 kHz stereo, 10 s | normal/abnormal; fan A2/A3; 9/12 V; imbalance configs 2–5 | real | MIT | no | ~1.76 MB per WAV, fetched per file | **Med**: permissive and real, but PC cooling fans with added weights. |
| [EitaNis/iitk-compressor-bearing](https://huggingface.co/datasets/EitaNis/iitk-compressor-bearing) | `raw` 320 (40 × 8 classes); `benchmark` 1,800 feature rows | audio arrays; sample rate unresolved (header 16 kHz, paper 50 kHz; no energy above ~4 kHz) | healthy, bearing, **leaking inlet valve, leaking outlet valve**, non-return valve, piston ring, flywheel, rider belt | real compressor | BSD-2-style (cite Verma 2016) | no | 32 MB | **Med** for fault type (has valve leaks); **low** for peak frequency (axis unknown). |
| [HTill/dcase2025_task2_dev](https://huggingface.co/datasets/HTill/dcase2025_task2_dev) (logged) | test 1,000 (500 normal / 500 anomaly; 200 each of bearing, fan, gearbox, slider, valve) | audio 16 kHz | normal/anomaly, domain | real | MIT (repo) | no | test parquet 319 MB | **Med-low**: one-file fetch, but 0–8 kHz only; subtle anomalies. |
| [renumics/dcase23-task2-enriched](https://huggingface.co/datasets/renumics/dcase23-task2-enriched) | `dev_test.tar.gz` + metadata CSV | audio 16 kHz | normal/anomaly, machine type (incl. valve, fan, bearing), baseline anomaly scores | real | CC BY 4.0 | no | 365 MB tarball | **Low-med**: like the row above, plus baseline scores. |
| [LakoreAI/stgram-mfn-dcase2020-dev](https://huggingface.co/datasets/LakoreAI/stgram-mfn-dcase2020-dev) | 30,987 WAVs (train 20,119 / test 10,868) | audio 16 kHz | normal/anomaly in test filenames; **pump, valve**, fan, slider, ToyCar, ToyConveyor | real (MIMII + ToyADMOS) | CC BY-NC-SA 4.0 | no | per-file WAV | **Low-med**: MIMII pump/valve test anomalies as plain WAVs; 16 kHz. |
| [mandipgoswami/AnomalyMachine-50K](https://huggingface.co/datasets/mandipgoswami/AnomalyMachine-50K) | 50,000 (35,001 / 7,500 / 7,499) | audio 22.05 kHz | normal/anomalous; subtype (bearing_fault, imbalance, cavitation, overheating, obstruction); machine (pump, valve, compressor…); SNR | **synthetic** DSP | CC BY 4.0 | no | 21.9 GB | **Low-med**: fault-type labels, but synthetic, ≤11 kHz. |
| [jiangab/RMIS `umged_sound`](https://huggingface.co/datasets/jiangab/RMIS) | 21,120 × 10 s | audio 51.2 kHz | normal/anomaly (480/4,800 in the indexed part); condition codes E00–E20 | real | not stated | no | 21.6 GB (44 shards) | **Low**: large, undocumented codes, 10:1 imbalance. |

## 3. Rejected
- **AI4Manufacturing/{CWRU, MAFAULDA-bearing, MFPT, XJTU, IMS, SEUG/SEUB}-perception** ([e.g.](https://huggingface.co/datasets/AI4Manufacturing/CWRU-perception)): labelled spectrogram/scalogram images, but manual-gated with placeholder cards; access denied.
- **[plutoAp/Zhaohua_128_20260210DATA](https://huggingface.co/datasets/plutoAp/Zhaohua_128_20260210DATA)**: 128-channel acoustic-imaging recordings of UAVs plus fusion videos (6.7 GB, CC BY 4.0); no source-position or dB labels, and not plant equipment.
- **[BinhPhuoc/Fault_Bearing_Datasets](https://huggingface.co/datasets/BinhPhuoc/Fault_Bearing_Datasets)**: 2,597 CWRU spectrogram PNGs in 20 classes; accelerometer data, no licence, and CWRU images are widely posted (contamination risk). Same for [GaumlessGraham/CWRUBaseSpec](https://huggingface.co/datasets/GaumlessGraham/CWRUBaseSpec) (4 images).
- **[stdt1/recorded_pump_datasets](https://huggingface.co/datasets/stdt1/recorded_pump_datasets)** (+ `_v2_44100hz`) and the logged [stdt1/mimii_pump_datasets](https://huggingface.co/datasets/stdt1/mimii_pump_datasets): mel/MFCC float arrays, no licence, can't be rendered as a linear 0–20 kHz image.
- **Too small, no card or no licence:** [hamezksm/machine_noise_dataset](https://huggingface.co/datasets/hamezksm/machine_noise_dataset) (99 clips, 16 kHz, partly synthetic), [Alieldin/Machine-Fault-Recognition-Data](https://huggingface.co/datasets/Alieldin/Machine-Fault-Recognition-Data) (zips, no card), [aemreozturk/water_leakage_voice_data](https://huggingface.co/datasets/aemreozturk/water_leakage_voice_data) (92 restroom faucet clips).
- **Not usable as images:** [wutZYH/cwru-bearing-fault-qa](https://huggingface.co/datasets/wutZYH/cwru-bearing-fault-qa) (text only), [xpertsystems/oil025-sample](https://huggingface.co/datasets/xpertsystems/oil025-sample) (synthetic tabular), [nubahador/ChirpLoc100K…](https://huggingface.co/datasets/nubahador/ChirpLoc100K___A_Synthetic_Spectrogram_Dataset_for_Chirp_Localization) (one 18.8 GB zip), [SDC-Unistra/SyntheticSpectrogram](https://huggingface.co/datasets/SDC-Unistra/SyntheticSpectrogram) (`.dat` matrices), [UniParser/SpecVQA](https://huggingface.co/datasets/UniParser/SpecVQA) (scientific spectra).
- **Optical gas imaging, not acoustic:** [TalonMeyer/gas-leak-source-localization-dataset-yolo](https://huggingface.co/datasets/TalonMeyer/gas-leak-source-localization-dataset-yolo), [samhormozian/GasLeakPlumes](https://huggingface.co/datasets/samhormozian/GasLeakPlumes), [weathon/sim_gas](https://huggingface.co/datasets/weathon/sim_gas).
- **Need audio input:** [PeacefulData/DCASE_2025_AudioQA_backup](https://huggingface.co/datasets/PeacefulData/DCASE_2025_AudioQA_backup), [VITA-MLLM/AudioQA-1M](https://huggingface.co/datasets/VITA-MLLM/AudioQA-1M).
- **[Fhrozen/dcase22_task3](https://huggingface.co/datasets/Fhrozen/dcase22_task3)** (STARSS22): direction labels, but domestic sounds and we'd need our own beamforming.
- **Off-domain:** [karankhatavkar/ak47-acoustic-rul-simulated](https://huggingface.co/datasets/karankhatavkar/ak47-acoustic-rul-simulated), [amberhandal24/acoustic-alert-5class](https://huggingface.co/datasets/amberhandal24/acoustic-alert-5class), [hommed/vibration-audio-processed](https://huggingface.co/datasets/hommed/vibration-audio-processed), [vucinatim/spectrogram-captions](https://huggingface.co/datasets/vucinatim/spectrogram-captions).

## 4. Recommendation
**Picks:** (1) **RMIS `iica`** for leak vs no-leak, (2) **RMIS `iiee`** for 3-class fault type, (3) **synthetic Orbit acoustic-camera renders** for source location and dB, since HF has none.

The logged sets are not the best fit after all: they are all 16 kHz, so they can't fill a kit-style 0–20 kHz image. Keep the HTill test parquet only as a cheap 0–8 kHz "hard mode".

**Rendering (laptop, one script that also writes the truth):**
- High-pass at 100 Hz (IICA has a 12 Hz rumble peak).
- Hann STFT, nperseg 2048, 50% overlap; power in dB, clipped to a 70 dB range per image.
- Jet colormap, y axis 0–20 kHz with kHz ticks, x axis in seconds, **no colorbar**, at the kit image's pixel size, JPEG q85.
- Truth, computed by code: `peak_khz` = argmax of the time-averaged PSD over 0.1–20 kHz, band fractions, and `hf_frac` (15–20 kHz).

**Prompt** (llama-server `response_format` json_schema, temperature 0, one image):
- System: "Inspection-robot spectrogram. x = time (s), y = 0–20 kHz, jet (blue = low, red = high energy), no colorbar: colour is relative only."
- Reply schema: `{status: normal|anomaly, fault_type: normal_hum|leak|bearing_impulsive|electrical|other, peak_khz: number, dominant_band_khz: "0-2"|"2-5"|"5-10"|"10-20", broadband_hiss: bool, confidence: 0..1}`.

**Scoring:** status macro-F1, balanced accuracy and AUROC (p(anomaly) = confidence if `status=anomaly`, else 1 − confidence), per noise condition and leak type; iiee 3-class macro-F1; peak-frequency median absolute error, % within ±0.5 kHz, and dominant-band accuracy; JSON-valid rate; p50/p95 latency. Baselines: majority class, and a code reader thresholding `hf_frac` (fit on a separate 100-clip split). Qwen has to beat the code reader to justify using it.

**Slices:** IICA **600** = 3 leak types × 5 noise conditions × {leak, noleak} × 20, one 10 s chunk per 30 s original (`ori`) and one mic per (session, knob), since the chunks and mics of one event are near-copies. IIEE **300** (100 per class) from `test_cut`, stratified by background.

**Fetching:**
- **Laptop:** `https://datasets-server.huggingface.co/rows?dataset=jiangab/RMIS&config=iica&split=fd&offset=N&length=100` returns labels plus a signed WAV URL per clip (600 clips ≈ 0.6 GB). Render locally and rsync only the JPEGs + `manifest.jsonl` (~20 MB) to `spot-amd`, which sidesteps the 4 GB `/tmp`.
- **Box:** `curl -L https://hf-mirror.com/datasets/jiangab/RMIS/resolve/main/iiee/fd-0000{0,1}-of-00002.parquet` into `$HOME`. For IICA, pull one 489 MB shard at a time and filter on `leak`/`noise`/`status`. hf-mirror returned 206 to range requests for RMIS, CANDAS and HTill.

**Licence:** IDMT data is CC BY-NC-ND, so evaluate with it but commit only the manifest (file names + labels) and the render script, not audio or renders. Cite Johnson et al., Inter-Noise 2020 (IICA) and Grollmisch et al., EUSIPCO 2019 (IIEE). Use CANDAS (MIT) if images must be committed.

**Contamination:** low for IICA and IIEE: they are audio, our renders are new images, and no image copies were found on HF. Higher for CWRU, MIMII and DCASE, which are heavily published.

**Acoustic camera:** render ~300 synthetic Orbit-style screenshots: a plant photo (e.g. [lg227210/gas-valve-anomaly-dataset](https://huggingface.co/datasets/lg227210/gas-valve-anomaly-dataset), MIT), a Gaussian source blob at a known (x, y), and a "Key Results" panel with SPL at source (dB), SNR value and SNR threshold. Truth: ANOMALY iff SNR > threshold. Score location hits (within 10% of image width) and pixel error, exact dB read, and status accuracy.

## 5. Open questions / next searches
- The Valladolid 64-MEMS acoustic-image fault papers (uvadoc.uva.es 10324/25818, 25815, 67210) returned ECONNREFUSED; data availability unknown. IEEE DataPort PD-Loc (array partial-discharge localisation) is not on HF and was not checked.
- IICA `knob` (0.0n–9.0n) and the filename tags `iO` (noleak) / `niO` (leak) are undocumented on the card; see the Zenodo README. Under hydraulic noise, the one leak clip checked had no extra energy above 10 kHz, so expect noisy conditions to be hard.
- MAFAULDA's ~22 kHz tone was checked in 2 clips only. In CANDAS, check why the viewer shows 2,900 rows against 21,211 files.
- Cards not read in depth: RMIS `umged` E-codes, [Samlzy/MCC5-THU-Motor](https://huggingface.co/datasets/Samlzy/MCC5-THU-Motor).
- Next: STARSS23 (direction labels + 360° video) as a route to real acoustic-map images; Kaggle and Zenodo acoustic-camera sets.
