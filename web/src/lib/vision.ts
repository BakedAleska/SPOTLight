// ─────────────────────────────────────────────────────────────────────────────
// Inspection imagery for the Assets page.
//
// The vision service (`amd/vision`, :8084) is the thing that reads a SPOT
// inspection image: RapidOCR plus code readers for thermal colorbars, dial
// gauges, spectrograms and Orbit screenshots. Its core rule is the one this
// page is built to show — **code computes every number, the model explains
// it** (`research/tech-spot-vision-cv-2026-10-10.md` §3). So the viewer's
// subject is the `V#` rows and where on the image each one was measured.
//
// WHERE THE ANALYSES COME FROM. `vision-samples.json` holds real
// `POST /v1/vision` responses for the six kit sample images, produced by
// `amd/vision/scripts/run_kit.py` and committed, not hand-written. They are
// the floor: the page always has something true to show, with no service
// running and no GPU. `BACKEND_VISION_PATH` lifts it to live (see the hook
// below).
//
// SERVER ONLY. It imports @/lib/api, which reads BACKEND_URL.
// ─────────────────────────────────────────────────────────────────────────────

import { unstable_rethrow } from "next/navigation";
import { connection } from "next/server";
import * as api from "./api";
import samples from "./vision-samples.json";
import type {
  InspectionImage,
  KeyResult,
  ModelLane,
  OcrLine,
  Severity,
  VisionAlert,
  VisionReading,
} from "./types";

// ── The live hook ────────────────────────────────────────────────────────────
//
// The console is a pure HTTP client of the backend (`research/decisions.md`,
// 2026-10-10): it must not call :8084 itself, because the vision service's key
// is a server secret, demo/linked pairing is the orchestrator's decision to
// make, and a second caller would be a second place that decides what an image
// means. So the hook is one backend path, and the backend proxies.
//
// That route does not exist yet — `docs/backend-services-contract.md` lists
// `assets` with no media sub-resource — which is why this is unset by default
// and the page renders the committed run. Setting it is the whole switch: the
// mapper below already takes the contract's response shape, so when the route
// lands, nothing in this file or the viewer changes.
//
// Read per call, not once at module scope. The console is built into an image
// and configured by compose, so this variable's value at `next build` time
// (unset) is not its value in the container — and a module-level constant
// would bake the build's answer in. `getInspectionBoard` pairs this with
// `connection()` for the same reason.
const livePath = () => process.env.BACKEND_VISION_PATH?.trim() || null;

/** Analysing an image is OCR plus a reader pass — about a second on CPU per
    the service's own numbers, but it queues at `VISION_MAX_CONCURRENCY` 2. */
const liveTimeoutMs = () => Number(process.env.BACKEND_VISION_TIMEOUT_MS ?? 20_000);

// ── Wire types ───────────────────────────────────────────────────────────────
//
// `POST /v1/vision` (`docs/vision-contract.md`), as the committed samples
// store it and as the backend proxy is expected to pass it through. A subset,
// like @/lib/api's: fields the viewer doesn't draw are left unnamed so adding
// one upstream can never break this file.

interface WireReading {
  id: string;
  quantity: string;
  value: number | null;
  unit?: string;
  confidence: number;
  method: string;
  flags?: string[];
  note?: string;
  bbox?: number[];
  point?: number[];
}

interface WireVision {
  media_id: string;
  kind: string;
  router?: { label?: string; method?: string; confidence?: number };
  ocr?: { text: string; bbox: number[]; conf: number }[];
  readings?: WireReading[];
  key_results?: { name: string; units?: string; value: unknown }[];
  alert?: { title: string; severity: number; source: string } | null;
  flags?: string[];
  /** Added to the committed samples; the service reports size only on
      `/v1/ocr`, so a live response may not carry it — see `dimensionsFor`. */
  width?: number;
  height?: number;
}

const SAMPLE_RESPONSES = samples.responses as unknown as Record<string, WireVision>;

