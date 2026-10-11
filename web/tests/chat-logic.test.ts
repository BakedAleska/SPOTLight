import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, test } from "node:test";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import { createSSEParser, readSSE, type SSEMessage } from "../src/lib/chat/sse";
import { citationGroups, citationOrder, citationsIn, markdownSegments, remarkCitations, sourceResolver, tokenizeCitations } from "../src/lib/chat/citations";
import { arrangeSources, retrievalNotes } from "../src/components/chat/SourcesPanel";
import { placeTooltip } from "../src/components/chat/CitationChip";
import { groupConversations } from "../src/lib/chat/history";
import { groundingBadge, modelLabel, nextModel } from "../src/lib/chat/grounding";
import type { ConversationSummary, Grounding, Message, ModelInfo, Source } from "../src/lib/chat/types";

/* ── SSE parser ─────────────────────────────────────────────────────────── */

function parseAll(chunks: string[]) {
  const out: SSEMessage[] = [];
  const parser = createSSEParser((m) => out.push(m));
  chunks.forEach((chunk) => parser.feed(chunk));
  parser.end();
  return out;
}

const STREAM =
  'event: meta\ndata: {"a":1}\n\n' +
  ": keep-alive\n\n" +
  "event: delta\ndata: line one\ndata: line two\n\n" +
  'event: done\ndata:{"ok":true}\n\n';

describe("SSE parser", () => {
  test("parses events, joins multi-line data, skips comments", () => {
    assert.deepEqual(parseAll([STREAM]), [
      { event: "meta", data: '{"a":1}' },
      { event: "delta", data: "line one\nline two" },
      { event: "done", data: '{"ok":true}' },
    ]);
  });

  test("gives the same result for every possible chunk split", () => {
    const whole = parseAll([STREAM]);
    for (let i = 1; i < STREAM.length; i++) {
      assert.deepEqual(parseAll([STREAM.slice(0, i), STREAM.slice(i)]), whole, `split at ${i}`);
    }
    assert.deepEqual(parseAll([...STREAM]), whole, "one character per chunk");
  });

  test("handles CRLF and bare CR line endings, including CRLF split across chunks", () => {
    const crlf = STREAM.replace(/\n/g, "\r\n");
    const expected = parseAll([STREAM]);
    assert.deepEqual(parseAll([crlf]), expected);
    assert.deepEqual(parseAll([STREAM.replace(/\n/g, "\r")]), expected);
    for (let i = 1; i < crlf.length; i++) {
      assert.deepEqual(parseAll([crlf.slice(0, i), crlf.slice(i)]), expected, `CRLF split at ${i}`);
    }
  });

  test("defaults the event name, strips one leading space, ignores data-less blocks and unknown fields", () => {
    assert.deepEqual(parseAll(["data:  two spaces\n\n", "event: ping\n\n", "retry: 10\nfoo: bar\ndata: x\n\n"]), [
      { event: "message", data: " two spaces" },
      { event: "message", data: "x" },
    ]);
  });

  test("an unterminated final block is discarded, per spec", () => {
    assert.deepEqual(parseAll(["event: delta\ndata: partial"]), []);
  });

  test("readSSE decodes UTF-8 split across byte chunks", async () => {
    const bytes = new TextEncoder().encode('event: delta\ndata: {"delta":"Grounded ✓ — 62.5%"}\n\n');
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 0; i < bytes.length; i += 3) controller.enqueue(bytes.slice(i, i + 3));
        controller.close();
      },
    });
    const out: SSEMessage[] = [];
    await readSSE(body, (m) => out.push(m));
    assert.equal(out.length, 1);
    assert.equal(JSON.parse(out[0].data).delta, "Grounded ✓ — 62.5%");
  });
});

/* ── citations: the rule shared with the backend (contract §2, chat.2) ──── */

const KNOWN = ["incident:AN012", "event:act-A0001", "stat:fleet", "failure_type:LOCALIZATION_FAILURE", "manual:p52-1"];
const resolve = sourceResolver(KNOWN);

