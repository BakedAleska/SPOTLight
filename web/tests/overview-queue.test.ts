/* The Overview: an opened flag is lifted to the head of the queue and drops
   back into place when closed (lib/overview.ts), and the analytics panel's
   numbers add up to the Analytics page's (lib/overview-analytics.ts). */
import assert from "node:assert/strict";
import { test } from "node:test";
import { activeChart, bandOf, bandsSpanned, failuresFor, openFirst, parseChart, typesFor } from "../src/lib/overview";
import { overviewAnalytics } from "../src/lib/overview-analytics";
import { fleetAnalytics } from "../src/lib/fleet-analytics";

const queue = ["57", "20", "14", "44", "21"].map((id) => ({ id }));
const ids = (rows: { id: string }[]) => rows.map((row) => row.id);

test("the open flag moves to the head; everything else keeps queue order", () => {
  assert.deepEqual(ids(openFirst(queue, "21")), ["21", "57", "20", "14", "44"]);
  assert.deepEqual(ids(openFirst(queue, "14")), ["14", "57", "20", "44", "21"]);
});

test("closing, opening the head, or an id the filters hid gives the queue back unchanged", () => {
  assert.equal(openFirst(queue, null), queue);
  assert.deepEqual(ids(openFirst(queue, "57")), ids(queue));
  assert.deepEqual(ids(openFirst(queue, "999")), ids(queue));
  assert.deepEqual(openFirst([], "21"), []);
  // The input is never reordered in place: closing really is "back where it was".
  openFirst(queue, "21");
  assert.deepEqual(ids(queue), ["57", "20", "14", "44", "21"]);
});

test("a stored chart choice is trusted only if it is one of today's charts", () => {
  assert.equal(parseChart("battery"), "battery");
  assert.equal(parseChart("heatmap"), null);
  assert.equal(parseChart(null), null);
});

test("opening a flag shows Missions until the operator picks a chart with a flag open", () => {
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
