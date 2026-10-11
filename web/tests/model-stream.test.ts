import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { predictionSamples, assessmentSource, type ModelStream } from "../src/lib/model-stream";
import { loadModelStream } from "../src/lib/model-stream-loader";
const empty: ModelStream = { connected: true, updatedAt: "", warnings: [], events: [], details: [], forecasts: [], calls: [], stats: [], routing: {} };
test("risk plots use actual model probabilities and reject baselines, invalid and missing numbers", () => {
  const data = { ...empty, details: [{ system1: [
    { prediction_id: 1, question: "will_fail", probs: { A: 0 }, action_id: "A1", created_at: "2026-10-10T12:00:00", model: "Laya" },
    { prediction_id: 2, question: "escalate", probs: { A: 0.85 }, action_id: "A2", created_at: "2026-10-10T12:01:00", model: "Laya" },
    { prediction_id: 3, question: "anomaly_expected", probs: { A: 4 } },
  ] }], forecasts: [{ action_id: "A3", p_fail: 0.9, source: "baseline" }, { action_id: "A4", p_fail: null, source: "system1_stored" }] };
  assert.deepEqual(predictionSamples(data).map(s => s.value), [0, 85]);
  assert.equal(assessmentSource({ state: "TEMPLATE", confidence: 1 }), "Fallback / evidence summary");
});
test("polls existing read endpoints, refreshes changed model results, and never requests inference", async () => {
  const previousBase = process.env.BACKEND_URL, previousToken = process.env.BACKEND_ADMIN_TOKEN;
  const requests: string[] = []; let confidence = 0.2;
  const server = createServer((request, response) => {
    requests.push(request.url!);assert.equal(request.method, "GET");
    let body: unknown = {};
    if (request.url?.startsWith("/api/v1/events?")) body = { items: [{ event_id: "E1", mission_id: "M001" }] };
    else if (request.url?.startsWith("/api/v1/flags?")) body = { items: [{ flag_id: 1, mission_id: "M001" }] };
    else if (request.url === "/api/v1/flags/1") body = { flag: { flag_id: 1 }, system1: [{ prediction_id: 1, question: "escalate", probs: { A: confidence }, action_id: "A1" }], assessment: { state: "DONE", model: "Qwen", answer: "Inspect valve" } };
    else if (request.url?.includes("/ops/model-calls")) {
      assert.equal(request.headers["x-admin-token"], "test-only-token");
      body = { recent: [{ model: "Qwen", latency_ms: 1200 }], stats: [], routing: {} };
    }
    response.setHeader("Content-Type", "application/json");response.end(JSON.stringify(body));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address() as { port: number };
  process.env.BACKEND_URL = `http://127.0.0.1:${address.port}/api/v1`;
  process.env.BACKEND_ADMIN_TOKEN = "test-only-token";
  try {
    const first = await loadModelStream();assert.equal(first.connected, true);
    assert.equal(predictionSamples(first)[0].value, 20);
    confidence = 0.8;
    const second = await loadModelStream();assert.equal(predictionSamples(second)[0].value, 80);
    assert.equal(second.calls[0].latency_ms, 1200);
    assert.ok(requests.some(url => url.includes("live=false")));
    assert.ok(requests.every(url => !url.includes("explain") && !url.includes("predict")));
    assert.ok(!JSON.stringify(second).includes("test-only-token"));
  } finally {
    if (previousBase === undefined) delete process.env.BACKEND_URL; else process.env.BACKEND_URL = previousBase;
    if (previousToken === undefined) delete process.env.BACKEND_ADMIN_TOKEN; else process.env.BACKEND_ADMIN_TOKEN = previousToken;
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
});
