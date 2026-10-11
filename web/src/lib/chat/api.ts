/* Browser-side calls to the /api/chat/** proxy routes. */

import { readSSE } from "./sse";
import type { ChatErrorCode, ChatEvent, Conversation, SendBody } from "./types";

export class ChatHttpError extends Error {
  constructor(public status: number, public code: ChatErrorCode, detail: string) {
    super(detail);
  }
}

async function failure(response: Response): Promise<ChatHttpError> {
  let code: ChatErrorCode = response.status === 404 ? "not_found" : "upstream_error";
  let detail = `Request failed (${response.status}).`;
  try {
    const body = (await response.json()) as { error?: string; detail?: unknown };
    if (typeof body.error === "string") code = body.error;
    if (typeof body.detail === "string") detail = body.detail;
    else if (body.detail != null) detail = JSON.stringify(body.detail);
  } catch {
    /* Non-JSON error body: keep the generic text. */
  }
  return new ChatHttpError(response.status, code, detail);
}

export async function chatFetch<T>(url: string, init: RequestInit = {}, timeout = 20_000): Promise<T> {
  const response = await fetch(url, {
    cache: "no-store",
    ...init,
    headers: init.body ? { "Content-Type": "application/json", ...init.headers } : init.headers,
    signal: init.signal ?? AbortSignal.timeout(timeout),
  });
  if (!response.ok) throw await failure(response);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

/** SWR fetcher. */
export const chatFetcher = <T>(url: string) => chatFetch<T>(url);

/** chat.3: `mission_id` makes it a mission chat; omitted for a normal one. */
export function createConversation(body: { model: string; mode: string; mission_id?: string }) {
  return chatFetch<Conversation>("/api/chat/conversations", { method: "POST", body: JSON.stringify(body) });
}

export function updateConversation(id: string, body: { title?: string; pinned?: boolean; model?: string; mode?: string }) {
  return chatFetch<Conversation>(`/api/chat/conversations/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function deleteConversation(id: string) {
  return chatFetch<void>(`/api/chat/conversations/${id}`, { method: "DELETE" });
}

const EVENTS = new Set(["meta", "sources", "thinking", "delta", "done", "title", "error"]);

/**
 * POSTs one turn and calls onEvent per SSE event until the server closes the
 * stream. Pre-stream failures (404/409/422/503, proxy 502) throw
 * ChatHttpError; aborting the signal throws an AbortError.
 */
export async function streamTurn(conversationId: string, body: SendBody, signal: AbortSignal, onEvent: (event: ChatEvent) => void): Promise<void> {
  const response = await fetch(`/api/chat/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    cache: "no-store",
    signal,
  });
  if (!response.ok || !response.body) throw await failure(response);
  await readSSE(response.body, (message) => {
    if (!EVENTS.has(message.event)) return;
    let data: unknown;
    try {
      data = JSON.parse(message.data);
    } catch {
      return; // A malformed event is skipped, not fatal.
    }
    onEvent({ event: message.event, data } as ChatEvent);
  });
}

/** Plain-language copy for each error code (contract §4). `model` is the
    label of the model that failed ("Gemini 3.8 Flash"), when known. */
export function describeError(code: ChatErrorCode, detail?: string | null, model?: string | null): { title: string; hint: string } {
  const name = model || "The model";
  switch (code) {
    case "rate_limited":
      return { title: model ? `Rate limit reached for ${model}` : "Rate limit reached", hint: "The Gemini free tier allows 5 requests a minute per model." };
    case "model_unavailable":
      return { title: `${name} is unavailable`, hint: "Gemini rejected the model or its settings, or it is overloaded. Try another model." };
    case "gemini_auth":
      return { title: "Gemini rejected the API key", hint: "Check GEMINI_API_KEY in the backend environment." };
    case "gemini_unset":
      return { title: "Gemini API key not configured", hint: "Set GEMINI_API_KEY in the root .env and restart the backend." };
    case "blocked":
      return { title: `${name} declined to answer`, hint: "A safety or recitation filter stopped the answer. Try rephrasing." };
    case "retrieval_failed":
      return { title: "Mission data is unavailable", hint: "The database is down, so there is nothing to ground an answer in." };
    case "conversation_busy":
      return { title: "This chat is still answering", hint: "The previous answer has not finished, possibly in another tab. Wait a moment, then retry." };
    case "not_found":
      return { title: "This chat no longer exists", hint: "It may have been deleted. Start a new chat." };
    case "unknown_mission":
      return { title: "That mission doesn’t exist", hint: "Pick another mission, or clear the focus to ask about the whole fleet." };
    case "invalid_request":
      return { title: "The request was rejected", hint: detail || "Check the message and model, then retry." };
    case "backend_unreachable":
    case "backend_timeout":
      return { title: "Cannot reach the backend", hint: "Check that it is running, then retry." };
    case "connection_lost":
      return { title: "The connection dropped", hint: "The answer stopped before it finished. Retry to ask again." };
    case "disabled":
      return { title: "AI Assistance is disabled", hint: "Set FEATURE_AI_ASSISTANT=true to turn it on." };
    case "upstream_error":
      return { title: `${name} did not answer`, hint: "It timed out or returned an error. Retry in a moment." };
    default:
      return { title: "Something went wrong", hint: "Retry in a moment." };
  }
}

/** Errors a different model can fix: offer a one-click switch. */
export function offersModelSwitch(code: ChatErrorCode) {
  return code === "rate_limited" || code === "model_unavailable";
}
