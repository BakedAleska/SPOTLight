/* Mission chats (contract §9, chat.3): URL and display helpers shared by the
   mission cards, the replay page, the chat and the proxy routes. Pure
   functions, no React, so they run in node tests and on the server. */

import type { MissionRef, MissionSummary } from "./types";

/** What the web accepts as a mission id before asking the backend: a short
    token of letters, digits, "-" and "_" ("M007", "m7"). The backend matches
    case-insensitively and owns the real check (422 unknown_mission); this only
    keeps junk out of URLs and query strings. */
const MISSION_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;

/** A mission id from a URL or a request, trimmed and upper-cased, or null
    when it is missing or malformed. */
export function normalizeMissionId(raw: string | null | undefined): string | null {
  const value = raw?.trim();
  return value && MISSION_ID.test(value) ? value.toUpperCase() : null;
}

/** The chat about one mission: /assistant?mission=M006. */
export function assistantHref(missionId: string) {
  return `/assistant?mission=${encodeURIComponent(missionId)}`;
}

/** The mission's replay: /missions/replay?mission=M006. */
export function replayHref(missionId: string) {
  return `/missions/replay?mission=${encodeURIComponent(missionId)}`;
}

/** The new-chat URL with the mission focus set (or cleared, with null),
    keeping any other query parameters. */
export function withMissionParam(search: string, missionId: string | null) {
  const params = new URLSearchParams(search);
  if (missionId) params.set("mission", missionId);
  else params.delete("mission");
  const query = params.toString();
  return `/assistant${query ? `?${query}` : ""}`;
}

/** "M006 · Autowalk Inspection Route 06" */
export function missionLabel(mission: Pick<MissionRef, "mission_id" | "mission_name">) {
  return mission.mission_name ? `${mission.mission_id} · ${mission.mission_name}` : mission.mission_id;
}

export function plural(n: number, one: string, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

/** The warning a mission context card shows after "Hidden failure:" when the reported status hides
    failed actions, or null when it doesn't apply. */
export function hiddenFailureText(mission: Pick<MissionSummary, "hidden_failure" | "failed">) {
  if (!mission.hidden_failure) return null;
  return `reported COMPLETED but ${plural(mission.failed, "action")} failed`;
}

export type MissionTone = "ok" | "warn" | "bad";

/** Colour role for a mission row: a reported failure or a hidden one is bad,
    a clean completion with anomalies is a warning, otherwise ok. */
export function missionTone(mission: Pick<MissionSummary, "mission_status" | "hidden_failure" | "failed" | "anomalies">): MissionTone {
  if (mission.mission_status !== "COMPLETED" || mission.hidden_failure) return "bad";
  if (mission.failed > 0 || mission.anomalies > 0) return "warn";
  return "ok";
}

/** The picker's search: every word must match the id, name, status or
    scenario, case-insensitively. "hidden" also finds hidden failures. */
export function filterMissions(items: MissionSummary[], query: string): MissionSummary[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return items;
  return items.filter((m) => {
    const hay = `${m.mission_id} ${m.mission_name} ${m.mission_status} ${m.scenario ?? ""} ${m.configuration_version ?? ""} ${m.hidden_failure ? "hidden" : ""}`.toLowerCase();
    return words.every((word) => hay.includes(word));
  });
}

/** Moves the picker's highlighted option with the arrow keys, wrapping. */
export function stepIndex(current: number, key: string, count: number): number {
  if (count === 0) return -1;
  switch (key) {
    case "ArrowDown": return current < 0 ? 0 : (current + 1) % count;
    case "ArrowUp": return current < 0 ? count - 1 : (current - 1 + count) % count;
    case "Home": return 0;
    case "End": return count - 1;
    default: return current;
  }
}
