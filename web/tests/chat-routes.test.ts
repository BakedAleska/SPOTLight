import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { GET as models } from "../src/app/api/chat/models/route";
import { GET as list, POST as create } from "../src/app/api/chat/conversations/route";
import { DELETE as remove, GET as detail, PATCH as patch } from "../src/app/api/chat/conversations/[id]/route";
import { POST as send } from "../src/app/api/chat/conversations/[id]/messages/route";
import { GET as suggestions } from "../src/app/api/chat/suggestions/route";
import { MAX_BODY_BYTES } from "../src/lib/chat/proxy";
import { assistantFeatures } from "../src/lib/features";

const ID = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b";
const originalFetch = globalThis.fetch;
const originalFlag = process.env.FEATURE_AI_ASSISTANT;
const originalBackend = process.env.BACKEND_URL;
const ctx = (id = ID) => ({ params: Promise.resolve({ id }) });

function req(path: string, method = "GET", body?: unknown, origin = "http://localhost:3000") {
  return new Request(`http://localhost:3000${path}`, {
    method,
    headers: { host: "localhost:3000", origin, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalFlag === undefined) delete process.env.FEATURE_AI_ASSISTANT; else process.env.FEATURE_AI_ASSISTANT = originalFlag;
  if (originalBackend === undefined) delete process.env.BACKEND_URL; else process.env.BACKEND_URL = originalBackend;
});

test("FEATURE_AI_ASSISTANT=false 404s every chat route before contacting the backend", async () => {
  process.env.FEATURE_AI_ASSISTANT = "false";
  globalThis.fetch = async () => { throw new Error("Backend must not be called"); };
  assert.deepEqual(assistantFeatures(), { aiAssistant: false });
  const responses = await Promise.all([
    models(req("/api/chat/models")),
    list(req("/api/chat/conversations")),
    create(req("/api/chat/conversations", "POST", {})),
    detail(req(`/api/chat/conversations/${ID}`), ctx()),
    patch(req(`/api/chat/conversations/${ID}`, "PATCH", { pinned: true }), ctx()),
    remove(req(`/api/chat/conversations/${ID}`, "DELETE"), ctx()),
    send(req(`/api/chat/conversations/${ID}/messages`, "POST", { content: "hi" }), ctx()),
  ]);
  for (const response of responses) {
    assert.equal(response.status, 404);
    assert.equal((await response.json()).error, "disabled");
  }
  delete process.env.FEATURE_AI_ASSISTANT;
  assert.deepEqual(assistantFeatures(), { aiAssistant: true });
});

test("malformed ids 404 and foreign origins 403, without reaching the backend", async () => {
  globalThis.fetch = async () => { throw new Error("Backend must not be called"); };
  assert.equal((await detail(req("/api/chat/conversations/../ops"), ctx("../ops"))).status, 404);
  assert.equal((await send(req("/api/chat/conversations/x/messages", "POST", { content: "hi" }), ctx("not-a-uuid"))).status, 404);
  assert.equal((await create(req("/api/chat/conversations", "POST", {}, "https://evil.example"))).status, 403);
  assert.equal((await remove(req(`/api/chat/conversations/${ID}`, "DELETE", undefined, "null"), ctx())).status, 403);
});

test("JSON routes forward to BACKEND_URL/chat and mirror status and body", async () => {
  process.env.BACKEND_URL = "http://backend.test/api/v1/";
  const calls: { url: string; method: string; body?: string }[] = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), method: init?.method ?? "GET", body: init?.body as string | undefined });
    if (init?.method === "DELETE") return new Response(null, { status: 204 });
    if (init?.method === "PATCH") return Response.json({ error: "invalid_request", detail: "title too long", request_id: "r1" }, { status: 422 });
    return Response.json({ items: [], total: 0 });
  };
  const listed = await list(req("/api/chat/conversations?q=battery%20M007&limit=50&offset=abc&evil=1"));
  assert.equal(listed.status, 200);
  assert.equal(calls[0].url, "http://backend.test/api/v1/chat/conversations?q=battery+M007&limit=50");
  const patched = await patch(req(`/api/chat/conversations/${ID}`, "PATCH", { title: "x" }), ctx());
  assert.equal(patched.status, 422);
  assert.deepEqual(await patched.json(), { error: "invalid_request", detail: "title too long", request_id: "r1" });
  assert.equal(calls[1].body, JSON.stringify({ title: "x" }));
  assert.equal((await remove(req(`/api/chat/conversations/${ID}`, "DELETE"), ctx())).status, 204);
});

