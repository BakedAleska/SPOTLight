import { backendRequest } from "@/lib/backend";
import type { PathFeed } from "@/lib/path-types";

export async function GET(request: Request) {
  const incoming = new URL(request.url).searchParams;
  const query = new URLSearchParams({ limit: "500" });
  for (const key of ["mission_id", "after"]) {
    const value = incoming.get(key);
    if (value) query.set(key, value);
  }
  try {
    const feed = await backendRequest<{ items: {
      event_id: string; mission_id: string; ts: string; event_type: string;
      action_id?: string; location_id?: string; battery_pct?: number; title?: string;
      severity?: string; escalated?: boolean; payload?: { details?: { failure_type?: string } };
    }[]; next_cursor: string | null }>(`/events?${query}`);
    const body: PathFeed = { nextCursor: feed.next_cursor, items: feed.items.map(e => ({
      id: e.event_id, missionId: e.mission_id, timestamp: e.ts, type: e.event_type,
      actionId: e.action_id, locationId: e.location_id, battery: e.battery_pct,
      error: e.event_type === "ACTION_FAILED" ? e.payload?.details?.failure_type ?? e.title ?? "Action failed" : e.escalated ? e.title : undefined,
      severity: e.severity,
    })) };
    return Response.json(body, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return Response.json({ error: "Live event feed unavailable" }, { status: 503 });
  }
}
