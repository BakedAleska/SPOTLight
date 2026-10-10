// ─────────────────────────────────────────────────────────────────────────────
// The challenge kit, typed and joined.
//
// Reads data/kit/*.csv and returns the SPOT dataset as typed rows plus the
// lookups every later question needs (asset by id, anomalies by action, …).
// The CSV columns are the ones db/migrations/001_core.sql uses, so the field
// names here are the Neon column names.
//
// THIS IS THE FALLBACK PATH. The console reads the backend (@/lib/api,
// @/lib/dashboard); this module and @/lib/analysis only run when the backend
// can't be reached, behind the banner in @/components/DegradedNotice.
//
// SERVER ONLY. This touches the filesystem, so it must never be reached from a
// Client Component — import types from @/lib/types there instead.
// ─────────────────────────────────────────────────────────────────────────────

import { readFileSync } from "node:fs";
import path from "node:path";
import { parseCsv } from "./csv";
import type { Severity } from "./types";

// ── Row types ────────────────────────────────────────────────────────────────

export type Criticality = Severity;
export type MissionStatus = "COMPLETED" | "FAILED";
export type ActionStatus = "SUCCESS" | "FAILED";
export type Quality = "GOOD" | "QUESTIONABLE";

export interface Location {
  location_id: string;
  location_name: string;
  zone_type: string;
  floor: number;
  x_coord: number;
  y_coord: number;
  /** NORMAL | HIGH_NOISE | LOW_LIGHT | INDOOR_DUSTY | … */
  environment: string;
}

export interface Asset {
  asset_id: string;
  asset_name: string;
  asset_type: string;
  location_id: string;
  criticality: Criticality;
}

export interface Mission {
  mission_id: string;
  mission_name: string;
  /** BASELINE | IMPROVED */
  scenario: string;
  /** V1 | V2 */
  configuration_version: string;
  start_time: string;
  end_time: string;
  duration_min: number;
  start_battery_pct: number;
  end_battery_pct: number;
  /** The *reported* status. Per Data_Assumptions.md it can say COMPLETED
      while actions inside the mission failed — see `hiddenFailureMissions`. */
  mission_status: MissionStatus;
  operator_mode: string;
}

export interface Action {
  action_id: string;
  mission_id: string;
  sequence: number;
  timestamp: string;
  location_id: string;
  asset_id: string;
  action_type: string;
  battery_pct: number;
  status: ActionStatus;
  /** Empty string when the action succeeded. */
  failure_type: string;
  duration_sec: number;
  scenario: string;
  configuration_version: string;
}

export interface Reading {
  reading_id: string;
  action_id: string;
  mission_id: string;
  asset_id: string;
  location_id: string;
  timestamp: string;
  measurement_type: string;
  value: number;
  unit: string;
  quality: Quality;
}

export interface Anomaly {
  anomaly_id: string;
  action_id: string;
  mission_id: string;
  location_id: string;
  asset_id: string;
  timestamp: string;
  anomaly_type: string;
  severity: Severity;
  operator_review_required: boolean;
  description: string;
}

// ── Coercion ─────────────────────────────────────────────────────────────────

/** Numeric columns are never partially filled in this dataset, so a missing or
    unparseable value is a data problem rather than something to paper over. */
function num(row: Record<string, string>, column: string, file: string) {
  const value = Number(row[column]);
  if (!Number.isFinite(value)) {
    throw new Error(
      `${file}: expected a number in column "${column}", got ${JSON.stringify(row[column])}`,
    );
  }
  return value;
}

/** Python's csv writer emits True/False, so accept those alongside true/false. */
function bool(row: Record<string, string>, column: string) {
  return /^(true|1)$/i.test(row[column] ?? "");
}

// ── Load ─────────────────────────────────────────────────────────────────────

const KIT_DIR = path.join(process.cwd(), "data", "kit");

function read(file: string) {
  const full = path.join(KIT_DIR, file);
  try {
    return parseCsv(readFileSync(full, "utf8"));
  } catch (cause) {
    throw new Error(
      `Could not read the challenge kit at ${full}. ` +
        `Copy 02_DATA/*.csv from the SPOT kit into web/data/kit/.`,
      { cause },
    );
  }
}

/** The whole dataset, with the lookups that make joins cheap downstream. */
export interface Kit {
  locations: Location[];
  assets: Asset[];
  missions: Mission[];
  actions: Action[];
  readings: Reading[];
  anomalies: Anomaly[];

