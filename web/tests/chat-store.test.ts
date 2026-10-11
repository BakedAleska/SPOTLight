/* The chat store's send rules (contract §6, chat.2), against a stubbed
   fetch: a turn is ready on done/error, a late title still lands, the next
   send waits (bounded) for the previous stream to release, and a 409 right
   after Stop is retried once. Nothing here talks to a real backend. */

import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";
import {
  applyConversation,
  forgetThread,
  getChatState,
  isLocal,
  loadThread,
  onConversationsChanged,
  regenerate,
  retryTurn,
  sendMessage,
  stopTurn,
  timing,
} from "../src/lib/chat/store";
import type { Conversation, Message } from "../src/lib/chat/types";

const realFetch = globalThis.fetch;
const encoder = new TextEncoder();

interface Stream {
  send: (event: string, data: unknown) => void;
  close: () => void;
  /** Errors the body, like a real fetch body does when its signal aborts. */
  fail: (error: unknown) => void;
  response: Response;
}

function sseStream(): Stream {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  let closed = false;
  const body = new ReadableStream<Uint8Array>({ start(c) { controller = c; } });
  return {
    response: new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    send: (event, data) => controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)),
    close: () => { if (!closed) { closed = true; controller.close(); } },
    fail: (error) => { if (!closed) { closed = true; controller.error(error); } },
  };
}

/** A queued response whose body errors with AbortError when the turn's signal aborts (Stop). */
function abortable(stream: Stream) {
  return (signal?: AbortSignal) => {
    signal?.addEventListener("abort", () => stream.fail(new DOMException("This operation was aborted", "AbortError")));
    return stream.response;
  };
}

function busy() {
  return Response.json({ error: "conversation_busy", detail: "this conversation is already generating an answer" }, { status: 409 });
}

/** Each POST takes the next queued response; every call is recorded. GETs
    go to `get` when given (the post-Stop resync), and fail otherwise. */
function stubFetch(queue: ((signal?: AbortSignal) => Response)[], get?: (url: string) => Response) {
  const posts: { at: number; url: string; body: Record<string, unknown> }[] = [];
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push({ at: Date.now(), url: String(url), body: JSON.parse(String(init.body)) });
      const next = queue.shift();
      if (!next) throw new Error("unexpected POST");
      return next(init.signal ?? undefined);
    }
    if (get) return get(String(url));
    throw new Error("unexpected GET");
  }) as typeof fetch;
  return posts;
}

const tick = (ms = 15) => new Promise((resolve) => setTimeout(resolve, ms));

let seq = 0;
function conversationId() {
  seq += 1;
  return `00000000-0000-4000-8000-${String(seq).padStart(12, "0")}`;
}

function message(id: string, n: number, role: "user" | "assistant", content: string): Message {
  return {
    message_id: `${id}-m${n}`, conversation_id: id, seq: n, role, content, thinking: null, thinking_ms: null,
    model: role === "assistant" ? "gemini-3.8-flash" : null, mode: role === "assistant" ? "fast" : null, status: "complete",
    sources: [], citations: [], grounding: null, usage: null, latency_ms: null, error: null, error_code: null,
    created_at: "2026-10-10T12:00:00Z",
  };
}

function conversation(id: string, title = "New chat"): Conversation {
  return {
    conversation_id: id, title, title_source: "auto", model: "gemini-3.8-flash", mode: "fast", pinned: false,
    created_at: "2026-10-10T12:00:00Z", updated_at: "2026-10-10T12:00:00Z", message_count: 2, mission_id: null, mission: null,
  };
}

/** Plays one full answer on `stream`, leaving the connection open. */
function answer(stream: Stream, id: string, userSeq: number, question: string, reply: string) {
  stream.send("meta", { conversation_id: id, request_id: "r", model: "gemini-3.8-flash", mode: "fast", user_message: message(id, userSeq, "user", question) });
  stream.send("sources", { sources: [], retrieval: { mode: "fulltext", gaps: ["embedding unavailable: timeout"] } });
  stream.send("delta", { delta: reply });
  stream.send("done", { message: message(id, userSeq + 1, "assistant", reply), conversation: conversation(id) });
}

