/* Analytics drill-down: the rows behind a chart mark add up to the mark (lib/breakdown.ts vs lib/fleet-analytics.ts). */
import assert from "node:assert/strict";
import { test } from "node:test";
import { breakdown, parseQuery } from "../src/lib/breakdown";
import { breakdownHref } from "../src/lib/breakdown-link";
import { fleetAnalytics } from "../src/lib/fleet-analytics";

const data = fleetAnalytics();

test("hrefs round-trip through parseQuery, unknown views fall back to actions", () => {
  const href = breakdownHref({ view: "actions", type: "VISUAL_INSPECTION", version: "V2" });
  assert.equal(href, "/analytics/breakdown?view=actions&type=VISUAL_INSPECTION&version=V2");
  const params = Object.fromEntries(new URL(href, "http://x").searchParams);
  assert.deepEqual(parseQuery(params), { view: "actions", type: "VISUAL_INSPECTION", version: "V2" });
  assert.equal(parseQuery({ view: "nope", bogus: "1" }).view, "actions");
  assert.equal("bogus" in parseQuery({ bogus: "1" }), false);
});

test("an inspection type lists every action of that type", () => {
  const b = breakdown({ view: "actions", type: "VISUAL_INSPECTION" });
  assert.equal(b.title, "Visual inspections");
  assert.ok(b.actions!.length > 0 && b.actions!.every((a) => a.type === "Visual"));
});

test("battery bands, environments, failure modes and V1/V2 match their chart counts", () => {
  for (const band of data.battery) {
    const b = breakdown({ view: "actions", band: band.key });
    assert.equal(b.actions!.length, band.n, band.band);
    const ok = b.actions!.filter((a) => a.status === "SUCCESS").length;
    assert.equal(Math.round((ok / band.n) * 1000) / 10, band.rate, band.band);
  }
  for (const env of data.environments) assert.equal(breakdown({ view: "actions", env: env.key }).actions!.length, env.n, env.key);
  for (const mode of data.failureModes) {
    const rows = breakdown({ view: "actions", failure: mode.key }).actions!;
    assert.equal(rows.length, mode.count, mode.key);
    assert.ok(rows.every((a) => a.status === "FAILED"));
  }
  for (const row of data.config) {
    assert.equal(breakdown({ view: "actions", type: row.key, version: "V1" }).actions!.length, row.nV1, row.key);
    assert.equal(breakdown({ view: "actions", type: row.key, version: "V2" }).actions!.length, row.nV2, row.key);
  }
});

test("missions, anomalies and readings views match their charts", () => {
  for (const slice of data.truth) assert.equal(breakdown({ view: "missions", truth: slice.key }).missions!.length, slice.count, slice.key);
  for (const v of data.drain) assert.equal(breakdown({ view: "missions", version: v.version }).missions!.length, v.missions, v.version);
  for (const row of data.anomalies) {
    assert.equal(breakdown({ view: "anomalies", anomaly: row.key }).anomalies!.length, row.total, row.key);
    assert.equal(breakdown({ view: "anomalies", anomaly: row.key, severity: "HIGH" }).anomalies!.length, row.HIGH, row.key);
  }
  for (const r of data.readings) {
    assert.equal(breakdown({ view: "readings", measurement: r.key, quality: "QUESTIONABLE" }).readings!.length, r.questionable, r.key);
  }
});

test("a mission bar opens that mission's actions, and the heatmap cell its waypoint x type", () => {
  const m = data.missions[0];
  const b = breakdown({ view: "actions", mission: m.id });
  assert.equal(b.actions!.length, m.succeeded + m.failed);
  assert.equal(b.mission?.id, m.id);
  assert.match(b.title, new RegExp(`^Mission ${m.id}`));
  const row = data.heatmap.rows[0];
  assert.equal(breakdown({ view: "actions", waypoint: row.id }).actions!.length, row.n);
  row.cells.forEach((cell, i) => {
    if (cell) assert.equal(breakdown({ view: "actions", waypoint: row.id, type: data.heatmap.types[i].key }).actions!.length, cell.n);
  });
});
