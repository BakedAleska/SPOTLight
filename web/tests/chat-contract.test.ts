/* Cross-layer contract (docs/chat-contract.md §3–§6, chat.2): the web side of
   tests/contract/test_chat_contract.py.

   That test drives real backend turns and saves their normalized SSE bodies
   in tests/contract/chat_golden/*.sse. This one replays the SAME files through
   the real web path:

     store.sendMessage → fetch("/api/chat/conversations/<id>/messages")
       → the real Next.js route handler → proxyStream → BACKEND_URL (stubbed:
         the golden bytes, in small chunks that split lines and UTF-8 chars)
       → readSSE / createSSEParser → the store's event reducer

   and checks the thread the operator ends up with: message content, chips,
   grounding badge, thinking text, the error card (code + retry_after), the
   updated title, and the rendered turn. A field renamed on either side fails
   here or there. The reload path (GET /conversations/{id}) and the model
   catalog (GET /chat/models) are replayed from their JSON goldens the same way.

   Regenerate the goldens with UPDATE_GOLDEN=1 make test-chat-contract. */

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { after, before, beforeEach, describe, test } from "node:test";
import { POST as sendRoute } from "../src/app/api/chat/conversations/[id]/messages/route";
import { GET as detailRoute } from "../src/app/api/chat/conversations/[id]/route";
import { GET as modelsRoute } from "../src/app/api/chat/models/route";
import { citationOrder, citationsIn, sourceResolver } from "../src/lib/chat/citations";
import { groundingBadge } from "../src/lib/chat/grounding";
import { forgetThread, getChatState, loadThread, retrievalFor, sendMessage, type Thread } from "../src/lib/chat/store";
import type { ChatMode, ConversationDetail, Message, ModelCatalog, ModelInfo, RetrievalMeta } from "../src/lib/chat/types";

const GOLDEN = path.resolve(__dirname, "../../tests/contract/chat_golden");
const BACKEND = "http://backend.test/api/v1";
const ORIGIN = "http://localhost:3000";
/** tests/contract/test_chat_contract.py normalizes uuids in order of first
    appearance: the conversation is the first one in every transcript. */
const CONV = "00000000-0000-4000-8000-000000000001";
const ANSWER = "HX-217's gauge read 412.5 psi [incident:AN012], above the 380 psi band [asset:HX-217, incident:AN012].";
const QUESTION = "What happened to HX-217's gauge?";

interface Expect {
  question: string;
  model: string;
  mode: ChatMode;
  content: string;
  /** Chip order: canonical ids by first citation. */
  chips: string[];
  badge: { tone: string; label: string } | null;
  thinking: string | null;
  error: { code: string; retryAfterS: number | null } | null;
  /** The thread's title afterwards. null: no conversation event reached the
      thread (an `error` carries none; the open chat keeps what it had). */
  title: string | null;
  /** Strings the rendered AssistantTurn must contain. */
  html: (string | RegExp)[];
}

