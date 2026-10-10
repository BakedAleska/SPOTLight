import { readFileSync } from "node:fs";
import path from "node:path";
import { getKit } from "./kit";
import type { PathMission, PathPoint } from "./path-types";

// Image-pixel anchors at the tips of the labeled pins in fictional_floor_plan.png.
// The kit's CSV coordinates are a separate synthetic space, not image pixels.
const anchors: Record<string, [number, number]> = {
  WP01: [109, 475], WP02: [113, 328], WP03: [253, 249], WP04: [215, 102],
  WP05: [447, 239], WP06: [554, 103], WP07: [664, 297], WP08: [380, 424],
  WP09: [551, 426], WP10: [672, 438], WP11: [850, 151], WP12: [1001, 158],
  WP13: [836, 338], WP14: [992, 365], WP15: [896, 489],
};
export function getPathData(): { points: PathPoint[]; missions: PathMission[] } {
  const kit = getKit();
  const events = readFileSync(path.join(process.cwd(), "data/kit/replay.ndjson"), "utf8")
    .trim().split(/\r?\n/).map(line => JSON.parse(line) as {
      event_id: string; mission_id: string; timestamp: string; event_type: string;
      details: { action_id?: string; location_id?: string; battery_pct?: number; failure_type?: string };
    });
  return {
    points: kit.locations.map(p => ({ id: p.location_id, name: p.location_name, floor: p.floor,
      x: anchors[p.location_id][0], y: anchors[p.location_id][1] })),
    missions: kit.missions.filter(m => events.some(e => e.mission_id === m.mission_id)).map(m => ({ id: m.mission_id, name: m.mission_name,
      events: events.filter(e => e.mission_id === m.mission_id).map(e => {
        const action = e.details.action_id ? kit.actionById.get(e.details.action_id) : undefined;
        const anomaly = action ? kit.anomaliesByAction.get(action.action_id)?.[0] : undefined;
        return { id: e.event_id, missionId: e.mission_id, timestamp: e.timestamp, type: e.event_type,
          actionId: e.details.action_id, locationId: e.details.location_id ?? action?.location_id,
          battery: e.details.battery_pct,
          error: e.event_type === "ACTION_FAILED" ? e.details.failure_type : e.event_type === "ACTION_COMPLETED" ? anomaly?.description : undefined,
          severity: anomaly?.severity };
      }) })),
  };
}
