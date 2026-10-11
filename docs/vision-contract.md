# Vision / OCR service contract (`amd/vision/`, port 8084)

Contract version **`2026-10-10.vision.2`** (`.2`: auth required, 12 MP pixel cap, JPEG/PNG/WebP only, request-body
cap, concurrency limit with 503; request/response shapes unchanged). **Change this file first, then code.** Research and tested numbers:
`research/tech-spot-vision-cv-2026-10-10.md`.

## What it is
A CPU-only, deterministic FastAPI service that reads SPOT inspection images: RapidOCR (PP-OCR on onnxruntime CPU)
plus code readers for thermal colorbars, dial gauges, spectrograms and Orbit "Key Results" screenshots.

**Core rule: code produces every number; the VLM never reads values off pixels.** This service makes no model calls.
The orchestrator (`amd/orchestrator/`, :8085) passes its readings as citable `V#` evidence rows plus the
(annotated) image to System 2 (`/v1/assess`, `context.vision[]`, `context.ocr[]`, `image_b64`).

Callers inside docker-compose use `http://vision:8084`; native callers on the AMD box use `http://127.0.0.1:8084`.

## Auth
Required (fail closed). `VISION_API_KEY` must be at least 16 printable ASCII characters with no surrounding
whitespace; every route except `GET /health` needs header `X-API-Key: <key>` (constant-time compare; 401
`{"detail": "missing or invalid X-API-Key"}` otherwise). The service refuses to start without a key unless
`VISION_ALLOW_NO_AUTH=1` (tests and local experiments only). There is no `/docs` or `/openapi.json`: this file is the
contract. The orchestrator sends the same key from its own `VISION_API_KEY`.

## `POST /v1/vision`
```http
POST /v1/vision
{"request_id": "r-123",                 // optional, echoed in logs only
 "media_id": "thermal_pump_bearing_01",  // optional; default "sha256:<16 hex>" of the decoded bytes
 "kind": "auto",                          // auto | thermal | gauge | acoustic_imager | spectrogram | orbit_screenshot | visual
 "action_type": "THERMAL_INSPECTION",     // optional; used by the router when kind=auto
 "image_b64": "<base64 or data: URL>",    // JPEG, PNG or WebP only (decided 2026-10-10: WebP was already supported)
 "logged": {"reading_id": "R0042", "measurement_type": "TEMPERATURE_C", "value": 83.5, "unit": "C"},   // optional
 "hints": {"range_min": 0, "range_max": 300, "unit": "psi", "sweep_deg": 270},                       // optional
 "annotate": true}                        // default true
```
- `logged.measurement_type`: `TEMPERATURE_C | PRESSURE_PSI | SOUND_DB` (readings.csv) or free text. `unit` is optional
  (`C`/`°C`/`F`/`°F`/`psi`/`dB`); temperatures are converted before comparing.
- `hints` (all optional) are per-asset fallbacks: a gauge whose labels can't be read uses `range_min`/`range_max` over
  `sweep_deg` (default 0–`range_max` over 270°); a thermal frame whose colorbar labels can't be read uses
  `range_min`/`range_max` as the bar's bottom/top. Readings that used a hint carry flag `USED_HINT`.

```http
→ 200
{"media_id": "thermal_pump_bearing_01",
 "kind": "thermal",
 "router": {"label": "thermal", "method": "action_type", "confidence": 1.0},
 "ocr": [{"text": "100", "bbox": [213, 28, 236, 40], "conf": 0.99}],
 "readings": [{"id": "V1", "quantity": "max_temperature", "value": 100.0, "unit": "°C", "confidence": 0.8,
               "method": "colorbar_lut", "flags": ["CLIPPED"], "bbox": [141, 60, 156, 76], "point": [149, 66],
               "note": "hottest spot reaches the 100 °C top of the scale; true value may be higher"},
              {"id": "V2", "quantity": "p95_temperature", "value": 81.5, "unit": "°C", "...": "..."}],
 "key_results": [{"name": "Max Temperature", "units": "°C", "value": 100.0},
                 {"name": "Reference Temperature", "units": "°C", "value": 41.0}],
 "comparison": {"logged_id": "R0042", "logged_value": 83.5, "logged_unit": "°C", "vision_id": "V1",
                "vision_value": 100.0, "delta": 16.5, "tolerance": 4.0, "agrees": false, "flags": ["VISION_CLIPPED"]},
 "alert": {"title": "Thermal hotspot ≥100.0 °C (ΔT 59 °C over reference)", "severity": 4, "source": "vision.thermal"},
 "flags": [],
 "annotated_image_b64": "<base64 JPEG>",
 "annotated_mime": "image/jpeg",
 "latency_ms": 412,
 "version": {"service": "0.2.0", "contract": "2026-10-10.vision.1", "rapidocr": "3.10.0", "onnxruntime": "1.31.0"}}
```

