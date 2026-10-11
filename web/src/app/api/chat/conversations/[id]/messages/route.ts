import { chatError, guard, proxyStream, readJsonBody, validConversationId } from "@/lib/chat/proxy";

/* The answer stream. No timeout here on purpose: a thinking-mode answer can
   run for minutes (CHAT_TIMEOUT_S on the backend bounds it), and the backend
   sends a keep-alive comment every 15 s. request.signal is passed through so
   the browser's Stop cancels the Gemini call upstream. */
export async function POST(request: Request, context: RouteContext<"/api/chat/conversations/[id]/messages">) {
  const blocked = guard(request, { write: true });
  if (blocked) return blocked;
  const { id } = await context.params;
  if (!validConversationId(id)) return chatError(404, "not_found", "This chat does not exist.");
  const body = await readJsonBody(request);
  if (body instanceof Response) return body;
  return proxyStream(`/conversations/${id}/messages`, body.text, request.signal);
}