test("an unreachable backend is a 502 with a readable error", async () => {
  globalThis.fetch = async () => { throw new TypeError("fetch failed"); };
  const response = await models(req("/api/chat/models"));
  assert.equal(response.status, 502);
  assert.equal((await response.json()).error, "backend_unreachable");
});

test("the messages route pipes the SSE body through and passes the abort signal", async () => {
  let upstreamSignal: AbortSignal | undefined;
  globalThis.fetch = async (_url, init) => {
    upstreamSignal = init?.signal ?? undefined;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('event: delta\ndata: {"delta":"hi"}\n\n'));
        controller.close();
      },
    });
    return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
  };
  const controller = new AbortController();
  const request = new Request(`http://localhost:3000/api/chat/conversations/${ID}/messages`, {
    method: "POST",
    headers: { host: "localhost:3000", origin: "http://localhost:3000" },
    body: JSON.stringify({ content: "hi", model: "m", mode: "fast", edit_message_id: null, regenerate: false }),
    signal: controller.signal,
  });
  const response = await send(request, ctx());
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /text\/event-stream/);
  assert.equal(response.headers.get("cache-control"), "no-cache, no-transform");
  assert.equal(response.headers.get("x-accel-buffering"), "no");
  assert.equal(await response.text(), 'event: delta\ndata: {"delta":"hi"}\n\n');
  assert.ok(upstreamSignal);
  controller.abort();
  assert.equal(upstreamSignal?.aborted, true);
});

test("pre-stream JSON errors from the messages endpoint keep their status", async () => {
  globalThis.fetch = async () => Response.json({ error: "conversation_busy", detail: "busy", request_id: "r" }, { status: 409 });
  const response = await send(req(`/api/chat/conversations/${ID}/messages`, "POST", { content: "hi" }), ctx());
  assert.equal(response.status, 409);
  assert.equal((await response.json()).error, "conversation_busy");
});

/* ── gaps: same-origin, body cap, error mapping, streaming edges ──────────── */

const noBackend = () => { globalThis.fetch = async () => { throw new Error("Backend must not be called"); }; };

function raw(path: string, method: string, body: string | undefined, headers: Record<string, string>, signal?: AbortSignal) {
  return new Request(`http://localhost:3000${path}`, { method, headers: { host: "localhost:3000", ...headers }, body, signal });
}

test("writes need an Origin from this host; reads don't", async () => {
  noBackend();
  const url = `/api/chat/conversations/${ID}`;
  const writes: [string, (r: Request) => Promise<Response>][] = [
    ["POST", (r) => create(r)],
    ["PATCH", (r) => patch(r, ctx())],
    ["DELETE", (r) => remove(r, ctx())],
    ["POST", (r) => send(r, ctx())],
  ];
  for (const [method, call] of writes) {
    const body = method === "DELETE" ? undefined : "{}";
    for (const origin of [undefined, "http://localhost:3001", "https://localhost.evil.test", "not a url", "null"]) {
      const response = await call(raw(url, method, body, origin ? { origin } : {}));
      assert.equal(response.status, 403, `${method} with origin ${origin}`);
      assert.deepEqual(await response.json(), { error: "forbidden", detail: "Use the SPOTlight console to change chats." });
    }
  }
  // Same host, any scheme or case, is this app.
  globalThis.fetch = async () => Response.json({ conversation_id: ID });
  assert.equal((await create(raw("/api/chat/conversations", "POST", "{}", { origin: "HTTP://LOCALHOST:3000" }))).status, 200);
  // A read from another origin is fine (the browser's CORS rules protect the response).
  globalThis.fetch = async () => Response.json({ models: [] });
  assert.equal((await models(raw("/api/chat/models", "GET", undefined, { origin: "https://evil.example" }))).status, 200);
});

