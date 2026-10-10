import { backendRequest } from "@/lib/backend";
import { dashboardFeatures } from "@/lib/features";
import { flagFailure, validFlagId } from "@/lib/flag-route";

export async function POST(request: Request, context: RouteContext<"/api/flags/[id]/actions">) {
  if (!dashboardFeatures().flagActions) return Response.json({ error: "Flag actions are disabled." }, { status: 404 });
  const { id } = await context.params;
  if (!validFlagId(id)) return Response.json({ error: "Invalid flag." }, { status: 400 });
  const origin = request.headers.get("origin");
  let sameOrigin = false;
  try { sameOrigin = !!origin && new URL(origin).host === request.headers.get("host"); }
  catch { /* Malformed origins fail closed. */ }
  if (!sameOrigin) {
    return Response.json({ error: "Use the dashboard to update flags." }, { status: 403 });
  }
  let body: unknown;
  try { body = await request.json(); }
  catch { return Response.json({ error: "Invalid request." }, { status: 400 }); }
  if (!body || typeof body !== "object") return Response.json({ error: "Invalid request." }, { status: 400 });
  const { action, note, operator } = body as Record<string, unknown>;
  if (typeof action !== "string" || !["ACKNOWLEDGE", "RESOLVE", "ESCALATE"].includes(action) ||
      typeof operator !== "string" || !operator.trim() || operator.length > 100 ||
      (note !== undefined && (typeof note !== "string" || note.length > 4000))) {
    return Response.json({ error: "Choose a valid action, operator name, and note (up to 4000 characters)." }, { status: 400 });
  }
  try {
    return Response.json(await backendRequest(`/flags/${id}/ack`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, operator: operator.trim(), note }),
    }, 30_000));
  } catch (error) { return flagFailure(error); }
}
