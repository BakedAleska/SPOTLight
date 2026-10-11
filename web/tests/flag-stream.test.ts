/* Streaming Qwen drafts, web side (docs/backend-services-contract.md "Streaming Qwen drafts"):
   the SSE proxy route, the browser reader with its JSON fallback, and the pure draft / poll helpers. */
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { GET as stream } from "../src/app/api/flags/[id]/explanation/stream/route";
import { loadExplanation, stageLabel, streamExplanation, type ExplainProgress } from "../src/lib/explanation-stream";
import { draftFromCapture, draftFromEvent, draftLength, newerDraft, revealDraft, type QwenDraft } from "../src/lib/qwen-draft";
import { detailPollMs, DRAFT_POLL_MS, POLL_MS } from "../src/lib/live";
import { maxDuration as streamMaxDuration } from "../src/app/api/flags/[id]/explanation/stream/route";
import { maxDuration as jsonMaxDuration } from "../src/app/api/flags/[id]/explanation/route";
import { pipeEventStream } from "../src/lib/sse-proxy";

const originalFetch = globalThis.fetch;
const keys = ["FEATURE_FLAG_DETAILS", "FEATURE_FLAG_EXPLANATIONS", "BACKEND_URL", "EXPLAIN_TIMEOUT_MS"] as const;
const originalEnv = keys.map((key) => process.env[key]);
// The fallback and failure paths log on purpose; keep the test output readable (each test file is its own process).
console.warn = () => undefined;
console.error = () => undefined;
afterEach(() => {
  globalThis.fetch = originalFetch;
  keys.forEach((key, index) => { if (originalEnv[index] === undefined) delete process.env[key]; else process.env[key] = originalEnv[index]; });
});

const ctx = (id = "7") => ({ params: Promise.resolve({ id }) });
const req = (signal?: AbortSignal) => new Request("http://localhost:3000/api/flags/7/explanation/stream", { headers: { host: "localhost:3000", accept: "text/event-stream" }, signal });
const encode = (text: string) => new TextEncoder().encode(text);
const sse = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const sseResponse = (body: string | ReadableStream<Uint8Array>) =>
  new Response(body, { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
const EXPLANATION = { answer: "Take a manual reading.", reasoning: "The gauge disagrees [V1]", recommended_action: "MANUAL_READING", source: "system2", model: "qwen" };

/* ── the route ─────────────────────────────────────────────────────────────── */

test("the feature switch and the flag id are checked before the backend is contacted", async () => {
  globalThis.fetch = async () => { throw new Error("Backend must not be called"); };
  process.env.FEATURE_FLAG_EXPLANATIONS = "false";
  assert.equal((await stream(req(), ctx())).status, 404);
  delete process.env.FEATURE_FLAG_EXPLANATIONS;
  process.env.FEATURE_FLAG_DETAILS = "false";
  assert.equal((await stream(req(), ctx())).status, 404);
  delete process.env.FEATURE_FLAG_DETAILS;
  for (const id of ["0", "../ops", "7?refresh=true", "abc", "99999999999999999999"]) {
    assert.equal((await stream(req(), ctx(id))).status, 400, id);
  }
});

test("POSTs the backend stream with refresh=false and pipes it through unbuffered", async () => {
  process.env.BACKEND_URL = "http://backend:8000/api/v1/";
  let push!: (text: string) => void;
  let seen: { url: string; init?: RequestInit } | undefined;
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    seen = { url: String(url), init };
    return sseResponse(new ReadableStream<Uint8Array>({ start(controller) { push = (text) => controller.enqueue(encode(text)); } }));
  }) as typeof fetch;
  const response = await stream(req(), ctx());
  assert.equal(seen?.url, "http://backend:8000/api/v1/flags/7/explain/stream?refresh=false");
  assert.equal(seen?.init?.method, "POST");
  assert.equal(seen?.init?.body, undefined);
  assert.equal(new Headers(seen?.init?.headers).get("accept"), "text/event-stream");
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /text\/event-stream/);
  assert.equal(response.headers.get("cache-control"), "no-cache, no-transform");
  assert.equal(response.headers.get("x-accel-buffering"), "no");
  const reader = response.body!.getReader();
  push(sse("start", { flag_id: 7, request_id: "r1" }));
  assert.match(new TextDecoder().decode((await reader.read()).value), /event: start/);
  push(": keep-alive\n\n");
  assert.equal(new TextDecoder().decode((await reader.read()).value), ": keep-alive\n\n");
  await reader.cancel();
});