### Fields
- `kind`: the reader that ran (= `router.label`).
- `router.method`: `kind` (explicit request field) > `action_type` (map below) > `heuristic` (colour/size) >
  `default` (`visual`). `confidence` is 1.0 for `kind`/`action_type`, the heuristic's margin otherwise.

  | action_type | kind |
  |---|---|
  | `THERMAL_INSPECTION` | `thermal` |
  | `GAUGE_READING` | `gauge` |
  | `ACOUSTIC_INSPECTION` | `spectrogram` if the image is mostly a saturated plot, else `acoustic_imager` |
  | `VISUAL_INSPECTION` | `visual` |
  | anything else | heuristic |

  Heuristic: very large and wide (≥1600 px wide, aspect ≥1.4, mostly dark-grey UI) → `orbit_screenshot`; ≥80 % of pixels
  high-saturation → `thermal` (warm hue share dominates) or `spectrogram` (blue share dominates); else `visual`.
- `ocr[]`: every OCR line, `bbox` = axis-aligned `[x0, y0, x1, y1]` in **original image pixels**, `conf` 0–1.
- `readings[]`: code-computed values. `id` is `V1`, `V2`, … in order (stable per kind, see the per-reader table).
  `confidence` 0–1 is the reader's own estimate; **callers should pass a reading to a model as a fact only when
  `confidence ≥ 0.5`**. `bbox` / `point` (original pixels) are optional and match the annotation. `note` is one plain
  sentence the orchestrator can quote.
- `key_results[]`: Orbit-style `{name, units, value}` (`value` is a number, a string, or `null` when a known row's
  value could not be read, with flag `MISSING_VALUE`), mirroring Orbit's `dataCaptures[].keyResults`. Display only:
  cite `readings[]`, not key_results.
- `comparison`: present only when `logged` was sent and the reader has a primary reading of the same quantity.
  Both values are in the reading's canonical unit (°C, psi, dB; a logged °F / bar / kPa value is converted first, and
  `logged_unit` names the canonical unit). `delta = vision_value − logged_value`. Tolerances: **4 °C**, **max(6 psi, 3 % of full scale)**,
  **3 dB**, **10 visual points**. When the primary reading's `confidence < 0.5`: `agrees = null` + flag
  `LOW_CONFIDENCE`. When the vision value is clipped at the scale top and `logged ≥ vision − tolerance`: `agrees = true`
  + flag `VISION_CLIPPED`. Otherwise `agrees = |delta| ≤ tolerance`. No primary reading → `comparison = null`.
- `alert`: Spot `AlertData` semantics, `severity` = `SeverityLevel` (0 unknown, 1 info, 2 warn, 3 error,
  4 critical); `null` when nothing is alert-worthy. Rules:
  - acoustic (Orbit Key Results): `anomaly = snr > snr_threshold`; ratio = snr/threshold: <1 none (no alert),
    1–1.5 → 2, 1.5–3 → 3, ≥3 → 4.
  - thermal: ΔT over reference (NETA-style bands, secondary sources): 1–3 °C → 1, 4–15 °C → 2, >15 °C → 3; max ≥ 95 °C
    (grease-bearing alarm/trip range) → 4.
  - Orbit screenshot whose badge says ANOMALY/FAILURE but whose SNR values are unreadable → 2 (`vision.orbit.status`).
  - any reader: a confident disagreement with the log (`agrees = false`) → at least 2 (`vision.compare`).
- `flags[]` (response level): `DOWNSCALED` (processed at ≤1280 px), `ROUTER_HEURISTIC`, `NO_TEXT`, `READER_FAILED`,
  plus reader-level failures: `NO_COLORBAR`, `AXIS_FIT_FAILED`, `LOW_PALETTE_MATCH`, `NO_DIAL`, `NO_PLOT`,
  `TIME_AXIS_UNREAD`, `NO_KEY_RESULTS`, `MISSING_VALUE`, `STATUS_MISMATCH` (Orbit's badge disagrees with
  `snr > threshold`).
- `annotated_image_b64`: the processed image with each reading's mark drawn and labelled with its `V#` (needle line,
  hotspot box, peak marker, Key Results row box), long side 480–1280 px, JPEG. `null` when `annotate=false`. **The raw
  input image is never echoed back.**

