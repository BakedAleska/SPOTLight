// ─────────────────────────────────────────────────────────────────────────────
// Mock live patrol: replay one kit mission (Mission 1 by default) on the Live
// patrol page with no robot runner. SERVER ONLY (reads the kit from disk).
//
// Every number is real: the stops are the mission's kit actions in order
// (waypoint, asset, battery, reading, anomaly, success or failure); a stop
// "needs attention" exactly when the seeded triage raised a flag on that
// action; its confidence is that flag's score and its explanation the stored
// SPOTbot answer when one exists, else the flag's own rule reasons. Nothing is
// written anywhere — the browser plays the stops back on a timer.
// ─────────────────────────────────────────────────────────────────────────────

import { getKit } from "./kit";
import { backendRequest } from "./backend";
import { MOCK_ROUTE_ID, type LiveCapture, type LiveStop, type MockPatrol } from "./live";

/** The kit sample photo each kind of work shows (the same set real live routes use). */
const PHOTO: Record<string, { path: string; kind: string }> = {
  VISUAL_INSPECTION: { path: "/kit/inspection/visual_pump_motor_01.jpg", kind: "visual" },
  THERMAL_INSPECTION: { path: "/kit/inspection/thermal_pump_bearing_01.jpg", kind: "thermal" },
  ACOUSTIC_INSPECTION: { path: "/kit/inspection/acoustic_valve_01.jpg", kind: "auto" },
  GAUGE_READING: { path: "/kit/inspection/gauge_pressure_01.jpg", kind: "gauge" },
  NAVIGATION: { path: "/kit/inspection/pipe_gallery_01.jpg", kind: "visual" },
};

interface BackendFlag {
  flag_id: number; action_id: string | null; triage_event_id?: string | null; priority?: string | null;
  severity?: string | null; score?: number | null; category?: string | null; title?: string | null;
  reasons?: { rule?: string; detail?: string }[] | null;
}
interface FlagDetail {
  assessment?: { answer?: unknown; recommended_action?: unknown; reasoning?: unknown; evidence?: unknown; state?: unknown } | null;
}

/** Verdict bookkeeping rows in a flag's reasons, not policy rules: never shown as "rules fired". */
const VERDICT_RULES = new Set(["laya_verdict", "rules_verdict", "laya_unavailable"]);
const word = (s?: string | null) => (s ?? "").replaceAll("_", " ").toLowerCase();

async function missionFlags(missionId: string): Promise<BackendFlag[] | null> {
  try {
    const d = await backendRequest<{ items: BackendFlag[] }>(
      `/flags?mission_id=${encodeURIComponent(missionId)}&escalated=true&limit=200`, {}, 5000);
    return d.items;
  } catch { return null; }
}

async function storedAnswer(flagId: number): Promise<FlagDetail["assessment"]> {
  try { return (await backendRequest<FlagDetail>(`/flags/${flagId}`, {}, 5000)).assessment ?? null; }
  catch { return null; }
}