test("pre-stream backend JSON errors keep their status and body", async () => {
  for (const [status, body] of [[404, { error: "not_found", detail: "flag 7" }], [504, { error: "deadline_exceeded" }]] as const) {
    globalThis.fetch = async () => Response.json(body, { status });
    const response = await stream(req(), ctx());
    assert.equal(response.status, status);
    assert.deepEqual(await response.json(), body);
  }
});

test("a dead backend, a timeout and a non-stream 200 are readable JSON errors", async () => {
  globalThis.fetch = async () => { throw new TypeError("fetch failed"); };
  assert.equal((await stream(req(), ctx())).status, 502);
  globalThis.fetch = async () => { throw new DOMException("The operation timed out.", "TimeoutError"); };
  assert.equal((await stream(req(), ctx())).status, 504);
  globalThis.fetch = async () => new Response("<html>", { headers: { "Content-Type": "text/html" } });
  const response = await stream(req(), ctx());
  assert.equal(response.status, 502);
  assert.equal(typeof (await response.json()).error, "string");
});

test("the timeout covers only the wait for headers, never the streamed body", async () => {
  const fail = () => Response.json({ error: "timeout" }, { status: 504 });
  // no headers in time: the fail callback's timeout answer
  globalThis.fetch = ((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(init.signal!.reason));
  })) as typeof fetch;
  const late = await pipeEventStream("http://backend/x", { signal: new AbortController().signal, headersTimeoutMs: 30 }, fail);
  assert.equal(late.status, 504);
  // headers in time: the body keeps flowing well past the timeout, and the upstream signal is never aborted
  let push!: (text: string) => void;
  let upstream: AbortSignal | undefined;
  globalThis.fetch = (async (_url: string, init?: RequestInit) => {
    upstream = init?.signal ?? undefined;
    return sseResponse(new ReadableStream<Uint8Array>({ start(controller) { push = (text) => controller.enqueue(encode(text)); } }));
  }) as typeof fetch;
  const response = await pipeEventStream("http://backend/x", { signal: new AbortController().signal, headersTimeoutMs: 30 }, fail);
  assert.equal(response.status, 200);
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(upstream?.aborted, false);
  const reader = response.body!.getReader();
  push(sse("draft", { call: 1, fields: { answer: "still streaming" } }));
  assert.match(new TextDecoder().decode((await reader.read()).value), /still streaming/);
  await reader.cancel();
});

test("both explanation routes lift Vercel's default function duration", () => {
  assert.equal(streamMaxDuration, 60);
  assert.equal(jsonMaxDuration, 60);
});

test("closing the dialog before the backend answers aborts the upstream fetch (499)", async () => {
  let called!: () => void;
  const fetchCalled = new Promise<void>((resolve) => { called = resolve; });
  let upstreamAborted = false;
  globalThis.fetch = ((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => { upstreamAborted = true; reject(new DOMException("aborted", "AbortError")); });
    called();
  })) as typeof fetch;
  const controller = new AbortController();
  const pending = stream(req(controller.signal), ctx());
  await fetchCalled;
  controller.abort();
  const response = await pending;
  assert.equal(upstreamAborted, true);
  assert.equal(response.status, 499);
});

/* ── the browser reader and its fallback ───────────────────────────────────── */

function mockBrowser(streamBody: () => Response | Promise<Response>, json: () => Response = () => Response.json(EXPLANATION)) {
  const calls: string[] = [];
  globalThis.fetch = (async (url: string) => {
    calls.push(String(url));
    return String(url).endsWith("/stream") ? streamBody() : json();
  }) as typeof fetch;
  return calls;
}