describe("citation tokenizer", () => {
  test("a single citation", () => {
    assert.deepEqual(tokenizeCitations("Gauge out of range [incident:AN012].", resolve), [
      { type: "text", value: "Gauge out of range " },
      { type: "cite", ids: ["incident:AN012"] },
      { type: "text", value: "." },
    ]);
  });

  test("adjacent citations merge into one group; `,` and `;` lists split", () => {
    assert.deepEqual(tokenizeCitations("A [incident:AN012][event:act-A0001] B [stat:fleet; failure_type:LOCALIZATION_FAILURE]", resolve), [
      { type: "text", value: "A " },
      { type: "cite", ids: ["incident:AN012", "event:act-A0001"] },
      { type: "text", value: " B " },
      { type: "cite", ids: ["stat:fleet", "failure_type:LOCALIZATION_FAILURE"] },
    ]);
  });

  test("only citing parts become chips; a group with none stays literal", () => {
    assert.deepEqual(tokenizeCitations("x [asset:HX-999]", resolve), [{ type: "text", value: "x [asset:HX-999]" }]);
    assert.deepEqual(tokenizeCitations("[manual:p52-1, asset:HX-999]", resolve), [{ type: "cite", ids: ["manual:p52-1"] }]);
  });

  test("case-insensitive, with quotes and backticks trimmed; chips use the canonical id", () => {
    assert.deepEqual(tokenizeCitations(`[INCIDENT:an012, "stat:fleet", 'manual:p52-1', \`event:act-A0001\`]`, resolve), [
      { type: "cite", ids: ["incident:AN012", "stat:fleet", "manual:p52-1", "event:act-A0001"] },
    ]);
  });

  test("brackets that cite nothing, and Markdown links, are left alone", () => {
    for (const text of ["see [1]", "[M007]", "[see incident:AN012]", "[incident:AN012 and more]", "a [] b", "[incident:AN012", "[incident:AN012](https://x.test)"]) {
      assert.deepEqual(tokenizeCitations(text, resolve), [{ type: "text", value: text }], text);
    }
  });

  test("shared contract fixtures (tests/contract/chat_citations.json): groups, chips, cited and unknown ids", () => {
    const file = path.resolve(__dirname, "../../tests/contract/chat_citations.json");
    const { sources, cases } = JSON.parse(readFileSync(file, "utf8")) as {
      sources: string[];
      cases: { name: string; text: string; cited_ids: string[]; unknown_ids: string[]; groups: { raw: string; chips: string[] }[] }[];
    };
    assert.ok(cases.length >= 13, "fixture file loaded");
    const fixtureResolve = sourceResolver(sources);
    for (const fixture of cases) {
      assert.deepEqual(citationGroups(fixture.text, fixtureResolve), fixture.groups, `${fixture.name}: groups`);
      assert.deepEqual(citationsIn(fixture.text, sources), { cited: fixture.cited_ids, unknown: fixture.unknown_ids }, `${fixture.name}: cited/unknown`);
      assert.deepEqual(citationOrder(fixture.text, fixtureResolve), fixture.cited_ids, `${fixture.name}: chip order`);
    }
  });

  test("the rendered chips match the fixtures' groups", () => {
    const file = path.resolve(__dirname, "../../tests/contract/chat_citations.json");
    const { sources, cases } = JSON.parse(readFileSync(file, "utf8")) as { sources: string[]; cases: { name: string; text: string; groups: { chips: string[] }[] }[] };
    const processor = unified().use(remarkParse).use(remarkGfm).use(remarkCitations, { resolve: sourceResolver(sources) });
    for (const fixture of cases) {
      const tree = processor.runSync(processor.parse(fixture.text));
      const rendered: string[][] = [];
      (function walk(node: { type: string; children?: unknown[]; data?: { hProperties?: { dataCitation?: string } } }) {
        if (node.type === "citation") rendered.push((node.data?.hProperties?.dataCitation ?? "").split(" "));
        (node.children as (typeof node)[] | undefined)?.forEach(walk);
      })(tree as never);
      // Adjacent citing groups render as one chip row; compare the chips.
      const expected = fixture.groups.map((g) => g.chips).filter((chips) => chips.length > 0);
      assert.deepEqual(rendered.flat(), expected.flat(), fixture.name);
    }
  });

  test("curly quotes are trimmed like straight ones", () => {
    assert.deepEqual(citationOrder("Seen [“incident:AN012”, ‘stat:fleet’].", resolve), ["incident:AN012", "stat:fleet"]);
  });

  test("citation order is first use, and code is never parsed", () => {
    const md = "B [stat:fleet]. A [incident:AN012]; again [stat:fleet].\n\n`[manual:p52-1]`\n\n```\n[event:act-A0001]\n```\n\n~~~md\n[failure_type:LOCALIZATION_FAILURE]\n~~~\n";
    assert.deepEqual(citationOrder(md, resolve), ["stat:fleet", "incident:AN012"]);
    assert.deepEqual(citationsIn(md, KNOWN).cited, ["stat:fleet", "incident:AN012"]);
    // An unclosed fence runs to the end, as in CommonMark.
    assert.deepEqual(citationOrder("[stat:fleet]\n```\n[incident:AN012]", resolve), ["stat:fleet"]);
    // A code span *inside* a group is just a quoted id.
    assert.deepEqual(citationOrder("Fell [`incident:AN012`].", resolve), ["incident:AN012"]);
  });

  test("markdownSegments splits fenced blocks and inline code spans, matching backtick runs", () => {
    assert.deepEqual(markdownSegments("a `b` c ``d ` e`` f `g"), [
      { code: false, value: "a " },
      { code: true, value: "`b`" },
      { code: false, value: " c " },
      { code: true, value: "``d ` e``" },
      { code: false, value: " f `g" },
    ]);
  });

  test("the remark plugin leaves code spans and fenced code untouched", () => {
    const md = "Battery fell [incident:AN012].\n\nUse `[stat:fleet]` here, and [`manual:p52-1`].\n\n```\n[incident:AN012]\n```\n\n| a | b |\n|---|---|\n| 1 [stat:fleet] | 2 |\n";
    const processor = unified().use(remarkParse).use(remarkGfm).use(remarkCitations, { resolve });
    const tree = processor.runSync(processor.parse(md)) as unknown as { children: unknown[] };
    const citations: string[] = [];
    const code: string[] = [];
    (function walk(node: { type: string; value?: string; children?: unknown[]; data?: { hProperties?: { dataCitation?: string } } }) {
      if (node.type === "citation") citations.push(node.data?.hProperties?.dataCitation ?? "");
      if (node.type === "code" || node.type === "inlineCode") code.push(node.value ?? "");
      (node.children as (typeof node)[] | undefined)?.forEach(walk);
    })(tree as never);
    assert.deepEqual(citations, ["incident:AN012", "manual:p52-1", "stat:fleet"]);
    assert.deepEqual(code, ["[stat:fleet]", "[incident:AN012]"]);
  });
});

