# Pre-registration: fresh evaluation of `gauge-agreement-v1` (frozen 2026-10-11, before any fresh photo was viewed)

Development evidence (118 MeasureBench images, now development data): `amd/vision/results/gauge-reading-dev-2026-10-10/`.
On that data this exact policy (ETH-registered + Qwen-registered, 2 % of span) agreed on 13/87 clean items, and the
final (ETH) reading was correct within 2 % of full scale on 9 of them. That is why the default is **not** automatic.

## Sample (frozen in `amd/vision/results/gauge-agreement-fresh-2026-10-11/fresh_sample_manifest.json`)
Wikimedia Commons, categories Pressure gauges, Dual/Single scale pressure gauges, Vacuum gauges, Steam gauges, Tyre
pressure gauges (222 files). Eligible: JPEG/PNG, ≥ 400 × 400 px, title not among MeasureBench's Wikimedia sources
(205). Sorted by title, `random.Random(20261011).shuffle`, first **72**. After download, any image whose SHA-256
matches a MeasureBench image is excluded and reported. All 72 stay in the operator-workflow evaluation; only the
reading-accuracy scoring set is restricted (below). Each file keeps its Commons licence, author and URL.

## Annotation (one reviewer, before any reader runs, blind to reader outputs)
Per image (`annotations.jsonl`): photo yes/no; instrument kind; target (`clear` = one gauge, one main pointer;
`ambiguous`; `none`); every printed scale (unit, min, max); the **registered scale** = the outermost numbered scale
(stands in for the facility asset register; the photo is the only source here, which is a simulation); the ground-truth
reading on the registered scale as an interval [lo, hi] no wider than the reviewer's reading resolution; label
confidence (`sure` / `unsure`); quality tags. Scoring set = photo, in-scope pressure/vacuum gauge, target `clear`,
label `sure`.

## Readers and policy (code as committed with this file)
- Reader A `eth_registered`: `ethz-asl/analog_gauge_reader` @ 518cdfa, models unchanged, offline CPU container
  (`env/eth-analog-gauge-reader/` in the dev record), registered mode: start/end notch → registered min/max.
- Reader B `qwen_registered`: Qwen3.8-27B Q8_0 + mmproj-F16 (hashes in the original archive), llama.cpp 23b0202 on one
  W7900D, `amd/vision/scripts/qwen_gauge_read.py --variant full --registered` (prompt, JSON schema, temperature 0,
  thinking off). Images are sent under opaque names (`fr_NNN`); only the registered record is added to the prompt.
- Policy `amd/vision/vision/agreement.py` `gauge-agreement-v1`: both outputs valid (numeric, registered unit after
  printed multipliers, within the registered scale ± 5 % of span; Qwen status `ok`; ETH no stage failure; photo
  decodes with shorter side ≥ 256 px) and |A − B| ≤ **2 %** of the registered span → agreement; final value = **A**
  (never an average); both readings kept. Default `automatic=False`.

## Metrics
On the scoring set: cases; agreement (accepted) cases; accepted readings correct / incorrect, where correct = |final −
GT interval| ≤ **2 %** of the registered span (primary) and ≤ 3 % (secondary); automatic-acceptance coverage;
`review_disagreement` and `recapture_or_review` counts; per-reader accuracy; error categories for wrong or rejected
readings (unit, scale selection, target needle / counterweight, other); Clopper-Pearson 95 % interval for accuracy among
accepted. Reader agreement is never used as the correctness criterion. All 72 images: decision distribution, so the
review/recapture path is exercised by ambiguous and out-of-scope photos too.

## Promotion rule (fixed now)
Set `automatic=True` only if the scoring set yields **≥ 20 agreement cases** and the **Clopper-Pearson two-sided 95 %
lower bound** of their accuracy (2 % criterion) is **≥ 0.85**. Otherwise agreement remains an operator-visible
supporting signal that requires human confirmation. Thresholds are not loosened to raise coverage. If the policy,
prompts or scoring change after the results are seen, this set becomes development data and any rerun on it is not an
untouched test.
