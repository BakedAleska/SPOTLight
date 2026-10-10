// ─────────────────────────────────────────────────────────────────────────────
// The Overview page's data, read from the backend.
//
// Triage now happens upstream: the backend ingests each event, asks Laya
// (System 1) and runs the deterministic `policy.py` validator, and writes the
// surviving escalations to `flags` in Neon. This module's job is only to map
// those rows — plus the live SQL insights from `/insights/overview` — onto the
// prop shapes the panels already take, so nothing below @/lib/types changes.
//
// If the backend can't be reached it falls back to @/lib/analysis, the
// TypeScript triage pass over the kit CSVs, and says so. A console that shows
// a blank page when one container is down is worse than one that shows the
// kit's own numbers and labels them.
//
// SERVER ONLY (reads BACKEND_URL via @/lib/api).
// ─────────────────────────────────────────────────────────────────────────────

import { unstable_rethrow } from "next/navigation";
import * as api from "./api";
import type { BatteryBand, Flag, Reason, Severity, Stat } from "./types";

export interface Headline {
  /** The banner's thesis: mission status is not a safe thing to trust. */
  hiddenFailures: { count: number; ofCompleted: number };
  /** The battery chart's caption. */
  batteryCliff: string;
  /** Action success across every band above the cliff — the chart's reference
      line, and the number the caption compares the cliff against. Null when
      the dataset has no actions above the cliff to average. */
  aboveCliffRate: number | null;
  /** How much of the queue the mission status field would never have shown. */
  queuedInHiddenMissions: number;
}

export interface Dashboard {
  stats: Stat[];
  flags: Flag[];
  batteryBands: BatteryBand[];
  headline: Headline;
  /** `backend` = live from Neon through the API. `kit` = the CSV fallback. */
  source: "backend" | "kit";
  /** Why the fallback ran, for the degraded-mode notice. Null when healthy. */
  degraded: string | null;
}

// ── Formatting ───────────────────────────────────────────────────────────────

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "1–10 Sep", or "28 Aug–3 Sep" across a month boundary. Hand-rolled on the
    date *string* so the output never depends on the server's timezone, which
    would desync SSR from the client. (@/lib/analysis keeps its own copy until
    the CSV fallback is retired.) */
function dateRange(from: string, to: string) {
  const day = (ts: string) => {
    const [, month, date] = ts.slice(0, 10).split("-");
    return { day: Number(date), month: MONTHS[Number(month) - 1] };
  };
  const a = day(from);
  const b = day(to);
  return a.month === b.month
    ? `${a.day}–${b.day} ${b.month}`
    : `${a.day} ${a.month}–${b.day} ${b.month}`;
}

/** The backend reports rates to one decimal; the tiles read as a column of
    figures, so they are shown whole. */
const whole = (value: number | null | undefined) => Math.round(value ?? 0);

/** `<=25` → `≤25%`, `26-40` → `26–40%`, `81+` → `81%+`. The band keys come
    from BATTERY_BAND_SQL and are the axis labels, so they get typeset here
    rather than being shown raw. */
function bandLabel(key: string) {
  if (key.startsWith("<=")) return `≤${key.slice(2)}%`;
  if (key.endsWith("+")) return `${key.slice(0, -1)}%+`;
  return `${key.replace("-", "–")}%`;
}

const SEVERITIES: Severity[] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

function asSeverity(value: string): Severity {
  return (SEVERITIES as string[]).includes(value) ? (value as Severity) : "MEDIUM";
}

// ── Insight cards ────────────────────────────────────────────────────────────
//
// `/insights/overview` returns a list of cards with stable `key`s, each with a
// per-card `detail` object (backend/app/services/analytics.py). Reading them
// through these accessors keeps the casts in one place and makes a missing
// card — an empty database, say — a null rather than a crash.

type Detail = Record<string, unknown>;

function card(cards: api.WireInsight[], key: string): api.WireInsight | undefined {
  return cards.find((c) => c.key === key);
}

function detail(cards: api.WireInsight[], key: string): Detail {
  return (card(cards, key)?.detail ?? {}) as Detail;
}

const asNumber = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

interface BandRow {
  grp: string;
  n: number;
  n_success: number;
  success_rate_pct: number | null;
}

// ── Flags ────────────────────────────────────────────────────────────────────

/**
 * `flag.title` is one self-contained line:
 *   "High abnormal temperature: Pump P-101 (P-101) at WP03 Turbine Hall South · battery 18%"
 * The queue row prints the asset and waypoint underneath, so the tail is
 * redundant there. The `where` clause is reconstructed exactly as the backend
 * built it (`validator.flag_title`) and matched literally — if the format ever
 * changes, nothing matches and the full title is shown, which is the safe way
 * to be wrong.
 */
