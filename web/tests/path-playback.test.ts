import test from "node:test";
import assert from "node:assert/strict";
import { buildPlayback, playbackAt } from "../src/lib/path-playback";
import { getPathData } from "../src/lib/path-data";

test("travel interpolates continuously while work holds position", () => {
  const { points, missions } = getPathData();
  const segments = buildPlayback(missions[0], points);
  const travel = segments.find(s => s.moving)!;
  assert.ok(travel);
  const mid = playbackAt(segments, (travel.start + travel.end) / 2)!;
  assert.equal(mid.x, (travel.from!.x + travel.to!.x) / 2);
  assert.equal(mid.y, (travel.from!.y + travel.to!.y) / 2);
  const work = segments.find(s => s.activity === "Capturing images")!;
  const working = playbackAt(segments, work.start + 100)!;
  assert.equal(working.x, work.to!.x);
  assert.equal(working.y, work.to!.y);
});
test("same-timestamp failures appear after their action activity, not before", () => {
  const { points, missions } = getPathData();
  const mission = missions[0];
  const failureIndex = mission.events.findIndex(e => e.type === "ACTION_FAILED");
  const segments = buildPlayback(mission, points);
  const failure = segments.find(s => s.eventIndex === failureIndex)!;
  const start = segments.find(s => s.eventIndex === failureIndex - 1)!;
  assert.ok(failure.start >= start.end);
  assert.equal(playbackAt(segments, start.start + 1)!.eventIndex, failureIndex - 1);
});
