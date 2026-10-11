import { backendRequest } from "@/lib/backend";
import { liveFailure, sameOrigin, validRunId } from "@/lib/live";

export async function POST(request: Request, context: RouteContext<"/api/live/runs/[id]/cancel">) {
  if (!sameOrigin(request)) return Response.json({ error: "Cancel runs from the console." }, { status: 403 });
  const token = process.env.BACKEND_ADMIN_TOKEN;
  if (!token) return Response.json({ error: "Cancelling a run needs the server's BACKEND_ADMIN_TOKEN." }, { status: 503 });
  const { id } = await context.params;
  if (!validRunId(id)) return Response.json({ error: "Invalid run." }, { status: 400 });
  try {
    return Response.json(await backendRequest(`/live/runs/${id}/cancel`, {
      method: "POST", headers: { "X-Admin-Token": token },
    }));
  } catch (error) { return liveFailure(error); }
}
