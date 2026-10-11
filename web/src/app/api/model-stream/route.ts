import { connection } from "next/server";
import { loadModelStream } from "@/lib/model-stream-loader";
export async function GET(request: Request) {
  await connection();
  const mission = new URL(request.url).searchParams.get("mission_id") ?? undefined;
  if (mission && !/^[\w-]{1,100}$/.test(mission)) return Response.json({ error: "Invalid mission." }, { status: 400 });
  const data = await loadModelStream(mission);
  return Response.json(data, { status: data.connected ? 200 : 503, headers: { "Cache-Control": "no-store" } });
}
