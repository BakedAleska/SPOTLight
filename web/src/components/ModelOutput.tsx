import { Fragment, type ReactNode } from "react";
import { citations, evidenceItems, humanizeCode, parseClaim, type EvidenceItem } from "@/lib/evidence";
import { displayText } from "@/lib/display";

/* One look for a System 2 answer everywhere it shows (flag detail, Qwen's
   draft, /live): the answer sentence, the recommended action in words, then
   each claim as labelled fields with the rows it cites (lib/evidence.ts). */

/** One claim: `key=value` claims as a label/value list, prose as written; `trailing` (a caret) ends the text. */
export function EvidenceClaim({ claim, ids, partial = false, compact = false, trailing }: {
  claim: string;
  ids: string[];
  /** A draft still being typed: an unfinished last field stays visible as text. */
  partial?: boolean;
  compact?: boolean;
  trailing?: ReactNode;
}) {
  const parsed = parseClaim(displayText(claim), partial);
  const last = parsed.fields.length - 1;
  return (
    <div className={`min-w-0 ${compact ? "text-xs" : "text-sm"}`}>
      {parsed.fields.length ? <>
        {parsed.lead && <p className="font-medium text-ink">{parsed.lead}</p>}
        <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-0.5">
          {parsed.fields.map((field, index) => <Fragment key={`${field.key}-${index}`}>
            <dt className="text-muted">{field.label}</dt>
            <dd className="break-words text-ink">{field.value}{index === last && !parsed.text && trailing}</dd>
          </Fragment>)}
        </dl>
        {parsed.text && <p className="break-words text-muted">{parsed.text}{trailing}</p>}
      </> : <p className="break-words text-ink">{parsed.text}{trailing}</p>}
      {ids.length > 0 && <Sources ids={ids} />}
    </div>
  );
}

function Sources({ ids }: { ids: string[] }) {
  return (
    <p className="mt-1 flex flex-wrap items-center gap-1 text-[11px] text-muted">
      <span>{ids.length > 1 ? "Sources" : "Source"}:</span>
      {citations(ids).map((c) => <span key={c.id} title={c.id} className="rounded border border-line bg-canvas px-1.5 py-px">{c.label}</span>)}
    </p>
  );
}

export function EvidenceList({ items, compact = false, limit }: { items: EvidenceItem[]; compact?: boolean; limit?: number }) {
  const shown = limit ? items.slice(0, limit) : items;
  return (
    <ul className="space-y-1.5">
      {shown.map((item, index) => <li key={index} className="rounded border border-line bg-surface-2 px-2.5 py-1.5">
        <EvidenceClaim claim={item.claim} ids={item.ids} compact={compact} />
      </li>)}
      {shown.length < items.length && <li className="text-[11px] text-muted">+{items.length - shown.length} more</li>}
    </ul>
  );
}

/** A recommended action code, said once in words. */
export function RecommendedAction({ code, compact = false }: { code: string; compact?: boolean }) {
  return (
    <p className={compact ? "text-xs" : "text-sm"}>
      <span className="text-muted">Recommended action: </span><strong className="text-ink">{humanizeCode(code)}</strong>
    </p>
  );
}

/** A checked System 2 answer (or its stored row): structured `evidence` when sent, else the flattened `reasoning`. */
export function ModelExplanation({ answer, evidence, reasoning, recommendedAction, compact = false, limit }: {
  answer?: string | null;
  evidence?: unknown;
  reasoning?: unknown;
  recommendedAction?: string | null;
  compact?: boolean;
  limit?: number;
}) {
  const items = evidenceItems(evidence, reasoning);
  const shown = answer ? displayText(answer) : null;
  const structuredAnswer = shown ? parseClaim(shown).fields.length > 1 : false;
  return (
    <div className="space-y-2">
      {shown && (structuredAnswer
        ? <EvidenceClaim claim={shown} ids={[]} compact={compact} />
        : <p className={`text-ink ${compact ? "text-sm" : ""}`}>{shown}</p>)}
      {recommendedAction && <RecommendedAction code={recommendedAction} compact={compact} />}
      {items.length > 0 && <div>
        <p className="mb-1 text-[11px] font-semibold tracking-wide text-muted uppercase">Cited evidence</p>
        <EvidenceList items={items} compact={compact} limit={limit} />
      </div>}
    </div>
  );
}
