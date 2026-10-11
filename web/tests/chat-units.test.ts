/* Unit gaps in web/src/lib/chat (contract §2, §4, §6): SSE parser fuzz over
   random chunk splits, citation tokenizer edge cases in Markdown (nested
   brackets, links, line ends, tables) checked against the backend's answers,
   a 50 KB performance bound, grounding badge precedence, and the browser API
   helpers' error mapping. Deterministic: a seeded PRNG, no network. */

import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import { createSSEParser, readSSE, type SSEMessage } from "../src/lib/chat/sse";
import { citationGroups, citationOrder, citationsIn, markdownSegments, remarkCitations, sourceResolver, tokenizeCitations } from "../src/lib/chat/citations";
import { groundingBadge } from "../src/lib/chat/grounding";
import { ChatHttpError, chatFetch, describeError, offersModelSwitch, streamTurn } from "../src/lib/chat/api";
import type { ChatErrorCode, ChatEvent, Grounding } from "../src/lib/chat/types";

/** mulberry32: a tiny seeded PRNG, so a failing split can be replayed. */
function prng(seed: number) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function randomCuts<T extends { length: number; slice(a: number, b?: number): T }>(whole: T, rand: () => number, max = 40): T[] {
  const cuts = new Set<number>();
  const n = 1 + Math.floor(rand() * Math.min(max, whole.length - 1));
  while (cuts.size < n) cuts.add(1 + Math.floor(rand() * (whole.length - 1)));
  const sorted = [...cuts].sort((a, b) => a - b);
  return [0, ...sorted].map((start, i) => whole.slice(start, sorted[i] ?? whole.length));
}

/* ── SSE parser fuzz ───────────────────────────────────────────────────── */

/** Every line-ending style, comments, ids, multi-line and empty data, and
    non-ASCII text: what a backend and two proxies could plausibly send. */
const GOLDEN =
  ": keep-alive\n\n" +
  'event: meta\r\ndata: {"conversation_id":"c","user_message":{"content":"≤ 25% — “why”?"}}\r\n\r\n' +
  "event: sources\rdata: {\"sources\":[]}\r\r" +
  "id: 7\nevent: thinking\ndata: {\"delta\":\"step 1\"}\n\n" +
  "event: delta\ndata: line one\ndata:\ndata: line three\n\n" +
  ": keep-alive\r\n\r\n" +
  "event: delta\ndata: {\"delta\":\"HX-217 read 412.5 psi [incident:AN012] ✓\"}\n\n" +
  "retry: 3000\nevent: done\ndata: {\"ok\":true}\n\n" +
  "event: title\ndata: {\"title\":\"Gauge 🚨 report\"}\n\n";

function parseAll(chunks: string[]) {
  const out: SSEMessage[] = [];
  const parser = createSSEParser((m) => out.push(m));
  chunks.forEach((chunk) => parser.feed(chunk));
  parser.end();
  return out;
}

describe("SSE parser fuzz", () => {
  const expected = parseAll([GOLDEN]);

  test("the golden stream parses to the expected events", () => {
    assert.deepEqual(expected.map((m) => m.event), ["meta", "sources", "thinking", "delta", "delta", "done", "title"]);
    assert.equal(expected[3].data, "line one\n\nline three");
    assert.equal(expected[2].id, "7");
    assert.equal(JSON.parse(expected[6].data).title, "Gauge 🚨 report");
  });

  test("500 random multi-way string splits give identical events", () => {
    const rand = prng(20261010);
    for (let i = 0; i < 500; i++) {
      const chunks = randomCuts(GOLDEN, rand);
      assert.deepEqual(parseAll(chunks), expected, `split ${JSON.stringify(chunks.map((c) => c.length))}`);
    }
  });

  test("random byte splits through readSSE (mid UTF-8 sequence) give identical events", async () => {
    const bytes = new TextEncoder().encode(GOLDEN);
    const rand = prng(42);
    for (let i = 0; i < 150; i++) {
      const pieces = randomCuts(bytes, rand, 60);
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          pieces.forEach((piece) => controller.enqueue(piece));
          controller.close();
        },
      });
      const out: SSEMessage[] = [];
      await readSSE(body, (m) => out.push(m));
      assert.deepEqual(out, expected);
    }
  });

  test("a stream cut mid-event yields only the complete events", () => {
    const cut = GOLDEN.indexOf("event: done");
    const out = parseAll([GOLDEN.slice(0, cut + 20)]);
    assert.deepEqual(out.map((m) => m.event), ["meta", "sources", "thinking", "delta", "delta"]);
  });
});