### Readers
| kind | readings (in V# order) | method | key flags |
|---|---|---|---|
| `thermal` | `max_temperature` °C (primary), `p95_temperature`, `median_temperature`, `reference_temperature` (scene median outside the hotspot), `delta_t` | colorbar LUT + OCR'd tick fit (RANSAC, several crops merged), nearest-colour inversion with a distance cut-off, 5×5 smoothing; hotspot = largest blob ≥ max−3 °C | `CLIPPED` (max at the bar top: value = the top label, a lower bound; confidence ≤ 0.8), `LABEL_OUTLIER`, `USED_HINT`, `LOW_PALETTE_MATCH` |
| `gauge` | `pressure` (psi/bar/kPa/MPa from OCR or the `unit` hint; with neither, the number is shown as psi but flagged `UNIT_UNKNOWN`, confidence ≤ 0.3; `dial_value` for other units) | Hough circle; needle = ray-darkness argmax; OCR'd dial labels → angle↔value RANSAC fit; fallback hint/270° sweep | `AMBIGUOUS_NEEDLE` (second ray peak > 0.6 × first, or a two-ended needle whose opposite arm is ≥ 0.25 r long; confidence ≤ 0.3, note names both candidate values), `LABEL_OUTLIER`, `DEFAULT_SWEEP` (≤ 0.4), `USED_HINT`, `WEAK_NEEDLE`, `NEEDLE_OUTSIDE_SCALE`, `UNIT_UNKNOWN` (≤ 0.3, since 0.2.0), `SHORT_NEEDLE` (followed arm < 0.6 r, usually the counterweight; ≤ 0.3, since 0.2.0) |
| `spectrogram` | `peak_frequency` kHz (primary), `band_energy_0_2khz`, `_2_5khz`, `_5_10khz`, `_10_20khz` (mean colour level 0–1), `high_frequency_ratio` (share of level above 5 kHz), `burst_count` | OCR axes → calibrate; jet-LUT inversion with off-table pixels masked; bursts = peaks (z > 1.5) of the >5 kHz level over time | always `RELATIVE_ONLY` (`absolute_db_available=false` in key_results; `Burst Times`, `Burst Interval` there too) |
| `orbit_screenshot` / `acoustic_imager` | each numeric Key Results row: `spl_at_source` dB (primary), `snr_value`, `snr_threshold`, `max_temperature` / `min_temperature` converted to °C (primary = max; the °F value stays in key_results and the note), `reading` (+unit, primary), `reading_confidence` %, `anomaly_prediction`, … Confidence = 0.95 × OCR confidence. | OCR overview (≤2000 px) to find the badge, title and "Key Results"; second near-native OCR pass over the table; `kv_rows` (label … value by y) | `MISSING_VALUE` (label without a value, e.g. occluded by a thumbnail in 11.29.20), `NO_KEY_RESULTS` (dashboards, maps) |
| `visual` | none | OCR only | — |

`key_results` for `orbit_screenshot` also carries `status` (`NOMINAL`/`WARN`/`ANOMALY` as shown) and, when SNR rows
exist, `snr_ratio` and `anomaly` (`snr > snr_threshold`, computed here, not read).

### Errors
| Status | When |
|---|---|
| 401 | `X-API-Key` missing or wrong |
| 413 | request body over `4/3 × VISION_MAX_IMAGE_BYTES + 64 KiB` (checked from `Content-Length`, and while streaming, before the JSON is parsed), `image_b64` longer than `4/3 × VISION_MAX_IMAGE_BYTES`, or decoded bytes > `VISION_MAX_IMAGE_BYTES` (default 12 MB). Base64 length is checked before decoding |
| 422 | invalid base64, undecodable image, a format other than JPEG/PNG/WebP (BMP, GIF, TIFF, ... are refused), more than `VISION_MAX_PIXELS` pixels (default 12 MP; checked from the image header before the raster is decoded), or a schema error |
| 503 | `VISION_MAX_CONCURRENCY` analyses already running and no slot freed within `VISION_QUEUE_TIMEOUT_S` (`Retry-After: 1`). Cache hits never wait |
| 200 + `READER_FAILED` | the reader raised; `readings` empty, `ocr` still filled when OCR worked |

## `POST /v1/ocr`
```http
POST /v1/ocr
{"image_b64": "...", "upscale": null}     // null = auto (3× when height < 300 px); true/false forces it
→ 200
{"words": [{"text": "SNR Value", "bbox": [x0, y0, x1, y1], "conf": 0.99}], "width": 3024, "height": 1676,
 "latency_ms": 640}
```
Same limits and errors as `/v1/vision`. Images are OCR'd at ≤2000 px long side (RapidOCR's limit); boxes are mapped
back to original pixels.

