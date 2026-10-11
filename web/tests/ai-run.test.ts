import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { handleAIRun } from "../src/lib/ai-run";
const request = (body: unknown, origin = "http://localhost:3000") => new Request("http://localhost:3000/api/ai-run", { method: "POST", headers: { origin, host: "localhost:3000", "Content-Type": "application/json" }, body: JSON.stringify(body) });
test("run rejects foreign origins and reports missing backend configuration without processing", async () => {
  const previous = process.env.BACKEND_ADMIN_TOKEN;delete process.env.BACKEND_ADMIN_TOKEN;
  try {
    assert.equal((await handleAIRun(request({ action: "start" }, "https://foreign.example"))).status, 403);
    const missing = await handleAIRun(request({ action: "start" }));assert.equal(missing.status, 503);
    assert.match((await missing.json()).error, /No data was processed/);
    assert.equal((await handleAIRun(request({ action: "explain", run: 1, startedAt: "x", flagId: -1 }))).status, 400);
  } finally { if (previous !== undefined) process.env.BACKEND_ADMIN_TOKEN = previous; }
});
test("explicit run starts kit replay, drives stepped mode and scopes explanations without clearing data", async () => {
  const previousURL = process.env.BACKEND_URL, previousToken = process.env.BACKEND_ADMIN_TOKEN;
  const received: { url: string; method: string; body: Record<string, unknown> }[] = [];
  let state = "running";
  const server = createServer(async (req, res) => {
    assert.equal(req.headers["x-admin-token"], req.url?.includes("/flags?") ? undefined : "test-only-token");
    let text = "";for await (const chunk of req) text += chunk;
    received.push({ url: req.url!, method: req.method!, body: text ? JSON.parse(text) : {} });
    let body: unknown = { state, mode: "stepped", run: 1, started_at: "2026-10-10T12:00:00", sent: 2, total: 2 };
    if (req.url?.includes("/flags?")) body = { items: [{ flag_id: 3 }] };
    if (req.url === "/api/v1/flags/3/explain") body = { source: "system2", answer: "Test fixture result" };
    res.setHeader("Content-Type", "application/json");res.end(JSON.stringify(body));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  process.env.BACKEND_URL = `http://127.0.0.1:${(server.address() as {port:number}).port}/api/v1`;process.env.BACKEND_ADMIN_TOKEN = "test-only-token";
  const identity = { run: 1, startedAt: "2026-10-10T12:00:00", mission: "M001" };
  try {
    const start = await handleAIRun(request({ action: "start", mission: "M001" }));assert.equal(start.status, 200);
    assert.equal(received[0].url, "/api/v1/replay/start");assert.equal(received[0].body.reset, false);assert.deepEqual(received[0].body.missions, ["M001"]);
    assert.equal((await handleAIRun(request({ action: "next", ...identity }))).status, 200);
    assert.ok(received.some(r => r.url === "/api/v1/replay/step" && r.body.run_id === 1));
    assert.equal((await handleAIRun(request({ action: "next", ...identity, run: 2 }))).status, 409);
    state = "finished";
    assert.equal((await handleAIRun(request({ action: "explain", ...identity, flagId: 4 }))).status, 400);
    const result = await handleAIRun(request({ action: "explain", ...identity, flagId: 3 }));assert.equal(result.status, 200);
    assert.equal((await result.json()).source, "system2");
    assert.ok(received.some(r => r.url === "/api/v1/flags/3/explain" && r.method === "POST"));
    assert.ok(received.every(r => !r.url.includes("reset")));
  } finally {
    if (previousURL === undefined) delete process.env.BACKEND_URL;else process.env.BACKEND_URL = previousURL;
    if (previousToken === undefined) delete process.env.BACKEND_ADMIN_TOKEN;else process.env.BACKEND_ADMIN_TOKEN = previousToken;
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
});
