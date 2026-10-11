// ─────────────────────────────────────────────────────────────────────────────
// The Analytics page's numbers, one block per chart.
//
// Computed from the kit CSVs — the same rows the backend seeds its actions
// table with — so every chart here works with or without a live backend. Each
// block is plain JSON so it can cross into the Client Component that draws it.
//
// SERVER ONLY (imports @/lib/kit, which reads the filesystem). Client code
// takes `import type { FleetAnalytics }` and nothing else.
// ─────────────────────────────────────────────────────────────────────────────

import { getKit, type Action } from "./kit";
import type { Severity } from "./types";
import { BATTERY_BANDS } from "./breakdown-link";

const SEVERITIES: Severity[] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

/** The backend's five bands (`BATTERY_BAND_SQL`), so the two never disagree. */
const BANDS = BATTERY_BANDS;

const ACTION_LABELS: Record<string, string> = {
  NAVIGATION: "Navigation",
  VISUAL_INSPECTION: "Visual",
  THERMAL_INSPECTION: "Thermal",
  ACOUSTIC_INSPECTION: "Acoustic",
  GAUGE_READING: "Gauge",
};

/** ABNORMAL_TEMPERATURE → "Abnormal temperature" */
export function humanize(token: string) {
  const text = token.toLowerCase().replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export const actionLabel = (type: string) => ACTION_LABELS[type] ?? humanize(type);

export const MEASUREMENT_LABELS: Record<string, string> = {
  SOUND_DB: "Sound (dB)",
  PRESSURE_PSI: "Pressure (psi)",
  LOCALIZATION_CONFIDENCE_PCT: "Localization conf.",
  TEMPERATURE_C: "Temperature (°C)",
  VISUAL_SCORE: "Visual score",
};

export function pct(part: number, whole: number) {
  return whole === 0 ? 0 : Math.round((part / whole) * 1000) / 10;
}

/** 95% Wilson interval, in percent — the same formula as the backend's
    `wilson()`, so a band's whiskers match what `/insights/reliability` says. */
function wilson(successes: number, n: number): [number, number] {
  if (n === 0) return [0, 0];
  const z = 1.96;
  const p = successes / n;
  const den = 1 + (z * z) / n;
  const centre = (p + (z * z) / (2 * n)) / den;
  const half = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / den;
  return [Math.round(Math.max(0, centre - half) * 1000) / 10, Math.round(Math.min(1, centre + half) * 1000) / 10];
}

function successOf(rows: Action[]) {
  return rows.filter((a) => a.status === "SUCCESS").length;
}

/** One action on the duration-vs-battery scatter; `mission` lets a click open it in its mission. */
export interface ScatterPoint { battery: number; duration: number; id: string; mission: string }

export interface FleetAnalytics {
  /** Success by battery band, with the Wilson interval drawn as a ribbon. */
  battery: { key: string; band: string; rate: number; ci: [number, number]; n: number }[];
  /** V1 vs V2 success, per action type. */
  config: { key: string; type: string; V1: number | null; V2: number | null; nV1: number; nV2: number }[];
  /** Mean battery drain per mission, per configuration. */
  drain: { version: string; drain: number; missions: number }[];
  /** One row per mission in time order: action outcomes and the battery it spent. */
  missions: {
    id: string;
    version: string;
    succeeded: number;
    failed: number;
    battery: [number, number];
    reported: string;
  }[];
  /** What a COMPLETED status was actually worth. */
  truth: { key: string; label: string; count: number }[];
  /** Why actions failed. */
  failureModes: { key: string; mode: string; count: number }[];
  /** Anomaly type × severity, for a stacked bar. */
  anomalies: ({ key: string; type: string; total: number } & Record<Severity, number>)[];
  /** Waypoint × action type failure rate. Null where the pair never happened. */
  heatmap: {
    types: { key: string; label: string }[];
    rows: { id: string; name: string; environment: string; n: number; rate: number; cells: ({ rate: number; n: number } | null)[] }[];
  };
  /** Success by the waypoint's environment. */
  environments: { key: string; environment: string; rate: number; n: number }[];
  /** Each action as a point: battery at the time vs how long it took. */
  scatter: { success: ScatterPoint[]; failed: ScatterPoint[] };
  /** Share of readings the robot marked QUESTIONABLE, by measurement. */
  readings: { key: string; measurement: string; good: number; questionable: number }[];
  /** What the numbers are, for the page's header. */
  coverage: string;
}

export function fleetAnalytics(): FleetAnalytics {
  const kit = getKit();
  const { actions, missions, anomalies, readings } = kit;

  const battery = BANDS.map(({ key, band, max }, i) => {
    const min = i === 0 ? -Infinity : BANDS[i - 1].max;
    const rows = actions.filter((a) => a.battery_pct > min && a.battery_pct <= max);
    const ok = successOf(rows);
    return { key, band, rate: pct(ok, rows.length), ci: wilson(ok, rows.length), n: rows.length };
  }).filter((row) => row.n > 0);

  const types = [...new Set(actions.map((a) => a.action_type))].sort();
  const config = types.map((type) => {
    const v1 = actions.filter((a) => a.action_type === type && a.configuration_version === "V1");
    const v2 = actions.filter((a) => a.action_type === type && a.configuration_version === "V2");
    return {
      key: type,
      type: actionLabel(type),
      V1: v1.length ? pct(successOf(v1), v1.length) : null,
      V2: v2.length ? pct(successOf(v2), v2.length) : null,
      nV1: v1.length,
      nV2: v2.length,
    };
  });

  const drain = [...new Set(missions.map((m) => m.configuration_version))].sort().map((version) => {
    const rows = missions.filter((m) => m.configuration_version === version);
    const total = rows.reduce((sum, m) => sum + (m.start_battery_pct - m.end_battery_pct), 0);
    return { version, drain: Math.round(total / rows.length), missions: rows.length };
  });

  const ordered = [...missions].sort((a, b) => a.start_time.localeCompare(b.start_time));
  const missionRows = ordered.map((m) => {
    const inside = kit.actionsByMission.get(m.mission_id) ?? [];
    const failed = inside.filter((a) => a.status === "FAILED").length;
    return {
      id: m.mission_id,
      version: m.configuration_version,
      succeeded: inside.length - failed,
      failed,
      battery: [m.end_battery_pct, m.start_battery_pct] as [number, number],
      reported: m.mission_status,
    };
  });

  const hiding = missionRows.filter((m) => m.reported === "COMPLETED" && m.failed > 0).length;
  const clean = missionRows.filter((m) => m.reported === "COMPLETED" && m.failed === 0).length;
  const truth = [
    { key: "clean", label: "Completed, clean", count: clean },
    { key: "hiding", label: "Completed, hiding failures", count: hiding },
    { key: "failed", label: "Reported failed", count: missionRows.length - clean - hiding },
  ].filter((slice) => slice.count > 0);

  const modes = new Map<string, number>();
  for (const a of actions) if (a.status === "FAILED") {
    const key = a.failure_type || "UNSPECIFIED";
    modes.set(key, (modes.get(key) ?? 0) + 1);
  }
  const failureModes = [...modes].map(([key, count]) => ({ key, mode: key === "UNSPECIFIED" ? "Unspecified" : humanize(key), count }))
    .sort((a, b) => b.count - a.count);

  const anomalyRows = [...new Set(anomalies.map((a) => a.anomaly_type))].map((type) => {
    const rows = anomalies.filter((a) => a.anomaly_type === type);
    const bySeverity = Object.fromEntries(SEVERITIES.map((s) => [s, rows.filter((a) => a.severity === s).length])) as Record<Severity, number>;
    return { key: type, type: humanize(type), total: rows.length, ...bySeverity };
  }).sort((a, b) => b.total - a.total);

  const heatRows = kit.locations
    .map((location) => {
      const here = actions.filter((a) => a.location_id === location.location_id);
      const cells = types.map((type) => {
        const rows = here.filter((a) => a.action_type === type);
        return rows.length ? { rate: pct(rows.length - successOf(rows), rows.length), n: rows.length } : null;
      });
      return {
        id: location.location_id,
        name: location.location_name,
        environment: humanize(location.environment),
        n: here.length,
        rate: pct(here.length - successOf(here), here.length),
        cells,
      };
    })
    .filter((row) => row.n > 0)
    // Worst waypoint at the top, so the hot rows are the first ones read.
    .sort((a, b) => b.rate - a.rate || b.n - a.n);

  const environments = [...new Set(kit.locations.map((l) => l.environment))].map((environment) => {
    const ids = new Set(kit.locations.filter((l) => l.environment === environment).map((l) => l.location_id));
    const rows = actions.filter((a) => ids.has(a.location_id));
    return { key: environment, environment: humanize(environment), rate: pct(successOf(rows), rows.length), n: rows.length };
  }).filter((row) => row.n > 0).sort((a, b) => b.rate - a.rate);

  const point = (a: Action): ScatterPoint => ({ battery: a.battery_pct, duration: a.duration_sec, id: a.action_id, mission: a.mission_id });
  const scatter = {
    success: actions.filter((a) => a.status === "SUCCESS").map(point),
    failed: actions.filter((a) => a.status === "FAILED").map(point),
  };

  const readingRows = [...new Set(readings.map((r) => r.measurement_type))].map((type) => {
    const rows = readings.filter((r) => r.measurement_type === type);
    const questionable = rows.filter((r) => r.quality === "QUESTIONABLE").length;
    return { key: type, measurement: MEASUREMENT_LABELS[type] ?? humanize(type), good: rows.length - questionable, questionable };
  }).sort((a, b) => b.questionable / (b.good + b.questionable) - a.questionable / (a.good + a.questionable));

  return {
    battery,
    config,
    drain,
    missions: missionRows,
    truth,
    failureModes,
    anomalies: anomalyRows,
    heatmap: { types: types.map((key) => ({ key, label: actionLabel(key) })), rows: heatRows },
    environments,
    scatter,
    readings: readingRows,
    coverage: `${missions.length} missions · ${actions.length} actions · ${anomalies.length} anomalies · ${readings.length} readings`,
  };
}
