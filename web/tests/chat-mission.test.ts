/* Mission chats (contract §9, chat.3): the proxy routes' mission_id
   passthrough and validation, the URL and picker helpers, the store creating
   a conversation with its mission, and the server-rendered chip, badge,
   context card and mission cards (no nested links). No real backend. */

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { GET as missions } from "../src/app/api/chat/missions/route";
import { GET as suggestions } from "../src/app/api/chat/suggestions/route";
import { GET as list, POST as create } from "../src/app/api/chat/conversations/route";
import {
  assistantHref, filterMissions, hiddenFailureText, missionLabel, missionTone, normalizeMissionId, replayHref, stepIndex, withMissionParam,
} from "../src/lib/chat/mission";
import { describeError } from "../src/lib/chat/api";
import { getChatState, retryDraft, startConversation } from "../src/lib/chat/store";
import type { MissionSummary } from "../src/lib/chat/types";
import { MissionBadge, MissionChip, MissionContextCard, MissionPicker, MissionStatusBadge } from "../src/components/chat/MissionFocus";
import { AskAiLink } from "../src/components/AskAiLink";
import { MissionList } from "../src/components/MissionList";
import { FlaggedMissions } from "../src/components/FlaggedMissions";

const originalFetch = globalThis.fetch;
const originalFlag = process.env.FEATURE_AI_ASSISTANT;
const originalBackend = process.env.BACKEND_URL;

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalFlag === undefined) delete process.env.FEATURE_AI_ASSISTANT; else process.env.FEATURE_AI_ASSISTANT = originalFlag;
  if (originalBackend === undefined) delete process.env.BACKEND_URL; else process.env.BACKEND_URL = originalBackend;
});

