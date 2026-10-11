import type { PathEvent, PathMission, PathPoint } from "./path-types";
import { floorOf, legLength, pointOnLeg, routeLegs, STAIRS, type Leg, type XY } from "./route-geometry";

export interface PlaybackSegment {
  start: number; end: number; eventIndex: number;
  from?: PathPoint; to?: PathPoint; activity: string; moving: boolean;
  /** The stretch of route walked or climbed during this segment. */
  leg?: Leg;
}
export const activityLabel = (type?: string) => ({
  NAVIGATION: "Checking route", VISUAL_INSPECTION: "Capturing images",
  ACOUSTIC_INSPECTION: "Listening for anomalies", THERMAL_INSPECTION: "Scanning temperature",
  GAUGE_READING: "Reading gauge",
}[type ?? ""] ?? (type ? type.replaceAll("_", " ").toLowerCase() : "Inspecting asset"));

/** Compressed presentation timing. Travel is interpolated, never measured telemetry.
 * Recorded event order is preserved, including start/result pairs sharing a timestamp.
 * Travel follows the hallways (route-geometry), taking the stairs between floors. */
export function buildPlayback(mission: PathMission, points: PathPoint[]): PlaybackSegment[] {
  const first = points.find(p => p.id === mission.events.find(e => e.locationId)?.locationId);
  // Same position tracking as the loop below, so travel k there is travel k here.
  const stops = first ? [first] : [];
  let walker = first;
  for (const event of mission.events) {
    const target = points.find(p => p.id === event.locationId) ?? walker;
    if (event.type === "ACTION_STARTED" && walker && target && walker.id !== target.id) stops.push(target);
    walker = target;
  }
  const legs = routeLegs(stops.map(s => s.id));
  const pointAt = (at: XY): PathPoint => points.find(p => p.x === at.x && p.y === at.y) ?? { ...STAIRS, floor: floorOf(at), ...at };
  let travel = 0;
  let cursor = 0;
  let position = first;
  const segments: PlaybackSegment[] = [];
  function append(duration: number, eventIndex: number, to: PathPoint | undefined, activity: string, moving = false, leg?: Leg) {
    segments.push({ start: cursor, end: cursor + duration, eventIndex, from: position, to, activity, moving, leg });
    cursor += duration;
    position = to;
  }
  mission.events.forEach((event, i) => {
    const target = points.find(p => p.id === event.locationId) ?? position;
    if (event.type === "ACTION_STARTED") {
      if (position && target && position.id !== target.id) {
        for (const leg of legs.filter(l => l.travel === travel)) {
          if (leg.kind === "walk") append(Math.max(400, legLength(leg) * 10), Math.max(0, i - 1), pointAt(leg.points.at(-1)!), `Traveling to ${target.id}`, true, leg);
          else append(1600, Math.max(0, i - 1), pointAt(leg.to), leg.up ? "Climbing the stairs" : "Descending the stairs", true, leg);
        }
        travel++;
        position = target;
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
  const at = segment.leg ? pointOnLeg(segment.leg, progress) : from && to ? { x: from.x + (to.x - from.x) * progress, y: from.y + (to.y - from.y) * progress } : { x: 0, y: 0 };
  return { ...segment, progress, ...at };
}
export function eventLabel(event: PathEvent) {
  if (event.type === "ACTION_STARTED") return activityLabel(event.actionType);
  return { MISSION_STARTED: "Mission started", MISSION_COMPLETED: "Mission complete", ACTION_FAILED: "Action failed", ACTION_COMPLETED: "Inspection complete" }[event.type] ?? event.type.replaceAll("_", " ");
}
