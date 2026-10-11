import type { SuccessGroup } from "./types";

/* Action success by inspection type: the dashboard's fleet-wide chart.

   Shared by the backend loader and the kit fallback, so live and sample data
   label, order and caption the chart identically. Pure — no server imports —
   so it is safe on either side. */

/** Short axis labels. Anything the backend adds later falls back to a
    title-cased version of its own id rather than disappearing. */
const LABELS: Record<string, string> = {
  NAVIGATION: "Navigation",
  VISUAL_INSPECTION: "Visual",
  THERMAL_INSPECTION: "Thermal",
  ACOUSTIC_INSPECTION: "Acoustic",
  GAUGE_READING: "Gauge",
};

function labelFor(type: string) {
  return LABELS[type] ?? type.toLowerCase().replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
}

export interface ActionSuccess {
  groups: SuccessGroup[];
  /** Success across every action, drawn as the reference line. */
  fleetRate: number | null;
  caption: string;
}

/** `rows` are raw counts per action type. Rates are recomputed from counts so
    the fleet line is weighted by volume, not an average of averages. */
export function summarizeActionSuccess(rows: { type: string; total: number; success: number }[]): ActionSuccess {
  const used = rows.filter((row) => row.total > 0);
  const groups = used
    .map((row) => ({
      key: row.type,
      label: labelFor(row.type),
      successRate: Math.round((row.success / row.total) * 100),
      sampleSize: row.total,
    }))
    // Best first, so the eye runs down the ranking to the weakest type.
    .sort((a, b) => b.successRate - a.successRate || b.sampleSize - a.sampleSize);

  const total = used.reduce((sum, row) => sum + row.total, 0);
  if (total === 0) return { groups: [], fleetRate: null, caption: "No actions recorded yet." };
  const fleetRate = Math.round((used.reduce((sum, row) => sum + row.success, 0) / total) * 100);

  const worst = groups[groups.length - 1];
  const gap = fleetRate - worst.successRate;
  const caption =
    gap > 0
      ? `${worst.label} actions succeed ${worst.successRate}% of the time, ${gap} pts under the fleet's ${fleetRate}% — the type to look at first.`
      : `Every action type is within a point of the fleet's ${fleetRate}% success rate.`;

  return { groups, fleetRate, caption };
}