/* ── Sources panel and tooltip placement ───────────────────────────────── */

function source(id: string, n: number): Source {
  return { id, n, origin: "rag", kind: id.split(":")[0], title: id, snippet: "", score: null };
}

describe("Sources panel order", () => {
  test("cited sources first, numbered like their chips; the rest by retrieval order, unnumbered", () => {
    const sources = [source("a:1", 1), source("b:2", 2), source("c:3", 3), source("d:4", 4), source("e:13", 13)];
    const { cited, rest } = arrangeSources(sources, ["e:13", "b:2", "zzz:missing"]);
    assert.deepEqual(cited.map((c) => [c.number, c.source.id]), [[1, "e:13"], [2, "b:2"]]);
    assert.deepEqual(rest.map((s) => s.id), ["a:1", "c:3", "d:4"]);
  });

  test("retrieval notes: keyword-only in plain words, raw gap strings as details", () => {
    assert.equal(retrievalNotes(null), null);
    assert.equal(retrievalNotes({ mode: "hybrid", gaps: [] }), null);
    const offline = retrievalNotes({ mode: "fulltext", gaps: ["embedding unavailable (connect timeout): full-text search only", "graph match timed out (retrieval budget)"] });
    assert.match(offline!.summary, /^Keyword search only: the embedding service is offline/);
    assert.deepEqual(offline!.details, ["embedding unavailable (connect timeout): full-text search only", "graph match timed out (retrieval budget)"]);
    assert.match(retrievalNotes({ mode: "fulltext", gaps: ["embedding slower than 2.4s: full-text search only"] })!.summary, /too slow/);
    assert.match(retrievalNotes({ mode: "fulltext" })!.summary, /offline/, "a reloaded answer knows only its mode");
    const skipped = retrievalNotes({ mode: "hybrid", gaps: ["graph neighbourhood skipped: retrieval budget used up"] });
    assert.match(skipped!.summary, /may have missed sources/);
  });
});