// ── The catalogue ────────────────────────────────────────────────────────────
//
// Which images exist, what to call them, and which action type the
// orchestrator pairs each with. This is structure, not data, so it lives in
// code: the files are committed under `web/public/kit/inspection/`, and the
// pairing mirrors `DEMO_IMAGES` in `amd/orchestrator/orchestrator/assess.py`.
//
// `actionType: null` is not an oversight. The orchestrator pairs exactly one
// sample per action type, so two of the kit's three visual images are never
// chosen by it — and saying so is more useful than implying all six are in
// play. Pairing is `demo` for every one of them: no CSV row in the kit links
// to an image (`research/tech-spot-vision-cv-2026-10-10.md` §2), so none of
// these is a photograph of any asset in the database.

interface Sample {
  mediaId: string;
  title: string;
  /** Mirrors the orchestrator's `DEMO_IMAGES`; null = it pairs with none. */
  actionType: string | null;
}

const CATALOGUE: Sample[] = [
  { mediaId: "thermal_pump_bearing_01", title: "Pump bearing, thermal", actionType: "THERMAL_INSPECTION" },
  { mediaId: "gauge_pressure_01", title: "Pressure gauge", actionType: "GAUGE_READING" },
  { mediaId: "acoustic_valve_01", title: "Valve, acoustic", actionType: "ACOUSTIC_INSPECTION" },
  { mediaId: "visual_pump_motor_01", title: "Pump motor, visual", actionType: "VISUAL_INSPECTION" },
  { mediaId: "valve_detail_01", title: "Valve detail", actionType: null },
  { mediaId: "pipe_gallery_01", title: "Pipe gallery", actionType: null },
];

const RAW_DIR = "/kit/inspection";
const ANNOTATED_DIR = "/kit/inspection/annotated";

/** The viewer draws its overlay in image pixels, so it needs the frame's size
    before the bytes load. The committed samples carry it; a live response does
    not (the service reports size only on `/v1/ocr`), so fall back to the
    sample's, which is the same file. */
function dimensionsFor(mediaId: string, wire: WireVision) {
  const known = SAMPLE_RESPONSES[mediaId];
  return {
    width: wire.width ?? known?.width ?? 0,
    height: wire.height ?? known?.height ?? 0,
  };
}

// ── Mapping ──────────────────────────────────────────────────────────────────

/** The handful of quantity names that snake_case can't be typeset out of —
    initialisms and a band label the generic rule below would mangle. */
const LABELS: Record<string, string> = {
  delta_t: "Delta T",
  p95_temperature: "95th-percentile temperature",
  snr_value: "SNR value",
  snr_threshold: "SNR threshold",
  spl_at_source: "SPL at source",
  high_frequency_ratio: "High-frequency ratio",
  anomaly_prediction: "Anomaly prediction",
};

/** `max_temperature` → `Max temperature`, `band_energy_0_2khz` → `Band energy
    0–2 kHz`. The machine name stays on screen beside the label, because it is
    what the contract, the packets and the fine-tune's targets all use — this
    is for reading, not for referring to. */
