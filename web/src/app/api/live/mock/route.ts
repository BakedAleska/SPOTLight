import { connection } from "next/server";
import { mockPatrol } from "@/lib/mock-patrol";

/* GET /api/live/mock?mission=M001 — a mock live patrol built from the kit (lib/mock-patrol.ts). Read-only: the
   browser plays it back; nothing is queued for a robot runner and nothing is written. */
export async function GET(request: Request) {
  await connection();
  const raw = new URL(request.url).searchParams.get("mission") ?? "M001";
  const mission = /^M\d{3}$/i.test(raw) ? raw.toUpperCase() : "M001";
  const plan = await mockPatrol(mission);
  if (!plan) return Response.json({ error: `No kit mission ${mission}.` }, { status: 404 });
  return Response.json(plan, { headers: { "Cache-Control": "no-store" } });
}
