import { guard, proxyJson } from "@/lib/chat/proxy";

/* chat.3: the mission picker's list, one MissionSummary per mission. */
export async function GET(request: Request) {
  const blocked = guard(request);
  if (blocked) return blocked;
  const q = new URL(request.url).searchParams.get("q")?.trim();
  return proxyJson(q ? `/missions?${new URLSearchParams({ q: q.slice(0, 100) })}` : "/missions");
}
