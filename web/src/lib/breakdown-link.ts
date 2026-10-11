/* Links from an Analytics chart to the rows behind the mark that was clicked
   (/analytics/breakdown). Client-safe: no kit, no filesystem — the charts in
   the browser build these hrefs, the server page parses them. */

export type BreakdownView = "actions" | "missions" | "anomalies" | "readings";

/** Every filter a chart can set. Values are the kit's raw codes (VISUAL_INSPECTION, INDOOR_DUSTY, ...). */
export interface BreakdownQuery {
  view: BreakdownView;
  type?: string;         // action_type
  version?: string;      // configuration_version: V1 | V2
  band?: string;         // battery band key, see BATTERY_BANDS
  env?: string;          // waypoint environment
  waypoint?: string;     // location_id
  mission?: string;      // mission_id
  status?: string;       // SUCCESS | FAILED
  failure?: string;      // failure_type (UNSPECIFIED = failed with none recorded)
  truth?: string;        // clean | hiding | failed (missions view)
  anomaly?: string;      // anomaly_type
  severity?: string;     // LOW | MEDIUM | HIGH | CRITICAL
  measurement?: string;  // measurement_type
  quality?: string;      // GOOD | QUESTIONABLE
  highlight?: string;    // an action_id to mark in the table
}

/** The backend's five bands (BATTERY_BAND_SQL), keyed for URLs. */
export const BATTERY_BANDS: { key: string; band: string; max: number }[] = [
  { key: "0-25", band: "≤25%", max: 25 },
  { key: "26-40", band: "26–40%", max: 40 },
  { key: "41-60", band: "41–60%", max: 60 },
  { key: "61-80", band: "61–80%", max: 80 },
  { key: "81-100", band: "81%+", max: Infinity },
];

export const QUERY_KEYS = ["type", "version", "band", "env", "waypoint", "mission", "status", "failure", "truth",
  "anomaly", "severity", "measurement", "quality", "highlight"] as const;

export function breakdownHref(query: BreakdownQuery): string {
  const params = new URLSearchParams({ view: query.view });
  for (const key of QUERY_KEYS) {
    const value = query[key];
    if (value) params.set(key, value);
  }
  return `/analytics/breakdown?${params.toString()}`;
}
