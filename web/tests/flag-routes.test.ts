import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { GET as detail } from "../src/app/api/flags/[id]/route";
import { GET as explanation } from "../src/app/api/flags/[id]/explanation/route";
import { POST as action } from "../src/app/api/flags/[id]/actions/route";
import { dashboardFeatures } from "../src/lib/features";

const originalFetch = globalThis.fetch;
const keys = ["FEATURE_FLAG_DETAILS", "FEATURE_FLAG_ACTIONS", "FEATURE_FLAG_EXPLANATIONS"] as const;
const originalEnv = keys.map((key) => process.env[key]);
const context = { params: Promise.resolve({ id: "7" }) };
function request(body: unknown = { action: "RESOLVE", operator: "operator" }, origin = "http://localhost:3000") {
  return new Request("http://localhost:3000/api/flags/7/actions", {
    method: "POST", headers: { host: "localhost:3000", origin, "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}
afterEach(() => {
  globalThis.fetch = originalFetch;
  keys.forEach((key, index) => { if (originalEnv[index] === undefined) delete process.env[key]; else process.env[key] = originalEnv[index]; });
});

test("disabled details block every dependent route before contacting the backend", async () => {
  process.env.FEATURE_FLAG_DETAILS = "false";
  globalThis.fetch = async () => { throw new Error("Backend must not be called"); };
  assert.deepEqual(dashboardFeatures(), { flagDetails: false, flagActions: false, flagExplanations: false });
  assert.equal((await detail(request(), context)).status, 404);
  assert.equal((await explanation(request(), context)).status, 404);
  assert.equal((await action(request(), context)).status, 404);
});

test("actions and explanations can be independently disabled and re-enabled", async () => {
  process.env.FEATURE_FLAG_DETAILS = "true";
  process.env.FEATURE_FLAG_ACTIONS = "false";
  process.env.FEATURE_FLAG_EXPLANATIONS = "false";
  globalThis.fetch = async () => Response.json({ flag: { flag_id: 7 } });
  assert.equal((await detail(request(), context)).status, 200);
  assert.equal((await action(request(), context)).status, 404);
  assert.equal((await explanation(request(), context)).status, 404);
  process.env.FEATURE_FLAG_ACTIONS = "true";
  assert.equal((await action(request(), context)).status, 200);
  assert.equal((await explanation(request(), context)).status, 404);
});

test("mutation rejects foreign/malformed origins and invalid input without backend writes", async () => {
  process.env.FEATURE_FLAG_DETAILS = process.env.FEATURE_FLAG_ACTIONS = "true";
  globalThis.fetch = async () => { throw new Error("Backend must not be called"); };
  assert.equal((await action(request(undefined, "https://example.com"), context)).status, 403);
  assert.equal((await action(request(undefined, "invalid"), context)).status, 403);
  assert.equal((await action(request({ action: "DELETE", operator: "operator" }), context)).status, 400);
  assert.equal((await action(request({ action: "RESOLVE", operator: " " }), context)).status, 400);
  assert.equal((await action(request({ action: "RESOLVE", operator: "operator", note: "x".repeat(4001) }), context)).status, 400);
});

test("reopen forwards the existing ESCALATE action, preserving operator and note", async () => {
  process.env.FEATURE_FLAG_DETAILS = process.env.FEATURE_FLAG_ACTIONS = "true";
  let calls = 0;
  globalThis.fetch = async (url, init) => {
    calls++;
    assert.ok(String(url).endsWith("/flags/7/ack"));
    assert.equal(init?.method, "POST");
    assert.deepEqual(JSON.parse(String(init?.body)), { action: "ESCALATE", operator: "Sam", note: "Review again" });
    return Response.json({ flag_status: "OPEN", ack: { ack_id: 1 } });
  };
  const result = await action(request({ action: "ESCALATE", operator: " Sam ", note: "Review again" }), context);
  assert.equal(result.status, 200);
  assert.equal((await result.json()).flag_status, "OPEN");
  assert.equal(calls, 1);
});

test("bad flag IDs never reach the backend", async () => {
  process.env.FEATURE_FLAG_DETAILS = "true";
  globalThis.fetch = async () => { throw new Error("Backend must not be called"); };
  assert.equal((await detail(request(), { params: Promise.resolve({ id: "../ops" }) })).status, 400);
});

test("explanation labels and the configurable explain timeout", async () => {
  const { explanationLabel, explainTimeoutMs } = await import("../src/lib/flag-detail");
  assert.equal(explanationLabel({ source: "system2_template", model: "template" }), "Rule-based explanation");
  assert.equal(explanationLabel({ source: "template" }), "Rule-based explanation");
  assert.equal(explanationLabel({ source: "system1" }), "Triage summary (not escalated)");
  assert.equal(explanationLabel({ source: "system2", model: "stub" }), "Evidence summary (stub)");
  assert.equal(explanationLabel({ source: "system2", model: "qwen3.5-9b-q4km" }), "Model explanation");
  assert.equal(explainTimeoutMs(undefined), 50_000);
  assert.equal(explainTimeoutMs("95000"), 95_000);
  assert.equal(explainTimeoutMs("abc"), 50_000);
  assert.equal(explainTimeoutMs("999999"), 50_000);
});
