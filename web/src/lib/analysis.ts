// ─────────────────────────────────────────────────────────────────────────────
// System 1: the explainable triage pass.
//
// Everything the dashboard shows is computed here, from the kit CSVs, with no
// model in the loop. Each rule is a hard gate with a fixed weight, a stable id
// and a sentence an operator can read — so a flag's score can always be taken
// apart and argued with. That is the whole point of putting statistics first
// and the LLM second (System 2 explains what this escalates, it doesn't rank).
//
// The grain is the ACTION. Per Data_Assumptions.md a mission can report
// COMPLETED while actions inside it failed, so the mission row is not a safe
// unit of attention — the action is.
//
// SERVER ONLY (imports @/lib/kit, which reads the filesystem).
// ─────────────────────────────────────────────────────────────────────────────

import { getKit, type Action, type Kit } from "./kit";
import type { BatteryBand, Flag, Reason, Severity, Stat } from "./types";

export type { BatteryBand, Flag, Reason, Severity, Stat } from "./types";

// ── Rule weights ─────────────────────────────────────────────────────────────
//
// One table, so the scoring model is a thing you can read in ten seconds and
// tune in one place. Scores are clamped to 100, so these are priorities
// relative to each other rather than a budget that has to sum to anything.

const WEIGHTS = {
  /** SPOT itself called this an anomaly — the strongest single signal. */
  anomaly: { CRITICAL: 34, HIGH: 22, MEDIUM: 11, LOW: 4 } as Record<Severity, number>,
  /** The anomaly row explicitly asks for a human. */
  operatorReview: 13,
  /** The action did not complete. */
  actionFailed: 18,
  /** …and the mission still reports COMPLETED. Deliberately light: this is
      true of nearly every failed action in the set, so as a *ranking* signal
      it barely discriminates. It earns its place as a reason — the operator
      needs to be told the status lied — but the insight itself is a headline,
      not a tiebreaker. */
  statusMismatch: 5,
  /** Consequence: what the asset is worth. */
  assetCriticality: { CRITICAL: 14, HIGH: 8, MEDIUM: 3, LOW: 0 } as Record<Severity, number>,
  /** The action ran inside the battery band where success collapses. */
  batteryCliff: 12,
  /** This asset keeps failing, across missions. */
  assetRepeat: 8,
  /** The measurement came back, but the robot doesn't trust it. */
  readingQuality: 5,
  /** This waypoint fails far more often than the floor average. */
  locationHotspot: 6,
};

// Score bands. Tuned against this dataset so the words keep their meaning: a
// queue where a quarter of the rows say CRITICAL has taught the operator to
// ignore the badge.
const SEVERITY_FLOOR = { CRITICAL: 72, HIGH: 50, MEDIUM: 22 } as const;

/** Below this, an action is noise — one weak signal and nothing corroborating. */
const QUEUE_FLOOR = SEVERITY_FLOOR.MEDIUM;

/** The battery band where behaviour changes, per the cliff chart. */
const CLIFF_CEILING = 25;

/** An asset failing in this many separate missions is a pattern, not luck.
    Four rather than three: at three it fired on two thirds of the queue, and a
    rule that is true of almost every row cannot help order it. */
const REPEAT_MISSIONS = 4;

/** A waypoint this much worse than the floor average is a hotspot… */
const HOTSPOT_RATIO = 1.5;
/** …but only once it has been visited enough times to mean anything. */
const HOTSPOT_MIN_VISITS = 8;

// ── Text helpers ─────────────────────────────────────────────────────────────

/** ABNORMAL_TEMPERATURE → "abnormal temperature" */
function humanize(token: string) {
  return token.toLowerCase().replace(/_/g, " ");
}