test("bodies over the cap are 413 and bad JSON is 400, before the backend", async () => {
  noBackend();
  const fill = (size: number) => JSON.stringify({ content: "x".repeat(size - '{"content":""}'.length) });
  assert.equal(fill(MAX_BODY_BYTES + 1).length, MAX_BODY_BYTES + 1);
  for (const call of [
    (b: string) => send(raw(`/api/chat/conversations/${ID}/messages`, "POST", b, { origin: "http://localhost:3000" }), ctx()),
    (b: string) => create(raw("/api/chat/conversations", "POST", b, { origin: "http://localhost:3000" })),
    (b: string) => patch(raw(`/api/chat/conversations/${ID}`, "PATCH", b, { origin: "http://localhost:3000" }), ctx()),
  ]) {
    const tooBig = await call(fill(MAX_BODY_BYTES + 1));
    assert.equal(tooBig.status, 413);
    assert.equal((await tooBig.json()).error, "invalid_request");
    for (const bad of ["not json", "[1,2]", "null", '"text"', "42"]) {
      const response = await call(bad);
      assert.equal(response.status, 400, bad);
      assert.equal((await response.json()).detail, "The request body must be a JSON object.");
    }
  }
});

test("a body exactly at the cap is forwarded verbatim; an empty body becomes {}", async () => {
  const bodies: (string | undefined)[] = [];
  globalThis.fetch = async (_url, init) => {
    bodies.push(init?.body as string | undefined);
    return Response.json({ conversation_id: ID }, { status: 201 });
  };
  const exact = JSON.stringify({ title: "t".repeat(MAX_BODY_BYTES - '{"title":""}'.length) });
  assert.equal(exact.length, MAX_BODY_BYTES);
  assert.equal((await create(raw("/api/chat/conversations", "POST", exact, { origin: "http://localhost:3000" }))).status, 201);
  assert.equal((await create(raw("/api/chat/conversations", "POST", undefined, { origin: "http://localhost:3000" }))).status, 201);
  assert.deepEqual(bodies, [exact, "{}"]);
});

test("the body cap counts bytes, not UTF-16 units (MAX_BODY_BYTES)", async () => {
  noBackend();
  const body = JSON.stringify({ content: "€".repeat(30_000) }); // 30k chars, ~90 KB of UTF-8
  assert.ok(new TextEncoder().encode(body).length > MAX_BODY_BYTES);
  const response = await create(raw("/api/chat/conversations", "POST", body, { origin: "http://localhost:3000" }));
  assert.equal(response.status, 413);
});

test("upstream failures map to readable 502/504 errors", async () => {
  globalThis.fetch = async () => { throw new DOMException("The operation was aborted due to timeout", "TimeoutError"); };
  let response = await list(req("/api/chat/conversations"));
  assert.equal(response.status, 504);
  assert.equal((await response.json()).error, "backend_timeout");

  globalThis.fetch = async () => new Response("<html>502 Bad Gateway</html>", { status: 502, headers: { "Content-Type": "text/html" } });
  response = await suggestions(req("/api/chat/suggestions"));
  assert.equal(response.status, 502);
  assert.deepEqual(await response.json(), { error: "upstream_error", detail: "The backend returned an unexpected response." });

  globalThis.fetch = async () => Response.json({ error: "database_unavailable", detail: "db down", request_id: "r9" }, { status: 503 });
  response = await detail(req(`/api/chat/conversations/${ID}`), ctx());
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(await response.json(), { error: "database_unavailable", detail: "db down", request_id: "r9" });
});

test("JSON routes forward to the documented backend paths only", async () => {
  process.env.BACKEND_URL = "http://backend.test/api/v1";
  const urls: string[] = [];
  globalThis.fetch = async (url) => { urls.push(String(url)); return Response.json({}); };
  await models(req("/api/chat/models"));
  await suggestions(req("/api/chat/suggestions"));
  await list(req(`/api/chat/conversations?q=${encodeURIComponent("  " + "y".repeat(250) + "  ")}&limit=12345&offset=0`));
  await list(req("/api/chat/conversations?q=%20%20&limit=-1&offset=1e3"));
  await detail(req(`/api/chat/conversations/${ID.toUpperCase()}`), ctx(ID.toUpperCase()));
  assert.deepEqual(urls, [
    "http://backend.test/api/v1/chat/models",
    "http://backend.test/api/v1/chat/suggestions",
    `http://backend.test/api/v1/chat/conversations?q=${"y".repeat(200)}&offset=0`,
    "http://backend.test/api/v1/chat/conversations",
    `http://backend.test/api/v1/chat/conversations/${ID.toUpperCase()}`,
  ]);
});

