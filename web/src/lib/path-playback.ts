import type { PathEvent, PathMission, PathPoint } from "./path-types";

export interface PlaybackSegment {
  start: number; end: number; eventIndex: number;
  from?: PathPoint; to?: PathPoint; activity: string; moving: boolean;
}
export const activityLabel = (type?: string) => ({
  NAVIGATION: "Checking route", VISUAL_INSPECTION: "Capturing images",
  ACOUSTIC_INSPECTION: "Listening for anomalies", THERMAL_INSPECTION: "Scanning temperature",
  GAUGE_READING: "Reading gauge",
}[type ?? ""] ?? (type ? type.replaceAll("_", " ").toLowerCase() : "Inspecting asset"));

/** Compressed presentation timing. Travel is interpolated, never measured telemetry.
 * Recorded event order is preserved, including start/result pairs sharing a timestamp. */
export function buildPlayback(mission: PathMission, points: PathPoint[]): PlaybackSegment[] {
  let cursor = 0;
  let position = points.find(p => p.id === mission.events.find(e => e.locationId)?.locationId);
  const segments: PlaybackSegment[] = [];
  function append(duration: number, eventIndex: number, to: PathPoint | undefined, activity: string, moving = false) {
    segments.push({ start: cursor, end: cursor + duration, eventIndex, from: position, to, activity, moving });
    cursor += duration;
    position = to;
  }
  mission.events.forEach((event, i) => {
    const target = points.find(p => p.id === event.locationId) ?? position;
    if (event.type === "ACTION_STARTED") {
      if (position && target && position.id !== target.id) {
        const distance = Math.hypot(target.x - position.x, target.y - position.y);
        append(Math.max(800, distance * 10), Math.max(0, i - 1), target, `Traveling to ${target.id}`, true);
      }
      append(Math.max(900, Math.min(4000, (event.durationSec ?? 30) * 40)), i, target, activityLabel(event.actionType));
    } else {
      append(event.type === "ACTION_FAILED" ? 900 : 250, i, target,
        event.type === "ACTION_FAILED" ? "Issue detected" : event.type === "MISSION_COMPLETED" ? "Mission complete" : event.type === "MISSION_STARTED" ? "Ready to begin" : "Inspection complete");
    }
  });
  return segments;
}
export function playbackAt(segments: PlaybackSegment[], elapsed: number) {
  const segment = segments.find(s => elapsed < s.end) ?? segments.at(-1);
  if (!segment) return undefined;
  const progress = Math.max(0, Math.min(1, (elapsed - segment.start) / (segment.end - segment.start)));
  const from = segment.from ?? segment.to;
  const to = segment.to ?? from;
  return { ...segment, progress, x: from && to ? from.x + (to.x - from.x) * progress : 0,
    y: from && to ? from.y + (to.y - from.y) * progress : 0 };
}
export function eventLabel(event: PathEvent) {
  if (event.type === "ACTION_STARTED") return activityLabel(event.actionType);
  return { MISSION_STARTED: "Mission started", MISSION_COMPLETED: "Mission complete", ACTION_FAILED: "Action failed", ACTION_COMPLETED: "Inspection complete" }[event.type] ?? event.type.replaceAll("_", " ");
}