function sentenceCase(text: string) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function pct(part: number, whole: number) {
  return whole === 0 ? 0 : Math.round((part / whole) * 100);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-09-01T18:00:00" → { day: 1, month: "Sep" }. Hand-rolled so output never
    depends on the server's locale or timezone, which would desync SSR from the
    client. */
function parseDay(timestamp: string) {
  const [, month, day] = timestamp.split("T")[0].split("-");
  return { day: Number(day), month: MONTHS[Number(month) - 1] };
}

/** "1–10 Sep", or "28 Aug–3 Sep" when the range crosses a month. */
function dateRange(first: string, last: string) {
  const from = parseDay(first);
  const to = parseDay(last);
  return from.month === to.month
    ? `${from.day}–${to.day} ${to.month}`
    : `${from.day} ${from.month}–${to.day} ${to.month}`;
}

const SEVERITY_ORDER: Severity[] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

function worstSeverity(a: Severity, b: Severity) {
  return SEVERITY_ORDER.indexOf(a) >= SEVERITY_ORDER.indexOf(b) ? a : b;
}

// ── Derived facts ────────────────────────────────────────────────────────────
//
// The cross-cutting numbers. These are what make a flag more than a restated
// CSV row: "this asset failed here before", "this waypoint is bad", "you were
// below the cliff" are all statements about the whole dataset, not the row.

interface Rate {
  total: number;
  failed: number;
  /** Failures as a percentage of attempts. */
  rate: number;
}

function rate(total: number, failed: number): Rate {
  return { total, failed, rate: total === 0 ? 0 : failed / total };
}

const BAND_SPECS: { band: string; max: number }[] = [
  { band: "≤25%", max: CLIFF_CEILING },
  { band: "26–40%", max: 40 },
  { band: "41–60%", max: 60 },
  { band: "61%+", max: Infinity },
];

function bandOf(batteryPct: number) {
  return BAND_SPECS.find((spec) => batteryPct <= spec.max)!.band;
}

interface Derived {
  /** Missions reporting COMPLETED that contain at least one FAILED action. */
  hiddenFailureMissions: Set<string>;
  completedMissions: number;
  batteryBands: BatteryBand[];
  /** Success rate inside the cliff band, and across every band above it. */
  cliff: { successRate: number; sampleSize: number; aboveRate: number };
  byConfig: Map<string, Rate>;
  /** Mean start→end battery drain per configuration version. */
  drainByConfig: Map<string, number>;
  byAsset: Map<string, Rate & { missionsFailed: Set<string> }>;
  byLocation: Map<string, Rate>;
  fleetFailureRate: number;
  /** Human-readable span the dataset covers, e.g. "1–10 Sep". */
  window: string;
}

function derive(kit: Kit): Derived {
  const { actions, missions, actionsByMission } = kit;

  // Hidden failures: the dataset's central trap. Mission status and action
  // status have to be read separately, so we read them separately.
  const hiddenFailureMissions = new Set<string>();
  let completedMissions = 0;
  for (const mission of missions) {
    if (mission.mission_status !== "COMPLETED") continue;
    completedMissions++;
    const inside = actionsByMission.get(mission.mission_id) ?? [];
    if (inside.some((action) => action.status === "FAILED")) {
      hiddenFailureMissions.add(mission.mission_id);
    }
  }

  // Success by battery band — the cliff.
  const bandTotals = new Map(BAND_SPECS.map((s) => [s.band, { total: 0, success: 0 }]));
  for (const action of actions) {
    const bucket = bandTotals.get(bandOf(action.battery_pct))!;
    bucket.total++;
    if (action.status === "SUCCESS") bucket.success++;
  }
  const batteryBands: BatteryBand[] = BAND_SPECS.map(({ band }) => {
    const { total, success } = bandTotals.get(band)!;
    return { band, successRate: pct(success, total), sampleSize: total };
  });

  // The comparison is "above the cliff", aggregated over every band over the
  // ceiling — the same number the backend computes. It used to be the best
  // single *band*, described in the copy as "on a full charge", which was
  // usually 41-60% and not a full charge at all (audit P-11).
  const above = BAND_SPECS.slice(1).map(({ band }) => bandTotals.get(band)!);
  const aboveRate = pct(
    above.reduce((sum, b) => sum + b.success, 0),
    above.reduce((sum, b) => sum + b.total, 0),
  );

  // V1 vs V2, on action success and on battery drain.
  const byConfig = new Map<string, Rate>();
  for (const version of new Set(actions.map((a) => a.configuration_version))) {
    const rows = actions.filter((a) => a.configuration_version === version);
    byConfig.set(version, rate(rows.length, rows.filter((a) => a.status === "FAILED").length));
  }

  const drainByConfig = new Map<string, number>();
  for (const version of new Set(missions.map((m) => m.configuration_version))) {
    const rows = missions.filter((m) => m.configuration_version === version);
    const drain = rows.reduce((sum, m) => sum + (m.start_battery_pct - m.end_battery_pct), 0);
    drainByConfig.set(version, drain / rows.length);
  }

  // Repeat offenders, by asset and by waypoint.
  const byAsset = new Map<string, Rate & { missionsFailed: Set<string> }>();
  for (const action of actions) {
    let row = byAsset.get(action.asset_id);
    if (!row) {
      row = { ...rate(0, 0), missionsFailed: new Set() };
      byAsset.set(action.asset_id, row);
    }
    row.total++;
    if (action.status === "FAILED") {
      row.failed++;
      row.missionsFailed.add(action.mission_id);
    }
    row.rate = row.failed / row.total;
  }

  const byLocation = new Map<string, Rate>();
  for (const locationId of new Set(actions.map((a) => a.location_id))) {
    const rows = actions.filter((a) => a.location_id === locationId);
    byLocation.set(locationId, rate(rows.length, rows.filter((a) => a.status === "FAILED").length));
  }

  const timestamps = actions.map((a) => a.timestamp).sort();

  return {
    hiddenFailureMissions,
    completedMissions,
    batteryBands,
    cliff: {
      successRate: batteryBands[0].successRate,
      sampleSize: batteryBands[0].sampleSize,
      aboveRate,
    },
    byConfig,
    drainByConfig,
    byAsset,
    byLocation,
    fleetFailureRate: actions.filter((a) => a.status === "FAILED").length / actions.length,
    window: dateRange(timestamps[0], timestamps[timestamps.length - 1]),
  };
}

// ── Triage ───────────────────────────────────────────────────────────────────

/** Runs every rule against one action. Returns null if nothing fired. */
function triage(action: Action, kit: Kit, facts: Derived): Flag | null {
  const asset = kit.assetById.get(action.asset_id);
  const location = kit.locationById.get(action.location_id);
  const mission = kit.missionById.get(action.mission_id);
  const anomalies = kit.anomaliesByAction.get(action.action_id) ?? [];
  const readings = kit.readingsByAction.get(action.action_id) ?? [];

  const reasons: Reason[] = [];
  const add = (rule: string, weight: number, detail: string) => {
    if (weight > 0) reasons.push({ rule, detail, weight });
  };

  // EVIDENCE GATES FIRST. A flag needs something that actually happened on
  // this action — an anomaly, a failure, or a measurement the robot distrusts.
  // The rules after that (criticality, battery, repeat history, hotspot) are
  // context: they decide how urgently a finding is read, never whether there
  // is one. Without this gate the queue fills with clean, successful actions
  // that merely happen to sit on an important asset, which is how a priority
  // queue decays back into a table.
  const questionable = readings.filter((r) => r.quality === "QUESTIONABLE");
  const hasFinding =
    anomalies.length > 0 || action.status === "FAILED" || questionable.length > 0;
  if (!hasFinding) return null;

  // 1. The robot's own finding. Worst one wins if an action ever carries more.
  const anomaly = anomalies.reduce<(typeof anomalies)[number] | undefined>(
    (worst, candidate) =>
      !worst || worstSeverity(worst.severity, candidate.severity) === candidate.severity
        ? candidate
        : worst,
    undefined,
  );

  if (anomaly) {
    add(
      "anomaly.raised",
      WEIGHTS.anomaly[anomaly.severity],
      `SPOT raised a ${anomaly.severity} ${humanize(anomaly.anomaly_type)} anomaly here.`,
    );
    if (anomaly.operator_review_required) {
      add("anomaly.review", WEIGHTS.operatorReview, "Flagged as requiring operator review.");
    }
  }

  // 2. The action failed — and whether the mission owned up to it.
  if (action.status === "FAILED") {
    add(
      "action.failed",
      WEIGHTS.actionFailed,
      action.failure_type
        ? `The ${humanize(action.action_type)} failed: ${humanize(action.failure_type)}.`
        : `The ${humanize(action.action_type)} failed.`,
    );

    if (mission && facts.hiddenFailureMissions.has(mission.mission_id)) {
      add(
        "status.mismatch",
        WEIGHTS.statusMismatch,
        `Mission ${mission.mission_id} still reports COMPLETED, so this never surfaced.`,
      );
    }
  }

  // 3. Consequence — what is on the other end of the failure.
  // The asset and waypoint names are already on the row, so these sentences
  // don't repeat them — three bullets that all open with the same asset name
  // read as boilerplate and stop being read at all.
  if (asset && (asset.criticality === "CRITICAL" || asset.criticality === "HIGH")) {
    add(
      "asset.criticality",
      WEIGHTS.assetCriticality[asset.criticality],
      `A ${asset.criticality}-criticality ${asset.asset_type.toLowerCase()}.`,
    );
  }

  // 4. Context the operator would otherwise have to go and compute.
  if (action.battery_pct <= CLIFF_CEILING) {
    add(
      "battery.cliff",
      WEIGHTS.batteryCliff,
      `Battery was ${action.battery_pct}% — the band where only ${facts.cliff.successRate}% of actions succeed.`,
    );
  }

  const assetHistory = asset ? facts.byAsset.get(asset.asset_id) : undefined;
  if (asset && assetHistory && assetHistory.missionsFailed.size >= REPEAT_MISSIONS) {
    add(
      "asset.repeat",
      WEIGHTS.assetRepeat,
      `Repeat offender: ${assetHistory.failed} of ${assetHistory.total} actions on this asset have failed, across ${assetHistory.missionsFailed.size} missions.`,
    );
  }

  if (questionable.length > 0) {
    add(
      "reading.quality",
      WEIGHTS.readingQuality,
      readings.length === 1
        ? "The measurement came back, but marked QUESTIONABLE."
        : questionable.length === readings.length
          ? `All ${readings.length} measurements came back QUESTIONABLE.`
          : `${questionable.length} of ${readings.length} measurements came back QUESTIONABLE.`,
    );
  }

  const locationHistory = location ? facts.byLocation.get(location.location_id) : undefined;
  if (
    location &&
    locationHistory &&
    locationHistory.total >= HOTSPOT_MIN_VISITS &&
    locationHistory.rate >= facts.fleetFailureRate * HOTSPOT_RATIO
  ) {
    add(
      "location.hotspot",
      WEIGHTS.locationHotspot,
      `${location.location_name} (${humanize(location.environment)}) fails ${Math.round(locationHistory.rate * 100)}% of actions vs ${Math.round(facts.fleetFailureRate * 100)}% floor-wide.`,
    );
  }

  if (reasons.length === 0) return null;

  const score = Math.min(100, reasons.reduce((sum, r) => sum + r.weight, 0));

  // Severity comes from the score, but can never fall below what SPOT already
  // called the anomaly: we are allowed to escalate the robot, not to quiet it.
  let severity: Severity =
    score >= SEVERITY_FLOOR.CRITICAL
      ? "CRITICAL"
      : score >= SEVERITY_FLOOR.HIGH
        ? "HIGH"
        : score >= SEVERITY_FLOOR.MEDIUM
          ? "MEDIUM"
          : "LOW";
  if (anomaly) severity = worstSeverity(severity, anomaly.severity);

  const assetName = asset?.asset_name ?? action.asset_id;
  const locationName = location?.location_name ?? action.location_id;

  const title = anomaly
    ? `${sentenceCase(humanize(anomaly.anomaly_type))} on ${assetName}`
    : action.status === "FAILED"
      ? `${sentenceCase(humanize(action.action_type))} failed at ${locationName}`
      : `${sentenceCase(humanize(action.action_type))} needs a second look on ${assetName}`;

  return {
    id: action.action_id,
    missionId: action.mission_id,
    severity,
    score: Math.round(score),
    title,
    assetName,
    locationName,
    // Heaviest first: the reason that earned the rank is the one to read.
    // Every rule that fired is kept, so the score is always the sum of this
    // list and a flag can be taken apart. The queue row decides how many of
    // them it has room to show.
    reasons: [...reasons].sort((a, b) => b.weight - a.weight),
  };
}

// ── The dashboard's data ─────────────────────────────────────────────────────

const kit = getKit();
const facts = derive(kit);

/** Success rate by battery band — the cliff chart. */
export const batteryBands = facts.batteryBands;

/**
 * The priority queue. Worst first — severity leads, score breaks ties.
 *
 * Anything under the MEDIUM floor is left out on purpose: a queue that lists
 * every imperfect action is a table, and the operator is back to reading rows.
 */
export const flags: Flag[] = kit.actions
  .map((action) => triage(action, kit, facts))
  .filter((flag): flag is Flag => flag !== null && flag.score >= QUEUE_FLOOR)
  .sort(
    (a, b) =>
      SEVERITY_ORDER.indexOf(b.severity) - SEVERITY_ORDER.indexOf(a.severity) ||
      b.score - a.score ||
      a.id.localeCompare(b.id),
  );

const v1 = facts.byConfig.get("V1");
const v2 = facts.byConfig.get("V2");
/** Change in action success rate from V1 to V2, in percentage points. */
const configDelta =
  v1 && v2 ? Math.round(((1 - v2.rate) - (1 - v1.rate)) * 100) : 0;

const hiddenCount = facts.hiddenFailureMissions.size;

/** The four headline numbers across the top of the dashboard. */
export const stats: Stat[] = [
  {
    label: "Missions",
    value: String(kit.missions.length),
    note: `${kit.actions.length} actions at ${kit.locations.length} waypoints, ${facts.window}.`,
  },
  {
    label: "Hiding failures",
    value: String(hiddenCount),
    unit: `of ${facts.completedMissions}`,
    note: `Missions reporting COMPLETED that contain at least one FAILED action. Status alone hides ${pct(hiddenCount, facts.completedMissions)}% of them.`,
  },
  {
    label: "Success below 25% battery",
    value: String(facts.cliff.successRate),
    unit: "%",
    note: `Across ${facts.cliff.sampleSize} actions, against ${facts.cliff.aboveRate}% above ${CLIFF_CEILING}% — a ${facts.cliff.aboveRate - facts.cliff.successRate}-point cliff.`,
  },
  {
    label: "V2 vs V1",
    value: `${configDelta > 0 ? "+" : ""}${configDelta}`,
    unit: "pts",
    note:
      v1 && v2
        ? `Action success ${Math.round((1 - v2.rate) * 100)}% on V2 vs ${Math.round((1 - v1.rate) * 100)}% on V1, at ${Math.round(facts.drainByConfig.get("V2") ?? 0)}% battery drain vs ${Math.round(facts.drainByConfig.get("V1") ?? 0)}%.`
        : "Only one configuration version is present in this dataset.",
  },
];

/**
 * Where the numbers came from, for the rail's footer.
 *
 * Worth stating plainly in the UI: the kit's Data_Assumptions.md is emphatic
 * that every row is synthetic and fictional, and a console that shows
 * failures on named equipment should say so on its own face rather than leave
 * a reader to assume otherwise.
 */
export const provenance = {
  source: "Challenge kit CSVs · synthetic",
  coverage: `${kit.missions.length} missions · ${kit.actions.length} actions · ${facts.window}`,
};

/**
 * The claims the page makes in prose. Derived here rather than typed into the
 * components, so a sentence in the copy can never drift from the data behind
 * it — if the CSVs change, the sentence changes with them.
 */
export const headline = {
  /** The banner's thesis: mission status is not a safe thing to trust. */
  hiddenFailures: { count: hiddenCount, ofCompleted: facts.completedMissions },
  /** The battery chart's caption. */
  batteryCliff:
    `Success falls to ${facts.cliff.successRate}% below ${CLIFF_CEILING}% battery, from ` +
    `${facts.cliff.aboveRate}% above it — a hard cue to send SPOT back to dock ` +
    `before it gets there.`,
  /** The battery chart's reference line. */
  aboveCliffRate: facts.cliff.aboveRate,
  /** How much of the queue the mission status field would never have shown. */
  queuedInHiddenMissions: flags.filter((flag) =>
    facts.hiddenFailureMissions.has(flag.missionId),
  ).length,
};
