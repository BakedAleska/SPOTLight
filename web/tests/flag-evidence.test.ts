/* System 2 evidence for reading (src/lib/evidence.ts): stored `reasoning` back into claims, key=value claims into
   labelled fields, cited rows by name. Inputs are real M001/flag-22 answers from the 2026-10-11 replay. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { citationLabel, evidenceItems, humanizeCode, parseClaim, parseReasoning } from "../src/lib/evidence";

const FLAG_22 = "action_type=ACOUSTIC_INSPECTION; asset_id=PANEL-212; location_id=WP09; status=FAILED; "
  + "failure_type=SIGNAL_NOISE; battery_pct=30.0 [action:A0089] measurement_type=SOUND_DB; value=81.5; unit=dB; "
  + "quality=QUESTIONABLE; trust=untrusted [reading:R0020] category=data_quality; priority=P2; escalate=yes; "
  + "suggested_action=REINSPECT_NOW [F1]";

test("stored reasoning splits back into claims at citation brackets", () => {
  const items = parseReasoning(FLAG_22);
  assert.deepEqual(items.map((i) => i.ids), [["action:A0089"], ["reading:R0020"], ["F1"]]);
  assert.ok(items[0].claim.startsWith("action_type=ACOUSTIC_INSPECTION") && items[0].claim.endsWith("battery_pct=30.0"));
});

test("value brackets are part of the claim, not citations", () => {
  const items = parseReasoning("measurement_type=VISUAL_SCORE; value=70.0; safe_band=[72.0, null] [reading:R0064, F2] battery_pct=15.0 [action:A0010]");
  assert.deepEqual(items.map((i) => i.ids), [["reading:R0064", "F2"], ["action:A0010"]]);
  assert.ok(items[0].claim.endsWith("safe_band=[72.0, null]"));
});

test("prose with no citations is one claim; structured evidence wins over reasoning", () => {
  assert.deepEqual(parseReasoning("Validator: priority P2."), [{ claim: "Validator: priority P2.", ids: [] }]);
  assert.deepEqual(evidenceItems([{ claim: "Battery 15%.", ids: ["F3"] }], FLAG_22), [{ claim: "Battery 15%.", ids: ["F3"] }]);
  assert.equal(evidenceItems(null, FLAG_22).length, 3);
  assert.deepEqual(evidenceItems(undefined, undefined), []);
});

test("key=value claims become labelled, readable fields", () => {
  const [action, reading, verdict] = parseReasoning(FLAG_22).map((i) => parseClaim(i.claim));
  assert.deepEqual(action.fields.map((f) => [f.label, f.value]), [
    ["Inspection", "Acoustic inspection"], ["Asset", "PANEL-212"], ["Waypoint", "WP09"], ["Status", "Failed"],
    ["Failure", "Signal noise"], ["Battery", "30.0%"]]);
  assert.deepEqual(reading.fields.map((f) => [f.label, f.value]), [
    ["Measurement", "Sound level"], ["Reading", "81.5 dB"], ["Quality", "Questionable"], ["Trust", "Untrusted"]]);
  assert.deepEqual(verdict.fields.map((f) => [f.label, f.value]), [
    ["Category", "Data quality"], ["Priority", "P2"], ["Escalate", "Yes"], ["Suggested action", "Reinspect now"]]);
});

test("a lead before the first field, space-separated fields, bands and counts", () => {
  const m = parseClaim('Mission M001 reported_status=COMPLETED; actions_failed=2; anomalies_by_severity={"CRITICAL": 0, "HIGH": 1, "LOW": 1}');
  assert.equal(m.lead, "Mission M001");
  assert.deepEqual(m.fields.map((f) => [f.label, f.value]), [["Reported status", "Completed"], ["Failed actions", "2"], ["Anomalies", "High 1, Low 1"]]);
  const r = parseClaim("quality=QUESTIONABLE trust=untrusted; safe_band=[72.0, null]");
  assert.deepEqual(r.fields.map((f) => f.value), ["Questionable", "Untrusted", "≥ 72.0"]);
});

test("prose stays prose; a draft keeps its unfinished last field as text", () => {
  for (const claim of ["HX-208 has a HIGH visual defect.", "The validator says escalate=yes for this flag."]) {
    assert.deepEqual(parseClaim(claim), { lead: "", fields: [], text: claim });
  }
  const draft = parseClaim("action_type=ACOUSTIC_INSPECTION; asset_id=PANEL-212; measurement_ty", true);
  assert.deepEqual([draft.fields.length, draft.text], [2, "measurement_ty"]);
  assert.equal(parseClaim("action_type=ACOUSTIC_INSPECTION; measurement_ty").fields.length, 0);   // finished: prose
});

test("cited rows and codes by name", () => {
  assert.equal(citationLabel("action:A0089"), "Action A0089");
  assert.equal(citationLabel("reading:R0020"), "Reading R0020");
  assert.equal(citationLabel("F1"), "Fact F1");
  assert.equal(citationLabel("V2"), "Image reading V2");
  assert.equal(citationLabel("location:WP03"), "Waypoint WP03");
  assert.equal(citationLabel("anomaly_type:VISUAL_DEFECT"), "Anomaly type VISUAL_DEFECT");
  assert.equal(humanizeCode("RETURN_TO_DOCK"), "Return to dock");
  assert.equal(humanizeCode("PANEL-212"), "PANEL-212");
  assert.equal(humanizeCode("SOUND_DBaptor"), "SOUND_DBaptor");   // not a clean code: shown as the model wrote it
});