function shortTitle(title: string, where: string | null) {
  const trimmed = title.replace(/ · battery \d+%$/, "");
  return where && trimmed.endsWith(`: ${where}`)
    ? trimmed.slice(0, -(where.length + 2))
    : trimmed;
}

/**
 * The reasons a queue row shows.
 *
 * `flags.reasons` carries both verdicts first (`laya_verdict` /
 * `laya_unavailable`, then `rules_verdict`) and after them one entry per fired
 * policy rule, which are the only ones with a `weight`. The row wants the
 * rules — the specific things that happened — so the verdicts are filtered out
 * and the rules sorted heaviest first.
 *
 * Unlike the CSV pass, the score is not the sum of these weights: the backend
 * derives it from the final priority (P1 → CRITICAL, and so on), after Laya
 * and the rules have been reconciled. The weights still order the list.
 */
const VERDICT_RULES = new Set(["laya_verdict", "laya_unavailable", "rules_verdict"]);

function reasonsOf(wire: api.WireReason[]): Reason[] {
  const rules = wire
    .filter((r) => !VERDICT_RULES.has(r.rule) && r.detail)
    .map((r) => ({ rule: r.rule, detail: r.detail as string, weight: r.weight ?? 0 }))
    .sort((a, b) => b.weight - a.weight);
  if (rules.length > 0) return rules;
  // Laya escalated with no rule firing (TRIAGE_MODE=parallel): the verdict
  // line is then the only thing there is to say, and a row with no reason at
  // all would look like a rendering bug.
  return wire
    .filter((r) => r.detail)
    .map((r) => ({ rule: r.rule, detail: r.detail as string, weight: 0 }));
}

// ── The page's data ──────────────────────────────────────────────────────────

/** Five independent reads, so they go out together. The two catalogs are small
    and fully cached in the backend's reference data; the overview is the only
    one that does real SQL. */
