/* The Overview page's pure logic: the queue's open-row order and the
   analytics panel's data and chart choice. Client-safe — no kit, no
   filesystem; the numbers are built on the server by @/lib/overview-analytics
   and cross into the browser as plain JSON. */

import type { FleetAnalytics } from "./fleet-analytics";
import { BATTERY_BANDS } from "./breakdown-link";

/** The opened flag first, everything else in queue (backend) order. Closing
    it (openId null) gives the queue back exactly as it came. */
export function openFirst<T extends { id: string }>(flags: T[], openId: string | null): T[] {
  if (!openId) return flags;
  const i = flags.findIndex((flag) => flag.id === openId);
  if (i <= 0) return flags;
  return [flags[i], ...flags.slice(0, i), ...flags.slice(i + 1)];
}

/** What the analytics panel beside the queue can draw. */
export interface OverviewAnalyticsData {
  /** Success by action type, best first. */
  types: { key: string; label: string; rate: number; n: number }[];
  /** Success across every action, for the reference line. */
  fleetRate: number;
  battery: FleetAnalytics["battery"];
  missions: FleetAnalytics["missions"];
  failureModes: FleetAnalytics["failureModes"];
  /** Per mission: actions by type and failures by mode, so an open flag's
      mission can be drawn against the fleet. */
  byMission: Record<string, { types: Record<string, { n: number; ok: number }>; failures: Record<string, number> }>;
}

/** `short` is the toggle's label when the panel is narrow; `section` is
    where the chart's fuller version lives on /analytics. */
export const OVERVIEW_CHARTS = [
  { key: "types", label: "Inspection types", short: "Types", section: "configuration" },
  { key: "battery", label: "Battery", short: "Battery", section: "reliability" },
  { key: "missions", label: "Missions", short: "Missions", section: "missions" },
  { key: "failures", label: "Failure modes", short: "Failures", section: "failures" },
] as const;

export type OverviewChart = (typeof OVERVIEW_CHARTS)[number]["key"];

export const DEFAULT_CHART: OverviewChart = "types";

/** A stored choice, or null if it isn't one of today's charts. */
export function parseChart(value: unknown): OverviewChart | null {
  return OVERVIEW_CHARTS.some((chart) => chart.key === value) ? (value as OverviewChart) : null;
}

/** Opening a flag shows its mission (the chart where it can be pointed at),
    until the operator picks a chart while a flag is open — from then on their
    pick holds. With nothing open, the remembered pick. */
export function activeChart(saved: OverviewChart, flagOpen: boolean, pickedWhileOpen: boolean): OverviewChart {
  return flagOpen && !pickedWhileOpen ? "missions" : saved;
}

/** Battery band keys a mission's charge passed through on its way from
    `start` down to `end` (bands are (previous max, max]). */
export function bandsSpanned([end, start]: [number, number]): string[] {
  const low = Math.min(end, start);
  const high = Math.max(end, start);
  return BATTERY_BANDS.filter((band, i) => {
    const min = i === 0 ? -Infinity : BATTERY_BANDS[i - 1].max;
    return low <= band.max && high > min;
  }).map((band) => band.key);
}

/** The band a battery level falls in. */
export function bandOf(battery: number) {
  return BATTERY_BANDS.find((band) => battery <= band.max) ?? BATTERY_BANDS[BATTERY_BANDS.length - 1];
}

function rate(ok: number, n: number) {
  return n === 0 ? 0 : Math.round((ok / n) * 1000) / 10;
}

/** Fleet success per type, plus the mission's own where it ran that type. */
export function typesFor(data: OverviewAnalyticsData, missionId: string | null) {
  const mission = missionId ? data.byMission[missionId] : undefined;
  return data.types.map((row) => {
    const own = mission?.types[row.key];
    return { ...row, mission: own ? rate(own.ok, own.n) : null, missionN: own?.n ?? 0 };
  });
}

/** Fleet failures per mode, plus how many of them happened in the mission. */
export function failuresFor(data: OverviewAnalyticsData, missionId: string | null) {
  const mission = missionId ? data.byMission[missionId] : undefined;
  return data.failureModes.map((row) => ({ ...row, mission: mission ? mission.failures[row.key] ?? 0 : null }));
}
