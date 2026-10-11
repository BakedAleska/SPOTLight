import { connection } from "next/server";
import { backendRequest } from "@/lib/backend";
import { liveFailure, validRunId } from "@/lib/live";

export async function GET(request: Request) {
  await connection();
  const runId = new URL(request.url).searchParams.get("run_id") ?? "";
  if (!validRunId(runId)) return Response.json({ error: "Invalid run." }, { status: 400 });
  try { return Response.json(await backendRequest(`/live/kg?run_id=${runId}`), { headers: { "Cache-Control": "no-store" } }); }
  catch (error) { return liveFailure(error); }
}
