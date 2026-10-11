import { guard, proxyJson } from "@/lib/chat/proxy";

export async function GET(request: Request) {
  return guard(request) ?? proxyJson("/suggestions");
}