const EXPECT: Record<string, Expect> = {
  fast_cited: {
    question: QUESTION, model: "gemini-3.8-flash", mode: "fast", content: ANSWER,
    chips: ["incident:AN012", "asset:HX-217"], badge: { tone: "ok", label: "Grounded · 2 sources" },
    thinking: null, error: null, title: QUESTION,
    html: [/aria-label="Source 1: Gauge out of range on HX-217 \(M014\)"[^>]*>1<\/button>/, /aria-label="Source 2: [^"]*HX-217[^"]*"[^>]*>2<\/button>/, "Grounded · 2 sources", "Gemini 3.8 Flash · 1.0 s"],
  },
  thinking: {
    question: QUESTION, model: "gemini-3.5-flash", mode: "thinking", content: ANSWER,
    chips: ["incident:AN012", "asset:HX-217"], badge: { tone: "ok", label: "Grounded · 2 sources" },
    thinking: "Looking at the gauge source. Comparing to the 380 psi band.", error: null, title: QUESTION,
    html: ["Thought for 2 s", "aria-expanded=\"false\"", "Gemini 3.5 Flash · thinking · 1.0 s"],
  },
  rate_limited: {
    question: QUESTION, model: "gemini-3.8-flash", mode: "fast", content: "",
    chips: [], badge: null, thinking: null, error: { code: "rate_limited", retryAfterS: 41 }, title: null,
    html: ["Rate limit reached for Gemini 3.8 Flash", /Try again in <span[^>]*>41s<\/span>/, "Try with Gemini 3.5 Flash", /<button[^>]*disabled[^>]*>.*?Retry in <span[^>]*>41s<\/span>/],
  },
  first_exchange_title: {
    question: QUESTION, model: "gemini-3.8-flash", mode: "fast", content: ANSWER,
    chips: ["incident:AN012", "asset:HX-217"], badge: { tone: "ok", label: "Grounded · 2 sources" },
    thinking: null, error: null, title: "HX-217 Gauge Reading",
    html: ["Grounded · 2 sources"],
  },
  ungrounded: {
    question: "How is the fleet doing?", model: "gemini-3.5-flash-lite", mode: "fast",
    content: "Fleet success fell to 12.3% [stat:fleet] after the gauge fault [incident:AN999].",
    chips: ["stat:fleet"], badge: { tone: "bad", label: "Cited an unknown source" },
    thinking: null, error: null, title: "How is the fleet doing?",
    html: ["Cited an unknown source", /aria-label="Source 1: [^"]*"[^>]*>1<\/button>/, "[incident:AN999]"],
  },
};

/* ── helpers ─────────────────────────────────────────────────────────────── */

const read = (name: string) => readFileSync(path.join(GOLDEN, name), "utf8");

/** An independent reading of a golden (not the parser under test): the
    backend writes `event: X\ndata: <json>\n\n` per event. */
function goldenEvents(text: string): { event: string; data: Record<string, unknown> }[] {
  return text.split("\n\n").filter((block) => block && !block.startsWith(":")).map((block) => {
    const [event, data] = block.split("\n");
    assert.ok(event.startsWith("event: ") && data.startsWith("data: "), block);
    return { event: event.slice(7), data: JSON.parse(data.slice(6)) };
  });
}

/** The bytes in `size`-byte chunks (0 = one chunk). Small sizes split lines,
    the CR/LF-free framing and multi-byte characters like "·" and "’". */
function chunked(bytes: Uint8Array, size: number): ReadableStream<Uint8Array> {
  let offset = 0;
  const step = size > 0 ? size : bytes.length;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (offset >= bytes.length) return controller.close();
      controller.enqueue(bytes.slice(offset, offset + step));
      offset += step;
    },
  });
}

interface Upstream {
  url: string;
  method: string;
  accept: string | null;
  body: unknown;
}

/** The browser's fetch goes to the real route handlers; their fetch to
    BACKEND_URL answers from the goldens. Returns the upstream requests. */