## `GET /health`
`{"status": "ok", "ocr_ready": true, "version": "0.2.0"}` (always 200 once the process is up; `ocr_ready=false` until
the OCR models have loaded).

## `GET /v1/info`
`{"version": {...}, "readers": ["thermal", "gauge", "spectrogram", "orbit_screenshot", "acoustic_imager", "visual"],
"kinds": [...], "action_type_map": {...}, "tolerances": {"temperature_c": 4.0, "pressure_psi": 6.0,
"pressure_fs_frac": 0.03, "sound_db": 3.0, "visual_points": 10.0}, "limits": {"max_image_bytes": ..., "max_pixels": ...,
"max_body_bytes": ..., "work_max_side": 1280, "max_concurrency": 2, "formats": ["JPEG", "PNG", "WEBP"]}}`

## Environment
| Var | Default | Meaning |
|---|---|---|
| `VISION_HOST` | `127.0.0.1` (Docker image: `0.0.0.0`) | bind address |
| `VISION_PORT` | `8084` | port (the image's HEALTHCHECK uses it too) |
| `VISION_API_KEY` | none (refuses to start) | required shared secret (`X-API-Key`), >= 16 printable ASCII characters |
| `VISION_ALLOW_NO_AUTH` | `0` | `1` = start without a key, every route open (tests only) |
| `VISION_MAX_IMAGE_BYTES` | `12000000` | max decoded image bytes (the request-body cap is derived from it) |
| `VISION_MAX_PIXELS` | `12000000` | max width × height, read from the image header before decoding |
| `VISION_MAX_CONCURRENCY` | `2` | analyses (`/v1/vision`, `/v1/ocr`) running at once; bounds peak memory |
| `VISION_QUEUE_TIMEOUT_S` | `3` | how long a request waits for a free slot before 503 |
| `VISION_WORK_MAX_SIDE` | `1280` | readers process a copy downscaled to this long side |
| `VISION_CACHE_SIZE` | `64` | in-memory LRU of responses keyed by sha256(image) + request options + version; 0 disables |
| `VISION_OCR_THREADS` | `-1` | onnxruntime intra-op threads (-1 = all cores) |
| `VISION_LOG_LEVEL` | `info` | uvicorn log level |

## Feeding System 2 (for the orchestrator)
The orchestrator (`docs/orchestrator-contract.md`, "Vision rows sent to System 2") turns each reading into one
`context.vision[]` row, for example:
`{"id": "V1", "tool": "colorbar_lut", "kind": "thermal", "quantity": "max_temperature", "value": 100.0, "unit": "°C",
"confidence": 0.8, "trust": "HIGH", "flags": ["CLIPPED", "VISION_CLIPPED"], "bound": "lower", "pairing": "linked",
"logged_id": "reading:R0042", "logged_value": 83.5, "delta": 16.5, "agrees": true}`
- `trust` is `HIGH` when `confidence >= 0.5`, else `LOW` (System 2 drops `value`/`delta`/`agrees` from LOW rows).
  A reading flagged `AMBIGUOUS_NEEDLE` or `DEFAULT_SWEEP` is always `LOW`, whatever its confidence.
- `flags`: the reading's flags (plus the comparison's flags on the compared reading), uppercase tokens, at most 8.
  `CLIPPED` keeps `trust: HIGH` but adds `"bound": "lower"`: the value is the top of the scale, so the true value is
  at least that. `RELATIVE_ONLY` values are relative levels, not absolute dB.
- `pairing`: `linked` = the operator's own image of this asset; `demo` = a kit sample image chosen only by action type
  (`DEMO_IMAGES=1`). **A demo image is not a reading of this asset**: the orchestrator does not send `logged` for it
  (so there is no `comparison` and no `vision.compare` alert), drops any comparison fields, and moves the image's
  `alert` to `vision.demo_alert`. System 2 ignores `agrees`/`delta`/`logged_*` on demo rows.
- Comparison fields (`logged_id`, `logged_value`, `delta`, `agrees`) only on the compared reading of a `linked` image.
- OCR lines go to `context.ocr[]` as `{text, conf}`. Send `annotated_image_b64` (not the raw upload) as `image_b64`,
  so the `V#` labels on the image match the evidence rows.
- Prompt rule (System 2 `VISION_RULES`): do not read numbers off the image; if `agrees=false`, cite both IDs; demo rows
  are illustrative only; `bound=lower` values are lower bounds.
