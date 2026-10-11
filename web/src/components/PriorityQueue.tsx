"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ChevronDown, ClipboardCheck, Route, Search, Sparkles } from "lucide-react";
import { Panel } from "./Panel";
import { SeverityBadge, severityAccent } from "./SeverityBadge";
import type { Flag } from "@/lib/types";
import { displayText } from "@/lib/display";
import { openFirst } from "@/lib/overview";

/* Clicking a row opens it: every signal that fired, then the places an
   operator can go from here. The row is a disclosure, not a link, because
   there is no single "right" next page — review it, watch the mission that
   raised it, see what else touches the asset, or ask about it.

   The opened row is lifted to the head of the list, above everything else
   that needs attention, and the list scrolls up to it. Closing it drops it
   back into its place in the queue's own order. */

/* A flag carries every rule that fired — often eight or nine. A closed row
   can't show eight bullets and still be scannable, so it shows the heaviest
   few and says how many it is holding back; opening the row shows them all.
   The backend score also accounts for priority and confidence. */
const REASONS_SHOWN = 2;

/* Colours live only in the two variants: Tailwind orders same-property
   utilities by its own stylesheet order, not class order, so bg-accent added
   on top of a bg-surface base lost to it and left on-accent (near-black) text
   on the dark surface. */
const OPTION_BASE =
  "inline-flex h-8 items-center gap-1.5 rounded border px-3 text-sm transition-colors focus-visible:outline-2 focus-visible:outline-accent";
const OPTION = `${OPTION_BASE} border-line bg-surface text-ink hover:border-accent hover:bg-surface-2`;
const OPTION_PRIMARY = `${OPTION_BASE} border-accent bg-accent font-semibold text-on-accent hover:bg-accent-hover`;

function hasMission(flag: Flag) {
  return !!flag.missionId && flag.missionId !== "Unassigned";
}

function askAbout(flag: Flag) {
  const context = [flag.locationName, hasMission(flag) && `mission ${flag.missionId}`].filter(Boolean).join(", ");
  return `Why was "${flag.title}"${context ? ` (${context})` : ""} flagged, and what should I check first?`;
}


