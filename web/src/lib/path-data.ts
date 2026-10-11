import { readFileSync } from "node:fs";
import path from "node:path";
import { getKit } from "./kit";
import type { PathMission, PathPoint } from "./path-types";
import { anchors } from "./waypoints";

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
          actionId: e.details.action_id, actionType: action?.action_type, durationSec: action?.duration_sec,
          assetName: action ? kit.assetById.get(action.asset_id)?.asset_name : undefined, locationId: e.details.location_id ?? action?.location_id,
          battery: e.details.battery_pct,
          error: e.event_type === "ACTION_FAILED" ? e.details.failure_type : e.event_type === "ACTION_COMPLETED" ? anomaly?.description : undefined,
          severity: anomaly?.severity };
      }) })),
  };
}
