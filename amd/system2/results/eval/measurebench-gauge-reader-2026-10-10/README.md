# Code gauge reader on MeasureBench real pressure gauges (2026-10-10)

> **Superseded (scorer S0).** This first scoring assumed every reading was psi; with unit-aware scoring the 7 HIGH-trust readings are 1 correct / 6 wrong, not 2 / 5. Corrected, labelled results and the full scorer history: `amd/vision/results/gauge-reading-dev-2026-10-10/README.md`. This file is kept unchanged below as the original record.


**What:** every `pressure_gauge` image (118) in MeasureBench `real_world` through our vision service
(`amd/vision`, `POST /v1/vision`, `kind: gauge`, `annotate: false`, default settings, version 0.1.0, repo `db3fb1d`)
on the W7900D box, scored against MeasureBench's human-annotated reading intervals. No model involved.

**Source and licence:** [FlagEval/MeasureBench](https://huggingface.co/datasets/FlagEval/MeasureBench), revision
`93c001671055f739bc396c73aa6b2d5e6171abed`, `data/real_world-00000-of-00001.parquet` (sha256
`8c0527ab972376bee9bcc363c75bc8f77ba3177b9b3360aea462aa5fa79fb449`), licence **CC BY-SA 4.0** (dataset card).
`vision.jsonl` contains MeasureBench question IDs, their `evaluator_kwargs` labels and source URLs, so this file is
a derivative and is shared under **CC BY-SA 4.0** with attribution to Lin et al., "Do Vision-Language Models Measure
Up? Benchmarking Visual Measurement Reading with MeasureBench", arXiv:2510.26865. No images are stored here.

## Result
| Vision output (first reading) | Photos |
|---|---|
| HIGH trust (confidence >= 0.5, no `AMBIGUOUS_NEEDLE`/`DEFAULT_SWEEP`), correct within tolerance | 2 |
| HIGH trust, **wrong** | 5 |
| LOW trust (System 2 drops its value), within tolerance | 14 |
| LOW trust, wrong | 75 |
| No value | 22 |

Flags on first readings: `AMBIGUOUS_NEEDLE` 78, `LABEL_OUTLIER` 44, `DEFAULT_SWEEP` 27, `NEEDLE_OUTSIDE_SCALE` 23,
`WEAK_NEEDLE` 3. 118 calls in 128 s.

**Meaning:** on real photos the code reader almost never yields a row System 2 may use, and 5 of its 7 HIGH-trust
readings are wrong. With `pairing: linked`, a wrong HIGH-trust row would drive a false "tool and log disagree"
recommendation. This is a vision-service limitation; fine-tuning Qwen does not change it.

## Scoring rule (as run)
Trust as in `orchestrator.assess.vision_rows` (`VISION_TRUST_MIN` 0.5, `AMBIGUOUS_NEEDLE`/`DEFAULT_SWEEP` force LOW).
The reader reports psi; each labelled interval is converted to psi (bar 14.5038, kPa 0.145038, MPa 145.038,
kgf/cm² and kg/cm² 14.2233; intervals in other units are skipped) and widened by the vision contract's gauge
tolerance `max(6 psi, 3 % of the interval's upper value)` (the contract uses 3 % of full scale, which the labels do
not give). "Correct" = the value falls inside any widened interval of the item (dual-scale items have two).

## Not checked yet
- Labels were not individually reviewed (one `pressure_gauge` item is labelled in °C).
- 102 of the 114 source groups are YouTube videos; MeasureBench does not state per-image licences beyond the dataset
  licence.