const thread = (id: string) => getChatState().threads[id];

beforeEach(() => {
  timing.releaseWaitMs = 8000;
  timing.busyRetryMs = 600;
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("chat store: follow-ups after done (chat.2 §6)", () => {
  test("a send right after done waits for the stream to release, then goes out; the late title still lands", async () => {
    const id = conversationId();
    const first = sseStream();
    const second = sseStream();
    const posts = stubFetch([() => first.response, () => second.response]);

    const turn1 = sendMessage(id, "Which missions failed?", "gemini-3.8-flash", "fast");
    await tick();
    answer(first, id, 1, "Which missions failed?", "M007 failed [stat:fleet].");
    await tick();
    assert.equal(thread(id).stream, null, "ready on done, before the connection closes");

    // The follow-up is sent while the server still holds the stream open.
    const turn2 = sendMessage(id, "Why?", "gemini-3.8-flash", "fast");
    await tick();
    assert.equal(posts.length, 1, "waits for the previous stream to release");
    assert.ok(thread(id).stream, "the follow-up shows as in progress, never vanishes");
    assert.equal(thread(id).messages.at(-1)?.content, "Why?");

    first.send("title", { conversation_id: id, title: "Failed missions" });
    first.close();
    await tick();
    assert.equal(await turn1, true);
    assert.equal(thread(id).conversation?.title, "Failed missions", "late title applied");
    assert.equal(posts.length, 2, "the follow-up goes out once released");
    assert.equal(posts[1].body.content, "Why?");

    answer(second, id, 3, "Why?", "Battery [stat:fleet].");
    second.close();
    assert.equal(await turn2, true);
    assert.deepEqual(thread(id).messages.map((m) => m.content), ["Which missions failed?", "M007 failed [stat:fleet].", "Why?", "Battery [stat:fleet]."]);
    assert.equal(thread(id).error, null);
  });

  test("a previous stream that never releases fails visibly after the bounded wait", async () => {
    timing.releaseWaitMs = 80;
    const id = conversationId();
    const first = sseStream();
    const posts = stubFetch([() => first.response]);
    void sendMessage(id, "Q1", "gemini-3.8-flash", "fast");
    await tick();
    answer(first, id, 1, "Q1", "A1");
    await tick();
    const started = Date.now();
    assert.equal(await sendMessage(id, "Q2", "gemini-3.8-flash", "fast"), true);
    assert.ok(Date.now() - started >= 70, "waited");
    assert.equal(posts.length, 1, "never POSTed into a busy chat");
    assert.equal(thread(id).error?.code, "conversation_busy");
    assert.equal(thread(id).stream, null);
    assert.equal(thread(id).messages.at(-1)?.content, "Q2", "the text stays on screen with a Retry");
    assert.equal(thread(id).error?.retry.content, "Q2");
    first.close();
  });

  test("Stop while waiting hands the turn back untouched", async () => {
    const id = conversationId();
    const first = sseStream();
    const posts = stubFetch([() => first.response]);
    void sendMessage(id, "Q1", "gemini-3.8-flash", "fast");
    await tick();
    answer(first, id, 1, "Q1", "A1");
    await tick();
    const before = thread(id).messages;
    const turn2 = sendMessage(id, "Q2", "gemini-3.8-flash", "fast");
    await tick();
    stopTurn(id);
    assert.equal(await turn2, false, "not sent: the caller restores the composer text");
    assert.equal(thread(id).messages, before);
    assert.equal(thread(id).stream, null);
    assert.equal(posts.length, 1);
    first.close();
  });
});

describe("chat store: 409 after Stop (chat.2 §6)", () => {
  test("conversation_busy is retried once after a short pause", async () => {
    timing.busyRetryMs = 60;
    const id = conversationId();
    const stream = sseStream();
    const posts = stubFetch([busy, () => stream.response]);
    const turn = sendMessage(id, "Q", "gemini-3.8-flash", "fast");
    await tick(100);
    assert.equal(posts.length, 2);
    assert.ok(posts[1].at - posts[0].at >= 55, "paused before retrying");
    answer(stream, id, 1, "Q", "A");
    stream.close();
    assert.equal(await turn, true);
    assert.equal(thread(id).error, null);
    assert.equal(thread(id).messages.at(-1)?.content, "A");
  });

  test("a second 409 shows the busy card instead of failing silently", async () => {
    timing.busyRetryMs = 20;
    const id = conversationId();
    const posts = stubFetch([busy, busy]);
    assert.equal(await sendMessage(id, "Q", "gemini-3.8-flash", "fast"), true);
    assert.equal(posts.length, 2, "exactly one retry");
    assert.equal(thread(id).error?.code, "conversation_busy");
    assert.equal(thread(id).messages.at(-1)?.content, "Q");
  });

  test("regenerate right after an answer also waits and goes out", async () => {
    const id = conversationId();
    const first = sseStream();
    const second = sseStream();
    const posts = stubFetch([() => first.response, () => second.response]);
    void sendMessage(id, "Q", "gemini-3.8-flash", "fast");
    await tick();
    answer(first, id, 1, "Q", "A");
    await tick();
    const again = regenerate(id, "gemini-3.5-flash", "fast");
    first.close();
    await tick();
    assert.equal(posts.length, 2);
    assert.equal(posts[1].body.regenerate, true);
    assert.equal(posts[1].body.model, "gemini-3.5-flash");
    answer(second, id, 1, "Q", "A2");
    second.close();
    assert.equal(await again, true);
    assert.equal(thread(id).messages.at(-1)?.content, "A2");
  });
});

describe("chat store: history refresh (chat.2: empty chats are not listed)", () => {
  test("the list refreshes on meta (first message stored) and again on done", async () => {
    const id = conversationId();
    const stream = sseStream();
    stubFetch([() => stream.response]);
    const events: string[] = [];
    const stop = onConversationsChanged(() => events.push(thread(id)?.stream ? "during" : "after"));
    const turn = sendMessage(id, "Q", "gemini-3.8-flash", "fast");
    await tick();
    stream.send("meta", { conversation_id: id, request_id: "r", model: "gemini-3.8-flash", mode: "fast", user_message: message(id, 1, "user", "Q") });
    await tick();
    assert.deepEqual(events, ["during"], "refreshed as soon as the user turn is stored");
    stream.send("done", { message: message(id, 2, "assistant", "A"), conversation: conversation(id) });
    stream.close();
    await turn;
    stop();
    assert.deepEqual(events, ["during", "after"]);
  });
});

/* ── gaps: errors mid-answer, Stop, switching chats, renames ───────────── */

function storedAssistant(id: string, seq: number, content: string, over: Partial<Message>): Message {
  return { ...message(id, seq, "assistant", content), ...over };
}

function detailOf(id: string, messages: Message[]) {
  return Response.json({ conversation: conversation(id), messages });
}

describe("chat store: errors after partial output", () => {
  test("an error event after deltas shows the stored partial, and Try-with-model regenerates", async () => {
    const id = conversationId();
    const first = sseStream();
    const second = sseStream();
    const posts = stubFetch([() => first.response, () => second.response]);
    const turn = sendMessage(id, "Why HX-217?", "gemini-3.8-flash", "fast");
    await tick();
    first.send("meta", { conversation_id: id, request_id: "r", model: "gemini-3.8-flash", mode: "fast", user_message: message(id, 1, "user", "Why HX-217?") });
    first.send("sources", { sources: [], retrieval: { mode: "hybrid", gaps: [] } });
    first.send("delta", { delta: "Partial " });
    await tick();
    assert.equal(thread(id).stream?.content, "Partial ");
    const stored = storedAssistant(id, 2, "Partial ", { status: "error", error: "overloaded", error_code: "model_unavailable" });
    first.send("error", { code: "model_unavailable", detail: "gemini-3.8-flash is unavailable right now", message: stored });
    first.close();
    assert.equal(await turn, true);
    const t = thread(id);
    assert.equal(t.stream, null);
    assert.equal(t.error?.code, "model_unavailable");
    assert.equal(t.error?.messageId, stored.message_id);
    assert.equal(t.error?.retryAfterS, null);
    assert.deepEqual(t.messages.map((m) => [m.role, m.content, m.status]), [["user", "Why HX-217?", "complete"], ["assistant", "Partial ", "error"]]);
    assert.deepEqual(t.error?.retry, { content: "Why HX-217?", model: "gemini-3.8-flash", mode: "fast", edit_message_id: null, regenerate: true });

    const again = retryTurn(id, "gemini-3.5-flash");
    await tick();
    assert.equal(posts.length, 2);
    assert.equal(posts[1].body.regenerate, true);
    assert.equal(posts[1].body.model, "gemini-3.5-flash");
    assert.deepEqual(thread(id).messages.map((m) => m.role), ["user"], "the failed answer leaves the view while regenerating");
    assert.equal(thread(id).error, null);
    answer(second, id, 1, "Why HX-217?", "Fixed [stat:fleet].");
    second.close();
    assert.equal(await again, true);
    assert.deepEqual(thread(id).messages.map((m) => m.content), ["Why HX-217?", "Fixed [stat:fleet]."]);
  });

  test("a rate limit keeps its countdown seconds", async () => {
    const id = conversationId();
    const stream = sseStream();
    stubFetch([() => stream.response]);
    const turn = sendMessage(id, "Q", "gemini-3.8-flash", "fast");
    await tick();
    stream.send("meta", { conversation_id: id, request_id: "r", model: "gemini-3.8-flash", mode: "fast", user_message: message(id, 1, "user", "Q") });
    const before = Date.now();
    stream.send("error", { code: "rate_limited", detail: "429", retry_after_s: 41, message: storedAssistant(id, 2, "", { status: "error", error_code: "rate_limited" }) });
    stream.close();
    await turn;
    assert.equal(thread(id).error?.retryAfterS, 41);
    assert.ok((thread(id).error?.at ?? 0) >= before);
    assert.equal(thread(id).error?.model, "gemini-3.8-flash");
  });

  test("a connection that drops mid-answer shows connection_lost, then resyncs to what the backend stored", async () => {
    const id = conversationId();
    const stream = sseStream();
    const gets: string[] = [];
    const stopped = storedAssistant(id, 2, "Half an ans", { status: "stopped" });
    stubFetch([() => stream.response], (url) => {
      gets.push(url);
      return detailOf(id, [message(id, 1, "user", "Q"), stopped]);
    });
    const turn = sendMessage(id, "Q", "gemini-3.8-flash", "fast");
    await tick();
    stream.send("meta", { conversation_id: id, request_id: "r", model: "gemini-3.8-flash", mode: "fast", user_message: message(id, 1, "user", "Q") });
    stream.send("delta", { delta: "Half an ans" });
    await tick();
    stream.close(); // no done, no error
    assert.equal(await turn, true);
    assert.equal(thread(id).error?.code, "connection_lost");
    assert.equal(thread(id).error?.retry.regenerate, true, "the question was stored: Retry regenerates");
    assert.equal(thread(id).stream, null);
    await tick(800);
    assert.deepEqual(gets, [`/api/chat/conversations/${id}`]);
    assert.equal(thread(id).messages.at(-1)?.message_id, stopped.message_id);
    assert.equal(thread(id).error, null, "the stored answer speaks for itself");
  });

  test("events after done are ignored, except a late title", async () => {
    const id = conversationId();
    const stream = sseStream();
    stubFetch([() => stream.response]);
    const turn = sendMessage(id, "Q", "gemini-3.8-flash", "fast");
    await tick();
    answer(stream, id, 1, "Q", "A");
    await tick();
    const after = thread(id).messages;
    stream.send("delta", { delta: "stray" });
    stream.send("error", { code: "internal_error", detail: "late", message: null });
    stream.send("title", { conversation_id: id, title: "Late title" });
    stream.close();
    await turn;
    assert.equal(thread(id).messages, after);
    assert.equal(thread(id).error, null);
    assert.equal(thread(id).stream, null);
    assert.equal(thread(id).conversation?.title, "Late title");
  });
});

describe("chat store: Stop", () => {
  test("Stop mid-answer shows what streamed as Stopped at once, then swaps in the stored copy", async () => {
    const id = conversationId();
    const stream = sseStream();
    const stored = storedAssistant(id, 2, "So far ", { status: "stopped" });
    stubFetch([abortable(stream)], () => detailOf(id, [message(id, 1, "user", "Q"), stored]));
    const turn = sendMessage(id, "Q", "gemini-3.8-flash", "thinking");
    await tick();
    stream.send("meta", { conversation_id: id, request_id: "r", model: "gemini-3.8-flash", mode: "thinking", user_message: message(id, 1, "user", "Q") });
    const src = { id: "stat:fleet", n: 1, origin: "sql", kind: "stat", title: "Fleet", snippet: "", score: null };
    stream.send("sources", { sources: [src], retrieval: { mode: "hybrid", gaps: [] } });
    stream.send("thinking", { delta: "Looking." });
    stream.send("delta", { delta: "So far " });
    await tick();
    stopTurn(id);
    assert.equal(await turn, true);
    const t = thread(id);
    assert.equal(t.stream, null);
    assert.equal(t.error, null, "a Stop is not an error");
    const partial = t.messages.at(-1)!;
    assert.ok(isLocal(partial));
    assert.deepEqual([partial.role, partial.status, partial.content, partial.thinking, partial.mode], ["assistant", "stopped", "So far ", "Looking.", "thinking"]);
    assert.deepEqual(partial.sources.map((s) => s.id), ["stat:fleet"]);
    assert.equal(partial.seq, 2);
    await tick(800);
    assert.equal(thread(id).messages.at(-1)?.message_id, stored.message_id, "the stored stopped answer replaces the local one");
    assert.equal(thread(id).messages.filter((m) => m.role === "assistant").length, 1);
  });

  test("Stop during the 409 retry pause hands the turn back untouched", async () => {
    timing.busyRetryMs = 300;
    const id = conversationId();
    const posts = stubFetch([busy]);
    const turn = sendMessage(id, "Q", "gemini-3.8-flash", "fast");
    await tick(40);
    assert.equal(thread(id).stream?.phase, "connecting", "the turn shows while it waits");
    stopTurn(id);
    assert.equal(await turn, false, "nothing was sent: the composer gets its text back");
    assert.equal(posts.length, 1);
    assert.deepEqual(thread(id).messages, []);
    assert.equal(thread(id).stream, null);
  });

  test("deleting a chat mid-answer aborts it and nothing of it comes back", async () => {
    const id = conversationId();
    const stream = sseStream();
    stubFetch([abortable(stream)], () => Response.json({ error: "not_found", detail: "gone" }, { status: 404 }));
    const turn = sendMessage(id, "Q", "gemini-3.8-flash", "fast");
    await tick();
    stream.send("meta", { conversation_id: id, request_id: "r", model: "gemini-3.8-flash", mode: "fast", user_message: message(id, 1, "user", "Q") });
    stream.send("delta", { delta: "partial" });
    await tick();
    forgetThread(id);
    await turn;
    assert.equal(thread(id)?.messages.length ?? 0, 0);
    assert.ok(!thread(id)?.stream);
    assert.ok(!thread(id)?.error);
  });
});

describe("chat store: several chats at once", () => {
  test("switching chats mid-answer: the first keeps streaming in the background, the second is unaffected", async () => {
    const a = conversationId();
    const b = conversationId();
    const sa = sseStream();
    const sb = sseStream();
    const gets: string[] = [];
    const posts = stubFetch([() => sa.response, () => sb.response], (url) => { gets.push(url); return detailOf(a, []); });
    const turnA = sendMessage(a, "QA", "gemini-3.8-flash", "fast");
    await tick();
    sa.send("meta", { conversation_id: a, request_id: "ra", model: "gemini-3.8-flash", mode: "fast", user_message: message(a, 1, "user", "QA") });
    sa.send("delta", { delta: "A1 " });
    await tick();

    const turnB = sendMessage(b, "QB", "gemini-3.5-flash", "fast");
    await tick();
    assert.equal(posts.length, 2, "B does not wait for A's stream");
    assert.equal(posts[1].url, `/api/chat/conversations/${b}/messages`);
    answer(sb, b, 1, "QB", "B answer");
    sb.close();
    assert.equal(await turnB, true);

    // Coming back to A must not replace its live answer with a stored copy.
    await loadThread(a, { quiet: true });
    assert.deepEqual(gets, []);
    assert.equal(thread(a).stream?.content, "A1 ");
    assert.deepEqual(thread(a).messages.map((m) => m.content), ["QA"]);

    sa.send("delta", { delta: "A2" });
    sa.send("done", { message: message(a, 2, "assistant", "A1 A2"), conversation: conversation(a) });
    sa.close();
    assert.equal(await turnA, true);
    assert.deepEqual(thread(a).messages.map((m) => m.content), ["QA", "A1 A2"]);
    assert.deepEqual(thread(b).messages.map((m) => m.content), ["QB", "B answer"]);
    assert.equal(thread(b).error, null);
  });

  test("a pre-stream 422 keeps the text with a plain resend, and the retry shows it once", async () => {
    const id = conversationId();
    const stream = sseStream();
    const posts = stubFetch([() => Response.json({ error: "invalid_request", detail: "unknown model 'gemini-old'" }, { status: 422 }), () => stream.response]);
    assert.equal(await sendMessage(id, "Q", "gemini-old", "fast"), true);
    const t = thread(id);
    assert.equal(t.error?.code, "invalid_request");
    assert.equal(t.error?.detail, "unknown model 'gemini-old'");
    assert.deepEqual(t.error?.retry, { content: "Q", model: "gemini-old", mode: "fast", edit_message_id: null, regenerate: false });
    assert.deepEqual(t.messages.map((m) => [m.content, isLocal(m)]), [["Q", true]]);
    const again = retryTurn(id, "gemini-3.8-flash");
    await tick();
    assert.deepEqual(posts[1].body, { content: "Q", model: "gemini-3.8-flash", mode: "fast", edit_message_id: null, regenerate: false });
    assert.deepEqual(thread(id).messages.map((m) => m.content), ["Q"], "not shown twice");
    answer(stream, id, 1, "Q", "A");
    stream.close();
    await again;
    assert.deepEqual(thread(id).messages.map((m) => [m.content, isLocal(m)]), [["Q", false], ["A", false]]);
  });

  test("a title arriving after the operator renamed the chat does not replace the rename (contract §4: a user title is never overwritten)", async () => {
    const id = conversationId();
    const stream = sseStream();
    stubFetch([() => stream.response]);
    const turn = sendMessage(id, "Q", "gemini-3.8-flash", "fast");
    await tick();
    answer(stream, id, 1, "Q", "A");
    await tick();
    // The rename's PATCH response lands before the title event does.
    applyConversation({ ...conversation(id), title: "My HX-217 notes", title_source: "user" });
    stream.send("title", { conversation_id: id, title: "HX-217 Gauge Reading" });
    stream.close();
    await turn;
    assert.equal(thread(id).conversation?.title, "My HX-217 notes");
  });
});