/* ── citation tokenizer on Markdown edge cases ─────────────────────────── */

const IDS = ["incident:AN012", "stat:fleet"];
const resolve = sourceResolver(IDS);
const processor = unified().use(remarkParse).use(remarkGfm).use(remarkCitations, { resolve });

function renderedChips(markdown: string) {
  const tree = processor.runSync(processor.parse(markdown));
  const out: string[] = [];
  (function walk(node: { type: string; children?: unknown[]; data?: { hProperties?: { dataCitation?: string } } }) {
    if (node.type === "citation") out.push(...(node.data?.hProperties?.dataCitation ?? "").split(" "));
    (node.children as (typeof node)[] | undefined)?.forEach(walk);
  })(tree as never);
  return out;
}

/* Expected values were produced by the backend's citations_in / citation_groups
   (backend/app/services/chat/grounding.py) on the same strings and sources. */
const EDGES: { name: string; text: string; cited: string[]; unknown?: string[]; groups: string[][] }[] = [
  { name: "nested brackets", text: "[[incident:AN012]]", cited: ["incident:AN012"], groups: [["incident:AN012"]] },
  { name: "a citation inside literal brackets", text: "[see [incident:AN012]]", cited: ["incident:AN012"], groups: [["incident:AN012"]] },
  { name: "citations at line ends", text: "claim [stat:fleet]\nnext [incident:AN012]\n", cited: ["stat:fleet", "incident:AN012"], groups: [["stat:fleet"], ["incident:AN012"]] },
  { name: "a group never spans a line", text: "claim [stat:fleet\n]", cited: [], groups: [] },
  { name: "a space before ( is not a link", text: "[stat:fleet] (note)", cited: ["stat:fleet"], groups: [["stat:fleet"]] },
  { name: "an empty link is a link", text: "[stat:fleet]() then [incident:AN012]", cited: ["incident:AN012"], groups: [["incident:AN012"]] },
  { name: "inside emphasis", text: "**[stat:fleet]** and _[incident:AN012]_", cited: ["stat:fleet", "incident:AN012"], groups: [["stat:fleet"], ["incident:AN012"]] },
  { name: "in a GFM table", text: "| a [stat:fleet] | b [incident:AN012, x:y] |\n|---|---|\n| 1 | 2 |", cited: ["stat:fleet", "incident:AN012"], unknown: ["x:y"], groups: [["stat:fleet"], ["incident:AN012"]] },
  { name: "in list items", text: "- [stat:fleet]\n- [incident:AN012]", cited: ["stat:fleet", "incident:AN012"], groups: [["stat:fleet"], ["incident:AN012"]] },
  { name: "a reference-style pair", text: "[stat:fleet][1]", cited: ["stat:fleet"], groups: [["stat:fleet"], []] },
  { name: "empty parts", text: "[ stat:fleet ; ; incident:AN012 , ]", cited: ["stat:fleet", "incident:AN012"], groups: [["stat:fleet", "incident:AN012"]] },
  { name: "at the very end", text: "[incident:AN012].", cited: ["incident:AN012"], groups: [["incident:AN012"]] },
  { name: "code spans around and inside", text: "`code` [stat:fleet] `[incident:AN012]` [x:`y`]", cited: ["stat:fleet"], groups: [["stat:fleet"], []] },
];

