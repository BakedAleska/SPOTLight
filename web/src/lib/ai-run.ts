import { backendRequest, BackendError } from "./backend";
import { dashboardFeatures } from "./features";
import { row, rows } from "./model-stream";
import { explainTimeoutMs } from "./flag-detail";

export async function handleAIRun(request: Request): Promise<Response> {
  try {
    const origin = request.headers.get("origin");
    if (!origin || new URL(origin).host !== request.headers.get("host")) return Response.json({ error: "Start AI runs from this dashboard." }, { status: 403 });
  } catch { return Response.json({ error: "Invalid origin." }, { status: 403 }); }
  let input: Record<string, unknown>;
  try { input = row(await request.json()); } catch { return Response.json({ error: "Invalid request." }, { status: 400 }); }
  const { action, mission, run, startedAt, flagId } = input;
  if (!["start", "next", "stop", "flags", "explain"].includes(String(action)) ||
    (mission !== undefined && (typeof mission !== "string" || !/^[\w-]{1,100}$/.test(mission))) ||
    (["next", "stop", "flags", "explain"].includes(String(action)) && (!Number.isSafeInteger(run) || Number(run) < 1 || typeof startedAt !== "string")) ||
    (action === "explain" && (!Number.isSafeInteger(flagId) || Number(flagId) < 1))) return Response.json({ error: "Invalid run request." }, { status: 400 });
  const token = process.env.BACKEND_ADMIN_TOKEN;
  if (!token) return Response.json({ error: "AI runs need a configured backend and server BACKEND_ADMIN_TOKEN. No data was processed." }, { status: 503 });
  const headers = { "X-Admin-Token": token, "Content-Type": "application/json" };
  const post = (path: string, body: unknown, timeout = 15000) => backendRequest<Record<string, unknown>>(path, { method: "POST", headers, body: JSON.stringify(body) }, timeout);
  try {
    if (action === "start") return Response.json(await post("/replay/start", { speed: 60, max_gap_s: 0.3, reset: false, ...(mission ? { missions: [mission] } : {}) }));
    const status = await backendRequest<Record<string, unknown>>("/replay/status", { headers }, 10000);
    if (status.run !== run || status.started_at !== startedAt) return Response.json({ error: "The backend is now tracking another run. Refresh before starting again." }, { status: 409 });
    if (action === "stop") return Response.json(await post("/replay/stop", {}));
    if (action === "next") return Response.json(status.state === "running" && status.mode === "stepped" ? await post("/replay/step", { run_id: run, budget_s: 5, max_events: 10 }, 15000) : status);
    if (status.state !== "finished") return Response.json({ error: "Wait for the data run to finish before generating explanations." }, { status: 409 });
    const flags = await backendRequest<Record<string, unknown>>(`/flags?limit=500${mission ? `&mission_id=${encodeURIComponent(mission)}` : ""}`, {}, 10000);
    const ids = rows(flags.items).map(f => Number(f.flag_id));
    if (action === "flags") return Response.json({ flagIds: dashboardFeatures().flagExplanations ? ids : [], explanationsEnabled: dashboardFeatures().flagExplanations });
    if (!dashboardFeatures().flagExplanations) return Response.json({ error: "Explanations are disabled by configuration." }, { status: 404 });
    if (!ids.includes(Number(flagId))) return Response.json({ error: "Flag is outside this run’s selected scope." }, { status: 400 });
    return Response.json(await backendRequest(`/flags/${flagId}/explain`, { method: "POST", headers, body: "{}" }, explainTimeoutMs()));
  } catch (error) {
    const status = error instanceof BackendError && error.status === 409 ? 409 : 502;
    return Response.json({ error: status === 409 ? "Another AI data run is already active. Wait for it to finish." : "Cannot reach the AI backend. The run may have started; check its status before retrying." }, { status });
  }
}