test("the messages route forwards the body verbatim and asks for an event stream", async () => {
  process.env.BACKEND_URL = "http://backend.test/api/v1";
  const calls: { url: string; init?: RequestInit }[] = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response("event: done\ndata: {}\n\n", { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
  };
  const body = '{"content":"≤ 25% — why?",  "model":"gemini-3.8-flash","mode":"thinking","regenerate":false}';
  const response = await send(raw(`/api/chat/conversations/${ID}/messages`, "POST", body, { origin: "http://localhost:3000" }), ctx());
  assert.equal(response.status, 200);
  assert.equal(calls[0].url, `http://backend.test/api/v1/chat/conversations/${ID}/messages`);
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.body, body);
  assert.equal(new Headers(calls[0].init?.headers).get("accept"), "text/event-stream");
  assert.equal(calls[0].init?.cache, "no-store");
});

test("the messages route: a non-stream 200, a non-JSON error and a dead backend are all readable errors", async () => {
  const post = () => send(req(`/api/chat/conversations/${ID}/messages`, "POST", { content: "hi" }), ctx());
  globalThis.fetch = async () => Response.json({ unexpected: true });
  let response = await post();
  assert.equal(response.status, 502, "a 200 that isn't an event stream");
  assert.deepEqual(await response.json(), { unexpected: true });

  globalThis.fetch = async () => new Response("Internal Server Error", { status: 500 });
  response = await post();
  assert.equal(response.status, 500);
  assert.deepEqual(await response.json(), { error: "upstream_error", detail: "The backend could not start an answer." });

  globalThis.fetch = async () => { throw new TypeError("fetch failed"); };
  response = await post();
  assert.equal(response.status, 502);
  assert.equal((await response.json()).error, "backend_unreachable");

  for (const [status, error] of [[404, "not_found"], [422, "invalid_request"], [503, "gemini_unset"]] as const) {
    globalThis.fetch = async () => Response.json({ error, detail: "d", request_id: "r" }, { status });
    response = await post();
    assert.equal(response.status, status);
    assert.equal((await response.json()).error, error);
  }
});

test("Stop before the backend answers: the upstream fetch is aborted and the route ends quietly (499)", async () => {
  let called!: () => void;
  const fetchCalled = new Promise<void>((resolve) => { called = resolve; });
  let upstreamAborted = false;
  globalThis.fetch = ((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => {
      upstreamAborted = true;
      reject(new DOMException("This operation was aborted", "AbortError"));
    });
    called();
  })) as typeof fetch;
  const controller = new AbortController();
  const pending = send(raw(`/api/chat/conversations/${ID}/messages`, "POST", '{"content":"hi"}', { origin: "http://localhost:3000" }, controller.signal), ctx());
  await fetchCalled;
  controller.abort();
  const response = await pending;
  assert.equal(upstreamAborted, true);
  assert.equal(response.status, 499);
});

test("Stop mid-stream: cancelling the response body cancels the backend's stream", async () => {
  let cancelled: unknown = "not cancelled";
  globalThis.fetch = async () => new Response(new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new TextEncoder().encode('event: delta\ndata: {"delta":"so far"}\n\n')); },
    cancel(reason) { cancelled = reason ?? "cancelled"; },
  }), { headers: { "Content-Type": "text/event-stream" } });
  const response = await send(req(`/api/chat/conversations/${ID}/messages`, "POST", { content: "hi" }), ctx());
  const reader = response.body!.getReader();
  const first = await reader.read();
  assert.match(new TextDecoder().decode(first.value), /so far/);
  await reader.cancel("operator pressed Stop");
  assert.notEqual(cancelled, "not cancelled");
});

test("the streaming body is passed through unbuffered: each upstream chunk is readable before the next exists", async () => {
  let push!: (text: string) => void;
  globalThis.fetch = async () => new Response(new ReadableStream<Uint8Array>({
    start(controller) { push = (text) => controller.enqueue(new TextEncoder().encode(text)); },
  }), { headers: { "Content-Type": "text/event-stream" } });
  const response = await send(req(`/api/chat/conversations/${ID}/messages`, "POST", { content: "hi" }), ctx());
  const reader = response.body!.getReader();
  push('event: meta\ndata: {"a":1}\n\n');
  assert.match(new TextDecoder().decode((await reader.read()).value), /event: meta/);
  push(": keep-alive\n\n");
  assert.equal(new TextDecoder().decode((await reader.read()).value), ": keep-alive\n\n");
  await reader.cancel();
});
