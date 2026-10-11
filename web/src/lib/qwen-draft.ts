/* Qwen drafts (docs/backend-services-contract.md "Streaming Qwen drafts").

   A draft is the model's partial JSON before any check: never an answer. Every
   draft is a full snapshot of the fields generated so far (replace, never
   append); the last string may stop mid-word. The checked result always
   replaces it. These helpers are pure so both the flag detail (SSE `draft`
   events) and /live (capture.qwen while qwen_source is "pending") share them. */

export interface DraftClaim { claim: string; ids: string[] }

export interface QwenDraft {
  /** 1-based model call inside one assessment; a client shows the newest only. */
  call: number;
  /** Content chunks received so far in this call (live rows only). */
  tokens: number | null;
  evidence: DraftClaim[];
  answer: string;
  recommendedAction: string;
  /** Only once the label is complete. */
  confidence: "low" | "medium" | "high" | null;
}

type Raw = Record<string, unknown>;
const record = (value: unknown): Raw | null => (value && typeof value === "object" && !Array.isArray(value) ? value as Raw : null);
const text = (value: unknown) => (typeof value === "string" ? value : "");
const count = (value: unknown) => (typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : null);

function parseDraft(fields: Raw, call: unknown, tokens: unknown): QwenDraft {
  const evidence = Array.isArray(fields.evidence)
    ? fields.evidence.flatMap((item) => {
      const row = record(item);
      if (!row) return [];
      const ids = Array.isArray(row.ids) ? row.ids.filter((id): id is string => typeof id === "string" && id.length > 0) : [];
      return [{ claim: text(row.claim), ids }];
    })
    : [];
  const confidence = text(fields.confidence).toLowerCase();
  return {
    call: count(call) || 1,
    tokens: count(tokens),
    evidence,
    answer: text(fields.answer),
    recommendedAction: text(fields.recommended_action),
    confidence: confidence === "low" || confidence === "medium" || confidence === "high" ? confidence : null,
  };
}

/** The backend SSE `draft` event: `{call, fields, reasoning}`. */
export function draftFromEvent(data: unknown): QwenDraft | null {
  const event = record(data);
  const fields = record(event?.fields);
  return event && fields ? parseDraft(fields, event.call, null) : null;
}

/** A /live capture's `qwen` while it holds a draft: `{draft: true, call, tokens, answer, evidence, ...}`. */
export function draftFromCapture(qwen: unknown): QwenDraft | null {
  const row = record(qwen);
  return row?.draft === true ? parseDraft(row, row.call, row.tokens) : null;
}

/** Newest call wins; within a call the newer snapshot replaces the older. */
export function newerDraft(current: QwenDraft | null, next: QwenDraft): QwenDraft {
  return current && next.call < current.call ? current : next;
}

/** Characters the typewriter reveals, in generation order: claims, answer, action. */
export function draftLength(draft: QwenDraft): number {
  return draft.evidence.reduce((sum, row) => sum + row.claim.length, 0) + draft.answer.length + draft.recommendedAction.length;
}

export type DraftCaret = { part: "evidence"; index: number } | { part: "answer" } | { part: "action" } | null;

export interface RevealedDraft {
  /** `complete`: the whole claim is on screen, so its ids may show. */
  evidence: (DraftClaim & { complete: boolean })[];
  answer: string;
  recommendedAction: string;
  /** Where the caret sits: after the last visible text (null: nothing yet). */
  caret: DraftCaret;
}

/** The first `chars` characters of a snapshot, in generation order. Never more than the snapshot holds. */
export function revealDraft(draft: QwenDraft, chars: number): RevealedDraft {
  let budget = Math.max(0, Math.floor(chars));
  let caret: DraftCaret = null;
  const take = (value: string) => {
    const shown = value.slice(0, budget);
    budget -= shown.length;
    return shown;
  };
  const evidence: RevealedDraft["evidence"] = [];
  for (const row of draft.evidence) {
    const claim = take(row.claim);
    if (!claim) continue;
    evidence.push({ claim, ids: row.ids, complete: claim.length === row.claim.length });
    caret = { part: "evidence", index: evidence.length - 1 };
  }
  const answer = take(draft.answer);
  if (answer) caret = { part: "answer" };
  const recommendedAction = take(draft.recommendedAction);
  if (recommendedAction) caret = { part: "action" };
  return { evidence, answer, recommendedAction, caret };
}