function req(path: string, method = "GET", body?: unknown, origin = "http://localhost:3000") {
  return new Request(`http://localhost:3000${path}`, {
    method,
    headers: { host: "localhost:3000", origin, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

const noBackend = () => { globalThis.fetch = async () => { throw new Error("Backend must not be called"); }; };

function mission(overrides: Partial<MissionSummary> = {}): MissionSummary {
  return {
    mission_id: "M006", mission_name: "Autowalk Inspection Route 06", scenario: "BASELINE", configuration_version: "V1",
    start_time: "2026-09-02T10:00:00", duration_min: 31, start_battery_pct: 73, end_battery_pct: 15, mission_status: "COMPLETED",
    actions: 10, failed: 3, anomalies: 3, serious_anomalies: 1, open_flags: 2, hidden_failure: true, chats: 1, ...overrides,
  };
}

/* ── proxy routes ─────────────────────────────────────────────────────── */

test("/api/chat/missions forwards to BACKEND_URL/chat/missions with a trimmed, encoded q", async () => {
  process.env.BACKEND_URL = "http://backend.test/api/v1";
  const urls: string[] = [];
  globalThis.fetch = async (url) => { urls.push(String(url)); return Response.json({ items: [mission()] }); };
  const response = await missions(req("/api/chat/missions"));
  assert.equal(response.status, 200);
  assert.equal((await response.json()).items[0].mission_id, "M006");
  await missions(req(`/api/chat/missions?q=${encodeURIComponent("  route 06&x=1 ")}`));
  await missions(req(`/api/chat/missions?q=${"z".repeat(300)}`));
  assert.deepEqual(urls, [
    "http://backend.test/api/v1/chat/missions",
    "http://backend.test/api/v1/chat/missions?q=route+06%26x%3D1",
    `http://backend.test/api/v1/chat/missions?q=${"z".repeat(100)}`,
  ]);
});

test("/api/chat/missions: disabled is 404, a dead backend is 502, backend errors pass through", async () => {
  process.env.FEATURE_AI_ASSISTANT = "false";
  noBackend();
  const off = await missions(req("/api/chat/missions"));
  assert.equal(off.status, 404);
  assert.equal((await off.json()).error, "disabled");
  delete process.env.FEATURE_AI_ASSISTANT;
  globalThis.fetch = async () => { throw new TypeError("fetch failed"); };
  assert.equal((await missions(req("/api/chat/missions"))).status, 502);
  globalThis.fetch = async () => Response.json({ error: "retrieval_failed", detail: "db down", request_id: "r" }, { status: 503 });
  const down = await missions(req("/api/chat/missions"));
  assert.equal(down.status, 503);
  assert.equal((await down.json()).error, "retrieval_failed");
});

test("suggestions and the conversation list pass mission_id through, upper-cased and re-encoded", async () => {
  process.env.BACKEND_URL = "http://backend.test/api/v1";
  const urls: string[] = [];
  globalThis.fetch = async (url) => { urls.push(String(url)); return Response.json({ suggestions: [], items: [], total: 0 }); };
  await suggestions(req("/api/chat/suggestions?mission_id=m006"));
  await suggestions(req("/api/chat/suggestions?mission_id="));
  await list(req("/api/chat/conversations?mission_id=%20M007%20&limit=50&q=battery"));
  assert.deepEqual(urls, [
    "http://backend.test/api/v1/chat/suggestions?mission_id=M006",
    "http://backend.test/api/v1/chat/suggestions",
    "http://backend.test/api/v1/chat/conversations?q=battery&limit=50&mission_id=M007",
  ]);
});

test("a malformed mission_id is a 422 unknown_mission without reaching the backend", async () => {
  noBackend();
  for (const bad of ["../ops", "M0 06", "a".repeat(40), "M006?x=1"]) {
    const s = await suggestions(req(`/api/chat/suggestions?mission_id=${encodeURIComponent(bad)}`));
    assert.equal(s.status, 422, bad);
    assert.equal((await s.json()).error, "unknown_mission");
    assert.equal((await list(req(`/api/chat/conversations?mission_id=${encodeURIComponent(bad)}`))).status, 422, bad);
  }
  for (const body of [{ mission_id: "../ops" }, { mission_id: 7 }, { mission_id: ["M006"] }, { mission_id: "" }]) {
    const response = await create(req("/api/chat/conversations", "POST", { model: "m", mode: "fast", ...body }));
    assert.equal(response.status, 422, JSON.stringify(body));
    assert.equal((await response.json()).error, "unknown_mission");
  }
});

test("create forwards a mission chat's body verbatim and mirrors the backend's unknown_mission", async () => {
  process.env.BACKEND_URL = "http://backend.test/api/v1";
  const bodies: string[] = [];
  globalThis.fetch = async (_url, init) => {
    bodies.push(String(init?.body));
    const parsed = JSON.parse(String(init?.body));
    return parsed.mission_id === "M999"
      ? Response.json({ error: "unknown_mission", detail: "No mission M999.", request_id: "r" }, { status: 422 })
      : Response.json({ conversation_id: "c", mission_id: "M006" }, { status: 201 });
  };
  const ok = await create(req("/api/chat/conversations", "POST", { model: "m", mode: "fast", mission_id: "m006" }));
  assert.equal(ok.status, 201);
  assert.equal(bodies[0], JSON.stringify({ model: "m", mode: "fast", mission_id: "m006" }));
  const plain = await create(req("/api/chat/conversations", "POST", { model: "m", mode: "fast", mission_id: null }));
  assert.equal(plain.status, 201);
  const unknown = await create(req("/api/chat/conversations", "POST", { mission_id: "M999" }));
  assert.equal(unknown.status, 422);
  assert.equal((await unknown.json()).error, "unknown_mission");
  // Writes still need this app's origin.
  noBackend();
  assert.equal((await create(req("/api/chat/conversations", "POST", { mission_id: "M006" }, "https://evil.example"))).status, 403);
});

/* ── URL and picker helpers ───────────────────────────────────────────── */

test("mission ids normalise to upper case; junk is rejected", () => {
  assert.equal(normalizeMissionId(" m006 "), "M006");
  assert.equal(normalizeMissionId("M-7_b"), "M-7_B");
  for (const bad of [null, undefined, "", "   ", "../x", "M 6", "-M6", "a".repeat(33), "M6<script>"]) assert.equal(normalizeMissionId(bad), null, String(bad));
});

test("hrefs: Ask SPOTbot, replay, and the ?mission= toggle that keeps other params", () => {
  assert.equal(assistantHref("M006"), "/assistant?mission=M006");
  assert.equal(assistantHref("M 6&x"), "/assistant?mission=M%206%26x");
  assert.equal(replayHref("M006"), "/missions/replay?mission=M006");
  assert.equal(withMissionParam("", "M006"), "/assistant?mission=M006");
  assert.equal(withMissionParam("mission=M001&debug=1", "M006"), "/assistant?mission=M006&debug=1");
  assert.equal(withMissionParam("mission=M001&debug=1", null), "/assistant?debug=1");
  assert.equal(withMissionParam("?mission=M001", null), "/assistant");
});

test("picker search matches id, name, status and 'hidden', every word", () => {
  const items = [
    mission(),
    mission({ mission_id: "M007", mission_name: "Autowalk Inspection Route 07", hidden_failure: false, failed: 0, scenario: "LOW_BATTERY" }),
    mission({ mission_id: "M021", mission_name: "Night Patrol", mission_status: "FAILED", hidden_failure: false }),
  ];
  const ids = (q: string) => filterMissions(items, q).map((m) => m.mission_id);
  assert.deepEqual(ids(""), ["M006", "M007", "M021"]);
  assert.deepEqual(ids("m00"), ["M006", "M007"]);
  assert.deepEqual(ids("route 07"), ["M007"]);
  assert.deepEqual(ids("HIDDEN"), ["M006"]);
  assert.deepEqual(ids("failed"), ["M021"]);
  assert.deepEqual(ids("low_battery"), ["M007"]);
  assert.deepEqual(ids("nothing-matches"), []);
});

test("picker keys wrap and clamp", () => {
  assert.equal(stepIndex(-1, "ArrowDown", 3), 0);
  assert.equal(stepIndex(-1, "ArrowUp", 3), 2);
  assert.equal(stepIndex(2, "ArrowDown", 3), 0);
  assert.equal(stepIndex(0, "ArrowUp", 3), 2);
  assert.equal(stepIndex(1, "Home", 3), 0);
  assert.equal(stepIndex(1, "End", 3), 2);
  assert.equal(stepIndex(1, "x", 3), 1);
  assert.equal(stepIndex(0, "ArrowDown", 0), -1);
});

test("hidden failure text, tone and labels", () => {
  assert.equal(hiddenFailureText(mission()), "reported COMPLETED but 3 actions failed");
  assert.equal(hiddenFailureText(mission({ failed: 1 })), "reported COMPLETED but 1 action failed");
  assert.equal(hiddenFailureText(mission({ hidden_failure: false })), null);
  assert.equal(missionTone(mission()), "bad");
  assert.equal(missionTone(mission({ hidden_failure: false, mission_status: "FAILED" })), "bad");
  assert.equal(missionTone(mission({ hidden_failure: false, failed: 0, anomalies: 2 })), "warn");
  assert.equal(missionTone(mission({ hidden_failure: false, failed: 0, anomalies: 0 })), "ok");
  assert.equal(missionLabel(mission()), "M006 · Autowalk Inspection Route 06");
  assert.equal(missionLabel({ mission_id: "M006", mission_name: "" }), "M006");
  assert.match(describeError("unknown_mission").title, /mission/i);
});

/* ── store: the first send creates the conversation with its mission ─── */

test("startConversation creates a mission chat with mission_id, and a draft retry keeps it", async () => {
  const posts: { url: string; body: Record<string, unknown> }[] = [];
  let failCreate = true;
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    posts.push({ url: String(url), body: JSON.parse(String(init?.body)) });
    if (String(url) === "/api/chat/conversations") {
      if (failCreate) { failCreate = false; return Response.json({ error: "upstream_error", detail: "x" }, { status: 502 }); }
      return Response.json({
        conversation_id: "00000000-0000-4000-8000-000000000001", title: "New chat", title_source: "default", model: "m", mode: "fast",
        pinned: false, created_at: "", updated_at: "", message_count: 0, mission_id: "M006",
        mission: { mission_id: "M006", mission_name: "Autowalk Inspection Route 06", mission_status: "COMPLETED" },
      }, { status: 201 });
    }
    return Response.json({ error: "conversation_busy", detail: "stop here" }, { status: 409 });
  }) as typeof fetch;

  const created: string[] = [];
  await startConversation("What failed?", "m", "fast", (id) => created.push(id), "M006");
  assert.deepEqual(posts[0], { url: "/api/chat/conversations", body: { model: "m", mode: "fast", mission_id: "M006" } });
  assert.equal(getChatState().threads.draft?.missionId, "M006");
  assert.equal(created.length, 0);

  retryDraft((id) => created.push(id));
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.deepEqual(posts[1].body, { model: "m", mode: "fast", mission_id: "M006" });
  assert.deepEqual(created, ["00000000-0000-4000-8000-000000000001"]);
  const thread = getChatState().threads[created[0]];
  assert.equal(thread.missionId, "M006");
  assert.equal(thread.conversation?.mission?.mission_name, "Autowalk Inspection Route 06");
});

test("a normal new chat sends no mission_id", async () => {
  const bodies: unknown[] = [];
  globalThis.fetch = (async (_url: string | URL | Request, init?: RequestInit) => {
    bodies.push(JSON.parse(String(init?.body)));
    return Response.json({ error: "upstream_error", detail: "x" }, { status: 502 });
  }) as typeof fetch;
  await startConversation("hi", "m", "fast", () => {});
  assert.deepEqual(bodies[0], { model: "m", mode: "fast" });
});

/* ── rendering ────────────────────────────────────────────────────────── */

const html = (element: Parameters<typeof renderToStaticMarkup>[0]) => renderToStaticMarkup(element);

test("the header chip links to the mission's replay with its id and name", () => {
  const out = html(createElement(MissionChip, { mission: { mission_id: "M006", mission_name: "Autowalk Inspection Route 06", mission_status: "COMPLETED" } }));
  assert.match(out, /href="\/missions\/replay\?mission=M006"/);
  assert.match(out, />M006</);
  assert.match(out, /· Autowalk Inspection Route 06/);
  assert.match(out, /aria-label="Mission M006 · Autowalk Inspection Route 06. Open replay"/);
});

test("the history badge names the mission for screen readers too", () => {
  const out = html(createElement(MissionBadge, { missionId: "M006" }));
  assert.match(out, /<span class="sr-only">Mission <\/span>M006/);
});

test("status is colour + icon + word", () => {
  const done = html(createElement(MissionStatusBadge, { status: "COMPLETED" }));
  assert.match(done, /text-ok/);
  assert.match(done, /<svg/);
  assert.match(done, /Completed/);
  const failed = html(createElement(MissionStatusBadge, { status: "FAILED" }));
  assert.match(failed, /text-bad/);
  assert.match(failed, /Failed/);
});

test("the context card shows the counts and the hidden-failure warning only when it applies", () => {
  const hidden = html(createElement(MissionContextCard, { mission: mission(), onClear: () => {} }));
  for (const text of ["M006", "Autowalk Inspection Route 06", "Completed", "Failed actions", "Anomalies", "Open flags", "Hidden failure:", "reported COMPLETED but 3 actions failed", "1 earlier chat"]) {
    assert.ok(hidden.includes(text), text);
  }
  assert.match(hidden, /aria-label="Stop focusing on M006"/);
  assert.match(hidden, /href="\/missions\/replay\?mission=M006"/);
  const clean = html(createElement(MissionContextCard, { mission: mission({ hidden_failure: false, failed: 0, chats: 0 }) }));
  assert.ok(!clean.includes("Hidden failure"));
  assert.ok(!clean.includes("Stop focusing"));
});

test("the picker trigger: 'Focus on a mission' when empty, a chip with × when set", () => {
  const props = { missions: [mission()], loading: false, error: false, onSelect: () => {}, onClear: () => {} };
  const empty = html(createElement(MissionPicker, { ...props, value: null }));
  assert.match(empty, /Focus on a mission/);
  assert.match(empty, /aria-haspopup="listbox"/);
  assert.match(empty, /aria-expanded="false"/);
  const set = html(createElement(MissionPicker, { ...props, value: "M006" }));
  assert.match(set, /aria-label="Focused on mission M006 · Autowalk Inspection Route 06. Change mission"/);
  assert.match(set, /aria-label="Clear mission focus \(M006\)"/);
});

/** Every <a> is closed before the next one opens: no link inside a link. */
function assertNoNestedLinks(markup: string) {
  let depth = 0;
  for (const tag of markup.match(/<a[\s>]|<\/a>/g) ?? []) {
    depth += tag === "</a>" ? -1 : 1;
    assert.ok(depth <= 1, "nested <a>");
  }
  assert.equal(depth, 0);
}

test("Ask SPOTbot links to /assistant?mission= with an accessible name", () => {
  const out = html(createElement(AskAiLink, { missionId: "M006" }));
  assert.match(out, /href="\/assistant\?mission=M006"/);
  assert.match(out, /aria-label="Ask SPOTbot about M006"/);
  const labelled = html(createElement(AskAiLink, { missionId: "M006", label: "Ask SPOTbot about M006" }));
  assert.ok(!labelled.includes("aria-label"));
});

test("All missions and Flagged cards carry replay and Ask SPOTbot as sibling links", () => {
  for (const [name, markup] of [["MissionList", html(createElement(MissionList))], ["FlaggedMissions", html(createElement(FlaggedMissions))]] as const) {
    assertNoNestedLinks(markup);
    const ask = markup.match(/href="\/assistant\?mission=M\d+"/g) ?? [];
    const replay = markup.match(/href="\/missions\/replay\?mission=M\d+"/g) ?? [];
    assert.ok(ask.length > 0, name);
    assert.equal(ask.length, replay.length, name);
    assert.match(markup, /href="\/assistant\?mission=M006"/, name);
  }
});
