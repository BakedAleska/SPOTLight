// ─────────────────────────────────────────────────────────────────────────────
// The Overview's analytics panel numbers: the few Analytics blocks it draws,
// plus a per-mission split so an open flag can be set against the fleet.
//
// SERVER ONLY (reads the kit through @/lib/fleet-analytics and @/lib/kit).
// The result is plain JSON for the client Dashboard.
// ─────────────────────────────────────────────────────────────────────────────

import { getKit } from "./kit";
import { actionLabel, fleetAnalytics, pct } from "./fleet-analytics";
import type { OverviewAnalyticsData } from "./overview";

export function overviewAnalytics(): OverviewAnalyticsData {
  const { battery, missions, failureModes } = fleetAnalytics();
  const { actions } = getKit();

  const counts = new Map<string, { n: number; ok: number }>();
  const byMission: OverviewAnalyticsData["byMission"] = {};
  for (const action of actions) {
    const ok = action.status === "SUCCESS" ? 1 : 0;
    const type = counts.get(action.action_type) ?? { n: 0, ok: 0 };
    counts.set(action.action_type, { n: type.n + 1, ok: type.ok + ok });

    const mission = (byMission[action.mission_id] ??= { types: {}, failures: {} });
    const own = (mission.types[action.action_type] ??= { n: 0, ok: 0 });
    own.n += 1;
    own.ok += ok;
    if (!ok) {
      const mode = action.failure_type || "UNSPECIFIED";
      mission.failures[mode] = (mission.failures[mode] ?? 0) + 1;
    }
  }

  const types = [...counts]
    .map(([key, { n, ok }]) => ({ key, label: actionLabel(key), rate: pct(ok, n), n }))
    // Best first, as on the Analytics page, so the eye runs down to the weakest.
    .sort((a, b) => b.rate - a.rate || b.n - a.n);
  const ok = actions.filter((action) => action.status === "SUCCESS").length;

  return { types, fleetRate: pct(ok, actions.length), battery, missions, failureModes, byMission };
}