describe("citation tokenizer: Markdown edge cases", () => {
  for (const edge of EDGES) {
    test(`${edge.name}: same answer as the backend, and the renderer shows exactly the cited chips`, () => {
      assert.deepEqual(citationsIn(edge.text, IDS), { cited: edge.cited, unknown: edge.unknown ?? [] });
      assert.deepEqual(citationGroups(edge.text, resolve).map((g) => g.chips), edge.groups);
      assert.deepEqual(citationOrder(edge.text, resolve), edge.cited);
      assert.deepEqual(renderedChips(edge.text), edge.cited.length ? edge.groups.flat() : []);
    });
  }

  test("nested brackets keep the outer brackets as text around the chip", () => {
    assert.deepEqual(tokenizeCitations("x [[incident:AN012]] y", resolve), [
      { type: "text", value: "x [" },
      { type: "cite", ids: ["incident:AN012"] },
      { type: "text", value: "] y" },
    ]);
  });

  test.todo("BUG: a citation inside link text or escaped brackets renders differently from cited_ids (contract §2: backend cited_ids and web chips must agree)", async () => {
    // `[see [stat:fleet]](url)`: the backend and citationOrder count stat:fleet
    // (chip 1), but links are skipped by the renderer, so the only visible chip
    // reads "2". `\[stat:fleet\]`: the renderer makes a chip node that has no
    // number (citationOrder skipped it), so the text vanishes from the answer.
    const { createElement } = await import("react");
    const { renderToStaticMarkup } = await import("react-dom/server");
    const { Markdown } = await import("../src/components/chat/Markdown");
    const sources = IDS.map((id, i) => ({ id, n: i + 1, origin: "rag" as const, kind: "x", title: id, snippet: "", score: null }));
    const linked = renderToStaticMarkup(createElement(Markdown, { content: "[see [stat:fleet]](https://x.test) and [incident:AN012]", sources }));
    assert.match(linked, /aria-label="Source 1: /, "the first visible chip reads 1");
    const escaped = renderToStaticMarkup(createElement(Markdown, { content: "\\[stat:fleet\\] and [incident:AN012]", sources }));
    assert.match(escaped, /stat:fleet/, "escaped brackets stay visible");
  });

  test("a 50 KB answer tokenizes in under 50 ms", () => {
    const paragraph =
      "HX-217 read **412.5 psi** [incident:AN012], above the band [stat:fleet; incident:AN012]. See `[stat:fleet]` and " +
      "[the manual](https://example.com/m) or [not an id] [mission:M001, M002].\n\n" +
      "| waypoint | rate |\n|---|---|\n| WP06 | 62.5% [stat:fleet] |\n\n- one [incident:AN012]\n- two\n\n```\n[stat:fleet]\n```\n\n";
    const answer = paragraph.repeat(Math.ceil(50_000 / paragraph.length)).slice(0, 50_000);
    assert.ok(answer.length === 50_000);
    const best = (fn: () => void) => {
      let min = Infinity;
      for (let i = 0; i < 5; i++) {
        const t0 = performance.now();
        fn();
        min = Math.min(min, performance.now() - t0);
      }
      return min;
    };
    let order: string[] = [];
    const ms = best(() => { order = citationOrder(answer, resolve); citationsIn(answer, IDS); tokenizeCitations(answer, resolve); });
    assert.deepEqual(order, ["incident:AN012", "stat:fleet"]);
    assert.ok(ms < 50, `took ${ms.toFixed(1)} ms`);
  });

  test("pathological inputs (bracket and backtick floods) stay linear", () => {
    const inputs = [
      "[".repeat(50_000),
      "]".repeat(50_000),
      "[a".repeat(25_000),
      "`".repeat(25_000) + "x" + "``".repeat(12_000),
      Array.from({ length: 300 }, (_, i) => "`".repeat(i + 1) + " ").join("").repeat(2),
      "[stat:fleet".repeat(5_000) + "]",
      "```\n".repeat(12_000),
    ];
    for (const text of inputs) {
      const t0 = performance.now();
      citationOrder(text, resolve);
      citationsIn(text, IDS);
      markdownSegments(text);
      const ms = performance.now() - t0;
      assert.ok(ms < 250, `${JSON.stringify(text.slice(0, 12))}… took ${ms.toFixed(1)} ms`);
    }
  });
});

/* ── grounding badge precedence ───────────────────────────────────────── */

function g(over: Partial<Grounding>): Grounding {
  return { grounded: true, cited_ids: ["stat:fleet"], unknown_ids: [], unverified_numbers: [], source_count: 9, retrieval_mode: "hybrid", graph_seeds: [], ...over };
}

describe("grounding badge precedence: unknown > no citations > unverified figures > grounded", () => {
  const cases: [string, Partial<Grounding>, string, string][] = [
    ["unknown + no citations", { grounded: false, cited_ids: [], unknown_ids: ["x:y"] }, "bad", "Cited an unknown source"],
    ["unknown + figures + cited", { grounded: false, unknown_ids: ["x:y", "a:b"], unverified_numbers: ["7"] }, "bad", "Cited an unknown source"],
    ["no citations + figures", { grounded: false, cited_ids: [], unverified_numbers: ["7", "8"] }, "muted", "No sources cited"],
    ["figures only", { unverified_numbers: ["7"] }, "warn", "1 figure not found in sources"],
    ["clean, three sources", { cited_ids: ["a:1", "b:2", "c:3"] }, "ok", "Grounded · 3 sources"],
    // The badge follows the arrays, which is exactly how `grounded` is defined.
    ["a stale grounded=false with clean arrays", { grounded: false }, "ok", "Grounded · 1 source"],
  ];
  for (const [name, over, tone, label] of cases) {
    test(name, () => {
      const badge = groundingBadge(g(over));
      assert.equal(badge?.tone, tone);
      assert.equal(badge?.label, label);
      assert.ok(badge?.detail);
    });
  }

  test("an old row with missing arrays degrades instead of throwing", () => {
    const partial = { grounded: false, retrieval_mode: "fulltext" } as unknown as Grounding;
    assert.equal(groundingBadge(partial)?.tone, "muted");
    assert.equal(groundingBadge(undefined), null);
  });

  test("the unknown-id detail names the ids", () => {
    assert.match(groundingBadge(g({ unknown_ids: ["incident:AN999", "x:y"] }))!.detail, /2 ids .*incident:AN999, x:y/);
  });
});

/* ── browser API helpers ───────────────────────────────────────────────── */

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("api: error mapping", () => {
  test("chatFetch maps JSON error bodies, non-JSON bodies and 204", async () => {
    const replies: Response[] = [
      Response.json({ error: "conversation_busy", detail: "busy" }, { status: 409 }),
      new Response("<html>nope</html>", { status: 404 }),
      new Response("oops", { status: 500 }),
      Response.json({ error: "invalid_request", detail: [{ loc: ["body", "title"], msg: "too long" }] }, { status: 422 }),
      new Response(null, { status: 204 }),
      Response.json({ ok: 1 }),
    ];
    globalThis.fetch = (async () => replies.shift()!) as typeof fetch;
    const errors: ChatHttpError[] = [];
    for (let i = 0; i < 4; i++) {
      try {
        await chatFetch("/api/chat/x");
        assert.fail("should throw");
      } catch (error) {
        assert.ok(error instanceof ChatHttpError);
        errors.push(error);
      }
    }
    assert.deepEqual(errors.map((e) => [e.status, e.code, e.message]), [
      [409, "conversation_busy", "busy"],
      [404, "not_found", "Request failed (404)."],
      [500, "upstream_error", "Request failed (500)."],
      [422, "invalid_request", '[{"loc":["body","title"],"msg":"too long"}]'],
    ]);
    assert.equal(await chatFetch("/api/chat/x", { method: "DELETE" }), undefined);
    assert.deepEqual(await chatFetch("/api/chat/x"), { ok: 1 });
  });

  test("streamTurn throws the proxy's pre-stream error, skips unknown and malformed events", async () => {
    globalThis.fetch = (async () => Response.json({ error: "gemini_unset", detail: "no key" }, { status: 503 })) as typeof fetch;
    await assert.rejects(
      streamTurn("c", { content: "q", model: "m", mode: "fast", edit_message_id: null, regenerate: false }, new AbortController().signal, () => {}),
      (error: unknown) => error instanceof ChatHttpError && error.status === 503 && error.code === "gemini_unset",
    );
    const sse = 'event: meta\ndata: {"a":1}\n\nevent: ping\ndata: {}\n\nevent: delta\ndata: {not json\n\ndata: {"untyped":1}\n\nevent: done\ndata: {"b":2}\n\n';
    globalThis.fetch = (async () => new Response(sse, { headers: { "Content-Type": "text/event-stream" } })) as typeof fetch;
    const seen: ChatEvent[] = [];
    await streamTurn("c", { content: "q", model: "m", mode: "fast", edit_message_id: null, regenerate: false }, new AbortController().signal, (e) => seen.push(e));
    assert.deepEqual(seen, [{ event: "meta", data: { a: 1 } }, { event: "done", data: { b: 2 } }] as unknown as ChatEvent[]);
  });

  test("every contract error code has plain-language copy; only model errors offer a switch", () => {
    const codes: ChatErrorCode[] = ["rate_limited", "model_unavailable", "gemini_auth", "gemini_unset", "blocked", "upstream_error",
      "retrieval_failed", "internal_error", "conversation_busy", "not_found", "invalid_request", "backend_unreachable",
      "backend_timeout", "connection_lost", "disabled", "something_new" as ChatErrorCode];
    for (const code of codes) {
      const copy = describeError(code, null, "Gemini 3.8 Flash");
      assert.ok(copy.title && copy.hint, code);
    }
    assert.equal(describeError("internal_error").title, "Something went wrong");
    assert.equal(describeError("blocked", null, null).title, "The model declined to answer");
    assert.equal(describeError("invalid_request", "unknown model 'x'").hint, "unknown model 'x'");
    assert.deepEqual(codes.filter(offersModelSwitch), ["rate_limited", "model_unavailable"]);
  });
});
