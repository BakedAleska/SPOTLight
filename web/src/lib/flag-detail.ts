export type FlagAction = "ACKNOWLEDGE" | "RESOLVE" | "ESCALATE";

export interface FlagDetailData {
  flag: {
    flag_id: number;
    title: string;
    status: string;
    severity: string;
    score: number;
    mission_id: string | null;
    action_id: string | null;
    asset_id: string | null;
    location_id: string | null;
    reasons: { rule: string; detail?: string }[];
  };
  acks: { ack_id: number; action: string; operator: string; note: string | null; created_at: string }[];
}

export interface FlagExplanation {
  answer: string | null;
  reasoning: string | null;
  recommended_action: string | null;
  // system2_template: System 2 fell back to its checked template; system1: not escalated, triage summary only
  source: "system2" | "system2_template" | "template" | "system1";
  model?: string | null;
}

export function explanationLabel(data: Pick<FlagExplanation, "source" | "model">): string {
  if (data.source === "template" || data.source === "system2_template") return "Rule-based explanation";
  if (data.source === "system1") return "Triage summary (not escalated)";
  return data.model === "stub" ? "Evidence summary (stub)" : "Model explanation";
}

// The backend explain call can take long with a slow local model; EXPLAIN_TIMEOUT_MS overrides (5–120 s).
export function explainTimeoutMs(raw = process.env.EXPLAIN_TIMEOUT_MS): number {
  const value = Number(raw);
  return Number.isFinite(value) && value >= 5_000 && value <= 120_000 ? value : 50_000;
}