async function fromBackend(): Promise<Dashboard> {
  const [overview, flagList, missions, assets, locations] = await Promise.all([
    api.get<api.WireOverview>("/insights/overview"),
    api.get<api.WireFlagList>("/flags", { query: { escalated: true, status: "OPEN", limit: 200 } }),
    api.get<api.WireMissionList>("/missions"),
    api.get<api.WireAsset[]>("/assets"),
    api.get<api.WireLocation[]>("/locations"),
  ]);

  const cards = overview.insights;
  const assetName = new Map(assets.map((a) => [a.asset_id, a.asset_name]));
  const locationName = new Map(locations.map((l) => [l.location_id, l.location_name]));
  const missionName = new Map(missions.items.map((m) => [m.mission_id, m.mission_name || m.mission_id]));
  const misleading = new Set(missions.items.filter((m) => m.status_misleading).map((m) => m.mission_id));

  // ── Battery cliff ──
  const bands = ((detail(cards, "battery_cliff").bands ?? []) as BandRow[]).filter((b) => b.n > 0);
  const batteryBands: BatteryBand[] = bands.map((b) => ({
    band: bandLabel(b.grp),
    successRate: whole(b.success_rate_pct),
    sampleSize: b.n,
  }));
  const cliff = batteryBands[0];
  // The comparison is "above the cliff", aggregated over every band over 25%,
  // which is the number the backend computes and quotes in its own pitch. The
  // old CSV copy said "on a full charge" while showing the best *band*, which
  // was usually 61-80% and not a full charge at all (audit P-11).
  const aboveRaw = asNumber(detail(cards, "battery_cliff").above_25_success_pct);
  const aboveCliff = whole(aboveRaw);

  // ── Green but broken ──
  const green = detail(cards, "green_but_broken");
  const completedWithFailures = asNumber(green.completed_with_failures) ?? 0;
  const reportedCompleted = asNumber(green.reported_completed) ?? 0;
  const missionCount = asNumber(green.missions) ?? missions.items.length;

  // ── V1 vs V2 ──
  const config = detail(cards, "v1_vs_v2_matched_battery");
  const headlineGain = asNumber(config.headline_gain_pts);
  const matchedGain = asNumber(config.matched_gain_pts);
  const batteryShare = asNumber(config.share_explained_by_battery_pct);
  const byVersion = (config.headline ?? {}) as Record<string, { success_rate_pct: number | null } | undefined>;
  const gain = matchedGain ?? headlineGain;

  // ── Coverage ──
  const actionCount = bands.reduce((sum, b) => sum + b.n, 0);
  const times = missions.items.flatMap((m) => [m.start_time, m.end_time]).filter((t): t is string => !!t).sort();
  const window = times.length > 0 ? dateRange(times[0], times[times.length - 1]) : "no missions loaded";

  const stats: Stat[] = [
    {
      label: "Missions",
      value: String(missionCount),
      note: `${actionCount} actions at ${locations.length} waypoints, ${window}.`,
    },
    {
      label: "Hiding failures",
      value: String(completedWithFailures),
      unit: `of ${reportedCompleted}`,
      note:
        `Missions reporting COMPLETED that contain at least one FAILED action. ` +
        `Status alone hides ${whole(reportedCompleted ? (100 * completedWithFailures) / reportedCompleted : 0)}% of them.`,
    },
    {
      label: "Success below 25% battery",
      value: String(cliff?.successRate ?? 0),
      unit: "%",
      note: cliff
        ? `Across ${cliff.sampleSize} actions, against ${aboveCliff}% above 25% — a ${aboveCliff - cliff.successRate}-point cliff.`
        : "No actions loaded yet.",
    },
    {
      label: "V2 vs V1",
      value: gain === null ? "—" : `${gain > 0 ? "+" : ""}${gain}`,
      unit: matchedGain === null ? "pts" : "pts at >40%",
      note:
        gain === null
          ? "Only one configuration version is present in this dataset."
          : matchedGain !== null && headlineGain !== null
            ? `Headline gain is +${headlineGain} pts (${whole(byVersion.V1?.success_rate_pct)}% → ` +
              `${whole(byVersion.V2?.success_rate_pct)}%), but +${matchedGain} pts at matched battery` +
              `${batteryShare === null ? "" : `: about ${batteryShare}% of it is battery management`}.`
            : `Action success ${whole(byVersion.V2?.success_rate_pct)}% on V2 vs ${whole(byVersion.V1?.success_rate_pct)}% on V1.`,
    },
  ];

  const flags: Flag[] = flagList.items.map((f) => {
    const asset = f.asset_id ? (assetName.get(f.asset_id) ?? f.asset_id) : null;
    const location = f.location_id ? (locationName.get(f.location_id) ?? f.location_id) : null;
    // Exactly the string validator.flag_title() interpolates, so the literal
    // match in shortTitle() either hits or cleanly misses.
    const where =
      asset && location && f.asset_id && f.location_id
        ? `${asset} (${f.asset_id}) at ${f.location_id} ${location}`
        : null;
    return {
      // flags.flag_id, not the action id: it is what /flags/{id}/explain and
      // /flags/{id}/ack take, so the row already holds its own detail route.
      id: String(f.flag_id),
      missionId: f.mission_id ?? "",
      severity: asSeverity(f.severity),
      score: whole(f.score),
      title: shortTitle(f.title, where),
      // A mission-level flag has no asset or waypoint of its own.
      assetName: asset ?? (f.mission_id ? missionName.get(f.mission_id) ?? f.mission_id : "—"),
      locationName: location ?? "whole mission",
      reasons: reasonsOf(f.reasons ?? []),
    };
  });

  return {
    stats,
    flags,
    batteryBands,
    headline: {
      hiddenFailures: { count: completedWithFailures, ofCompleted: reportedCompleted },
      aboveCliffRate: aboveRaw === null ? null : whole(aboveRaw),
      batteryCliff: cliff
        ? `Success falls to ${cliff.successRate}% below 25% battery, from ${aboveCliff}% above it — ` +
          `a hard cue to send SPOT back to dock before it gets there.`
        : "No actions loaded yet — start a replay.",
      queuedInHiddenMissions: flags.filter((f) => misleading.has(f.missionId)).length,
    },
    source: "backend",
    degraded: null,
  };
}

/** The kit CSVs, triaged in-process. Imported lazily so the happy path never
    pays to parse them — and so a deployment without `web/data/kit` still
    serves the live console. */
async function fromKit(why: string): Promise<Dashboard> {
  const kit = await import("./analysis");
  return {
    stats: kit.stats,
    flags: kit.flags,
    batteryBands: kit.batteryBands,
    headline: kit.headline,
    source: "kit",
    degraded: why,
  };
}

export async function getDashboard(): Promise<Dashboard> {
  try {
    return await fromBackend();
  } catch (error) {
    // Never let the kit fallback swallow notFound/redirect or the prerender's
    // own abort signal — see the note in @/lib/api.
    unstable_rethrow(error);
    const why = error instanceof api.BackendError ? error.message : String(error);
    // Server-side only: the operator sees the notice, the logs get the cause.
    console.error("[dashboard] backend read failed, falling back to the kit CSVs:", error);
    return fromKit(why);
  }
}
