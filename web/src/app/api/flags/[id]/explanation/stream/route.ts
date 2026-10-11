import { backendUrl } from "@/lib/backend";
import { dashboardFeatures } from "@/lib/features";
import { explainTimeoutMs } from "@/lib/flag-detail";
import { validFlagId } from "@/lib/flag-route";
import { pipeEventStream } from "@/lib/sse-proxy";

/* The explanation as a stream (contract "Streaming Qwen drafts"): the
   backend's POST /flags/{id}/explain/stream piped through unbuffered, so the
   flag detail can show Qwen's unverified draft while it is written and swap in
   the checked `done` explanation. refresh=false keeps the JSON route's cache
   semantics. Pre-stream backend errors (404, 504) are mirrored as JSON; the
   browser then falls back to GET /api/flags/[id]/explanation.

   EXPLAIN_TIMEOUT_MS (as for the JSON route) bounds only the wait for the
   backend's response headers; the body then runs until the backend ends it
   (its REQUEST_DEADLINE_S) or the browser aborts (dialog closed, through
   request.signal). maxDuration lifts a non-Fluid Vercel function's 10 s
   default (the backend's 45 s deadline must fit). */
export const maxDuration = 60;

export async function GET(request: Request, context: RouteContext<"/api/flags/[id]/explanation/stream">) {
  // Read the request before anything else: with Cache Components a GET handler
  // that touches no request data can be prerendered, freezing the feature
  // switch into the build (see lib/chat/proxy.ts). request.signal alone does not
  // count as a request read; request.headers does.
  request.headers.get("accept");
  if (!dashboardFeatures().flagExplanations) return Response.json({ error: "Explanations are disabled." }, { status: 404 });
  const { id } = await context.params;
  if (!validFlagId(id)) return Response.json({ error: "Invalid flag." }, { status: 400 });
  const init = { method: "POST", signal: request.signal, headersTimeoutMs: explainTimeoutMs() };
  return pipeEventStream(backendUrl(`/flags/${id}/explain/stream?refresh=false`), init, (failure) => {
    if (failure.kind === "unreachable") {
      console.error("Explanation stream failed to start", failure.error);
      return failure.timedOut
        ? Response.json({ error: "The explanation took too long." }, { status: 504, headers: { "Cache-Control": "no-store" } })
        : Response.json({ error: "Cannot reach the backend." }, { status: 502, headers: { "Cache-Control": "no-store" } });
    }
    return Response.json({ error: "The backend could not start an explanation." }, { status: failure.status, headers: { "Cache-Control": "no-store" } });
  });
}
