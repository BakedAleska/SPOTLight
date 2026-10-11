import { connection } from "next/server";
import { backendRequest } from "@/lib/backend";
import { liveFailure, validRunId } from "@/lib/live";

export async function GET(_request: Request, context: RouteContext<"/api/live/runs/[id]">) {
  await connection();
  const { id } = await context.params;
  if (!validRunId(id)) return Response.json({ error: "Invalid run." }, { status: 400 });
  try { return Response.json(await backendRequest(`/live/runs/${id}`), { headers: { "Cache-Control": "no-store" } }); }
  catch (error) { return liveFailure(error); }
}
