import type { DashboardData } from "./dashboard";
import type { Flag, Reason, Severity } from "./types";
import { dashboardFeatures } from "./features";
import { summarizeActionSuccess } from "./action-success";

interface Insight {
  key: string;
  value: number | null;
  pitch: string;
  detail: {
    above_25_success_pct?: number | null;
    missions?: number;
    reported_completed?: number;
    completed_with_failures?: number;
  };
}

interface BackendFlag {
  flag_id: number;
  mission_id: string | null;
  asset_id: string | null;
  location_id: string | null;
  severity: string;
  score: number;
  title: string;
  reasons: { rule: string; detail?: unknown; weight?: unknown }[];
}

// Called only by the server route. Browser requests stay on the frontend origin.
export class BackendError extends Error {
  constructor(public status: number) { super(`Backend returned ${status}`); }
}

export function backendUrl(path: string) {
  return `${(process.env.BACKEND_URL || "http://127.0.0.1:8000/api/v1").replace(/\/$/, "")}${path}`;
}

export async function backendRequest<T>(path: string, init: RequestInit = {}, timeout = 10_000): Promise<T> {
  const response = await fetch(backendUrl(path), {
    ...init,
    cache: "no-store",
    signal: AbortSignal.timeout(timeout),
  });
  if (!response.ok) throw new BackendError(response.status);
  return response.json() as Promise<T>;
}

const read = backendRequest;

export async function loadDashboard(): Promise<DashboardData> {
  const [overview, queue, assets, locations, reliability] = await Promise.all([
    read<{ insights: Insight[]; generated_at: string }>("/insights/overview"),
    read<{ items: BackendFlag[]; page: { total: number } }>("/flags?status=OPEN&limit=500"),
    read<{ asset_id: string; asset_name: string }[]>("/assets"),
    read<{ location_id: string; location_name: string }[]>("/locations"),
    read<{ rows: { group: string; n: number; n_success: number }[] }>("/insights/reliability?group_by=action_type"),
  ]);
  const assetNames = new Map(assets.map((asset) => [asset.asset_id, asset.asset_name]));
  const locationNames = new Map(locations.map((location) => [location.location_id, location.location_name]));
  const green = overview.insights.find((insight) => insight.key === "green_but_broken");
  const battery = overview.insights.find((insight) => insight.key === "battery_cliff");
  const config = overview.insights.find((insight) => insight.key === "v1_vs_v2_matched_battery");
  const byType = reliability.rows;
  const hiddenFailures = {
    count: green?.detail.completed_with_failures ?? 0,
    ofCompleted: green?.detail.reported_completed ?? 0,
  };
  const flags: Flag[] = queue.items.map((flag) => {
    if (!["LOW", "MEDIUM", "HIGH", "CRITICAL"].includes(flag.severity)) {
      throw new Error(`Unsupported backend severity: ${flag.severity}`);
    }
    const reasons: Reason[] = flag.reasons.flatMap((reason) =>
      typeof reason.detail === "string"
        ? [{ rule: reason.rule, detail: reason.detail, weight: typeof reason.weight === "number" ? reason.weight : 0 }]
        : [],
    );
    return {
      id: String(flag.flag_id),
      missionId: flag.mission_id ?? "Unassigned",
      severity: flag.severity as Severity,
      score: flag.score,
      title: flag.title,
      assetName: flag.asset_id ? assetNames.get(flag.asset_id) ?? flag.asset_id : "No asset",
      locationName: flag.location_id ? locationNames.get(flag.location_id) ?? flag.location_id : "No waypoint",
      reasons,
    };
  });
  return {
    source: "backend",
    degraded: null,
    features: dashboardFeatures(),
    stats: [
      { label: "Missions", value: String(green?.detail.missions ?? 0), note: `${byType.reduce((sum, row) => sum + row.n, 0)} actions at ${locations.length} waypoints.` },
      { label: "Hiding failures", value: String(hiddenFailures.count), unit: `of ${hiddenFailures.ofCompleted}`, note: green?.pitch ?? "No completed missions yet." },
      { label: "Success at ≤25% battery", value: battery?.value == null ? "—" : String(battery.value), unit: "%", note: battery?.pitch ?? "No actions in this battery band yet." },
      { label: "V2 vs V1 at matched battery", value: config?.value == null ? "—" : `${config.value > 0 ? "+" : ""}${config.value}`, unit: "pts", note: config?.pitch ?? "Both configurations need actions above 40% battery to compare." },
    ],
    flags,
    flagCount: queue.page.total,
    actionSuccess: summarizeActionSuccess(byType.map((row) => ({ type: row.group, total: row.n, success: row.n_success }))),
    hiddenFailures,
    generatedAt: overview.generated_at,
  };
}
