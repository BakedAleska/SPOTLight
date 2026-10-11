// ─────────────────────────────────────────────────────────────────────────────
// The shapes the dashboard renders.
//
// Deliberately free of imports: a Client Component can take these types
// without dragging the loader — and `node:fs` with it — into the browser
// bundle. Live dashboard data is adapted by @/lib/backend.
//
// Backend response fields are mapped to these presentation shapes.
// ─────────────────────────────────────────────────────────────────────────────

export type Severity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

/** One rule that fired, and what it contributed to the score. */
export interface Reason {
  /** Stable rule id, e.g. "status.mismatch". Lets a flag be audited. */
  rule: string;
  /** The operator-readable sentence. */
  detail: string;
  /** Backend policy weight; backend score also includes priority/confidence. */
  weight: number;
}

export interface Flag {
  id: string;
  missionId: string;
  severity: Severity;
  /** Backend priority score (0–100), independent of the reason weights. */
  score: number;
  title: string;
  assetName: string;
  locationName: string;
  reasons: Reason[];
}

export interface Stat {
  label: string;
  value: string;
  unit?: string;
  note: string;
}

export interface BatteryBand {
  band: string;
  successRate: number;
  sampleSize: number;
}

/** One column of a success-rate chart: a group of actions and how many of
    them succeeded. */
export interface SuccessGroup {
  key: string;
  label: string;
  successRate: number;
  sampleSize: number;
}

// ── Inspection imagery ───────────────────────────────────────────────────────
//
// The Assets page's shapes. These mirror `POST /v1/vision`
// (`docs/vision-contract.md`) one level up: the wire response is mapped in
// @/lib/vision, so the viewer never sees a raw service payload and doesn't
// care whether the analysis came from the committed kit run or from a live
// call. Same split as Flag/Stat above — @/lib/dashboard fills those.

/** Whether System 2 may state a reading as fact. `confidence >= 0.5` is HIGH;
    a reading the reader itself is unsure of (`AMBIGUOUS_NEEDLE`,
    `DEFAULT_SWEEP`) is always LOW whatever its confidence. A LOW row reaches
    the model with no value at all, so the viewer shows none either. */
export type Trust = "HIGH" | "LOW";

/** One code-computed number. `V1`, `V2`, … in the reader's own order, which is
    the ID System 2 cites — so these strings are evidence IDs, not labels. */
export interface VisionReading {
  id: string;
  /** The reader's machine name, e.g. `max_temperature`. */
  quantity: string;
  /** The same thing, typeset for a human. */
  label: string;
  /** Null on a LOW-trust row: there is no number anyone may quote. */
  value: number | null;
  unit: string;
  confidence: number;
  trust: Trust;
  /** How it was computed, e.g. `colorbar_lut`. The audit trail in one word. */
  method: string;
  /** Reader caveats: CLIPPED, AMBIGUOUS_NEEDLE, RELATIVE_ONLY, … */
  flags: string[];
  /** The reader's own sentence about this value, when it wrote one. */
  note?: string;
  /** Where it was measured, in ORIGINAL image pixels: `[x0, y0, x1, y1]`. */
  bbox?: [number, number, number, number];
  /** A single point, in original image pixels. */
  point?: [number, number];
}

/** One line of text read off the image. Context only: never evidence, never
    citable. A number that appears only here cannot be stated by System 2. */
export interface OcrLine {
  text: string;
  conf: number;
  bbox: [number, number, number, number];
}

/** The reader's summary table — the same shape Orbit prints under a finding. */
export interface KeyResult {
  name: string;
  units: string;
  value: string;
}

export interface VisionAlert {
  title: string;
  severity: Severity;
  /** e.g. `vision.thermal` — which reader raised it. */
  source: string;
}

/** `linked` = the operator's own image of this asset, comparable with its
    logged reading. `demo` = a kit sample picked by action type, which is
    illustrative and never judges an asset (`research/decisions.md`). */
export type Pairing = "demo" | "linked";

export interface InspectionImage {
  mediaId: string;
  title: string;
  /** Public paths. `annotated` is the service's own JPEG, V# marks drawn on —
      the exact bytes System 2 receives. Null when it wasn't produced. */
  src: string;
  annotated: string | null;
  /** Natural size of `src`, so the overlay can use image pixels directly. */
  width: number;
  height: number;
  /** The reader that ran: thermal | gauge | spectrogram | visual | … */
  kind: string;
  /** How the router picked it, and how sure it was. */
  router: { method: string; confidence: number };
  /** The action type the orchestrator pairs this sample with, or null when it
      pairs with none (`DEMO_IMAGES` in `amd/orchestrator/.../assess.py`). */
  actionType: string | null;
  pairing: Pairing;
  readings: VisionReading[];
  ocr: OcrLine[];
  keyResults: KeyResult[];
  /** What this image alone would raise. On a `demo` image it describes the
      sample, not an asset — the orchestrator files it as `demo_alert`. */
  alert: VisionAlert | null;
  /** Whole-response caveats, e.g. NO_TEXT, READER_FAILED. */
  flags: string[];
}

/** One stage of the image pipeline, for the lane strip under the viewer.
    `live` = it produced what is on screen; `pending` = wired but not yet
    answering, so the viewer shows the slot and no output. */
export interface ModelLane {
  id: string;
  stage: string;
  /** The actual model or library, named — the rubric asks for this. */
  model: string;
  role: string;
  state: "live" | "pending";
  note: string;
}
