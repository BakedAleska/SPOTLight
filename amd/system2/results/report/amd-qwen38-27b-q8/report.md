# System 2 evaluation report

Held-out dataset content 588700df763b, 38 of 38 flags, runners template, model. Label `amd-gfx1100-rocm-qwen38-27b-q8`, 2026-10-10T20:21:01.885989Z to 2026-10-10T20:28:48.619358Z. Report system2-report-2026-10-10.1.

**Headline-eligible (held-out fold 2, full set).**

## Reproduce

```
python scripts/report.py --runner both --model-url http://127.0.0.1:8080 --model qwen3.8-27b-q8_0 --label amd-gfx1100-rocm-qwen38-27b-q8 --out-dir results/report/amd-qwen38-27b-q8
python scripts/report.py --rebuild results/report/amd-qwen38-27b-q8
```

`--rebuild` recomputes every number below from cases.jsonl and the checked-in held-out dataset, and exits 4 on any difference.

## Headline: held-out fold-2 missions

38 flags in 8 missions (M006, M009, M012, M013, M017, M018, M020, M023). Controlled packets from System 1 fold-2 held-out kit missions: dynamic evidence comes only from the flag's own mission at or before as_of, with no labels, targets or prior generated answers, and few-shots come from the training pool only. Headline numbers only for the full set; grounding is still mechanical consistency, not entailment. Grounding = mechanical consistency with cited packet content (not entailment, not correctness).

Each cell: value [95% CI] (k/n flags, missions). CI = mission-level bootstrap: whole missions resampled with replacement, 2.5/97.5 percentiles, linear interpolation, 1000 resamples, seed 20261010.

### Served output (what the operator sees, after retry/fallback)

| metric | template | model | model − template |
|---|---|---|---|
| public response valid | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | +0.0 pp [+0.0, +0.0] (38 pairs, 8 missions) |
| schema valid | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | +0.0 pp [+0.0, +0.0] (38 pairs, 8 missions) |
| citation IDs valid | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | +0.0 pp [+0.0, +0.0] (38 pairs, 8 missions) |
| numbers consistent with cited rows | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | +0.0 pp [+0.0, +0.0] (38 pairs, 8 missions) |
| names consistent with cited rows | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | +0.0 pp [+0.0, +0.0] (38 pairs, 8 missions) |
| all four mechanical checks (grounding pass) | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | +0.0 pp [+0.0, +0.0] (38 pairs, 8 missions) |
| agreement with validator suggested_action | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) | +0.0 pp [+0.0, +0.0] (38 pairs, 8 missions) |

### Model's own output

| metric | template | model |
|---|---|---|
| first attempt schema valid | - | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) |
| first attempt citation IDs valid | - | 100.0% [100.0%, 100.0%] (38/38 flags, 8 missions) |
| first attempt numbers consistent | - | 97.4% [91.9%, 100.0%] (37/38 flags, 8 missions) |
| first attempt names consistent | - | 97.4% [92.3%, 100.0%] (37/38 flags, 8 missions) |
| accepted on first attempt (path llm) | - | 94.7% [88.6%, 100.0%] (36/38 flags, 8 missions) |
| model output served (llm + retry) | - | 94.7% [88.6%, 100.0%] (36/38 flags, 8 missions) |
| needed a retry | - | 5.3% [0.0%, 11.4%] (2/38 flags, 8 missions) |
| fell back to the checked template | - | 5.3% [0.0%, 11.4%] (2/38 flags, 8 missions) |

### Fallback and paths

- `model` fallback to the checked template: 5.3% [0.0%, 11.4%] (2/38 flags, 8 missions)
- `model` retry: 5.3% [0.0%, 11.4%] (2/38 flags, 8 missions)
- `template` paths: llm 0, retry 0, fallback 38, insufficient_context 0
- `model` paths: llm 36, retry 0, fallback 2, insufficient_context 0

### By category

| category | template | model |
|---|---|---|
| data_quality | 4 flags, 3 missions | 4 flags, 3 missions, model served 4, fallback 0 |
| equipment | 12 flags, 7 missions | 12 flags, 7 missions, model served 10, fallback 2 |
| mission | 6 flags, 6 missions | 6 flags, 6 missions, model served 6, fallback 0 |
| navigation | 2 flags, 2 missions | 2 flags, 2 missions, model served 2, fallback 0 |
| robot_health | 14 flags, 6 missions | 14 flags, 6 missions, model served 14, fallback 0 |