export async function mockPatrol(missionId = "M001"): Promise<MockPatrol | null> {
  const kit = getKit();
  const mission = kit.missionById.get(missionId);
  const actions = kit.actionsByMission.get(missionId);
  if (!mission || !actions?.length) return null;
  const ordered = [...actions].sort((a, b) => a.sequence - b.sequence);

  const flags = await missionFlags(missionId);
  const byAction = new Map((flags ?? []).filter((f) => f.action_id).map((f) => [f.action_id as string, f]));
  const answers = new Map<number, FlagDetail["assessment"]>();
  await Promise.all([...byAction.values()].map(async (f) => answers.set(f.flag_id, await storedAnswer(f.flag_id))));

  const stops: LiveStop[] = [];
  const captures: LiveCapture[] = [];
  ordered.forEach((a, i) => {
    const stop = String.fromCharCode(65 + i);
    const location = kit.locationById.get(a.location_id);
    const asset = kit.assetById.get(a.asset_id);
    const reading = kit.readingsByAction.get(a.action_id)?.[0] ?? null;
    const anomaly = kit.anomaliesByAction.get(a.action_id)?.[0] ?? null;
    const flag = byAction.get(a.action_id);
    // With the backend unreachable, fall back to the plainest rule: a serious anomaly or a failed action.
    const red = flags ? !!flag : a.status === "FAILED" || anomaly?.severity === "HIGH" || anomaly?.severity === "CRITICAL";
    const photo = PHOTO[a.action_type] ?? PHOTO.VISUAL_INSPECTION;
    const story = [a.status === "FAILED" ? `${word(a.action_type)} failed (${word(a.failure_type) || "unknown"})` : word(a.action_type),
      reading ? `${word(reading.measurement_type)} ${reading.value} ${reading.unit} (${reading.quality})` : null,
      anomaly ? `${word(anomaly.anomaly_type)} ${anomaly.severity}` : null].filter(Boolean).join(" · ");
    stops.push({
      stop, source_action_id: a.action_id, location_id: a.location_id, asset_id: a.asset_id, action_type: a.action_type,
      battery_pct: a.battery_pct, planned_verdict: red ? "red" : "green", planned_priority: flag?.priority ?? (red ? "P2" : ""),
      story, photo: photo.path, photo_kind: photo.kind, location_name: location?.location_name, floor: location?.floor,
      asset_name: asset?.asset_name, criticality: asset?.criticality,
    });
    const stored = flag ? answers.get(flag.flag_id) : null;
    const answer = typeof stored?.answer === "string" ? stored.answer : null;
    const reasons = (flag?.reasons ?? []).map((r) => r.detail).filter((d): d is string => typeof d === "string");
    captures.push({
      capture_id: i + 1, run_id: 0, stop_index: i, stop, action_id: a.action_id, source_action_id: a.action_id,
      location_id: a.location_id, asset_id: a.asset_id, action_type: a.action_type, battery_pct: a.battery_pct,
      status: a.status, failure_type: a.failure_type || null,
      reading: reading ? { reading_id: reading.reading_id, measurement_type: reading.measurement_type, value: reading.value,
        unit: reading.unit, quality: reading.quality } : null,
      anomaly: anomaly ? { anomaly_id: anomaly.anomaly_id, anomaly_type: anomaly.anomaly_type, severity: anomaly.severity,
        description: anomaly.description } : null,
      phase: "done", event_id: null, flag_id: null,
      // Unflagged stops carry no priority: the triage logged them without escalating, and the mock does not guess one.
      verdict: red ? "red" : "green", priority: flag?.priority ?? (red ? "P2" : null), severity: flag?.severity ?? null,
      category: flag?.category ?? null,
      // A flagged stop shows the flag's score as its confidence; an unflagged one shows none (nothing was escalated).
      laya: flag && typeof flag.score === "number"
        ? { status: "ok", model: "Confidence model", verdict: { escalate: true, p_escalate: flag.score / 100, priority: flag.priority ?? undefined } }
        : { status: "ok", model: "Confidence model", verdict: { escalate: false } },
      rules: { escalate: red, priority: flag?.priority ?? undefined, fired: (flag?.reasons ?? []).map((r) => r.rule)
        .filter((r): r is string => !!r && !VERDICT_RULES.has(r)) },
      decision: { mode: "parallel", title: flag?.title ?? undefined },
      photo: { path: photo.path, label: "simulated capture", note: "Kit sample image picked by action type (mock patrol)." },
      vision: null,
      qwen: red ? (answer
        ? { source: "system2", answer, recommended_action: typeof stored?.recommended_action === "string" ? stored.recommended_action : undefined,
            reasoning: typeof stored?.reasoning === "string" ? stored.reasoning : undefined,
            evidence: Array.isArray(stored?.evidence) ? stored.evidence as { claim: string; ids: string[] }[] : undefined }
        : { source: "template", answer: flag?.title ?? story, reasoning: reasons.join(" ") || undefined,
            fallback_reason: "mock patrol: no stored SPOTbot answer for this flag yet" })
        : null,
      qwen_source: red ? (answer ? "system2" : "template") : null, doc_id: null, kg: {}, timings: {},
      arrived_at: a.timestamp, triaged_at: a.timestamp, analysed_at: a.timestamp, updated_at: a.timestamp,
    });
  });

  const red = captures.filter((c) => c.verdict === "red").length;
  return {
    route: { route_id: MOCK_ROUTE_ID, name: `Mission ${Number(missionId.slice(1))} (mock patrol)`,
      description: `${mission.mission_name}: the kit's ${missionId} replayed stop by stop, no robot needed.`,
      stops, planned_red: red },
    captures,
    verdicts: flags ? "flags" : "kit",
  };
}
