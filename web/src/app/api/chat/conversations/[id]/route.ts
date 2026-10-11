import { chatError, guard, proxyJson, readJsonBody, validConversationId } from "@/lib/chat/proxy";

type Context = RouteContext<"/api/chat/conversations/[id]">;

const missing = () => chatError(404, "not_found", "This chat does not exist.");

export async function GET(request: Request, context: Context) {
  const blocked = guard(request);
  if (blocked) return blocked;
  const { id } = await context.params;
  if (!validConversationId(id)) return missing();
  return proxyJson(`/conversations/${id}`);
}

export async function PATCH(request: Request, context: Context) {
  const blocked = guard(request, { write: true });
  if (blocked) return blocked;
  const { id } = await context.params;
  if (!validConversationId(id)) return missing();
  const body = await readJsonBody(request);
  if (body instanceof Response) return body;
  return proxyJson(`/conversations/${id}`, { method: "PATCH", body: body.text });
}

export async function DELETE(request: Request, context: Context) {
  const blocked = guard(request, { write: true });
  if (blocked) return blocked;
  const { id } = await context.params;
  if (!validConversationId(id)) return missing();
  return proxyJson(`/conversations/${id}`, { method: "DELETE" });
}
