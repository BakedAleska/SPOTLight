import type { Flag, Severity } from "./types";
import { displayText } from "./display";

/* The overview's filters over the open-flag queue. The dashboard loads every
   open flag (limit 500), so filtering here covers the whole queue. */

export interface QueueFilter {
  /** Free text: every word must appear in the flag's id, title, asset, waypoint, mission or reasons. */
  q: string;
  severity: Severity | "ALL";
  mission: string;
  location: string;
}

export const NO_FILTER: QueueFilter = { q: "", severity: "ALL", mission: "", location: "" };

export const SEVERITY_ORDER: Severity[] = ["CRITICAL", "HIGH", "MEDIUM", "LOW"];

export function isFiltered(f: QueueFilter): boolean {
  return f.q.trim() !== "" || f.severity !== "ALL" || f.mission !== "" || f.location !== "";
}

function haystack(flag: Flag): string {
  return [`#${flag.id}`, flag.title, flag.assetName, flag.locationName, flag.missionId,
    ...flag.reasons.map((r) => displayText(r.detail))].join(" ").toLowerCase();
}

export function filterFlags(flags: Flag[], f: QueueFilter): Flag[] {
  const words = f.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return flags.filter((flag) =>
    (f.severity === "ALL" || flag.severity === f.severity)
    && (!f.mission || flag.missionId === f.mission)
    && (!f.location || flag.locationName === f.location)
    && (!words.length || words.every((w) => haystack(flag).includes(w))));
}

/** The choices each filter offers, from the flags actually in the queue (with counts). */
export function filterOptions(flags: Flag[]) {
  const count = (key: (f: Flag) => string) => {
    const m = new Map<string, number>();
    for (const f of flags) { const k = key(f); if (k && k !== "Unassigned") m.set(k, (m.get(k) ?? 0) + 1); }
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true })).map(([value, n]) => ({ value, n }));
  };
  return {
    severities: SEVERITY_ORDER.map((s) => ({ value: s, n: flags.filter((f) => f.severity === s).length })).filter((s) => s.n > 0),
    missions: count((f) => f.missionId),
    locations: count((f) => f.locationName),
  };
}