test("stages and drafts are reported as they arrive; done resolves with the checked explanation", async () => {
  const draft = (call: number, answer: string) => sse("draft", { call, reasoning: "", fields: { evidence: [{ claim: "Gauge V1 reads 121 psi", ids: ["V1"] }], answer } });
  // One event split across chunks, a keep-alive, and a stale draft from an older call after a newer one.
  const chunks = [sse("start", { flag_id: 7, request_id: "r1" }), sse("stage", { stage: "context" }), "event: stage\nda", "ta: {\"stage\":\"system2\"}\n\n",
    ": keep-alive\n\n", draft(1, "The gau"), draft(2, "Take a"), draft(1, "stale"), draft(2, "Take a manual"), sse("stage", { stage: "storing" }),
    sse("done", { explanation: EXPLANATION }), sse("draft", { call: 3, fields: { answer: "after done" } })];
  const calls = mockBrowser(() => sseResponse(new ReadableStream({ start(c) { chunks.forEach((chunk) => c.enqueue(encode(chunk))); c.close(); } })));
  const seen: ExplainProgress[] = [];
  const result = await streamExplanation("7", new AbortController().signal, (progress) => seen.push(progress));
  assert.deepEqual(result, EXPLANATION);
  assert.deepEqual(calls, ["/api/flags/7/explanation/stream"]);
  assert.deepEqual(seen.map((p) => [p.stage, p.draft?.call ?? null, p.draft?.answer ?? null]), [
    ["context", null, null], ["system2", null, null], ["system2", 1, "The gau"], ["system2", 2, "Take a"],
    ["system2", 2, "Take a manual"], ["storing", 2, "Take a manual"],
  ]);
});

test("every stream failure falls back to the plain JSON route", async () => {
  const cases: [string, () => Response][] = [
    ["non-200", () => Response.json({ error: "not_found" }, { status: 404 })],
    ["error event", () => sseResponse(sse("stage", { stage: "context" }) + sse("error", { error: "internal", detail: "boom" }))],
    ["ends without done", () => sseResponse(sse("start", { flag_id: 7 }) + sse("draft", { call: 1, fields: { answer: "half" } }))],
    ["unreadable done", () => sseResponse(sse("done", { explanation: "nope" }))],
    ["not an event stream", () => Response.json(EXPLANATION)],
  ];
  for (const [name, body] of cases) {
    const calls = mockBrowser(body);
    let fellBack = false;
    const result = await loadExplanation("7", new AbortController().signal, { onProgress: () => undefined, onFallback: () => { fellBack = true; } });
    assert.deepEqual(result, EXPLANATION, name);
    assert.equal(fellBack, true, name);
    assert.deepEqual(calls, ["/api/flags/7/explanation/stream", "/api/flags/7/explanation"], name);
  }
  // A network error mid-stream, after a draft, also falls back.
  const calls = mockBrowser(() => sseResponse(new ReadableStream({
    start(c) { c.enqueue(encode(sse("draft", { call: 1, fields: { answer: "half" } }))); },
    pull(c) { c.error(new TypeError("network error")); },
  })));
  assert.deepEqual(await loadExplanation("7", new AbortController().signal, { onProgress: () => undefined, onFallback: () => undefined }), EXPLANATION);
  assert.equal(calls.length, 2);
});

test("the fallback's own error is the old route's message; a closed dialog never falls back", async () => {
  mockBrowser(() => Response.json({}, { status: 502 }), () => Response.json({ error: "This flag no longer exists." }, { status: 404 }));
  await assert.rejects(loadExplanation("7", new AbortController().signal, { onProgress: () => undefined, onFallback: () => undefined }), /This flag no longer exists/);

  const controller = new AbortController();
  const calls: string[] = [];
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    calls.push(String(url));
    return new Promise<Response>((_resolve, reject) => init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
  }) as typeof fetch;
  let fellBack = false;
  const pending = loadExplanation("7", controller.signal, { onProgress: () => undefined, onFallback: () => { fellBack = true; } });
  controller.abort();
  await assert.rejects(pending);
  assert.equal(fellBack, false);
  assert.deepEqual(calls, ["/api/flags/7/explanation/stream"]);
});

/* ── pure helpers ──────────────────────────────────────────────────────────── */

