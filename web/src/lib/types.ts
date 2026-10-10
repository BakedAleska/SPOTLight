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
