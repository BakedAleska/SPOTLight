/* How model names and System 1 wording reach the screen.

   The console never shows a model's file path (a llama.cpp server reports its
   GGUF path as the model name), calls the explaining model SPOTbot, and never
   names System 1: operators see its escalation probability as "Confidence NN%".
   Backend text — flag reasons, template explanations — still says "Laya", so it
   is rewritten here at display time rather than in the backend, where the same
   text grounds System 2. */

/** A model identifier as operators see it: System 1 is the "Confidence model", the explaining LLM is always
    "SPOTbot", the embedder is the "Embedding model"; anything else loses its directory and `.gguf`. */
export function displayModel(model: unknown): string {
  const raw = String(model ?? "").trim();
  if (!raw) return "";
  if (/laya/i.test(raw)) return "Confidence model";
  if (/embed/i.test(raw)) return "Embedding model";
  if (/qwen|gemini|\.gguf$/i.test(raw)) return "SPOTbot";
  return raw.split(/[\\/]/).filter(Boolean).pop()!.replace(/\.gguf$/i, "");
}

/** A 0–1 probability as "Confidence NN%"; null when it is not a probability. */
export function confidenceLabel(p: unknown): string | null {
  return typeof p === "number" && Number.isFinite(p) && p >= 0 && p <= 1 ? `Confidence ${Math.round(p * 100)}%` : null;
}

/** Backend text -> the console's wording: System 1 ("Laya") as the confidence model, the LLM as SPOTbot. */
export function displayText(text: string): string {
  if (!/laya|qwen/i.test(text)) return text;
  return text
    .replace(/\bQwen[\w.-]*(?:\s+\d[\d.]*B?\b)*/gi, "SPOTbot")   // "Qwen3.8-27B", "Qwen 3.8 27B"
    // validator reasons: "Laya: escalate (0.91)" / "Laya: log only (0.12)"
    .replace(/\bLaya:\s*(escalate|log only)\s*\((\d*\.?\d+)\)/gi,
      (_, verdict: string, p: string) => `${confidenceLabel(Number(p)) ?? "Confidence"} (${verdict})`)
    .replace(/\bLaya:\s*unavailable/gi, "Confidence model unavailable")
    .replace(/\b(?:System 1 \(Laya\)|Laya \(System 1\))/g, "Confidence model")
    .replace(/\bLaya's\b/g, "the confidence model's")
    .replace(/\bLaya\b/g, "the confidence model")
    .replace(/\blaya\b/g, "confidence model");
}

/** JSON for a raw-fields disclosure: model names without paths, System 1 not named. */
export function displayJson(value: unknown): string {
  return JSON.stringify(value, (key, v) => {
    if (typeof v !== "string") return v;
    if (key === "model" || key === "checkpoint") return displayModel(v);
    return displayText(v.replace(/(?:\/[\w.-]+)+\/([\w.-]+\.gguf)\b/g, "$1"));
  }, 2);
}
