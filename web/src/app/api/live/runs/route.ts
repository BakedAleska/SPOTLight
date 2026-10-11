import { connection } from "next/server";
import { BackendError, backendRequest } from "@/lib/backend";
import { liveFailure, sameOrigin } from "@/lib/live";

export async function GET() {
  await connection();
  try { return Response.json(await backendRequest("/live/runs?limit=15"), { headers: { "Cache-Control": "no-store" } }); }
  catch (error) { return liveFailure(error); }
}

/* The operator's Start button. The admin token stays on the server (BACKEND_ADMIN_TOKEN). */
export async function POST(request: Request) {
  if (!sameOrigin(request)) return Response.json({ error: "Start runs from the console." }, { status: 403 });
  const token = process.env.BACKEND_ADMIN_TOKEN;
  if (!token) return Response.json({ error: "Starting a run needs the server's BACKEND_ADMIN_TOKEN." }, { status: 503 });
  let body: unknown;
  try { body = await request.json(); } catch { return Response.json({ error: "Invalid request." }, { status: 400 }); }
  const { route_id, operator } = (body ?? {}) as Record<string, unknown>;
  if (typeof route_id !== "string" || !/^R[1-5]$/.test(route_id)) {
    return Response.json({ error: "Pick a route." }, { status: 400 });
  }
  try {
    return Response.json(await backendRequest("/live/runs", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Admin-Token": token },
      body: JSON.stringify({ route_id, requested_by: typeof operator === "string" ? operator.slice(0, 100) : "operator" }),
    }));
  } catch (error) {
    if (error instanceof BackendError && error.status === 409) {
      return Response.json({ error: "A run is already queued or walking. Wait for it or cancel it." }, { status: 409 });
    }
    return liveFailure(error);
  }
}
