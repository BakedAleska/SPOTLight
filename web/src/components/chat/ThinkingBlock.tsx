"use client";

import { useId, useState } from "react";
import { Brain, ChevronRight } from "lucide-react";
import { Markdown } from "./Markdown";

function seconds(ms: number | null | undefined) {
  if (!ms || ms < 0) return null;
  return Math.max(1, Math.round(ms / 1000));
}

/** Live: a shimmering "Thinking…" with the tail of the thought summary
    underneath, so the operator sees it isn't stuck. */
export function ThinkingLive({ text }: { text: string }) {
  const tail = text.length > 320 ? `…${text.slice(-320)}` : text;
  return (
    <div className="mb-3">
      <div className="flex items-center gap-2 text-sm">
        <Brain size={15} aria-hidden="true" className="text-muted" />
        <span className="chat-shimmer font-medium">Thinking…</span>
      </div>
      {tail && (
        <p className="mt-1.5 line-clamp-3 border-l-2 border-line pl-3 text-[13px] leading-relaxed text-muted">{tail}</p>
      )}
    </div>
  );
}

/** Settled: "Thought for N s", collapsible. Flash-Lite models think without
    returning a summary, so the empty case still reports the time. */
export function ThinkingDone({ text, ms, stopped = false }: { text: string | null; ms: number | null; stopped?: boolean }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const secs = seconds(ms);
  const label = stopped ? "Stopped while thinking" : secs ? `Thought for ${secs} s` : "Thought";
  if (!text?.trim()) {
    return (
      <p className="mb-3 flex items-center gap-2 text-sm text-muted" title="This model returned no thought summary.">
        <Brain size={15} aria-hidden="true" />
        {label}
        <span className="text-xs text-faint">· no summary returned</span>
      </p>
    );
  }
  return (
    <div className="mb-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={panelId}
        className="-ml-1 inline-flex cursor-pointer items-center gap-2 rounded-md px-1 py-0.5 text-sm text-muted transition-colors hover:text-ink"
      >
        <Brain size={15} aria-hidden="true" />
        {label}
        <ChevronRight size={14} aria-hidden="true" className={`transition-transform ${open ? "rotate-90" : ""}`} />
      </button>
      {open && (
        <div id={panelId} className="mt-2 border-l-2 border-line pl-4 [&_*]:text-muted">
          <Markdown content={text} className="text-[13.5px]" />
        </div>
      )}
    </div>
  );
}
