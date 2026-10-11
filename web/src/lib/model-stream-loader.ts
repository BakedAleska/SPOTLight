import { backendRequest } from "./backend";
import { row, rows, type JsonRow, type ModelStream } from "./model-stream";
import { dashboardFeatures } from "./features";

export async function loadModelStream(mission?: string): Promise<ModelStream> {
  const warnings: string[] = [];
  const read = <T>(path: string) => backendRequest<T>(path, {}, 5000);
  const query = new URLSearchParams({ limit: "200" });
  if (mission) query.set("mission_id", mission);
  const base = await Promise.allSettled([
    read<JsonRow>(`/events?${query}`),
    read<JsonRow>(`/flags?limit=12${mission ? `&mission_id=${encodeURIComponent(mission)}` : ""}`),
  ]);
  let connected = false;
  const bodies = base.map((result, i) => {
    if (result.status === "fulfilled") { connected = true; return result.value; }
    warnings.push(i === 0 ? "Event feed unavailable." : "Flag outputs unavailable."); return {};
  });
  const events = rows(bodies[0].items);
  const flags = rows(bodies[1].items);
  const detailResults = dashboardFeatures().flagDetails ? await Promise.allSettled(flags.map(flag => read<JsonRow>(`/flags/${flag.flag_id}`))) : [];
  if (!dashboardFeatures().flagDetails) warnings.push("Flag model details are disabled by configuration.");
  const details = detailResults.flatMap(result => result.status === "fulfilled" ? [result.value] : []);
  if (detailResults.some(r => r.status === "rejected")) warnings.push("Some flag results could not be refreshed.");
  const missionId = mission ?? String([...events].reverse().find(e => e.mission_id)?.mission_id ?? flags[0]?.mission_id ?? "");
  let forecasts: JsonRow[] = [];
  let calls: JsonRow[] = [], stats: JsonRow[] = [], routing: JsonRow = {};
  const extra = await Promise.allSettled([
    missionId ? read<JsonRow>(`/system1/forecast?mission_id=${encodeURIComponent(missionId)}&live=false`) : Promise.resolve<JsonRow>({}),
    process.env.BACKEND_ADMIN_TOKEN ? backendRequest<JsonRow>("/ops/model-calls?recent=100", { headers: { "X-Admin-Token": process.env.BACKEND_ADMIN_TOKEN } }, 5000) : Promise.resolve<JsonRow>({}),
  ]);
  if (extra[0].status === "fulfilled") forecasts = rows(extra[0].value.items);
  else warnings.push("Stored forecasts unavailable.");
  if (extra[1].status === "fulfilled") {
    calls = rows(extra[1].value.recent); stats = rows(extra[1].value.stats); routing = row(extra[1].value.routing);
  } else warnings.push("Model-call metrics unavailable (backend or admin access).");
  if (!process.env.BACKEND_ADMIN_TOKEN) warnings.push("Model-call metrics need the server’s BACKEND_ADMIN_TOKEN.");
  return { connected, updatedAt: new Date().toISOString(), warnings, events, details, forecasts, calls, stats, routing };
}
