# Quick image benchmarks for Qwen3.8-27B on the AMD box (2026-10-11)

Untuned **Qwen3.8-27B Q8_0 + mmproj-F16**, served by llama.cpp `llama-server` on the SSH **Radeon Pro W7900D**
(gfx1100, 48 GB), scored on small seeded slices of the Hugging Face datasets chosen in
`research/tech-hf-bench-*-2026-10-11.md`. One image per call, temperature 0, thinking off, and each item's JSON
schema as `response_format`. Unparseable answers count as wrong. Rates carry a Wilson 95% interval.

## Results

| Benchmark (kit modality) | Dataset slice | n | Qwen3.8-27B | Reference |
|---|---|---|---|---|
| **Visual**: object hallucination | RePOPE adversarial, yes/no, 100 yes / 100 no | 200 | **89.5%** accuracy [84.5–93.0], F1 88.8, **false-yes 4.0%** | balanced, so always-"no" = 50% |
| **Thermal**: motor overheating | herislab `electric_motor`, 100 normal / 100 fault, no colour scale | 200 | **73.0%** accuracy [66.5–78.7], F1 78.7; recall 100%, false-alarm 54% | always-"overheating" = 50% |
| **Gauge**: real substation dials | RPM-10K test, `biao1`–`biao6` | 147 | **36.7%** within ±2% FS [29.4–44.8]; **59.9%** within ±5% [51.8–67.4]; 76.9% within ±10%; median error 3.3% FS; scale max right 89.1% | DialBench Acc@1%: Qwen2.5-VL-7B 25.6% |
| **Navigation**: map / route reading | SpatialEval Maze-Nav, 4-option MCQ | 100 | **64.0%** [54.2–72.7] | chance 25% |
| **Navigation**: blocked path | NaviTrace val, "can a wheeled robot do this task?" | 117 | 48.3% balanced: answers "blocked" for 113 of 117 | balanced chance 50% |
| **Acoustic**: air leak | RMIS `iica` leak / no-leak, rendered as kit-style 0–20 kHz spectrograms | 100 | 50.0%: answers "normal" for all 100 | code baseline (15–20 kHz energy threshold): **71.0%** |

Speed: single-client p50 per image was 1.5 s (yes/no) to 3.3 s (gauge JSON) in the first pass. The second pass ran
three clients against one llama-server slot, so its recorded latencies (p50 1.6–5.9 s) include queueing.

## What it shows
- **Strong:** not inventing objects (4% false-yes on adversarial probes) and reading a scale's range (89% got the
  dial's full scale right). Route reading is well above chance.
- **Usable with checks:** thermal. Qwen never misses an overheating motor (recall 100%) but calls half of the cold
  ones overheating too. It errs towards raising a flag, so keep a human on the call.
- **Not reliable alone:** needle reading at ±2% (37%). The code readers and the agreement policy in
  `docs/GAUGE-READING-FINDINGS-2026-10-11.md` still own gauge numbers.
- **Fails:** leak spectrograms and blocked-path judgements, where it gives the same answer every time. A simple code
  feature (high-frequency energy share) beats it on leaks, which supports the decision that code computes these
  signals and the model only explains them.

## Caveats
- Small slices: n = 100–200, intervals ±5–10 points. Untuned model, one prompt per task, no prompt search.
- The thermal "normal" images show idle, cold motors, so the task is easier than a real hotspot call.
- Gauge n = 147 unique items: a 36-item first draw and a 120-item second draw (9 in both). The second draw excludes
  RPM-10K rows whose reading is text ("Too vague. Unable to read."). Unit = mA; ±x% FS uses the label's `range` as
  full scale.
- NaviTrace n = 117 unique items: a 20-item first draw and a 100-item second draw (3 in both).
- Acoustic truth is the recording's leak label. The spectrogram renders are ours: no colorbar, colour is relative.
- RePOPE images are COCO photos, which are in Qwen's training data; the questions and fixed labels are RePOPE's.

## Files
- `prep.py`: builds each slice on the box (`/root/bench/run/<name>/manifest.jsonl` + `img/`) from hf-mirror
  downloads. RPM-10K is read by HTTP range requests from its 3 GB zip.
- `bench.py`: runs the manifests against llama-server, resumable per item id.
- `score.py`: the metrics and `summary.json`.
- `results/qwen38-q8-2026-10-11/`: `summary.json`, plus per benchmark the manifest (prompts, schemas and labels, no
  images) and the results (raw model text, parsed JSON, latency).

Licences: the datasets keep their own licences (herislab MIT, MeasureBench and RPM-10K as noted in the research
files, RMIS CC BY-NC-ND, RePOPE MIT on COCO images, SpatialEval and NaviTrace per their cards). Only labels, file names
and model outputs are committed here, never images or audio.
