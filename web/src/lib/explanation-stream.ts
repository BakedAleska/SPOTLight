/* Browser side of the flag explanation stream (contract "Streaming Qwen
   drafts"). GET /api/flags/[id]/explanation/stream relays the backend's
   events: start · stage · draft · done | error. Drafts are shown while Qwen
   writes; `done.explanation` (the same JSON as the plain route) replaces them.

   Anything short of a `done` (non-200, network error, `error` event, stream
   ending early, timeout) falls back to GET /api/flags/[id]/explanation, so
   streaming can only ever add to the old behaviour. Aborting the caller's
   signal (dialog closed) stops both without a fallback. */

import { createSSEParser } from "./chat/sse";
import type { FlagExplanation } from "./flag-detail";
import { draftFromEvent, newerDraft, type QwenDraft } from "./qwen-draft";

export type ExplainStage = "cached" | "context" | "system2" | "storing";
export interface ExplainProgress { stage: ExplainStage | null; draft: QwenDraft | null }

const STAGES = new Set<string>(["cached", "context", "system2", "storing"]);
/** Backstop above the server's EXPLAIN_TIMEOUT_MS default (50 s); the backend deadline (45 s) ends it first. */
export const STREAM_TIMEOUT_MS = 60_000;
/** The plain route's existing browser timeout. */
export const JSON_TIMEOUT_MS = 55_000;

export function stageLabel(stage: ExplainStage | null, drafting: boolean): string {
  if (drafting) return stage === "storing" ? "Checking and saving the answer…" : "Qwen is writing…";
  switch (stage) {
    case "cached": return "Loading the saved explanation…";
    case "context": return "Gathering context…";
    case "system2": return "Qwen is thinking…";
    case "storing": return "Checking and saving the answer…";
    default: return "Starting the explanation…";
  }
}

function isExplanation(value: unknown): value is FlagExplanation {
  return !!value && typeof value === "object" && typeof (value as { source?: unknown }).source === "string";
}

/** A signal that aborts with `signal` or after `ms` (TimeoutError). */
function withTimeout(signal: AbortSignal, ms: number) {
  const controller = new AbortController();
  const forward = () => controller.abort(signal.reason);
  if (signal.aborted) forward();
  else signal.addEventListener("abort", forward, { once: true });
  const timer = setTimeout(() => controller.abort(new DOMException("The explanation timed out.", "TimeoutError")), ms);
  return { signal: controller.signal, clear: () => { clearTimeout(timer); signal.removeEventListener("abort", forward); } };
}

async function drain(reader: ReadableStreamDefaultReader<Uint8Array>) {
  try { while (!(await reader.read()).done) { /* discard */ } } catch { /* closed or aborted */ }
}

/** Reads the stream to its `done` event. Throws on anything else. */
export async function streamExplanation(flagId: string, signal: AbortSignal, onProgress: (progress: ExplainProgress) => void): Promise<FlagExplanation> {
  const response = await fetch(`/api/flags/${flagId}/explanation/stream`, { headers: { Accept: "text/event-stream" }, cache: "no-store", signal });
  if (!response.ok || !response.body || !(response.headers.get("content-type") ?? "").includes("text/event-stream")) {
    throw new Error(`The explanation stream did not start (HTTP ${response.status}).`);
  }
  let progress: ExplainProgress = { stage: null, draft: null };
  // Written by the parser callback; a done or error event ends the read loop.
  const outcome: { result?: FlagExplanation; failure?: string } = {};
  const parser = createSSEParser((message) => {
    if (outcome.result || outcome.failure) return;
    let data: unknown;
    try { data = JSON.parse(message.data); } catch { return; } // a malformed event is skipped, not fatal
    const body = data && typeof data === "object" ? data as Record<string, unknown> : {};
    if (message.event === "stage" && typeof body.stage === "string" && STAGES.has(body.stage)) {
      progress = { ...progress, stage: body.stage as ExplainStage };
      onProgress(progress);
    } else if (message.event === "draft") {
      const draft = draftFromEvent(data);
      // Show the newest call only: a late draft from an older call is dropped.
      if (draft && newerDraft(progress.draft, draft) === draft) {
        progress = { ...progress, draft };
        onProgress(progress);
      }
    } else if (message.event === "done") {
      if (isExplanation(body.explanation)) outcome.result = body.explanation;
      else outcome.failure = "The explanation stream ended with an unreadable answer.";
    } else if (message.event === "error") {
      outcome.failure = typeof body.detail === "string" && body.detail ? body.detail : typeof body.error === "string" ? body.error : "The explanation stream failed.";
    }
  });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  try {
    while (!outcome.result && !outcome.failure) {
      const { value, done } = await reader.read();
      if (done) { parser.feed(decoder.decode()); break; }
      parser.feed(decoder.decode(value, { stream: true }));
    }
  } catch (error) {
    reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    parser.end();
  }
  // The backend closes the stream right after `done` / `error`. Let it end on its
  // own instead of cutting it short, so no post-answer work is cancelled.
  void drain(reader);
  if (outcome.result) return outcome.result;
  throw new Error(outcome.failure ?? "The explanation stream ended before the answer.");
}

/** The plain JSON route, exactly as before streaming. */
export async function readExplanation(flagId: string, signal: AbortSignal): Promise<FlagExplanation> {
  const timed = withTimeout(signal, JSON_TIMEOUT_MS);
  try {
    const response = await fetch(`/api/flags/${flagId}/explanation`, { cache: "no-store", signal: timed.signal });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error((body as { error?: string }).error || "Cannot load this flag.");
    return body as FlagExplanation;
  } finally { timed.clear(); }
}

/** Stream first; on any stream failure, the plain route. */
export async function loadExplanation(flagId: string, signal: AbortSignal, handlers: {
  onProgress: (progress: ExplainProgress) => void;
  onFallback: () => void;
}): Promise<FlagExplanation> {
  const timed = withTimeout(signal, STREAM_TIMEOUT_MS);
  try {
    return await streamExplanation(flagId, timed.signal, handlers.onProgress);
  } catch (error) {
    if (signal.aborted) throw error;
    console.warn("Explanation stream failed; using the plain route", error);
  } finally { timed.clear(); }
  handlers.onFallback();
  return readExplanation(flagId, signal);
}
