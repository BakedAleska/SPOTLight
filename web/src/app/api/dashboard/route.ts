import { connection } from "next/server";

import { loadDashboard } from "@/lib/backend";

export async function GET() {
  // Live data: run at request time only. Without this, `cacheComponents` tries to prerender the route at build time,
  // the backend fetch is aborted, and the catch below logs a misleading HANGING_PROMISE_REJECTION.
  await connection();
  try {
    return Response.json(await loadDashboard(), { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    console.error("Dashboard backend request failed", error);
    return Response.json(
      { error: "Cannot reach the dashboard data. Check that the backend is running, then retry." },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
}
