import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { POST as start } from "../src/app/api/live/runs/route";
import { POST as cancel } from "../src/app/api/live/runs/[id]/cancel/route";

const originalFetch = globalThis.fetch;
const originalToken = process.env.BACKEND_ADMIN_TOKEN;
function request(body: unknown = { route_id: "R2" }, origin = "http://localhost:3000") {
  return new Request("http://localhost:3000/api/live/runs", {
    method: "POST", headers: { host: "localhost:3000", origin, "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}
afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalToken === undefined) delete process.env.BACKEND_ADMIN_TOKEN; else process.env.BACKEND_ADMIN_TOKEN = originalToken;
});

test("starting a run needs the console's origin, a server token and a known route", async () => {
  globalThis.fetch = async () => { throw new Error("Backend must not be called"); };
  delete process.env.BACKEND_ADMIN_TOKEN;
  assert.equal((await start(request({ route_id: "R2" }, "https://evil.example"))).status, 403);
  assert.equal((await start(request())).status, 503);
  process.env.BACKEND_ADMIN_TOKEN = "t-123";
  assert.equal((await start(request({ route_id: "R9" }))).status, 400);
  assert.equal((await start(request({ route_id: "R1; drop" }))).status, 400);
});

test("the admin token is added server-side and a busy backend reads as 409", async () => {
  process.env.BACKEND_ADMIN_TOKEN = "t-123";
  const seen: { url: string; token: string | null; body: string }[] = [];
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), token: new Headers(init?.headers).get("X-Admin-Token"), body: String(init?.body) });
    return Response.json({ run: { run_id: 4, route_id: "R2", status: "pending" } });
  };
  const ok = await start(request({ route_id: "R2", operator: "jo" }));
  assert.equal(ok.status, 200);
  assert.match(seen[0].url, /\/live\/runs$/);
  assert.equal(seen[0].token, "t-123");
  assert.deepEqual(JSON.parse(seen[0].body), { route_id: "R2", requested_by: "jo" });
  globalThis.fetch = async () => Response.json({ error: "live_conflict" }, { status: 409 });
  assert.equal((await start(request())).status, 409);
});

test("cancel validates the run id and keeps the token server-side", async () => {
  process.env.BACKEND_ADMIN_TOKEN = "t-123";
  let token: string | null = null;
  globalThis.fetch = async (_url, init) => { token = new Headers(init?.headers).get("X-Admin-Token"); return Response.json({ run: {} }); };
  const req = (id: string) => [new Request(`http://localhost:3000/api/live/runs/${id}/cancel`, {
    method: "POST", headers: { host: "localhost:3000", origin: "http://localhost:3000" } }), { params: Promise.resolve({ id }) }] as const;
  assert.equal((await cancel(...req("abc"))).status, 400);
  assert.equal((await cancel(...req("4"))).status, 200);
  assert.equal(token, "t-123");
});
