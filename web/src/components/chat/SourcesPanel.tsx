"use client";

import { useEffect, useRef } from "react";
import Link from "next/link";
import { ArrowUpRight, Info, Library, X } from "lucide-react";
import type { RetrievalMeta, Source } from "@/lib/chat/types";
import { OriginLabel } from "./SourceMeta";

export interface SourcesView {
  /** Which answer these belong to (message id or "live"). */
  key: string;
  sources: Source[];
  /** Cited source ids in order of first citation: chip 1, 2, 3 … */
  cited: string[];
  focusId?: string;
  /** How retrieval went; gaps are only known for answers streamed in this tab. */
  retrieval?: Pick<RetrievalMeta, "mode" | "gaps"> | null;
}

/** Cited sources first, numbered like their chips; then the rest in
    retrieval order, unnumbered. */
export function arrangeSources(sources: Source[], cited: string[]) {
  const byId = new Map(sources.map((source) => [source.id, source]));
  const citedSources = cited.map((id) => byId.get(id)).filter((source): source is Source => !!source);
  const citedIds = new Set(citedSources.map((source) => source.id));
  return {
    cited: citedSources.map((source, index) => ({ source, number: index + 1 })),
    rest: sources.filter((source) => !citedIds.has(source.id)).sort((a, b) => a.n - b.n),
  };
}

/**
 * A quiet note when retrieval was degraded (chat.2 `retrieval.gaps`): one
 * plain-language line, plus the backend's raw gap strings as details.
 * Gap shapes: "embedding unavailable (<reason>): full-text search only",
 * "embedding slower than 2.4s: full-text search only", "<step> timed out
 * (retrieval budget)", "<step> failed: <Error>", "graph neighbourhood
 * skipped: retrieval budget used up".
 */
export function retrievalNotes(retrieval: SourcesView["retrieval"]): { summary: string; details: string[] } | null {
  if (!retrieval) return null;
  const details = (retrieval.gaps ?? []).filter(Boolean);
  const slow = details.some((gap) => /^embedding slower/i.test(gap));
  const keywordOnly = retrieval.mode === "fulltext" || details.some((gap) => /^embedding/i.test(gap));
  if (keywordOnly) {
    return {
      summary: slow
        ? "Keyword search only: the embedding service was too slow, so matches are by wording, not meaning."
        : "Keyword search only: the embedding service is offline, so matches are by wording, not meaning.",
      details,
    };
  }
  if (details.length === 0) return null;
  return { summary: "Some retrieval steps were skipped, so this answer may have missed sources.", details };
}

function RefChip({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded border border-line bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-muted">
      <span className="sr-only">{label} </span>
      {value}
    </span>
  );
}

