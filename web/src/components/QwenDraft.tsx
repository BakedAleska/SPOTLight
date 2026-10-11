"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { PenLine } from "lucide-react";
import { draftLength, revealDraft, type QwenDraft } from "@/lib/qwen-draft";

/* Qwen's unverified draft, typed out as it arrives (contract "Streaming Qwen
   drafts"). Snapshots land in jumps (SSE every ~100 ms on the flag detail, a
   poll every 750 ms on /live), so a light typewriter eases toward the newest
   snapshot: it never shows more than the snapshot holds and restarts when the
   model call changes. Reduced motion shows each snapshot as it lands.

   Fields render in generation order (claims, answer, action) so text only
   ever grows at the bottom. The region is aria-busy and not a live region:
   screen readers hear the finished answer, not every token. */

const MIN_CHARS_PER_S = 45;

function subscribeReducedMotion(onChange: () => void) {
  const query = window.matchMedia("(prefers-reduced-motion: reduce)");
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

export function useReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribeReducedMotion,
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    () => false,
  );
}

/** Characters to show: eases toward `target`, resets to 0 when `resetKey` changes. */
export function useTypewriter(target: number, resetKey: string | number, catchUpMs = 300): number {
  const reduced = useReducedMotion();
  const [shown, setShown] = useState(0);
  const [key, setKey] = useState(resetKey);
  if (key !== resetKey) { // a new model call: start over (state adjusted during render, not in an effect)
    setKey(resetKey);
    setShown(0);
  }
  const value = useRef(0);
  const valueKey = useRef(resetKey);
  useEffect(() => {
    if (valueKey.current !== resetKey) { valueKey.current = resetKey; value.current = 0; }
    if (reduced) return;
    value.current = Math.min(value.current, target);
    let frame = 0;
    let last: number | null = null;
    const step = (now: number) => {
      const dt = last === null ? 16 : Math.min(now - last, 100);
      last = now;
      const gap = target - value.current;
      value.current = Math.min(target, value.current + Math.max((MIN_CHARS_PER_S * dt) / 1000, gap * (1 - Math.exp(-dt / catchUpMs))));
      setShown(value.current);
      if (value.current < target) frame = requestAnimationFrame(step);
    };
    if (value.current < target) frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, resetKey, reduced, catchUpMs]);
  return reduced ? target : Math.min(Math.floor(shown), target);
}

function Caret() {
  return <span aria-hidden="true" className="qwen-caret" />;
}

export function QwenDraftView({ draft, label, formatAction = (action) => action, catchUpMs, compact = false, finished = false }: {
  draft: QwenDraft;
  label: string;
  formatAction?: (action: string) => string;
  catchUpMs?: number;
  /** /live card sizing: smaller claim text. */
  compact?: boolean;
  /** Qwen stopped writing (the answer is being checked): show the whole draft, no caret. */
  finished?: boolean;
}) {
  const typed = useTypewriter(draftLength(draft), draft.call, catchUpMs);
  const shown = revealDraft(draft, finished ? draftLength(draft) : typed);
  const caret = finished ? null : shown.caret;
  return (
    <div aria-busy="true" className="space-y-1.5 rounded border border-dashed border-accent/50 bg-surface-2 p-2.5">
      <p className="flex flex-wrap items-center gap-x-1.5 text-[11px] font-semibold tracking-wide text-accent-ink uppercase">
        <PenLine size={12} aria-hidden="true" />{label}
        {draft.call > 1 && <span className="font-normal normal-case text-muted">· attempt {draft.call}</span>}
        {draft.tokens != null && <span className="font-normal normal-case text-muted">· {draft.tokens} tokens</span>}
        {draft.confidence && <span className="font-normal normal-case text-muted">· confidence {draft.confidence}</span>}
      </p>
      {shown.evidence.length > 0 && <ul className={`list-disc space-y-0.5 pl-4 text-muted ${compact ? "text-xs" : "text-sm"}`}>
        {shown.evidence.map((row, index) => <li key={index}>
          {row.claim}
          {row.complete && row.ids.length > 0 && <span className="font-mono text-[11px] break-all"> [{row.ids.join(", ")}]</span>}
          {caret?.part === "evidence" && caret.index === index && <Caret />}
        </li>)}
      </ul>}
      {shown.answer && <p className="text-ink">{shown.answer}{caret?.part === "answer" && <Caret />}</p>}
      {shown.recommendedAction && <p className="text-ink">Recommended: <strong>{formatAction(shown.recommendedAction)}</strong>{caret?.part === "action" && <Caret />}</p>}
      {!caret && !finished && <p className="text-muted"><Caret /></p>}
    </div>
  );
}
