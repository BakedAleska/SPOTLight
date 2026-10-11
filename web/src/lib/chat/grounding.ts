import type { Grounding, ModelInfo } from "./types";

export type BadgeTone = "ok" | "warn" | "bad" | "muted";

export interface GroundingBadge {
  tone: BadgeTone;
  label: string;
  /** Longer explanation for the tooltip / screen readers. */
  detail: string;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * One badge per answer, worst finding first: an invented source id is the
 * most serious (the answer points at evidence that doesn't exist), then an
 * answer with no citations at all, then figures the check couldn't find in
 * the sources. Only a fully clean answer reads "Grounded".
 */
export function groundingBadge(grounding: Grounding | null | undefined): GroundingBadge | null {
  if (!grounding) return null;
  const cited = grounding.cited_ids?.length ?? 0;
  const unknown = grounding.unknown_ids?.length ?? 0;
  const numbers = grounding.unverified_numbers?.length ?? 0;
  if (unknown > 0) {
    return {
      tone: "bad",
      label: "Cited an unknown source",
      detail: `The answer cites ${plural(unknown, "id")} that were not among the sources: ${grounding.unknown_ids.join(", ")}.`,
    };
  }
  if (cited === 0) {
    return {
      tone: "muted",
      label: "No sources cited",
      detail: "The answer does not cite any of the sources it was given. Treat it as unverified.",
    };
  }
  if (numbers > 0) {
    return {
      tone: "warn",
      label: `${plural(numbers, "figure")} not found in sources`,
      detail: `These numbers do not appear in the sources or your question: ${grounding.unverified_numbers.join(", ")}.`,
    };
  }
  return {
    tone: "ok",
    label: `Grounded · ${plural(cited, "source")}`,
    detail: `Every cited id matches a source, and every figure appears in the sources (${grounding.retrieval_mode} retrieval).`,
  };
}

/** The model to offer after a rate limit: the next one in catalog order. */
export function nextModel(models: ModelInfo[], current: string): ModelInfo | null {
  if (models.length < 2) return null;
  const index = models.findIndex((m) => m.id === current);
  return models[(index + 1) % models.length] ?? null;
}

export function modelLabel(models: ModelInfo[] | undefined, id: string | null | undefined): string {
  if (!id) return "";
  return models?.find((m) => m.id === id)?.label ?? readableModelId(id);
}

/** A label for a model that has left the catalog (an old answer from
    `gemma-4-31b-it` reads "Gemma 4 31B"). */
export function readableModelId(id: string): string {
  return id
    .split("-")
    .filter((part) => part !== "it")
    .map((part) => (/^\d+(\.\d+)?[bm]$/i.test(part) ? part.toUpperCase() : /^[a-z]\d+[a-z]$/i.test(part) ? part.toUpperCase() : part.charAt(0).toUpperCase() + part.slice(1)))
    .join(" ");
}
