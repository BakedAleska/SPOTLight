/* Live simulation (backend /api/v1/live, docs/backend-services-contract.md "Live simulation").
   Browser code talks only to the same-origin /api/live/* routes; they call the backend server-side. */

export interface LiveStop {
  stop: string; source_action_id: string; location_id: string; asset_id: string; action_type: string;
  battery_pct: number; planned_verdict: "red" | "green"; planned_priority: string; story: string;
  photo: string; photo_kind: string; location_name?: string; floor?: number; asset_name?: string; criticality?: string;
}
export interface LiveRoute { route_id: string; name: string; description: string; stops: LiveStop[]; planned_red: number }
export interface LiveRun {
  run_id: number; route_id: string; mission_id: string; status: "pending" | "running" | "done" | "error" | "cancelled";
  current_stop: number; total_stops: number; runner: string | null; error: string | null;
  counts: Record<string, number>; created_at: string; claimed_at: string | null; finished_at: string | null;
  updated_at: string; red?: number; green?: number;
}
export interface Answer { answer?: string; confidence?: number; meaning?: string }
export interface VisionRow {
  id: string; tool?: string; kind?: string; quantity?: string; value?: number; unit?: string; confidence?: number;
  trust?: string; flags?: string[]; pairing?: string;
}
export interface LiveCapture {
  capture_id: number; run_id: number; stop_index: number; stop: string; action_id: string; source_action_id: string;
  location_id: string; asset_id: string; action_type: string; battery_pct: number; status: string;
  failure_type: string | null;
  reading: { reading_id?: string; measurement_type?: string; value?: number; unit?: string; quality?: string } | null;
  anomaly: { anomaly_id?: string; anomaly_type?: string; severity?: string; description?: string } | null;
  phase: "arrived" | "triaged" | "analysing" | "done" | "error"; event_id: string | null; flag_id: number | null;
  verdict: "red" | "green" | null; priority: string | null; severity: string | null; category: string | null;
  laya: {
    status?: string; error?: string; model?: string; device?: string; latency_ms?: number; fine_tuned?: boolean;
    answers?: Record<string, Answer>; verdict?: { escalate?: boolean; p_escalate?: number; priority?: string } | null;
  } | null;
  rules: { escalate?: boolean; priority?: string; category?: string; fired?: string[]; policy_version?: string } | null;
  decision: { mode?: string; agreement?: string; summary?: string; title?: string } | null;
  photo: { path?: string; label?: string; note?: string; sha256?: string } | null;
  vision: {
    error?: string; kind?: string; rows?: VisionRow[]; ocr?: { text: string }[]; latency_ms?: number;
    annotated_image_b64?: string | null; demo_alert?: { title?: string } | null;
  } | null;
  qwen: {
    source?: string; question?: string; answer?: string; reasoning?: string; recommended_action?: string;
    confidence_label?: string; confidence?: number; model?: string; latency_ms?: number; latency_ms_total?: number;
    tokens_per_sec?: number; fallback_reason?: string; evidence?: { claim: string; ids: string[] }[]; image?: string;
  } | null;
  qwen_source: "pending" | "system2" | "template" | "error" | null; doc_id: string | null;
  kg: { triage?: { nodes?: string[]; edges?: number }; explanation?: string };
  timings: Record<string, number>; arrived_at: string; triaged_at: string | null; analysed_at: string | null;
  updated_at: string;
}
export interface LiveRunDetail { run: LiveRun; route: LiveRoute | null; captures: LiveCapture[] }
export interface LiveKg {
  nodes: { node_id: string; kind: string; label: string; embedded: boolean; stop: string | null }[];
  edges: { src: string; rel: string; dst: string; stop: string | null }[];
  totals: { nodes: number; edges: number; live_nodes?: number; live_edges?: number } | null;
}

export function sameOrigin(request: Request) {
  const origin = request.headers.get("origin");
  try { return !!origin && new URL(origin).host === request.headers.get("host"); }
  catch { return false; }
}

export function liveFailure(error: unknown, message = "The live backend is unavailable.") {
  console.error("Live request failed", error);
  return Response.json({ error: message }, { status: 502, headers: { "Cache-Control": "no-store" } });
}

export const validRunId = (id: string) => /^[1-9]\d{0,9}$/.test(id);
