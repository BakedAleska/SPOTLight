import { guard, proxyJson, readJsonBody } from "@/lib/chat/proxy";

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
  const query = params.toString();
  return proxyJson(`/conversations${query ? `?${query}` : ""}`);
}

export async function POST(request: Request) {
  const blocked = guard(request, { write: true });
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (body instanceof Response) return body;
  return proxyJson("/conversations", { method: "POST", body: body.text });
}
