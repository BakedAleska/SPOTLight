/* Mock live patrol (lib/mock-patrol.ts + lib/mock-playback.ts) and the live analytics tally. */
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { mockPatrol } from "../src/lib/mock-patrol";
import { advanceMock, cancelMock, mockCaptures, mockRun, startMock } from "../src/lib/mock-playback";
import { liveTally } from "../src/components/LiveAnalytics";
import { MOCK_ROUTE_ID, type MockPatrol } from "../src/lib/live";

const originalFetch = globalThis.fetch;
const originalBackend = process.env.BACKEND_URL;
afterEach(() => { globalThis.fetch = originalFetch; process.env.BACKEND_URL = originalBackend; });

function backendDown() {
  process.env.BACKEND_URL = "http://backend.test/api/v1";
  globalThis.fetch = (async () => { throw new Error("down"); }) as typeof fetch;
}

test("Mission 1 becomes one stop per kit action, in order, with real photos and no invented priority", async () => {
  backendDown();
  const plan = (await mockPatrol("M001"))!;
  assert.equal(plan.route.route_id, MOCK_ROUTE_ID);
  assert.equal(plan.verdicts, "kit");                       // backend down: the anomaly / failure rule decides
  assert.equal(plan.route.stops.length, 10);
  assert.deepEqual(plan.route.stops.map((s) => s.stop), "ABCDEFGHIJ".split(""));
  assert.deepEqual(plan.captures.map((c) => c.source_action_id), plan.route.stops.map((s) => s.source_action_id));
  assert.ok(plan.captures.every((c) => c.photo?.path?.startsWith("/kit/inspection/")));
  assert.ok(plan.captures.filter((c) => c.verdict === "green").every((c) => c.priority === null));
  assert.equal(await mockPatrol("M999"), null);
});

test("with the backend up, exactly the flagged actions need attention and carry SPOTbot's stored answer", async () => {
  process.env.BACKEND_URL = "http://backend.test/api/v1";
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes("/flags?")
      ? { items: [{ flag_id: 7, action_id: "A0009", priority: "P2", severity: "HIGH", score: 78.9, title: "Visual defect on HX-208 (WP03)", reasons: [{ rule: "anomaly_high", detail: "HIGH visual defect" }] }] }
      : { assessment: { answer: "HX-208 has a HIGH visual defect.", recommended_action: "SCHEDULE_MAINTENANCE" } };
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  const plan = (await mockPatrol("M001"))!;
  assert.equal(plan.verdicts, "flags");
  const red = plan.captures.filter((c) => c.verdict === "red");
  assert.deepEqual(red.map((c) => c.source_action_id), ["A0009"]);
  assert.equal(red[0].qwen_source, "system2");
  assert.equal(red[0].qwen?.answer, "HX-208 has a HIGH visual defect.");
  assert.equal(red[0].laya?.verdict?.p_escalate, 0.789);
  assert.equal(red[0].flag_id, null);                       // a mock card never acknowledges a real flag
});

function plan(n: number): MockPatrol {
  const captures = Array.from({ length: n }, (_, i) => ({
    capture_id: i + 1, run_id: 0, stop_index: i, stop: "ABC"[i], action_id: `A${i}`, source_action_id: `A${i}`,
    location_id: "WP05", asset_id: "HX-204", action_type: "VISUAL_INSPECTION", battery_pct: 80 - i * 30,
    status: i === 1 ? "FAILED" : "SUCCESS", failure_type: i === 1 ? "SENSOR_TIMEOUT" : null, reading: null,
    anomaly: i === 2 ? { anomaly_type: "VISUAL_DEFECT", severity: "HIGH" } : null, phase: "done" as const, event_id: null,
    flag_id: null, verdict: (i === 2 ? "red" : "green") as "red" | "green", priority: i === 2 ? "P2" : null, severity: null,
    category: null, laya: null, rules: null, decision: null, photo: null, vision: null,
    qwen: i === 2 ? { source: "system2", answer: "x" } : null, qwen_source: (i === 2 ? "system2" : null) as "system2" | null,
    doc_id: null, kg: {}, timings: {}, arrived_at: "2026-09-01T18:00:00", triaged_at: null, analysed_at: null, updated_at: "",
  }));
  return { route: { route_id: MOCK_ROUTE_ID, name: "Mission 1 (mock patrol)", description: "", stops: [], planned_red: 1 }, captures, verdicts: "flags" };
}

test("playback reveals each stop arrived -> triaged -> done, then finishes", () => {
  let s = startMock(plan(3), "2026-10-11T12:00:00Z");
  let shown = mockCaptures(s);
  assert.equal(shown.length, 1);
  assert.equal(shown[0].phase, "arrived");
  assert.equal(shown[0].verdict, null);
  s = advanceMock(s);
  assert.equal(mockCaptures(s)[0].phase, "analysing");
  assert.equal(mockCaptures(s)[0].verdict, "green");
  s = advanceMock(s);
  assert.equal(mockCaptures(s)[0].phase, "done");
  for (let i = 0; i < 5; i++) s = advanceMock(s);       // stop B arrives, is triaged, done; stop C arrives, is triaged
  shown = mockCaptures(s);
  assert.equal(shown.length, 3);
  assert.equal(shown[2].qwen_source, "pending");         // a red stop shows SPOTbot "analysing" before its answer
  s = advanceMock(s);
  assert.equal(s.status, "running");                     // C's answer shows for one more beat
  assert.equal(mockCaptures(s)[2].qwen_source, "system2");
  s = advanceMock(s);
  assert.equal(s.status, "done");
  assert.equal(mockCaptures(s).length, 3);
  assert.equal(mockRun(s).status, "done");
  assert.equal(mockRun(s).red, 1);
  assert.equal(mockRun(s).mission_id, "M001");
});

test("cancel stops the playback where it is", () => {
  let s = advanceMock(startMock(plan(3), "t"));
  s = cancelMock(s);
  assert.equal(s.status, "cancelled");
  assert.equal(advanceMock(s), s);
  assert.equal(mockRun(s).status, "cancelled");
});

test("live analytics tallies stops, verdicts, battery and findings", () => {
  const p = plan(3);
  const t = liveTally(p.captures, { ...p.route, stops: Array.from({ length: 4 }) as never });
  assert.deepEqual([t.done, t.total, t.red, t.green, t.battery], [3, 4, 1, 2, 20]);
  assert.deepEqual(t.findings.map((f) => f.stop + ":" + f.tone), ["B:bad", "C:bad"]);
  assert.equal(liveTally([], null).battery, null);
});
