export type JsonRow = Record<string, unknown>;
export interface ModelStream {
  connected: boolean; updatedAt: string; warnings: string[];
  events: JsonRow[]; details: JsonRow[]; forecasts: JsonRow[];
  calls: JsonRow[]; stats: JsonRow[]; routing: JsonRow;
}
export interface ModelSample { id: string; time: string; model: string; metric: string; value: number; action: string }
export const row = (value: unknown): JsonRow => value && typeof value === "object" && !Array.isArray(value) ? value as JsonRow : {};
export const rows = (value: unknown): JsonRow[] => Array.isArray(value) ? value.map(row) : [];
export const probability = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? value * 100 : null;
export function predictionSamples(data: ModelStream): ModelSample[] {
  const samples = new Map<string, ModelSample>();
  for (const detail of data.details) for (const prediction of rows(detail.system1)) {
    const question = String(prediction.question ?? "");
    const value = probability(row(prediction.probs).A);
    if (!["escalate", "will_fail", "anomaly_expected"].includes(question) || value === null) continue;
    const id = String(prediction.prediction_id ?? `${prediction.action_id}:${question}:${prediction.created_at}`);
    samples.set(id, { id, time: String(prediction.created_at ?? ""), model: String(prediction.model ?? "Laya"),
      metric: question, value, action: String(prediction.action_id ?? prediction.mission_id ?? "") });
  }
  // Forecasts are model probabilities only when their source identifies System 1.
  for (const forecast of data.forecasts) {
    if (!String(forecast.source).startsWith("system1_")) continue;
    for (const [key, metric] of [["p_fail", "will_fail"], ["p_anomaly", "anomaly_expected"]]) {
      const value = probability(forecast[key]);
      const id = `forecast:${forecast.action_id}:${metric}`;
      if (value !== null && ![...samples.values()].some(s => s.action === forecast.action_id && s.metric === metric))
        samples.set(id, { id, time: String(forecast.timestamp ?? ""), model: String(forecast.model ?? "Laya"), metric, value, action: String(forecast.action_id) });
    }
  }
  return [...samples.values()].sort((a, b) => a.time.localeCompare(b.time));
}
export function assessmentSource(assessment: JsonRow) {
  return assessment.state === "DONE" && assessment.model !== "template" && assessment.model !== "stub" ? "Model result" : "Fallback / evidence summary";
}
