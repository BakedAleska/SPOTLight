# Gauge reading on real photos: development record (2026-10-10)

**Label: development data.** The 118 MeasureBench `real_world` `pressure_gauge` images were used to diagnose readers,
fix code and the scorer, review labels and discover the agreement signal. Nothing here is an untouched test result.
Keep these images as regression cases; never present a rerun on them as a fresh evaluation.

Source: [FlagEval/MeasureBench](https://huggingface.co/datasets/FlagEval/MeasureBench) @ `93c00167…`,
`real_world-00000-of-00001.parquet` sha256 `8c0527ab…fb449`, **CC BY-SA 4.0** (Lin et al., arXiv:2510.26865). Files
here that carry its labels or question IDs are derivatives under CC BY-SA 4.0. No images are committed (see "Not in
Git").

## Labels: original vs reviewed
- **Original:** MeasureBench `evaluator_kwargs` (reading interval(s) + units), read from the parquet by the scorer.
- **Reviewed (`labels/reviewed_labels.jsonl`):** one human pass over all 118 images (contact sheets, 2026-10-10):
  photo or not, single clear target or not, number of needles, every printed scale (unit, min, max), label
  plausibility (`yes` / `uncertain` / `no`), quality tags, a physical-instrument / scene `group`, instrument `kind`.
  `eval_clean` = photo, clear target, label `yes`, in-scope kind → **87 items**. The reviewer saw the labels; the
  review checks them, it does not replace them. Two labels are wrong (`pg_10`: 1.9-2.1 MPa on a 0-1 MPa dial, needle at
  ~0.2; `pg_69`: its two intervals contradict each other). See `ACCOUNTING-118.md` for every item and why it is in or
  out. Excluded items stay in the operator-workflow evaluation as review/recapture cases.
- Two-pointer dials (drag/set pointer): target = `clear` when the label follows the main pointer, `ambiguous`
  otherwise.

## Scorer corrections (all in `amd/vision/scripts/measurebench_eval.py`; the final version produced `final-scores/`)
| Rev | Change | Effect found |
|---|---|---|
| S0 | ad-hoc inline scoring (first report, `results/eval/measurebench-gauge-reader-2026-10-10/`) | **assumed every reading was psi**; superseded |
| S1 | unit-aware: reading unit must match a label unit; 3 % of the interval's upper value | our reader: 7 accepted, 1 correct (not 2/7) |
| S2 | empty label units = any unit (MeasureBench `unit_correct=None`) | +1 unit-correct |
| S3 | more unit spellings (hPa, inH2O, mmH2O, …); printed multipliers (`x10^-1 MPa`); `number_correct_ignoring_unit` | Qwen `9.8 X10⁻¹MPa` on pg_68 now correct |
| S4 | `--reviewed`: score only `eval_clean` items; `fsN_correct` = unit matches and the value is within N % of the reviewed scale span of the label interval | gauge accuracy is a % of full scale; 3 %-of-value was harsh on point labels |
Dual scales: a reading on either labelled scale counts (as MeasureBench's `multi_interval_matching`). Abstentions are
never correct. `historical-scores/` files were produced by intermediate revisions and are **not comparable**;
`final-scores/*.all118.json` and `*.clean87.json` re-score every run with S4.

## Runs (all on the same 118 images)
| Run | What | Valid? |
|---|---|---|
| `ours-v0.1.0` | our code reader as deployed (`amd/vision` 0.1.0) | yes |
| `ours-v0.2.0` | + `UNIT_UNKNOWN` (no silent psi), `SHORT_NEEDLE` (thresholds tuned on these images) | yes |
| `eth-518cdfa-INVALID-network-504` | ETH `analog_gauge_reader` @ 518cdfa, CPU container | **no**: 74 images failed on GitHub 504s from a per-image `torch.hub` call (my setup) |
| `eth-518cdfa-offline` | same, DINOv2 loaded from the pinned local copy, `--network none` | yes |
| `eth-518cdfa-registered` | + registered scale range replaces OCR'd numbers (start/end notch → min/max), 87 clean items | yes (dev) |
| `qwen-q8-full` (+ `rerun10`) | untuned Qwen3.8-27B Q8_0 reads the full image, structured JSON, temp 0 | yes after merging: the last 10 hit a llama.cpp grammar-state error and were rerun after a server restart |
| `qwen-q8-dialcrop-INVALID-server-grammar-state` | dial crop, run while the server was stuck | **no**: all 118 HTTP 500 |
| `qwen-q8-dialcrop` | dial crop (our Hough circle + 15 %) | yes |
| `qwen-q8-full-registered` | full image + registered scale ranges in the prompt (no readings), 87 clean items | yes (dev) |

Environment: Qwen on the SSH W7900D (`llama-server` @ 23b0202, flags as in the original archive, pinned to GPU 0);
ETH in a Python 3.8 CPU container (`env/eth-analog-gauge-reader/`: Dockerfile, the two patches, model hashes). The
Qwen prompt, schema and settings are in each run's `run.json`.

## Results (S4 scorer)
Clean 87 items, correct = right unit and within 2 % (3 %) of the dial's full scale:
| Reader | readings | correct 2 % | correct 3 % |
|---|---|---|---|
| ours v0.1.0 | 75 | 19 | 19 |
| ours v0.2.0 | 75 | 19 | 19 (accepted 3, all correct) |
| ETH as published | 49 | 14 | 14 |
| ETH + registered range | 73 | 31 | 41 |
| Qwen full image | 87 | 40 | 47 |
| Qwen dial crop | 79 | 34 | 36 |
| Qwen + registered range in prompt | 87 | 37 | 45 |
At least one of ETH-registered / Qwen-full is correct on 53 of 87.

Development-only signals (chosen after looking at these results; not validated):
- Qwen full **agrees** (within 2 % of span) with ETH as published: 10 items, 10 correct. With our reader: 11, 8
  correct. Qwen full vs its own crop: 30, 19 correct. Agreement is supporting evidence, not verification, and 10/10
  only bounds precision at ≥ 0.69 (Clopper-Pearson 95 % two-sided lower limit; one-sided 95 %: ≥ 0.74).
- Qwen's self-reported `status: ok` does not separate right from wrong (87 "ok", 47 wrong).
- Registered ranges fix Qwen's units (86/87) but not its needle reading; they turn ETH's OCR failures into readings.

## Not in Git
Original images, processed inputs, reader overlays, ETH debug plots and the review contact sheets (~150 MB,
MeasureBench-derived) are in `local-artifacts/gauge-reading-dev-2026-10-10/` in the working copy (gitignored), with
hash lists. They are not published anywhere.