describe("citation tooltip placement", () => {
  const viewport = { width: 390, height: 844 };
  const tip = { width: 288, height: 120 };
  test("clamped inside the right edge", () => {
    const { left } = placeTooltip({ left: 370, top: 400, bottom: 418, width: 18 }, tip, viewport);
    assert.equal(left, 390 - 288 - 8);
  });
  test("clamped inside the left edge", () => {
    assert.equal(placeTooltip({ left: 2, top: 400, bottom: 418, width: 18 }, tip, viewport).left, 8);
  });
  test("above when it fits, below near the top, clamped near the bottom", () => {
    assert.equal(placeTooltip({ left: 100, top: 400, bottom: 418, width: 18 }, tip, viewport).top, 400 - 6 - 120);
    assert.equal(placeTooltip({ left: 100, top: 60, bottom: 78, width: 18 }, tip, viewport).top, 78 + 6);
    const squeezed = placeTooltip({ left: 100, top: 100, bottom: 118, width: 18 }, { width: 288, height: 200 }, { width: 390, height: 300 });
    assert.ok(squeezed.top >= 8 && squeezed.top + 200 <= 300 - 8 + 0.001, JSON.stringify(squeezed));
  });
});

/* ── history grouping ──────────────────────────────────────────────────── */

function conv(id: string, updated: Date | string, pinned = false): ConversationSummary {
  return {
    conversation_id: id, title: id, title_source: "auto", model: "m", mode: "fast", pinned,
    created_at: "2026-10-01T00:00:00Z", updated_at: typeof updated === "string" ? updated : updated.toISOString(),
    message_count: 2, last_message_preview: null,
  };
}

describe("history date groups", () => {
  const now = new Date(2026, 9, 10, 15, 30); // local time, Oct 10 2026 15:30
  const at = (days: number, hour = 12) => new Date(2026, 9, 10 - days, hour);

  test("pinned first, then Today / Yesterday / Previous 7 days / Older, newest first", () => {
    const groups = groupConversations([
      conv("old", at(30)),
      conv("today-early", at(0, 0)),
      conv("yesterday-late", at(1, 23)),
      conv("week", at(6)),
      conv("pinned-old", at(40), true),
      conv("today-late", at(0, 15)),
      conv("eight-days", at(8)),
    ], now);
    assert.deepEqual(groups.map((g) => [g.label, g.items.map((c) => c.conversation_id)]), [
      ["Pinned", ["pinned-old"]],
      ["Today", ["today-late", "today-early"]],
      ["Yesterday", ["yesterday-late"]],
      ["Previous 7 days", ["week"]],
      ["Older", ["eight-days", "old"]],
    ]);
  });

  test("empty groups are dropped and bad dates fall into Older", () => {
    const groups = groupConversations([conv("today", at(0)), conv("broken", "not a date")], now);
    assert.deepEqual(groups.map((g) => g.label), ["Today", "Older"]);
    assert.deepEqual(groupConversations([], now), []);
  });
});

/* ── grounding badge ───────────────────────────────────────────────────── */

function grounding(over: Partial<Grounding>): Grounding {
  return { grounded: true, cited_ids: ["incident:AN012", "stat:fleet"], unknown_ids: [], unverified_numbers: [], source_count: 11, retrieval_mode: "hybrid", graph_seeds: [], ...over };
}

