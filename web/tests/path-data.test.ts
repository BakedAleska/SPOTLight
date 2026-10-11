import test from "node:test";
import assert from "node:assert/strict";
import { getPathData } from "../src/lib/path-data";

test("every replay action anchors to a labeled waypoint within the map", () => {
  const data = getPathData();
  assert.equal(data.points.length, 15);
  for (const point of data.points) {
    assert.ok(point.x > 0 && point.x < 1095 && point.y > 0 && point.y < 595);
  }
  for (const mission of data.missions) {
    assert.ok(mission.events.length > 0);
    for (const event of mission.events.filter(e => e.type === "ACTION_STARTED")) {
      assert.ok(data.points.some(p => p.id === event.locationId));
    }
  }
});
test("failure is attached to the failed event, never the preceding action start", () => {
  const { missions } = getPathData();
  const events = missions[0].events;
  const failure = events.find(e => e.type === "ACTION_FAILED");
  assert.ok(failure?.error);
  assert.ok(failure.locationId);
  const start = events.find(e => e.type === "ACTION_STARTED" && e.actionId === failure.actionId);
  assert.ok(start);
  assert.equal(start.error, undefined);
});
