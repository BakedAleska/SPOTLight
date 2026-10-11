/* The Overview: the analytics panel's chart choice (lib/overview.ts) and its
   numbers adding up to the Analytics page's (lib/overview-analytics.ts). */
import assert from "node:assert/strict";
import { test } from "node:test";
import { activeChart, bandOf, bandsSpanned, failuresFor, parseChart, typesFor } from "../src/lib/overview";
import { overviewAnalytics } from "../src/lib/overview-analytics";
import { fleetAnalytics } from "../src/lib/fleet-analytics";

test("a stored chart choice is trusted only if it is one of today's charts", () => {
  assert.equal(parseChart("battery"), "battery");
  assert.equal(parseChart("heatmap"), null);
  assert.equal(parseChart(null), null);
});

test("opening a flag shows Missions until the operator picks a chart for that flag", () => {
  assert.equal(activeChart("battery", false, false), "battery");
  assert.equal(activeChart("battery", true, false), "missions");
  assert.equal(activeChart("failures", true, true), "failures");
});

test("battery bands a mission passed through, and the one it ended in", () => {
  assert.deepEqual(bandsSpanned([26, 68]), ["26-40", "41-60", "61-80"]);
  assert.deepEqual(bandsSpanned([15, 82]), ["0-25", "26-40", "41-60", "61-80", "81-100"]);
  assert.deepEqual(bandsSpanned([40, 41]), ["26-40", "41-60"]);
  assert.equal(bandOf(25).key, "0-25");
  assert.equal(bandOf(26).key, "26-40");
  assert.equal(bandOf(100).key, "81-100");
});

const data = overviewAnalytics();
const fleet = fleetAnalytics();

test("per-type and per-mission splits add up to the fleet numbers", () => {
  const actions = fleet.missions.reduce((sum, m) => sum + m.succeeded + m.failed, 0);
  assert.equal(data.types.reduce((sum, t) => sum + t.n, 0), actions);
  for (const m of fleet.missions) {
    const own = data.byMission[m.id];
    assert.ok(own, m.id);
    assert.equal(Object.values(own.failures).reduce((a, b) => a + b, 0), m.failed, m.id);
    assert.equal(Object.values(own.types).reduce((a, t) => a + t.n, 0), m.succeeded + m.failed, m.id);
  }
  for (const mode of data.failureModes) {
    const inMissions = Object.values(data.byMission).reduce((sum, m) => sum + (m.failures[mode.key] ?? 0), 0);
    assert.equal(inMissions, mode.count, mode.key);
  }
  // Best first, as on the Analytics page.
  assert.ok(data.types.every((t, i) => i === 0 || data.types[i - 1].rate >= t.rate));
});

test("a flag's mission is drawn against the fleet; no mission means fleet only", () => {
  const id = fleet.missions[0].id;
  const types = typesFor(data, id);
  assert.ok(types.some((t) => t.mission !== null));
  assert.ok(types.every((t) => t.mission === null || (t.mission >= 0 && t.mission <= 100)));
  assert.ok(typesFor(data, null).every((t) => t.mission === null));
  assert.equal(failuresFor(data, id).reduce((sum, f) => sum + (f.mission ?? 0), 0), fleet.missions[0].failed);
  assert.ok(failuresFor(data, "NO-SUCH-MISSION").every((f) => f.mission === null));
});
