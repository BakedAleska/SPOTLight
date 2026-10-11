import { chatError, guard, missionParam, proxyJson, readJsonBody } from "@/lib/chat/proxy";
import { normalizeMissionId } from "@/lib/chat/mission";

export async function GET(request: Request) {
  const blocked = guard(request);
  if (blocked) return blocked;
  // Forward only the documented query parameters, re-encoded.
  const incoming = new URL(request.url).searchParams;
  const params = new URLSearchParams();
  const q = incoming.get("q")?.trim();
  if (q) params.set("q", q.slice(0, 200));
  for (const key of ["limit", "offset"] as const) {
    const value = incoming.get(key);
    if (value && /^\d{1,4}$/.test(value)) params.set(key, value);
  }
  // chat.3: only this mission's chats.
  const mission = missionParam(request);
  if (mission instanceof Response) return mission;
  if (mission.missionId) params.set("mission_id", mission.missionId);
  const query = params.toString();
  return proxyJson(`/conversations${query ? `?${query}` : ""}`);
}

export async function POST(request: Request) {
  const blocked = guard(request, { write: true });
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (body instanceof Response) return body;
  // chat.3: an optional mission_id must look like one; the backend decides
  // whether it exists (422 unknown_mission). The body is forwarded verbatim.
  const missionId = (JSON.parse(body.text) as { mission_id?: unknown }).mission_id;
  if (missionId != null && (typeof missionId !== "string" || !normalizeMissionId(missionId))) {
    return chatError(422, "unknown_mission", "That is not a mission id.");
  }
  return proxyJson("/conversations", { method: "POST", body: body.text });
}
