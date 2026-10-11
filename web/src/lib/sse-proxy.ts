/* Server-only: pipes a backend Server-Sent Events response through a route
   handler, shared by the chat answer stream and the flag explanation stream.

   The backend's SSE body is handed to the Response as-is (no reading, no
   TransformStream), so each event leaves as soon as it arrives. `no-transform`
   stops Next's gzip from buffering it, and X-Accel-Buffering does the same for
   an nginx in front. A browser abort reaches the backend two ways: the caller's
   signal (request.signal) aborts the upstream fetch, and a cancelled response
   body cancels upstream.body.

   `headersTimeoutMs` bounds only the wait for the upstream's response headers:
   once the backend answers, the timer is cleared and the body runs for as long
   as the backend keeps it open (its own deadline ends it) or the browser stays. */

const NO_STORE = { "Cache-Control": "no-store" };

export const EVENT_STREAM_HEADERS = {
  "Content-Type": "text/event-stream; charset=utf-8",
  "Cache-Control": "no-cache, no-transform",
  "X-Accel-Buffering": "no",
};

/** Why the stream never started, for the caller to word its own error body. */
export type StreamStartFailure =
  /** fetch threw: the backend is down, or (timedOut) the caller's timeout fired. */
  | { kind: "unreachable"; error: unknown; timedOut: boolean }
  /** The backend answered, but not with an event stream and not with JSON. */
  | { kind: "upstream"; status: number };

const isTimeout = (value: unknown) => value instanceof DOMException && value.name === "TimeoutError";

export async function pipeEventStream(
  url: string,
  init: { method?: string; body?: string; signal: AbortSignal; headersTimeoutMs?: number },
  fail: (failure: StreamStartFailure) => Response,
): Promise<Response> {
  const timer = init.headersTimeoutMs ? new AbortController() : null;
  const timeout = timer
    ? setTimeout(() => timer.abort(new DOMException("No response headers in time.", "TimeoutError")), init.headersTimeoutMs)
    : undefined;
  const signal = timer ? AbortSignal.any([init.signal, timer.signal]) : init.signal;
  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: init.method ?? "POST",
      headers: init.body !== undefined ? { "Content-Type": "application/json", Accept: "text/event-stream" } : { Accept: "text/event-stream" },
      body: init.body,
      cache: "no-store",
      signal,
    });
  } catch (error) {
    const timedOut = isTimeout(error) || (signal.aborted && isTimeout(signal.reason));
    // The browser went away before the backend answered: end quietly.
    if (signal.aborted && !timedOut) return new Response(null, { status: 499 });
    return fail({ kind: "unreachable", error, timedOut });
  } finally {
    clearTimeout(timeout);   // headers are in (or the fetch failed): the body is not under this timer
  }
  const type = upstream.headers.get("content-type") ?? "";
  if (!upstream.ok || !type.includes("text/event-stream") || !upstream.body) {
    // Pre-stream errors (404/409/422/503/504) are JSON; mirror them.
    const text = await upstream.text().catch(() => "");
    try {
      JSON.parse(text);
      return new Response(text, { status: upstream.ok ? 502 : upstream.status, headers: { "Content-Type": "application/json", ...NO_STORE } });
    } catch {
      return fail({ kind: "upstream", status: upstream.ok ? 502 : upstream.status });
    }
  }
  return new Response(upstream.body, { status: 200, headers: EVENT_STREAM_HEADERS });
}
