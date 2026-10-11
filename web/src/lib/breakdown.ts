// ─────────────────────────────────────────────────────────────────────────────
// The rows behind one mark on an Analytics chart (/analytics/breakdown).
//
// Filters the same kit rows fleet-analytics.ts counts, so the table under a
// bar always adds up to the bar. SERVER ONLY (reads the kit from disk).
// ─────────────────────────────────────────────────────────────────────────────

import { getKit, type Action, type Anomaly, type Mission, type Reading } from "./kit";
import { BATTERY_BANDS, QUERY_KEYS, type BreakdownQuery, type BreakdownView } from "./breakdown-link";
import { actionLabel, humanize, MEASUREMENT_LABELS, pct } from "./fleet-analytics";

const VIEWS: BreakdownView[] = ["actions", "missions", "anomalies", "readings"];

const TYPE_NOUN: Record<string, string> = {
  NAVIGATION: "navigation actions",
  VISUAL_INSPECTION: "visual inspections",
  THERMAL_INSPECTION: "thermal inspections",
  ACOUSTIC_INSPECTION: "acoustic inspections",
  GAUGE_READING: "gauge readings",
};

const TRUTH_TITLE: Record<string, string> = {
  clean: "Missions completed cleanly",
  hiding: "Missions marked complete but hiding failures",
  failed: "Missions reported failed",
};

export interface ActionRow {
  id: string; mission: string; version: string; when: string; waypoint: string; waypointName: string;
  asset: string; assetName: string; type: string; battery: number; duration: number;
  status: string; failure: string | null; reading: string | null; anomaly: { type: string; severity: string } | null;
}
export interface MissionRow {
  id: string; name: string; version: string; start: string; reported: string;
  succeeded: number; failed: number; anomalies: number; battery: [number, number];
}
export interface AnomalyRow {
  id: string; action: string; mission: string; when: string; waypoint: string; waypointName: string;
  asset: string; assetName: string; type: string; severity: string; review: boolean; description: string;
}
export interface ReadingRow {
  id: string; action: string; mission: string; when: string; waypoint: string; waypointName: string;
  asset: string; measurement: string; value: number; unit: string; quality: string;
}

export interface Breakdown {
  query: BreakdownQuery;
  title: string;
  stats: { label: string; value: string; tone?: "bad" | "ok" }[];
  actions?: ActionRow[];
  missions?: MissionRow[];
  anomalies?: AnomalyRow[];
  readings?: ReadingRow[];
  /** The mission being looked at, when the breakdown is one mission. */
  mission?: { id: string; name: string; reported: string; version: string };
}

/** searchParams -> a query; unknown views fall back to actions, unknown keys are dropped. */
export function parseQuery(raw: Record<string, string | string[] | undefined>): BreakdownQuery {
  const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v)?.trim() || undefined;
  const view = one(raw.view) as BreakdownView | undefined;
  const query: BreakdownQuery = { view: view && VIEWS.includes(view) ? view : "actions" };
  for (const key of QUERY_KEYS) {
    const value = one(raw[key]);
    if (value) query[key] = value.slice(0, 64);
  }
  return query;
}

const avg = (values: number[]) => (values.length ? Math.round(values.reduce((s, v) => s + v, 0) / values.length) : 0);
const fmt = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));