  locationById: Map<string, Location>;
  assetById: Map<string, Asset>;
  missionById: Map<string, Mission>;
  actionById: Map<string, Action>;
  /** Readings and anomalies hang off an action — the grain triage works at. */
  readingsByAction: Map<string, Reading[]>;
  anomaliesByAction: Map<string, Anomaly[]>;
  actionsByMission: Map<string, Action[]>;
}

function groupBy<T>(rows: T[], key: (row: T) => string) {
  const out = new Map<string, T[]>();
  for (const row of rows) {
    const k = key(row);
    const bucket = out.get(k);
    if (bucket) bucket.push(row);
    else out.set(k, [row]);
  }
  return out;
}

function indexBy<T>(rows: T[], key: (row: T) => string) {
  return new Map(rows.map((row) => [key(row), row]));
}

function load(): Kit {
  const locations: Location[] = read("locations.csv").map((r) => ({
    location_id: r.location_id,
    location_name: r.location_name,
    zone_type: r.zone_type,
    floor: num(r, "floor", "locations.csv"),
    x_coord: num(r, "x_coord", "locations.csv"),
    y_coord: num(r, "y_coord", "locations.csv"),
    environment: r.environment,
  }));

  const assets: Asset[] = read("assets.csv").map((r) => ({
    asset_id: r.asset_id,
    asset_name: r.asset_name,
    asset_type: r.asset_type,
    location_id: r.location_id,
    criticality: r.criticality as Criticality,
  }));

  const missions: Mission[] = read("missions.csv").map((r) => ({
    mission_id: r.mission_id,
    mission_name: r.mission_name,
    scenario: r.scenario,
    configuration_version: r.configuration_version,
    start_time: r.start_time,
    end_time: r.end_time,
    duration_min: num(r, "duration_min", "missions.csv"),
    start_battery_pct: num(r, "start_battery_pct", "missions.csv"),
    end_battery_pct: num(r, "end_battery_pct", "missions.csv"),
    mission_status: r.mission_status as MissionStatus,
    operator_mode: r.operator_mode,
  }));

  const actions: Action[] = read("actions.csv").map((r) => ({
    action_id: r.action_id,
    mission_id: r.mission_id,
    sequence: num(r, "sequence", "actions.csv"),
    timestamp: r.timestamp,
    location_id: r.location_id,
    asset_id: r.asset_id,
    action_type: r.action_type,
    battery_pct: num(r, "battery_pct", "actions.csv"),
    status: r.status as ActionStatus,
    failure_type: r.failure_type,
    duration_sec: num(r, "duration_sec", "actions.csv"),
    scenario: r.scenario,
    configuration_version: r.configuration_version,
  }));

  const readings: Reading[] = read("readings.csv").map((r) => ({
    reading_id: r.reading_id,
    action_id: r.action_id,
    mission_id: r.mission_id,
    asset_id: r.asset_id,
    location_id: r.location_id,
    timestamp: r.timestamp,
    measurement_type: r.measurement_type,
    value: num(r, "value", "readings.csv"),
    unit: r.unit,
    quality: r.quality as Quality,
  }));

  const anomalies: Anomaly[] = read("anomalies.csv").map((r) => ({
    anomaly_id: r.anomaly_id,
    action_id: r.action_id,
    mission_id: r.mission_id,
    location_id: r.location_id,
    asset_id: r.asset_id,
    timestamp: r.timestamp,
    anomaly_type: r.anomaly_type,
    severity: r.severity as Severity,
    operator_review_required: bool(r, "operator_review_required"),
    description: r.description,
  }));

  return {
    locations,
    assets,
    missions,
    actions,
    readings,
    anomalies,

    locationById: indexBy(locations, (l) => l.location_id),
    assetById: indexBy(assets, (a) => a.asset_id),
    missionById: indexBy(missions, (m) => m.mission_id),
    actionById: indexBy(actions, (a) => a.action_id),
    readingsByAction: groupBy(readings, (r) => r.action_id),
    anomaliesByAction: groupBy(anomalies, (a) => a.action_id),
    actionsByMission: groupBy(actions, (a) => a.mission_id),
  };
}

let cached: Kit | undefined;

/**
 * The kit, parsed once per server process.
 *
 * The CSVs are static files committed next to the app, so there is nothing to
 * revalidate — the first caller pays ~1ms of parsing and every later one reads
 * the same object. This is the seam to replace with a Neon query later.
 */
export function getKit(): Kit {
  return (cached ??= load());
}