describe("grounding badge", () => {
  test("clean answers are grounded with the cited-source count", () => {
    assert.deepEqual(
      { tone: groundingBadge(grounding({}))?.tone, label: groundingBadge(grounding({}))?.label },
      { tone: "ok", label: "Grounded · 2 sources" },
    );
    assert.equal(groundingBadge(grounding({ cited_ids: ["stat:fleet"] }))?.label, "Grounded · 1 source");
  });

  test("unverified figures warn", () => {
    const badge = groundingBadge(grounding({ unverified_numbers: ["62.5", "14"] }));
    assert.equal(badge?.tone, "warn");
    assert.equal(badge?.label, "2 figures not found in sources");
    assert.equal(groundingBadge(grounding({ unverified_numbers: ["3"] }))?.label, "1 figure not found in sources");
  });

  test("an unknown source id is the worst finding and wins over everything", () => {
    const badge = groundingBadge(grounding({ grounded: false, unknown_ids: ["incident:AN999"], unverified_numbers: ["7"] }));
    assert.equal(badge?.tone, "bad");
    assert.equal(badge?.label, "Cited an unknown source");
  });

  test("no citations is muted, and missing grounding shows no badge", () => {
    const badge = groundingBadge(grounding({ grounded: false, cited_ids: [], unverified_numbers: ["7"] }));
    assert.equal(badge?.tone, "muted");
    assert.equal(badge?.label, "No sources cited");
    assert.equal(groundingBadge(null), null);
  });
});

describe("rate-limit fallback model", () => {
  const models = ["a", "b", "c"].map((id) => ({ id, label: id.toUpperCase() })) as ModelInfo[];
  test("a model that left the catalog still gets a readable label", () => {
    assert.equal(modelLabel(models, "a"), "A");
    assert.equal(modelLabel(models, "gemma-4-31b-it"), "Gemma 4 31B");
    assert.equal(modelLabel(models, "gemma-4-26b-a4b-it"), "Gemma 4 26B A4B");
    assert.equal(modelLabel(models, "gemini-2.5-flash"), "Gemini 2.5 Flash");
  });

  test("offers the next model in catalog order, wrapping around", () => {
    assert.equal(nextModel(models, "a")?.id, "b");
    assert.equal(nextModel(models, "c")?.id, "a");
    assert.equal(nextModel(models, "unknown")?.id, "a");
    assert.equal(nextModel(models.slice(0, 1), "a"), null);
  });
});

/* ── rendering ─────────────────────────────────────────────────────────── */

async function render(element: unknown) {
  const { renderToStaticMarkup } = await import("react-dom/server");
  return renderToStaticMarkup(element as never);
}

describe("Markdown rendering", () => {
  test("chips are numbered by first citation; unknown ids, code and raw HTML stay inert text", async () => {
    const { createElement } = await import("react");
    const { Markdown } = await import("../src/components/chat/Markdown");
    const sources = [
      { id: "incident:AN012", n: 13, origin: "rag", kind: "incident", title: "Gauge out of range", snippet: "s", score: 0.8 },
      { id: "stat:fleet", n: 1, origin: "sql", kind: "stat", title: "Fleet", snippet: "s", score: null },
    ] as const;
    const html = await render(createElement(Markdown, {
      content: "Fell [incident:AN012][stat:fleet] and [asset:HX-999]. Again [stat:fleet].\n\n`[stat:fleet]`\n\n<script>alert(1)</script>",
      sources: [...sources],
    }));
    // Retrieval order (n = 13) is never shown: the first chip reads 1.
    assert.match(html, /aria-label="Source 1: Gauge out of range"[^>]*>1<\/button>/);
    assert.match(html, /aria-label="Source 2: Fleet"[^>]*>2<\/button>/);
    assert.doesNotMatch(html, />13<\/button>/);
    assert.match(html, /\[asset:HX-999\]/);
    assert.match(html, /<code[^>]*>\[stat:fleet\]<\/code>/);
    assert.doesNotMatch(html, /<script>/);
  });

  test("images render as their alt text and never as <img>", async () => {
    const { createElement } = await import("react");
    const { Markdown } = await import("../src/components/chat/Markdown");
    const html = await render(createElement(Markdown, { content: "See ![tracking pixel](https://evil.test/p.png) and ![](https://evil.test/q.png) now." }));
    assert.doesNotMatch(html, /<img/);
    assert.doesNotMatch(html, /evil\.test/);
    assert.match(html, /tracking pixel/);
  });

  test("links keep only http(s), mailto and same-origin relative URLs, with rel=noopener noreferrer", async () => {
    const { createElement } = await import("react");
    const { Markdown, safeHref } = await import("../src/components/chat/Markdown");
    const html = await render(createElement(Markdown, {
      content: "[web](https://example.com/a) [mail](mailto:ops@example.com) [flag](/?flag=14) [js](javascript:alert(1)) [proto](//evil.test/x) [data](data:text/html,hi)",
    }));
    assert.match(html, /<a href="https:\/\/example.com\/a" rel="noopener noreferrer" target="_blank"/);
    assert.match(html, /<a href="mailto:ops@example.com" rel="noopener noreferrer"/);
    assert.match(html, /<a href="\/\?flag=14" rel="noopener noreferrer"/);
    assert.doesNotMatch(html, /javascript:|evil\.test|data:text/);
    for (const text of ["js", "proto", "data"]) assert.match(html, new RegExp(`<span>${text}</span>`));
    assert.equal(safeHref("java\tscript:alert(1)"), null);
    assert.equal(safeHref("\\\\evil.test"), null);
    assert.equal(safeHref("HTTPS://Example.com"), "HTTPS://Example.com");
    assert.equal(safeHref("#section"), "#section");
  });
});

