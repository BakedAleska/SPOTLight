import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { loadDashboard } from "../src/lib/backend";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("dashboard uses backend flag IDs, total count and matched battery metrics", async () => {
  globalThis.fetch = async (input) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith("/insights/overview")) return Response.json({ generated_at: "2026-10-10T12:00:00Z", insights: [
      { key: "green_but_broken", detail: { missions: 27, reported_completed: 26, completed_with_failures: 21 } },
      { key: "battery_cliff", value: 10, detail: { above_25_success_pct: 90 } },
      { key: "v1_vs_v2_matched_battery", value: 8, detail: { headline_gain_pts: 20 } },
    ] });
    if (url.pathname.endsWith("/flags")) {
      assert.equal(url.searchParams.get("status"), "OPEN");
      return Response.json({ items: [{ flag_id: 7, mission_id: "M1", asset_id: "A1", location_id: "WP1", severity: "HIGH", score: 73, title: "Flag", reasons: [] }], page: { total: 501 } });
    }
    if (url.pathname.endsWith("/assets")) return Response.json([{ asset_id: "A1", asset_name: "Pump" }]);
    if (url.pathname.endsWith("/locations")) return Response.json([{ location_id: "WP1", location_name: "Hall" }]);
    if (url.pathname.endsWith("/insights/reliability")) return Response.json({ rows: [{ group: "<=25", n: 10, success_rate_pct: 10 }] });
    throw new Error(`Unexpected request ${url}`);
  };
  const result = await loadDashboard();
  assert.equal(result.flags[0].id, "7");
  assert.equal(result.flags[0].score, 73);
  assert.equal(result.flags[0].assetName, "Pump");
  assert.equal(result.flagCount, 501);
  assert.equal(result.stats[3].value, "+8");
  assert.equal(result.aboveCliffRate, 90);
  assert.deepEqual(result.hiddenFailures, { count: 21, ofCompleted: 26 });
});

test("backend failure propagates instead of substituting CSV flags", async () => {
  globalThis.fetch = async () => new Response(null, { status: 503 });
  await assert.rejects(loadDashboard(), /Backend returned 503/);
});