function humanise(quantity: string) {
  const known = LABELS[quantity];
  if (known) return known;

  const words = quantity
    // `_0_2khz` is one token meaning a frequency band, not three words.
    .replace(/_(\d+)_(\d+)khz/g, " $1–$2 kHz")
    .replace(/(\d)khz/g, "$1 kHz")
    .replace(/_/g, " ")
    .trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The service's `confidence` cut, copied from the orchestrator's
    `VISION_TRUST_MIN`, plus the two flags that mean the reader could not tell
    which value it was looking at. Those are LOW however confident the number
    looks — an ambiguous needle with 0.9 confidence is still a guess between
    two readings (`docs/vision-contract.md`, "Feeding System 2"). */
const TRUST_MIN = 0.5;
const FORCE_LOW = new Set(["AMBIGUOUS_NEEDLE", "DEFAULT_SWEEP"]);

/** 1–4 from the readers (ΔT bands, then the bearing alarm at ≥95 °C —
    `amd/vision/vision/thermal.py`), onto the console's own scale so one badge
    component serves flags and image alerts alike. */
const ALERT_SEVERITY: Severity[] = ["LOW", "LOW", "MEDIUM", "HIGH", "CRITICAL"];

function pair(values: number[] | undefined, n: number) {
  return values && values.length === n ? values : undefined;
}

function toReading(wire: WireReading): VisionReading {
  const flags = wire.flags ?? [];
  const trust =
    wire.confidence >= TRUST_MIN && !flags.some((f) => FORCE_LOW.has(f)) ? "HIGH" : "LOW";

  return {
    id: wire.id,
    quantity: wire.quantity,
    label: humanise(wire.quantity),
    // Dropped on a LOW row for the same reason System 2 drops it before the
    // prompt: if the model may not cite the number, an operator reading over
    // its shoulder should not be handed it either. The note survives, and the
    // note is where the reader says what it saw instead.
    value: trust === "HIGH" ? wire.value : null,
    unit: wire.unit ?? "",
    confidence: wire.confidence,
    trust,
    method: wire.method,
    flags,
    note: wire.note,
    bbox: pair(wire.bbox, 4) as InspectionImage["readings"][number]["bbox"],
    point: pair(wire.point, 2) as InspectionImage["readings"][number]["point"],
  };
}

function toOcr(wire: NonNullable<WireVision["ocr"]>): OcrLine[] {
  return wire
    .filter((line) => pair(line.bbox, 4))
    .map((line) => ({
      text: line.text,
      conf: line.conf,
      bbox: line.bbox as [number, number, number, number],
    }));
}

function toKeyResults(wire: NonNullable<WireVision["key_results"]>): KeyResult[] {
  return wire.map((row) => ({
    name: row.name,
    units: row.units ?? "",
    value: typeof row.value === "number" ? String(row.value) : String(row.value ?? ""),
  }));
}

function toAlert(wire: WireVision["alert"]): VisionAlert | null {
  if (!wire) return null;
  return {
    title: wire.title,
    severity: ALERT_SEVERITY[Math.min(Math.max(wire.severity, 0), 4)],
    source: wire.source,
  };
}

function toImage(sample: Sample, wire: WireVision): InspectionImage {
  const { width, height } = dimensionsFor(sample.mediaId, wire);

  return {
    mediaId: sample.mediaId,
    title: sample.title,
    src: `${RAW_DIR}/${sample.mediaId}.jpg`,
    annotated: `${ANNOTATED_DIR}/${sample.mediaId}.jpg`,
    width,
    height,
    kind: wire.kind,
    router: {
      method: wire.router?.method ?? "default",
      confidence: wire.router?.confidence ?? 0,
    },
    actionType: sample.actionType,
    pairing: "demo",
    readings: (wire.readings ?? []).map(toReading),
    ocr: toOcr(wire.ocr ?? []),
    keyResults: toKeyResults(wire.key_results ?? []),
    alert: toAlert(wire.alert),
    flags: wire.flags ?? [],
  };
}

// ── The pipeline lanes ───────────────────────────────────────────────────────
//
// Named models, not "AI": the rubric asks which tools we used, and the split
// between them is the project's actual argument. The two Qwen slots are the
// image side of the stack — one reads the picture qualitatively, one writes
// the assessment — and both sit behind the LiteLLM gateway on :4000, so
// swapping either is a gateway alias change, not a code change.
//
// They are `pending` here because this page has no model output to show yet:
// System 2's answer arrives with an escalated flag (`/flags/{id}/explain`),
// not with an image on its own. The slots are drawn anyway so the viewer
// states what is and isn't a model's work — and so the moment the backend
// returns model text for an image, there is already a place to put it.

/** The gateway aliases (`docker-compose.yml`, LiteLLM). Rename here when the
    fine-tuned adapter replaces the stock weights — this is the only place the
    console names a model. */
const DESCRIBE_MODEL = "Qwen3-VL-8B-Instruct";
const ANALYST_MODEL = "Qwen3.5-9B Q4_K_M + mmproj (gateway `system2-vision`)";

export function visionLanes(): ModelLane[] {
  return [
    {
      id: "readers",
      stage: "Code readers",
      model: "RapidOCR 3.10 (PP-OCR, ONNX CPU) + OpenCV",
      role: "Computes every number on this page and marks where it measured it.",
      state: "live",
      note:
        "Deterministic and testable. VLMs score 25–30% reading real gauges and fail " +
        "temperature-grounded questions outright, so no model is allowed to read a value off pixels.",
    },
    {
      id: "describe",
      stage: "Qwen vision",
      model: DESCRIBE_MODEL,
      role: "Says what is visible: which component is hot, the scale's labels and units, glare or occlusion.",
      state: "pending",
      note:
        "Qualitative only, and never a number. Fine-tuned on thermal and gauge sets " +
        "(docs/TEAMMATE-qwen-vision-finetune.md); serves behind the gateway on the AMD box.",
    },
    {
      id: "analyst",
      stage: "Qwen analyst",
      model: ANALYST_MODEL,
      role: "Writes the Orbit-style assessment — question, answer, reasoning, recommended action — citing V# rows.",
      state: "pending",
      note:
        "Receives the annotated JPEG plus these rows, not the raw upload, so the marks it sees " +
        "carry the same V# IDs it has to cite. Runs on an escalated flag, not on an image alone.",
    },
  ];
}

// ── Reading the board ────────────────────────────────────────────────────────

export interface InspectionBoard {
  images: InspectionImage[];
  lanes: ModelLane[];
  /** `sample` = the committed kit run. `live` = analysed through the backend
      on this request. */
  source: "sample" | "live";
  /** Why the live path didn't run, when it was configured and failed. Null
      when it is simply not configured — that is the normal state, not a
      fault, and the page says so in its own words. */
  degraded: string | null;
}

/** One image, analysed live. Exported because it is the seam a detail page or
    an upload flow would call; `getInspectionBoard` is just this over the
    catalogue. Throws on any backend trouble — callers decide the fallback. */
export async function analyzeInspectionImage(mediaId: string): Promise<WireVision> {
  const path = livePath();
  if (!path) throw new Error("BACKEND_VISION_PATH is not set");
  return api.post<WireVision>(
    path,
    { media_id: mediaId, kind: "auto", annotate: true },
    { timeoutMs: liveTimeoutMs() },
  );
}

/**
 * Every inspection image with its analysis.
 *
 * With no live path configured this is pure: the committed responses, mapped.
 * With one, every image is re-analysed in parallel and the committed run
 * becomes the per-image fallback — one dead reader degrades one card, never
 * the page, which is the same bargain @/lib/dashboard makes with the flags.
 */
export async function getInspectionBoard(): Promise<InspectionBoard> {
  // Stop prerendering here. With no live path this function is pure, so
  // `cacheComponents` would happily fold the whole page into build-time HTML
  // — and a container started with BACKEND_VISION_PATH set would then serve
  // the committed samples for ever, because nothing would ever read the
  // variable again. The page's shell still prerenders; only the board waits.
  await connection();

  const lanes = visionLanes();

  /** The committed run for one catalogue entry. Null only if the JSON and the
      catalogue have drifted apart, which drops that one card rather than
      throwing the page away over a missing sample. */
  const fromSample = (sample: Sample) => {
    const wire = SAMPLE_RESPONSES[sample.mediaId];
    return wire ? toImage(sample, wire) : null;
  };

  if (!livePath()) {
    const images = CATALOGUE.map(fromSample).filter((i) => i !== null);
    return { images, lanes, source: "sample", degraded: null };
  }

  let failure: string | null = null;
  const analysed = await Promise.all(
    CATALOGUE.map(async (sample) => {
      try {
        return toImage(sample, await analyzeInspectionImage(sample.mediaId));
      } catch (cause) {
        unstable_rethrow(cause);
        // First failure wins the banner: six timeouts against one dead service
        // are one fact, and printing all six would say it six times.
        failure ??= cause instanceof Error ? cause.message : String(cause);
        return fromSample(sample);
      }
    }),
  );

  return {
    images: analysed.filter((i) => i !== null),
    lanes,
    source: failure ? "sample" : "live",
    degraded: failure,
  };
}
