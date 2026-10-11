/* What operators see: model names (no paths, SPOTbot / Confidence model), System 1 wording, and the
   overview queue's search and filters. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { confidenceLabel, displayJson, displayModel, displayText } from "../src/lib/display";
import { filterFlags, filterOptions, isFiltered, NO_FILTER } from "../src/lib/queue-filter";
import type { Flag } from "../src/lib/types";

test("model names never show a path and always use the console's names", () => {
  assert.equal(displayModel("/root/models/q38-27b/Qwen3.8-27B-Q8_0.gguf"), "SPOTbot");
  assert.equal(displayModel("qwen3.5-9b-q4km"), "SPOTbot");
  assert.equal(displayModel("Qwen/Qwen3-Embedding-0.6B"), "Embedding model");
  assert.equal(displayModel("/root/ckpt/laya-lora-r16"), "Confidence model");
  assert.equal(displayModel("/opt/models/other-model.gguf"), "SPOTbot");
  assert.equal(displayModel("template"), "template");
  assert.equal(displayModel(null), "");
});

test("System 1 is shown as a confidence percentage, never by name", () => {
  assert.equal(confidenceLabel(0.913), "Confidence 91%");
  assert.equal(confidenceLabel(1.4), null);
  assert.equal(displayText("Laya: escalate (0.91), rules P2 battery_low"), "Confidence 91% (escalate), rules P2 battery_low");
  assert.equal(displayText("System 1 (Laya) unavailable; rules-only verdict"), "Confidence model unavailable; rules-only verdict");
  assert.equal(displayText("Laya: unavailable"), "Confidence model unavailable");
  assert.equal(displayText("Qwen 3.8 27B did not answer"), "SPOTbot did not answer");
  assert.equal(displayText("battery 15% (low <= 25%)"), "battery 15% (low <= 25%)");
  assert.doesNotMatch(displayText("Laya flags it and Laya's p is high"), /Laya/);
});

test("the raw-fields dump hides model paths and names", () => {
  const json = displayJson({ assessment: { model: "/root/models/q38-27b/Qwen3.8-27B-Q8_0.gguf", note: "loaded /root/models/x/Qwen.gguf" },
    system1: [{ model: "laya-lora", question: "escalate", rule: "laya_verdict" }] });
  assert.doesNotMatch(json, /\/root\/|Qwen|"laya-lora"/);
  assert.match(json, /"model": "SPOTbot"/);
  assert.match(json, /"model": "Confidence model"/);
  assert.match(json, /"rule": "laya_verdict"/);   // rule ids are identifiers, not prose: left alone
});

const flag = (id: string, severity: Flag["severity"], missionId: string, locationName: string, title: string, detail = ""): Flag =>
  ({ id, severity, missionId, locationName, title, assetName: `Asset ${id}`, score: 50, reasons: detail ? [{ rule: "r", detail, weight: 1 }] : [] });
const flags = [
  flag("1", "CRITICAL", "M001", "Pump Room", "Thermal inspection HX-204", "CRITICAL anomaly"),
  flag("2", "HIGH", "M002", "Pipe Gallery", "Gauge reading PANEL-211", "Laya: escalate (0.91)"),
  flag("3", "HIGH", "M001", "Pipe Gallery", "Visual inspection HX-204"),
  flag("4", "MEDIUM", "Unassigned", "Turbine Hall", "Mission M024 incomplete"),
];

test("queue filters combine search, severity, mission and waypoint", () => {
  assert.equal(isFiltered(NO_FILTER), false);
  assert.equal(filterFlags(flags, NO_FILTER).length, 4);
  assert.deepEqual(filterFlags(flags, { ...NO_FILTER, q: "hx-204" }).map((f) => f.id), ["1", "3"]);
  assert.deepEqual(filterFlags(flags, { ...NO_FILTER, q: "hx-204 visual" }).map((f) => f.id), ["3"]);
  assert.deepEqual(filterFlags(flags, { ...NO_FILTER, q: "#2" }).map((f) => f.id), ["2"]);
  assert.deepEqual(filterFlags(flags, { ...NO_FILTER, q: "confidence 91%" }).map((f) => f.id), ["2"]);  // searches what is shown
  assert.deepEqual(filterFlags(flags, { ...NO_FILTER, severity: "HIGH" }).map((f) => f.id), ["2", "3"]);
  assert.deepEqual(filterFlags(flags, { ...NO_FILTER, severity: "HIGH", mission: "M001" }).map((f) => f.id), ["3"]);
  assert.deepEqual(filterFlags(flags, { ...NO_FILTER, location: "Pipe Gallery", q: "panel" }).map((f) => f.id), ["2"]);
  assert.equal(filterFlags(flags, { ...NO_FILTER, q: "nothing-matches" }).length, 0);
});

test("filter options come from the queue, with counts", () => {
  const o = filterOptions(flags);
  assert.deepEqual(o.severities, [{ value: "CRITICAL", n: 1 }, { value: "HIGH", n: 2 }, { value: "MEDIUM", n: 1 }]);
  assert.deepEqual(o.missions, [{ value: "M001", n: 2 }, { value: "M002", n: 1 }]);   // "Unassigned" is not a choice
  assert.deepEqual(o.locations.map((l) => l.value), ["Pipe Gallery", "Pump Room", "Turbine Hall"]);
});
