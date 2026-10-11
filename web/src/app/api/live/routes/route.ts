import { connection } from "next/server";
import { backendRequest } from "@/lib/backend";
import { liveFailure } from "@/lib/live";

export async function GET() {
  await connection();
  try { return Response.json(await backendRequest("/live/routes"), { headers: { "Cache-Control": "no-store" } }); }
  catch (error) { return liveFailure(error); }
}