function reducedMotion() {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/* Closed rows share one hairline list. The open row leaves it: it becomes a
   card at the head of the list, ringed in the accent, so it reads as "the one
   in hand" rather than as row three of fifty. Background and border colours
   live only in these two variants, never one on top of the other (see
   OPTION_BASE). */
const ROW_CLOSED = "border-b border-line transition-colors last:border-0 hover:bg-surface-2";
const ROW_OPEN = "anim-rise m-2 rounded-panel border border-accent bg-accent/10 panel-shadow";

function QueueRow({
  flag,
  open,
  position,
  onToggle,
  onSelect,
}: {
  flag: Flag;
  open: boolean;
  /** 1-based place in the queue's own order, shown while the row is lifted out of it. */
  position: number;
  onToggle: () => void;
  onSelect?: (flag: Flag) => void;
}) {
  const shown = open ? flag.reasons : flag.reasons.slice(0, REASONS_SHOWN);
  const hidden = flag.reasons.length - shown.length;
  const panelId = `flag-${flag.id}-options`;
  const title = useRef<HTMLButtonElement>(null);

  return (
    <div
      data-flag-id={flag.id}
      onKeyDown={open ? (event) => {
        if (event.key !== "Escape") return;
        event.stopPropagation();
        onToggle();
        title.current?.focus();
      } : undefined}
      className={`group relative px-4 py-3 pl-5 text-left ${open ? ROW_OPEN : ROW_CLOSED}`}
    >
      {/* A severity-coloured spine, so the shape of the backlog is legible
          from across the room before any text is read. */}
      <span
        aria-hidden="true"
        className={`absolute inset-y-0 left-0 ${open ? "w-1 rounded-l-[3px]" : "w-0.5"} ${severityAccent(flag.severity)}`}
      />

      <div className="flex items-baseline gap-3">
        <SeverityBadge severity={flag.severity} />
        <span className="nums font-mono text-xs text-muted">
          Confidence {Math.round(flag.score)}%
        </span>
        {open && <span className="nums text-xs text-muted">Queue position {position}</span>}
        {open ? (
          <button
            type="button"
            onClick={onToggle}
            aria-label={`Close ${flag.title}`}
            title="Close (Esc)"
            className="-my-1 ml-auto shrink-0 self-center rounded p-1 text-muted transition-colors hover:bg-surface-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
          >
            <ChevronDown size={16} aria-hidden="true" className="rotate-180" />
          </button>
        ) : (
          <ChevronDown
            size={16}
            aria-hidden="true"
            className="ml-auto shrink-0 self-center text-faint transition-transform group-hover:text-ink"
          />
        )}
      </div>

      <p className="mt-1.5 leading-snug font-medium text-ink">
        {/* The ::after stretches this button over the closed row, so the whole
            row is the hit area. Once open it shrinks back to the title, or it
            would sit on top of the option links below. */}
        <button
          ref={title}
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={panelId}
          className={`cursor-pointer text-left hover:underline focus-visible:outline-2 focus-visible:outline-accent ${open ? "" : "after:absolute after:inset-0"}`}
        >
          {flag.title}
        </button>
      </p>
      <p className="mt-0.5 text-sm text-muted">
        {flag.assetName} · {flag.locationName}
        {hasMission(flag) && <> · mission <span className="font-mono">{flag.missionId}</span></>}
      </p>

      <ul className="mt-2 space-y-1">
        {shown.map((reason) => (
          <li
            key={reason.rule}
            className="flex gap-2 text-sm leading-snug text-muted before:text-faint before:content-['•']"
          >
            {displayText(reason.detail)}
          </li>
        ))}
        {hidden > 0 && (
          <li className="pl-4 text-xs text-faint">
            +{hidden} more signal{hidden > 1 ? "s" : ""}
          </li>
        )}
      </ul>

      {open && (
        <nav id={panelId} aria-label={`Next steps for ${flag.title}`} className="relative mt-3 flex flex-wrap gap-2">
          {onSelect && (
            <button type="button" onClick={() => onSelect(flag)} className={OPTION_PRIMARY}>
              <ClipboardCheck size={14} aria-hidden="true" /> Review &amp; act
            </button>
          )}
          {hasMission(flag) && (
            <Link href={`/missions/replay?mission=${encodeURIComponent(flag.missionId)}`} className={OPTION}>
              <Route size={14} aria-hidden="true" /> Replay mission
            </Link>
          )}
          {flag.assetName && (
            <Link href={`/search?q=${encodeURIComponent(flag.assetName)}`} className={OPTION}>
              <Search size={14} aria-hidden="true" /> More on this asset
            </Link>
          )}
          <Link href={`/assistant?ask=${encodeURIComponent(askAbout(flag))}`} className={OPTION}>
            <Sparkles size={14} aria-hidden="true" /> Ask SPOTbot
          </Link>
        </nav>
      )}
    </div>
  );
}

export function PriorityQueue({
  flags,
  total = flags.length,
  delay,
  onSelect,
  openId: controlledOpenId,
  onOpenChange,
  emptyMessage = "No open escalated flags need attention.",
  filters,
}: {
  flags: Flag[];
  total?: number;
  delay?: number;
  onSelect?: (flag: Flag) => void;
  /** The opened row, when the parent owns it (the Overview's analytics follow it). */
  openId?: string | null;
  onOpenChange?: (id: string | null) => void;
  /** Shown when the list is empty, e.g. when filters match nothing. */
  emptyMessage?: string;
  /** Header controls (the Overview's severity pills), in place of the count. */
  filters?: React.ReactNode;
}) {
  const list = useRef<HTMLDivElement>(null);
  const [ownOpenId, setOwnOpenId] = useState<string | null>(null);
  const openId = controlledOpenId === undefined ? ownOpenId : controlledOpenId;
  const setOpenId = onOpenChange ?? setOwnOpenId;
  const previous = useRef<string | null>(null);

  // Queue order is the backend's; only the open row is lifted out of it.
  const ordered = openFirst(flags, openId);
  const position = new Map(flags.map((flag, i) => [flag.id, i + 1]));
  const lifted = !!openId && ordered[0]?.id === openId;

  /* Opening lifts the row to the head of the list, so the list goes to the
     top to show it. Closing puts the row back in its place, and the list
     follows it there, which is also where the operator was reading. */
  useEffect(() => {
    const el = list.current;
    const closed = previous.current;
    previous.current = openId;
    if (!el || openId === closed) return;
    const behavior: ScrollBehavior = reducedMotion() ? "auto" : "smooth";
    if (openId) {
      el.scrollTo({ top: 0, behavior });
      // And if the page has been scrolled past the panel's head, bring it back.
      const page = el.closest("main")?.getBoundingClientRect().top ?? 0;
      if (el.getBoundingClientRect().top < page) el.closest("section")?.scrollIntoView({ block: "start", behavior });
      return;
    }
    const row = closed ? el.querySelector<HTMLElement>(`[data-flag-id="${CSS.escape(closed)}"]`) : null;
    if (!row) return;
    const box = el.getBoundingClientRect();
    const at = row.getBoundingClientRect();
    if (at.top < box.top || at.bottom > box.bottom) el.scrollTo({ top: el.scrollTop + at.top - box.top, behavior });
  }, [openId]);

  return (
    <Panel
      title="Needs attention"
      aside={filters ?? (total > flags.length ? `${flags.length} of ${total} open flags` : `${total} open flags`)}
      toolbar={!!filters}
      bodyClassName=""
      className="h-full"
      delay={delay}
    >
      {flags.length === 0 ? (
        <p className="px-4 py-8 text-center text-sm text-muted">
          {emptyMessage}
        </p>
      ) : (
        /* Tall enough to work through: the list fills the viewport under the
           banner and filters (never under 28rem), and scrolls inside the
           panel so the analytics beside it stay put. */
        <div ref={list} className="max-h-[max(28rem,calc(100dvh-14rem))] overflow-y-auto overscroll-contain">
          {ordered.map((flag, i) => (
            <Fragment key={flag.id}>
              <QueueRow
                flag={flag}
                open={openId === flag.id}
                position={position.get(flag.id) ?? i + 1}
                onToggle={() => setOpenId(openId === flag.id ? null : flag.id)}
                onSelect={onSelect}
              />
              {lifted && i === 0 && ordered.length > 1 && (
                <p className="border-b border-line px-5 pt-2 pb-1.5 text-xs text-faint">Rest of the queue</p>
              )}
            </Fragment>
          ))}
        </div>
      )}
    </Panel>
  );
}