describe("stored error turns (chat.2 error_code)", () => {
  const models = [
    { id: "gemini-3.8-flash", label: "Gemini 3.8 Flash" },
    { id: "gemini-3.5-flash", label: "Gemini 3.5 Flash" },
    { id: "gemma-4-31b-it", label: "Gemma 4 31B" },
  ] as ModelInfo[];

  function failed(over: Partial<Message>): Message {
    return {
      message_id: "m-err", conversation_id: "c", seq: 2, role: "assistant", content: "", thinking: null, thinking_ms: null,
      model: "gemini-3.8-flash", mode: "fast", status: "error", sources: [], citations: [], grounding: null, usage: null,
      latency_ms: null, error: "429 from Gemini", error_code: null, created_at: "2026-10-10T12:00:00Z", ...over,
    };
  }

  async function renderTurn(message: Message, isLast = true) {
    const { createElement } = await import("react");
    const { AssistantTurn } = await import("../src/components/chat/Turns");
    const noop = () => {};
    return render(createElement(AssistantTurn, {
      message, models, isLast, busy: false, liveError: null,
      onRegenerate: noop, onRetry: noop, onTryModel: noop, onOpenSources: noop,
    }));
  }

  test("a stored rate_limited turn keeps the model switch but has no countdown", async () => {
    const html = await renderTurn(failed({ error_code: "rate_limited" }));
    assert.match(html, /Rate limit reached for Gemini 3\.8 Flash/);
    assert.match(html, /Try with Gemini 3\.5 Flash/);
    assert.doesNotMatch(html, /Try again in|Retry in/);
    assert.match(html, />Retry</);
  });

  test("model_unavailable offers the switch too, named for the model that failed", async () => {
    const html = await renderTurn(failed({ error_code: "model_unavailable", model: "gemma-4-31b-it" }));
    assert.match(html, /Gemma 4 31B is unavailable/);
    assert.match(html, /Try with Gemini 3\.8 Flash/);
  });

  test("upstream_error (and a row without error_code) names the model, not Gemini", async () => {
    const html = await renderTurn(failed({ error_code: "upstream_error", model: "gemma-4-31b-it" }));
    assert.match(html, /Gemma 4 31B did not answer/);
    assert.doesNotMatch(html, /Gemini did not answer|Try with/);
    assert.match(await renderTurn(failed({ error_code: null })), /Gemini 3\.8 Flash did not answer/);
  });

  test("an earlier failed turn shows the card without actions", async () => {
    const html = await renderTurn(failed({ error_code: "rate_limited" }), false);
    assert.match(html, /Rate limit reached/);
    assert.doesNotMatch(html, /Try with|>Retry</);
  });
});