export function breakdown(query: BreakdownQuery): Breakdown {
  const kit = getKit();
  const place = (id: string) => kit.locationById.get(id)?.location_name ?? id;
  const assetName = (id: string) => kit.assetById.get(id)?.asset_name ?? id;
  const band = BATTERY_BANDS.find((b) => b.key === query.band);
  const bandMin = band ? (BATTERY_BANDS.indexOf(band) === 0 ? -Infinity : BATTERY_BANDS[BATTERY_BANDS.indexOf(band) - 1].max) : 0;
  const envIds = query.env ? new Set(kit.locations.filter((l) => l.environment === query.env).map((l) => l.location_id)) : null;

  const actionMatches = (a: Action) =>
    (!query.type || a.action_type === query.type)
    && (!query.version || a.configuration_version === query.version)
    && (!band || (a.battery_pct > bandMin && a.battery_pct <= band.max))
    && (!envIds || envIds.has(a.location_id))
    && (!query.waypoint || a.location_id === query.waypoint)
    && (!query.mission || a.mission_id === query.mission)
    && (!query.status || a.status === query.status)
    && (!query.failure || (a.status === "FAILED" && (a.failure_type || "UNSPECIFIED") === query.failure));

  const qualifiers = () => {
    const q: string[] = [];
    if (band) q.push(`at ${band.band} battery`);
    if (query.env) q.push(`in ${humanize(query.env).toLowerCase()} waypoints`);
    if (query.waypoint) q.push(`at ${query.waypoint} ${place(query.waypoint)}`);
    if (query.version) q.push(`on ${query.version}`);
    if (query.mission) q.push(`in mission ${query.mission}`);
    return q.length ? " " + q.join(" ") : "";
  };

  if (query.view === "missions") {
    const ordered = [...kit.missions].sort((a, b) => a.start_time.localeCompare(b.start_time));
    const rows = ordered.map((m: Mission): MissionRow => {
      const inside = kit.actionsByMission.get(m.mission_id) ?? [];
      const failed = inside.filter((a) => a.status === "FAILED").length;
      return { id: m.mission_id, name: m.mission_name, version: m.configuration_version, start: m.start_time,
        reported: m.mission_status, succeeded: inside.length - failed, failed,
        anomalies: kit.anomalies.filter((x) => x.mission_id === m.mission_id).length,
        battery: [m.start_battery_pct, m.end_battery_pct] };
    }).filter((m) => (!query.version || m.version === query.version)
      && (!query.truth || (query.truth === "clean" ? m.reported === "COMPLETED" && m.failed === 0
        : query.truth === "hiding" ? m.reported === "COMPLETED" && m.failed > 0 : m.reported !== "COMPLETED")));
    const hiding = rows.filter((m) => m.reported === "COMPLETED" && m.failed > 0).length;
    return {
      query,
      title: query.truth ? TRUTH_TITLE[query.truth] ?? "Missions" : `${query.version ? query.version + " " : ""}missions`.replace(/^m/, "M"),
      stats: [
        { label: "Missions", value: String(rows.length) },
        { label: "Hiding failures", value: String(hiding), tone: hiding ? "bad" : undefined },
        { label: "Failed actions", value: String(rows.reduce((s, m) => s + m.failed, 0)) },
        { label: "Mean battery drain", value: `${avg(rows.map((m) => m.battery[0] - m.battery[1]))} pts` },
      ],
      missions: rows,
    };
  }

  if (query.view === "anomalies") {
    const rows = kit.anomalies.filter((x: Anomaly) => (!query.anomaly || x.anomaly_type === query.anomaly)
      && (!query.severity || x.severity === query.severity) && (!query.mission || x.mission_id === query.mission)
      && (!query.waypoint || x.location_id === query.waypoint))
      .sort((a, b) => a.timestamp.localeCompare(b.timestamp))
      .map((x): AnomalyRow => ({ id: x.anomaly_id, action: x.action_id, mission: x.mission_id, when: x.timestamp,
        waypoint: x.location_id, waypointName: place(x.location_id), asset: x.asset_id, assetName: assetName(x.asset_id),
        type: humanize(x.anomaly_type), severity: x.severity, review: x.operator_review_required, description: x.description }));
    const name = query.anomaly ? humanize(query.anomaly) + " anomalies" : "Anomalies";
    const count = (s: string) => rows.filter((r) => r.severity === s).length;
    return {
      query,
      title: `${query.severity ? humanize(query.severity) + " " + name.toLowerCase() : name}${qualifiers()}`,
      stats: [
        { label: "Anomalies", value: String(rows.length) },
        { label: "Critical / high", value: `${count("CRITICAL")} / ${count("HIGH")}`, tone: count("CRITICAL") ? "bad" : undefined },
        { label: "Need operator review", value: String(rows.filter((r) => r.review).length) },
        { label: "Assets affected", value: String(new Set(rows.map((r) => r.asset)).size) },
      ],
      anomalies: rows,
    };
  }

  if (query.view === "readings") {
    const rows = kit.readings.filter((r: Reading) => (!query.measurement || r.measurement_type === query.measurement)
      && (!query.quality || r.quality === query.quality) && (!query.mission || r.mission_id === query.mission))
      .sort((a, b) => a.timestamp.localeCompare(b.timestamp))
      .map((r): ReadingRow => ({ id: r.reading_id, action: r.action_id, mission: r.mission_id, when: r.timestamp,
        waypoint: r.location_id, waypointName: place(r.location_id), asset: r.asset_id,
        measurement: MEASUREMENT_LABELS[r.measurement_type] ?? humanize(r.measurement_type), value: r.value, unit: r.unit, quality: r.quality }));
    const questionable = rows.filter((r) => r.quality === "QUESTIONABLE").length;
    const name = query.measurement ? (MEASUREMENT_LABELS[query.measurement] ?? humanize(query.measurement)) : "All";
    return {
      query,
      title: `${name} readings${query.quality ? " · " + humanize(query.quality).toLowerCase() : ""}${qualifiers()}`,
      stats: [
        { label: "Readings", value: String(rows.length) },
        { label: "Questionable", value: `${questionable} (${pct(questionable, rows.length)}%)`, tone: questionable ? "bad" : undefined },
        { label: "Assets", value: String(new Set(rows.map((r) => r.asset)).size) },
        { label: "Missions", value: String(new Set(rows.map((r) => r.mission)).size) },
      ],
      readings: rows,
    };
  }

  // actions
  const rows = kit.actions.filter(actionMatches).sort((a, b) => a.timestamp.localeCompare(b.timestamp)).map((a): ActionRow => {
    const reading = kit.readingsByAction.get(a.action_id)?.[0];
    const anomaly = kit.anomaliesByAction.get(a.action_id)?.[0];
    return { id: a.action_id, mission: a.mission_id, version: a.configuration_version, when: a.timestamp,
      waypoint: a.location_id, waypointName: place(a.location_id), asset: a.asset_id, assetName: assetName(a.asset_id),
      type: actionLabel(a.action_type), battery: a.battery_pct, duration: a.duration_sec, status: a.status,
      failure: a.status === "FAILED" ? (a.failure_type ? humanize(a.failure_type) : "Unspecified") : null,
      reading: reading ? `${fmt(reading.value)} ${reading.unit}${reading.quality === "QUESTIONABLE" ? " (questionable)" : ""}` : null,
      anomaly: anomaly ? { type: humanize(anomaly.anomaly_type), severity: anomaly.severity } : null };
  });
  const failed = rows.filter((r) => r.status === "FAILED").length;
  const failedOnly = query.status === "FAILED" || !!query.failure;
  const noun = query.type ? TYPE_NOUN[query.type] ?? `${actionLabel(query.type).toLowerCase()} actions` : "actions";
  const head = failedOnly
    ? `Failed ${noun}${query.failure ? `: ${query.failure === "UNSPECIFIED" ? "unspecified" : humanize(query.failure).toLowerCase()}` : ""}`
    : noun.charAt(0).toUpperCase() + noun.slice(1);
  const mission = query.mission ? kit.missionById.get(query.mission) : undefined;
  return {
    query,
    title: mission && !query.type && !failedOnly && !band && !query.env && !query.waypoint
      ? `Mission ${mission.mission_id} · ${mission.mission_name}`
      : head + qualifiers(),
    stats: [
      { label: "Actions", value: String(rows.length) },
      { label: "Success rate", value: `${pct(rows.length - failed, rows.length)}%`, tone: failed / Math.max(1, rows.length) > 0.2 ? "bad" : "ok" },
      { label: "Failed", value: String(failed), tone: failed ? "bad" : undefined },
      { label: "With an anomaly", value: String(rows.filter((r) => r.anomaly).length) },
      { label: "Mean battery", value: `${avg(rows.map((r) => r.battery))}%` },
      { label: "Mean duration", value: `${avg(rows.map((r) => r.duration))} s` },
    ],
    actions: rows,
    mission: mission ? { id: mission.mission_id, name: mission.mission_name, reported: mission.mission_status, version: mission.configuration_version } : undefined,
  };
}