## Speed

Label `amd-gfx1100-rocm-qwen38-27b-q8`, endpoint `http://127.0.0.1:8080` (loopback True), host `j-4khskqsnluneuceyxwcyvvuj56`. Speed is attributed to the label and endpoint above; only an amd-* labelled run on the gfx1151 box is an AMD measurement.

| | |
|---|---|
| latency p50 / p95 per flag (nearest-rank, incl. retry/fallback) | 11188 / 15003 ms (38 flags) |
| p95 within the 15 s live budget | no |
| decode tokens/s p50 / mean (min-max, n attempts) | 22.557410024248743 / 22.528 (22.18266778977539-22.690164273723106, 38) |
| prompt tokens p50 / max | 1897 / 2307 |
| completion tokens p50 / max | 238 / 316 |
| truncated / timed-out attempts | 0 / 2 of 40 |
| peak device memory | 29314.9 MiB peak (VRAM 29187.3 + GTT 127.6; baseline 29126.4 MiB; 932 samples) on card1 0x1002:0x744b, card2 0x1002:0x744b, card3 0x1002:0x744b, card4 0x1002:0x744b, card5 0x1002:0x744b, card6 0x1002:0x744b, card7 0x1002:0x744b, card8 0x1002:0x744b |
| models observed | /root/models/q38-27b/Qwen3.8-27B-Q8_0.gguf |

## Development set (training missions; not headline)

6 flags. Approved development kit packets from training missions only (synthetic kit data, few-shots drawn from the same pool with the current case excluded). Not held-out and not independent real-world observations; never a headline number.

| metric | template | model |
|---|---|---|
| public response valid | 6/6 (100.0%) | 6/6 (100.0%) |
| final schema_valid | 5/5 (100.0%) | 5/5 (100.0%) |
| final citation_valid | 5/5 (100.0%) | 5/5 (100.0%) |
| final numeric_consistent | 5/5 (100.0%) | 5/5 (100.0%) |
| final name_consistent | 5/5 (100.0%) | 5/5 (100.0%) |
| agreement with validator suggested_action | 6/6 (100.0%) | 6/6 (100.0%) |
| model output served (llm + retry) | n/a (0) | 5/5 (100.0%) |
| fell back to the checked template | n/a (0) | 0/5 (0.0%) |

## Limitations and deferred items

- **EVAL-02** claim support judged by a different model: not built. Only the mechanical part is reported: schema, citation-ID, numeric and name consistency with the cited packet content.
- **EVAL-03** LLM-vs-template judge rubric: not built. No judge model scores usefulness or clarity; model and template are compared on mechanical checks only.
- **EVAL-04** action quality against human labels: not built. Only agreement with the validator's suggested_action is reported. That is agreement, not accuracy.
- **EVAL-07** judge-vs-human agreement: not built. Neither a judge nor human labels exist, so their agreement cannot be measured.
- **grounding** entailment / correctness of claims: not built. Grounding means mechanical consistency with cited packet content (not entailment, not correctness).
- **comparison** evidence that the model beats the template: not built. The template passes the mechanical checks by construction (it copies cited rows), so mechanical parity is expected and is not evidence that the model is better.
- **sample size** tight intervals: not built. n is small (38 flags in 8 held-out missions), so the mission-level CIs are wide.
- **data** independent real-world observations: not built. "Real" means sponsor kit missions as opposed to simulator output; the kit itself carries synthetic=True metadata. No synthetic or unrestricted-retrieval results are included.

## Method

- Held-out packets: System 1 fold-2 kit missions; dynamic evidence only from the flag's own mission at or before as_of; no labels, targets or prior answers; few-shots from the training pool only.
- Both runners see the identical normalized packet per flag (packet hashes checked) and finish in the same checker. A fallback is the checked template and never counts as model success.
- Rates: k/n over the stated denominator; failed, timed-out and fallback flags stay in it. Rows for packets marked insufficient are excluded from the final-check denominators.
- CI: mission-level bootstrap: whole missions resampled with replacement, 2.5/97.5 percentiles, linear interpolation; 1000 resamples; seed 20261010; fewer than 2 missions gives no CI.
- Identity hash 76dcd391f6be; code revision df63f64cf7a8.