function stubFetch(backend: { sse?: Uint8Array; chunk?: number; json?: Record<string, string> }) {
  const upstream: Upstream[] = [];
  globalThis.fetch = (async (input: string | URL | Request, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (url.startsWith("/api/chat/")) {
      const request = new Request(ORIGIN + url, {
        method: init.method ?? "GET",
        headers: { host: "localhost:3000", origin: ORIGIN, ...(init.headers as Record<string, string> | undefined) },
        body: init.body,
        signal: init.signal,
      });
      const id = url.split("/")[4];
      const params = { params: Promise.resolve({ id }) };
      if (url === "/api/chat/models") return modelsRoute(request);
      if (url.endsWith("/messages")) return sendRoute(request, params as never);
      return detailRoute(request, params as never);
    }
    if (url.startsWith(BACKEND)) {
      const headers = new Headers(init.headers);
      upstream.push({ url, method: init.method ?? "GET", accept: headers.get("accept"), body: init.body ? JSON.parse(String(init.body)) : null });
      const route = url.slice(BACKEND.length);
      if (route.endsWith("/messages") && backend.sse) {
        return new Response(chunked(backend.sse, backend.chunk ?? 0), { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      const json = backend.json?.[route];
      if (json !== undefined) return new Response(json, { status: 200, headers: { "Content-Type": "application/json" } });
    }
    throw new Error(`unexpected fetch ${init.method ?? "GET"} ${url}`);
  }) as typeof fetch;
  return upstream;
}

async function render(element: unknown) {
  const { renderToStaticMarkup } = await import("react-dom/server");
  return renderToStaticMarkup(element as never);
}

async function renderTurn(message: Message, thread: Thread, models: ModelInfo[]) {
  const { createElement } = await import("react");
  const { AssistantTurn } = await import("../src/components/chat/Turns");
  const noop = () => {};
  return render(createElement(AssistantTurn, {
    message, models, isLast: true, busy: false, liveError: thread.error,
    onRegenerate: noop, onRetry: noop, onTryModel: noop, onOpenSources: noop,
  }));
}

const thread = (id: string) => getChatState().threads[id];

/* ── setup ───────────────────────────────────────────────────────────────── */

const realFetch = globalThis.fetch;
const env = { backend: process.env.BACKEND_URL, flag: process.env.FEATURE_AI_ASSISTANT };
let models: ModelInfo[] = [];

before(async () => {
  process.env.BACKEND_URL = BACKEND;
  delete process.env.FEATURE_AI_ASSISTANT;
  // The model catalog the labels and "Try with …" come from, via the real route.
  stubFetch({ json: { "/chat/models": read("models.json") } });
  const response = await fetch("/api/chat/models", { cache: "no-store" });
  assert.equal(response.status, 200);
  const catalog = (await response.json()) as ModelCatalog;
  assert.ok(catalog.models.some((m) => m.id === catalog.default_model), "default_model is in the catalog");
  assert.deepEqual(catalog.modes.map((m) => m.id), ["fast", "thinking"]);
  models = catalog.models;
});

beforeEach(() => forgetThread(CONV));

after(() => {
  globalThis.fetch = realFetch;
  if (env.backend === undefined) delete process.env.BACKEND_URL; else process.env.BACKEND_URL = env.backend;
  if (env.flag === undefined) delete process.env.FEATURE_AI_ASSISTANT; else process.env.FEATURE_AI_ASSISTANT = env.flag;
});

/* ── tests ───────────────────────────────────────────────────────────────── */

describe("golden SSE transcripts through the web path (contract §4, §6)", () => {
  test("every golden transcript has web expectations, and vice versa", () => {
    const files = readdirSync(GOLDEN).filter((f) => f.endsWith(".sse")).sort();
    assert.deepEqual(files, Object.keys(EXPECT).map((name) => `${name}.sse`).sort());
  });

  for (const [name, want] of Object.entries(EXPECT)) {
    for (const chunk of [0, 5, 64]) {
      test(`${name}.sse (${chunk ? `${chunk}-byte chunks` : "one chunk"})`, async () => {
        const text = read(`${name}.sse`);
        const golden = goldenEvents(text);
        const byName = Object.fromEntries(golden.map((e) => [e.event, e.data]));
        const upstream = stubFetch({ sse: new TextEncoder().encode(text), chunk });

        assert.equal(await sendMessage(CONV, want.question, want.model, want.mode), true, "the turn went ahead");

        // The proxy forwarded exactly the contract's request body.
        assert.equal(upstream.length, 1);
        assert.deepEqual(upstream[0], {
          url: `${BACKEND}/chat/conversations/${CONV}/messages`, method: "POST", accept: "text/event-stream",
          body: { content: want.question, model: want.model, mode: want.mode, edit_message_id: null, regenerate: false },
        });

        const t = thread(CONV);
        assert.equal(t.stream, null, "ready for the next send");
        assert.equal(t.load, "ready");
        // Stored user turn (from meta) replaced the optimistic one; then the answer.
        const meta = byName.meta as { user_message: Message; request_id: string };
        const final = ((byName.done ?? byName.error) as { message: Message }).message;
        assert.deepEqual(t.messages, [meta.user_message, final], "the thread holds exactly what the backend stored");
        const [user, answer] = t.messages;
        assert.equal(user.message_id, "00000000-0000-4000-8000-000000000002");
        assert.equal(user.content, want.question);
        assert.equal(answer.role, "assistant");

        // Content, thinking, chips.
        assert.equal(answer.content, want.content);
        assert.equal(answer.content, golden.filter((e) => e.event === "delta").map((e) => e.data.delta).join(""), "deltas add up to the stored answer");
        assert.equal(answer.thinking, want.thinking);
        if (want.thinking) assert.equal(golden.filter((e) => e.event === "thinking").map((e) => e.data.delta).join(""), want.thinking);
        const ids = answer.sources.map((s) => s.id);
        const chips = citationOrder(answer.content, sourceResolver(ids));
        assert.deepEqual(chips, want.chips, "chip order");
        assert.deepEqual(chips, answer.citations, "web chips == backend citations (one shared rule, §2)");
        if (answer.grounding) {
          assert.deepEqual(citationsIn(answer.content, ids), { cited: answer.grounding.cited_ids, unknown: answer.grounding.unknown_ids });
        }

        // Grounding badge.
        const badge = groundingBadge(answer.grounding);
        assert.deepEqual(badge && { tone: badge.tone, label: badge.label }, want.badge);

        // Error card state.
        if (want.error) {
          assert.equal(answer.status, "error");
          assert.equal(answer.error_code, want.error.code);
          assert.ok(t.error, "a live error card");
          assert.equal(t.error.code, want.error.code);
          assert.equal(t.error.retryAfterS, want.error.retryAfterS);
          assert.equal(t.error.messageId, answer.message_id);
          assert.equal(t.error.model, want.model);
          assert.deepEqual(t.error.retry, { content: want.question, model: want.model, mode: want.mode, edit_message_id: null, regenerate: true }, "Retry regenerates the stored question");
        } else {
          assert.equal(answer.status, "complete");
          assert.equal(t.error, null);
          // The retrieval meta of a streamed answer is kept for its Sources panel.
          assert.deepEqual(retrievalFor(answer.message_id), (byName.sources as { retrieval: RetrievalMeta }).retrieval);
        }

        // Title (a late `title` after `done` still lands).
        if (want.title === null) assert.equal(t.conversation, null);
        else {
          assert.equal(t.conversation?.conversation_id, CONV);
          assert.equal(t.conversation?.title, want.title);
        }
        if (byName.title) assert.equal(golden.at(-1)?.event, "title");

        // The rendered turn.
        const html = await renderTurn(answer, t, models);
        for (const needle of want.html) {
          if (typeof needle === "string") assert.ok(html.includes(needle), `${name}: rendered turn lacks ${JSON.stringify(needle)}`);
          else assert.match(html, needle);
        }
      });
    }
  }
});

describe("golden reload through the web path (contract §4 GET /conversations/{id}, chat.2 error_code)", () => {
  test("a stored rate_limited turn reloads with the same card, minus the countdown", async () => {
    const detail = read("rate_limited.detail.json");
    const upstream = stubFetch({ json: { [`/chat/conversations/${CONV}`]: detail } });
    await loadThread(CONV);
    assert.deepEqual(upstream.map((u) => [u.method, u.url]), [["GET", `${BACKEND}/chat/conversations/${CONV}`]]);
    const t = thread(CONV);
    const stored = JSON.parse(detail) as ConversationDetail;
    assert.equal(t.load, "ready");
    assert.deepEqual(t.messages, stored.messages);
    assert.deepEqual(t.conversation, stored.conversation);
    assert.equal(t.error, null, "no live error after a reload");
    // The reload holds what the stream ended with.
    const streamed = goldenEvents(read("rate_limited.sse"));
    assert.deepEqual(t.messages.at(-1), (streamed.at(-1)?.data as { message: Message }).message);
    const html = await renderTurn(t.messages[1], t, models);
    assert.match(html, /Rate limit reached for Gemini 3\.8 Flash/);
    assert.match(html, /Try with Gemini 3\.5 Flash/);
    assert.doesNotMatch(html, /Try again in/);
  });
});
