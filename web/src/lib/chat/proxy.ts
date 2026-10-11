/* Server-only helpers for the /api/chat/** route handlers. The browser only
   ever talks to these routes; they forward to FastAPI's /api/v1/chat with
   BACKEND_URL (same pattern as lib/backend.ts) and pass the backend's JSON
   error bodies — {error, detail, request_id} — through with their status, so
   the UI can branch on `error` codes like conversation_busy or gemini_unset. */

import { assistantFeatures } from "@/lib/features";
import { pipeEventStream } from "@/lib/sse-proxy";
import { normalizeMissionId } from "./mission";

const NO_STORE = { "Cache-Control": "no-store" };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Bodies above this are rejected before reaching the backend (8000-char
    content plus JSON overhead fits comfortably). */
export const MAX_BODY_BYTES = 64 * 1024;

export function chatBackendUrl(path: string) {
  const base = (process.env.BACKEND_URL || "http://127.0.0.1:8000/api/v1").replace(/\/$/, "");
  return `${base}/chat${path}`;
}

export function chatError(status: number, error: string, detail: string) {
  return Response.json({ error, detail }, { status, headers: NO_STORE });
}

/** Contract §4: a malformed id is a 404, not a 500 — and it never reaches
    the backend, so it can't be used to probe other paths. */
export function validConversationId(id: string) {
  return UUID.test(id);
}

/**
 * Feature switch, then (for writes) a same-origin check like the flag
 * actions route: a browser always sends Origin on POST/PATCH/DELETE, so a
 * missing or foreign one means the request didn't come from this app.
 */
export function guard(request: Request, { write = false } = {}): Response | null {
  // Read the request before anything else. With Cache Components a GET
  // handler that touches no request data is prerendered at build time, which
  // would freeze the feature switch (and a backend response) into the build;
  // a request-property read marks it request-time.
  const origin = request.headers.get("origin");
  if (!assistantFeatures().aiAssistant) return chatError(404, "disabled", "SPOTbot is disabled.");
  if (!write) return null;
  let sameOrigin = false;
  try { sameOrigin = !!origin && new URL(origin).host === request.headers.get("host"); }
  catch { /* Malformed origins fail closed. */ }
  return sameOrigin ? null : chatError(403, "forbidden", "Use the SPOTlight console to change chats.");
}

/**
 * chat.3: the `mission_id` query parameter, checked before it reaches the
 * backend. Absent → null (no filter); malformed → a 422 `unknown_mission`
 * Response, the same answer the backend gives an id it doesn't know.
 */
export function missionParam(request: Request): { missionId: string | null } | Response {
  const raw = new URL(request.url).searchParams.get("mission_id");
  if (raw === null || raw.trim() === "") return { missionId: null };
  const missionId = normalizeMissionId(raw);
  return missionId ? { missionId } : chatError(422, "unknown_mission", "That is not a mission id.");
}

/** Reads a JSON request body with a size cap. Returns a Response on failure. */
export async function readJsonBody(request: Request): Promise<{ text: string } | Response> {
  const text = await request.text();
  if (new TextEncoder().encode(text).length > MAX_BODY_BYTES) return chatError(413, "invalid_request", "The message is too long.");
  if (!text) return { text: "{}" };
  try {
    const parsed: unknown = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
  } catch {
    return chatError(400, "invalid_request", "The request body must be a JSON object.");
  }
  return { text };
}

function unreachable(error: unknown) {
  const timedOut = error instanceof DOMException && error.name === "TimeoutError";
  console.error("Chat backend request failed", error);
  return timedOut
    ? chatError(504, "backend_timeout", "The backend took too long to answer. Retry in a moment.")
    : chatError(502, "backend_unreachable", "Cannot reach the backend. Check that it is running, then retry.");
}

/** Forwards one JSON request and mirrors the backend's status and body. */
export async function proxyJson(path: string, init: { method?: string; body?: string } = {}, timeout = 15_000): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await fetch(chatBackendUrl(path), {
      method: init.method ?? "GET",
      headers: init.body !== undefined ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
      body: init.body,
      cache: "no-store",
      signal: AbortSignal.timeout(timeout),
    });
  } catch (error) {
    return unreachable(error);
  }
  if (upstream.status === 204) return new Response(null, { status: 204, headers: NO_STORE });
  const text = await upstream.text();
  try {
    JSON.parse(text);
  } catch {
    console.error(`Chat backend returned non-JSON (${upstream.status}) for ${path}`);
    return chatError(502, "upstream_error", "The backend returned an unexpected response.");
  }
  return new Response(text, { status: upstream.status, headers: { "Content-Type": "application/json", ...NO_STORE } });
}

/**
 * The streaming route: the backend's SSE body is piped through unbuffered
 * (lib/sse-proxy.ts). The browser's abort (Stop) reaches the backend through
 * request.signal and through the cancelled response body; pre-stream JSON
 * errors (404/409/422/503) are mirrored with their status.
 */
export function proxyStream(path: string, body: string, signal: AbortSignal): Promise<Response> {
  return pipeEventStream(chatBackendUrl(path), { method: "POST", body, signal }, (failure) =>
    failure.kind === "unreachable"
      ? unreachable(failure.error)
      : chatError(failure.status, "upstream_error", "The backend could not start an answer."));
}