function SourceCard({ source, number, focused }: { source: Source; number: number | null; focused: boolean }) {
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [focused]);
  const refs = source.refs ?? {};
  const flagId = refs.flag_id != null && /^[1-9]\d*$/.test(String(refs.flag_id)) ? String(refs.flag_id) : null;
  return (
    <li
      ref={ref}
      className={`rounded-lg border p-3 transition-colors ${focused ? "border-accent bg-surface-2" : "border-line bg-surface"}`}
    >
      <div className="flex items-center gap-2 text-[11px] text-muted">
        {number != null && (
          <span className="nums grid h-5 min-w-5 place-items-center rounded-full bg-surface-2 px-1 font-mono font-semibold text-ink">
            <span className="sr-only">Source </span>{number}
          </span>
        )}
        <OriginLabel origin={source.origin} kind={source.kind} />
        {number != null && <span className="ml-auto shrink-0 rounded-full border border-ok/35 px-1.5 text-[10px] font-medium text-ok">Cited</span>}
      </div>
      <p className="mt-2 text-sm leading-snug font-medium text-ink">{source.title}</p>
      {source.snippet && <p className="mt-1 text-[13px] leading-relaxed text-muted">{source.snippet}</p>}
      {source.path && (
        <p className="mt-2 rounded bg-surface-3 px-2 py-1 font-mono text-[11px] leading-relaxed break-words text-muted">
          <span className="sr-only">Graph path: </span>
          {source.path}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <RefChip label="Source id" value={source.id} />
        {refs.mission_id && <RefChip label="Mission" value={refs.mission_id} />}
        {refs.asset_id && <RefChip label="Asset" value={refs.asset_id} />}
        {refs.location_id && <RefChip label="Location" value={refs.location_id} />}
        {refs.action_id && <RefChip label="Action" value={refs.action_id} />}
        {flagId && (
          <Link
            href={`/?flag=${flagId}`}
            className="inline-flex items-center gap-0.5 rounded px-1 text-xs font-medium text-info hover:underline"
          >
            Open flag #{flagId}
            <ArrowUpRight size={12} aria-hidden="true" />
          </Link>
        )}
        {source.score != null && (
          <span className="nums ml-auto font-mono text-[11px] text-faint">score {source.score.toFixed(2)}</span>
        )}
      </div>
    </li>
  );
}

/**
 * Every source the answer was given, in display order. Docked beside the
 * chat on very wide screens; a sheet over it everywhere else.
 */
export function SourcesPanel({ view, onClose }: { view: SourcesView; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus({ preventScroll: true });
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, view.key]);

  const { cited, rest } = arrangeSources(view.sources, view.cited);
  const total = view.sources.length;
  const notes = retrievalNotes(view.retrieval);

  return (
    <>
      <button
        type="button"
        tabIndex={-1}
        aria-hidden="true"
        onClick={onClose}
        className="fixed inset-0 z-30 cursor-default bg-black/40 2xl:hidden"
      />
      <aside
        aria-label="Sources"
        className="fixed inset-x-0 bottom-0 z-40 flex max-h-[80dvh] flex-col rounded-t-2xl border-t border-line bg-chrome sm:inset-x-auto sm:inset-y-0 sm:right-0 sm:max-h-none sm:w-96 sm:rounded-none sm:border-t-0 sm:border-l 2xl:static 2xl:z-auto 2xl:w-96 2xl:shrink-0"
      >
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-4">
          <Library size={16} aria-hidden="true" className="text-muted" />
          <h2 className="text-sm font-medium text-ink">
            Sources <span className="nums text-muted">({total})</span>
          </h2>
          {cited.length > 0 && <span className="text-xs text-muted">· {cited.length} cited</span>}
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close sources"
            className="ml-auto grid size-8 cursor-pointer place-items-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-ink"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </header>
        <div className="relative min-h-0 flex-1 overflow-y-auto overscroll-contain p-3">
          {notes && (
            <div role="note" className="mb-3 flex gap-2 rounded-lg border border-line bg-surface-2 px-3 py-2 text-xs leading-relaxed text-muted">
              <Info size={14} aria-hidden="true" className="mt-0.5 shrink-0" />
              <div className="min-w-0 break-words">
                <p>{notes.summary}</p>
                {notes.details.length > 0 && (
                  <details className="mt-1">
                    <summary className="cursor-pointer text-faint hover:text-muted">Details</summary>
                    <ul className="mt-1 space-y-0.5 font-mono text-[11px] text-faint">
                      {notes.details.map((gap) => <li key={gap}>{gap}</li>)}
                    </ul>
                  </details>
                )}
              </div>
            </div>
          )}
          {total === 0 ? (
            <p className="p-1 text-sm text-muted">No sources were retrieved for this answer.</p>
          ) : (
            <>
              {cited.length > 0 && (
                <ol aria-label="Cited sources" className="space-y-2">
                  {cited.map(({ source, number }) => (
                    <SourceCard key={source.id} source={source} number={number} focused={source.id === view.focusId} />
                  ))}
                </ol>
              )}
              {rest.length > 0 && (
                <>
                  {cited.length > 0 && (
                    <h3 className="mt-5 mb-2 px-1 text-xs font-medium text-muted">
                      Also retrieved <span className="nums">({rest.length})</span>
                    </h3>
                  )}
                  <ul aria-label={cited.length > 0 ? "Also retrieved" : "Retrieved sources"} className="space-y-2">
                    {rest.map((source) => (
                      <SourceCard key={source.id} source={source} number={null} focused={source.id === view.focusId} />
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </div>
      </aside>
    </>
  );
}