test("drafts parse defensively from the SSE event and from a live capture row", () => {
  const event = draftFromEvent({ call: 2, reasoning: "x", fields: {
    evidence: [{ claim: "Gauge reads 121", ids: ["V1", 3, ""] }, "junk", { ids: ["R1"] }], answer: "Take a man", recommended_action: "MANUAL_RE", confidence: "med",
  } });
  assert.deepEqual(event, {
    call: 2, tokens: null, answer: "Take a man", recommendedAction: "MANUAL_RE", confidence: null,
    evidence: [{ claim: "Gauge reads 121", ids: ["V1"] }, { claim: "", ids: ["R1"] }],
  });
  assert.equal(draftFromEvent({ call: 1 }), null);
  assert.equal(draftFromEvent("nope"), null);
  assert.equal(draftFromEvent({ fields: {} })?.call, 1, "a missing call counts as the first");

  const live = draftFromCapture({ draft: true, call: 1, tokens: 312, answer: "Send SPOT back", evidence: [], confidence: "HIGH", drafted_at: "t" });
  assert.equal(live?.tokens, 312);
  assert.equal(live?.confidence, "high");
  assert.equal(draftFromCapture({ answer: "final", source: "system2" }), null, "a checked result is not a draft");
  assert.equal(draftFromCapture(null), null);
});

test("the newest call wins; within a call the newer snapshot replaces the older", () => {
  const d = (call: number, answer: string): QwenDraft => ({ call, tokens: null, evidence: [], answer, recommendedAction: "", confidence: null });
  assert.equal(newerDraft(null, d(1, "a")).answer, "a");
  assert.equal(newerDraft(d(2, "new"), d(1, "old")).answer, "new");
  assert.equal(newerDraft(d(1, "Take"), d(1, "Take a")).answer, "Take a");
  assert.equal(newerDraft(d(1, "a long first try"), d(2, "b")).answer, "b");
});

test("the typewriter reveal never shows more than the snapshot and puts the caret after the newest text", () => {
  const draft: QwenDraft = {
    call: 1, tokens: null, confidence: null, answer: "Read it", recommendedAction: "MANUAL_READING",
    evidence: [{ claim: "Gauge 121", ids: ["V1"] }, { claim: "Log 114", ids: [] }],
  };
  assert.equal(draftLength(draft), 9 + 7 + 7 + 14);
  assert.deepEqual(revealDraft(draft, 0), { evidence: [], answer: "", recommendedAction: "", caret: null });
  const partial = revealDraft(draft, 5);
  assert.deepEqual(partial.evidence, [{ claim: "Gauge", ids: ["V1"], complete: false }]);
  assert.deepEqual(partial.caret, { part: "evidence", index: 0 });
  const second = revealDraft(draft, 12);
  assert.deepEqual(second.evidence.map((row) => [row.claim, row.complete]), [["Gauge 121", true], ["Log", false]]);
  assert.deepEqual(second.caret, { part: "evidence", index: 1 });
  assert.deepEqual(revealDraft(draft, 20).caret, { part: "answer" });
  const all = revealDraft(draft, 10_000);
  assert.equal(all.answer, "Read it");
  assert.equal(all.recommendedAction, "MANUAL_READING");
  assert.deepEqual(all.caret, { part: "action" });
  assert.equal(revealDraft(draft, -3).caret, null);
});

test("stage labels and the live poll interval", () => {
  assert.equal(stageLabel(null, false), "Starting the explanation…");
  assert.equal(stageLabel("context", false), "Gathering context…");
  assert.equal(stageLabel("system2", true), "Qwen is writing…");
  assert.equal(stageLabel("storing", true), "Checking and saving the answer…");
  assert.equal(stageLabel("cached", false), "Loading the saved explanation…");
  const running = { status: "running" } as const;
  assert.equal(detailPollMs(running, []), POLL_MS);
  assert.equal(detailPollMs(running, [{ qwen_source: "system2" }, { qwen_source: "template" }, { qwen_source: null }]), POLL_MS);
  assert.equal(detailPollMs(running, [{ qwen_source: "system2" }, { qwen_source: "pending" }]), DRAFT_POLL_MS);
  assert.equal(detailPollMs({ status: "pending" }, [{ qwen_source: "pending" }]), DRAFT_POLL_MS);
  // an ended run never fast-polls, even with a stop left pending (its /analysis will not come)
  for (const status of ["done", "cancelled", "error"] as const) {
    assert.equal(detailPollMs({ status }, [{ qwen_source: "pending" }]), POLL_MS, status);
  }
  assert.equal(detailPollMs(null, [{ qwen_source: "pending" }]), POLL_MS);
  assert.equal(DRAFT_POLL_MS, 750);
  assert.equal(POLL_MS, 2000);
});
