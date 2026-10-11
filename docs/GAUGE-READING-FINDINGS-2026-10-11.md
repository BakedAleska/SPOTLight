# SPOTlight: can a photo give a trustworthy gauge reading? (findings, 2026-10-11)

**Bottom line.** Neither our gauge reader nor untuned Qwen can be trusted to read real gauge photos on its own. What
we ship is a **selective inspection workflow**: use a photo reading only when the evidence meets an explicit
acceptance policy, otherwise show the photo, explain the uncertainty and ask for a recapture or a human reading. No
model was fine-tuned; nothing here is a fine-tuning result.

All numbers below are **development data** (118 MeasureBench real pressure-gauge photos that we also used to debug and
tune). The pre-registered fresh test has **not been run yet** (status at the end).

## 1. What we found

**Readers on the 87 photos whose labels we verified** (correct = right unit and within 2 % of the dial's full scale):

| Reader | Gave a reading | Correct | Notes |
|---|---|---|---|
| Our code reader (v0.1, as deployed) | 75 | 19 (22 %) | Trusted 7 readings, **6 of them wrong** (all 118 photos) |
| Our code reader (v0.2, after fixes) | 75 | 19 | Trusts 3, all correct, but coverage ≈ 3 % |
| ETH `analog_gauge_reader` (published) | 49 | 14 | Fails mostly at reading the printed numbers (OCR) |
| ETH + registered gauge range | 73 | **31** | We supply unit and scale range; it measures the needle |
| Untuned Qwen3.8-27B, full photo | 87 | **40 (46 %)** | Units right 95 %, but says "ok" to every one of 47 wrong answers |
| Untuned Qwen + registered range | 87 | 37 | Knowing the range fixes units, not needle reading |

- At least one of ETH-registered / Qwen is right on 53 of 87: the readers fail on different photos.
- **Agreement is a signal, not proof.** Qwen + published ETH agreed 10 times, all correct (dev only; 95 % lower bound
  0.69). The policy pair the spec requires (both using the registered range) agreed 13 times, **9 correct**: two
  readers can make the same mistake.

**Why readings fail (inspected case by case):** needle vs counterweight confusion; dual-scale dials (wrong scale or
mixed labels); scale-mapping error of 1-3 % of full scale; light needle on a dark face; gauge too small in the frame;
unsupported instruments (extinguisher bands, multi-pointer dials); our reader **silently assumed psi** on 38 of 96
readings (22 on gauges with no psi scale).

**Our own evaluation had bugs, now fixed and documented:** the first score assumed psi everywhere; it mishandled empty
units and printed multipliers (`x10⁻¹ MPa`); a 3 %-of-value tolerance was unfair to point labels. MeasureBench itself
has wrong labels (2 found) and 12 photos with no single clear target (two needles, several gauges).

**Capture matters but is not the whole story:** Qwen is right on 4/13 small-in-frame and 3/11 angled shots vs 40/87
overall, but only 11/25 even on clean shots. Practical capture rules: stop and hold still, fill ≥ 1/3 of the frame
with the dial, shoot square-on, and retake when the readers disagree.

## 2. What changed in the product
- **Vision service 0.2.0:** no silent units (`UNIT_UNKNOWN`), short "needle" (counterweight) cannot be HIGH trust.
  Wrong accepted readings 6 → 2 on all 118 photos (the dev set used to choose the fix).
- **System 2 explanations:** the thermal answer that was cut off at the 240-character schema limit now comes out complete
  (few-shot answers were teaching the model to dump every field); no doomed image retry with 5 s left; no few-shot that
  is the same event as the flag (it leaked an asset name). Smoke test: 8/8 model answers on the first attempt.
- **Measurement runs are auditable:** `SYSTEM2_RECORD_DIR` keeps every raw attempt; `SYSTEM2_CACHE_DIR=off` forces
  real generations.
- **Acceptance policy `gauge-agreement-v1`** (`amd/vision/vision/agreement.py`): both readers must return valid
  readings on the registered scale and agree within 2 % of its span; the final value is the geometric reader's (never
  an average), both are kept; **default is "needs operator confirmation"**, not automatic.

## 3. Recommended architecture
Linked photo → **geometric reader with registered range** (traceable needle/notch overlay) + **Qwen reading the same
photo** → agreement policy → evidence row only if accepted or human-confirmed → Qwen explains and recommends from rows.
Disagreement or invalid output → photo shown, both readings shown, "retake or read manually". Operating limits stay
separate from the gauge's scale range. Demo images never count as evidence about an asset.

## 4. Training: not now
- Qwen vision fine-tune: 27B in bf16 needs ~70 GB (both W7900Ds with serving paused, ~a day) or a 192 GB-class GPU
  (unpriced); published results show small real-photo gains. Not worth it before the deadline.
- If we train later, train the **reader's needle/keypoint models** (small, cheap) with needle and scale annotations,
  and keep a real-photo test set untouched.

## 5. Status of the fresh test (honest)
Pre-registered at commit `08472af` (sample, policy, metrics, promotion rule: ≥ 20 agreement cases and 95 % lower bound
≥ 0.85, else confirmation required). 72 Commons photos are downloaded and hash-recorded. **Not yet done:** blind
ground-truth annotation, the two reader runs, scoring, and the near-duplicate check against MeasureBench. Until then
the policy stays at "operator confirmation required".

## Where everything is
- Original untuned deployment: `amd/system2/results/archive/untuned-q8-original-2026-10-10/`
- System 2 after fixes: `amd/system2/results/smoke/after-fixes-2026-10-10/`
- Gauge-reading development record (all 118 accounted for, scorer history, invalid runs kept): `amd/vision/results/gauge-reading-dev-2026-10-10/`
- Fresh test pre-registration: `docs/GAUGE-AGREEMENT-EVAL-PREREG.md`; System 2 training-path analysis: `docs/SYSTEM2-FINETUNE-PLAN-2026-10-10.md`
- Images and overlays (~370 MB, not in Git): `local-artifacts/` in the working copy
