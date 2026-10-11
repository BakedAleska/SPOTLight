import { guard, missionParam, proxyJson } from "@/lib/chat/proxy";

export async function GET(request: Request) {
  const blocked = guard(request);
  if (blocked) return blocked;
  // chat.3: ?mission_id= asks for that mission's four prompts.
  const mission = missionParam(request);
  if (mission instanceof Response) return mission;
  return proxyJson(mission.missionId ? `/suggestions?mission_id=${encodeURIComponent(mission.missionId)}` : "/suggestions");
}
