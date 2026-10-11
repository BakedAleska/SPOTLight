"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ChevronDown, ClipboardCheck, Route, Search, Sparkles } from "lucide-react";
import { Panel } from "./Panel";
import { SeverityBadge, severityAccent } from "./SeverityBadge";
import type { Flag } from "@/lib/types";

/* Clicking a row opens it in place: every signal that fired, then the places
   an operator can go from here. The row is a disclosure, not a link, because
   there is no single "right" next page — review it, watch the mission that
   raised it, see what else touches the asset, or ask about it. */

/* A flag carries every rule that fired — often eight or nine. A closed row
   can't show eight bullets and still be scannable, so it shows the heaviest
   few and says how many it is holding back; opening the row shows them all.
   The backend score also accounts for priority and confidence. */
const REASONS_SHOWN = 2;

const OPTION =
  "inline-flex h-8 items-center gap-1.5 rounded border border-line bg-surface px-3 text-sm text-ink transition-colors hover:border-accent hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-accent";

function hasMission(flag: Flag) {
  return !!flag.missionId && flag.missionId !== "Unassigned";
}

function askAbout(flag: Flag) {
  const context = [flag.locationName, hasMission(flag) && `mission ${flag.missionId}`].filter(Boolean).join(", ");
  return `Why was "${flag.title}"${context ? ` (${context})` : ""} flagged, and what should I check first?`;
}

function QueueRow({
  flag,
  open,
  onToggle,
  onSelect,
}: {
  flag: Flag;
  open: boolean;
  onToggle: () => void;
  onSelect?: (flag: Flag) => void;
}) {
  const shown = open ? flag.reasons : flag.reasons.slice(0, REASONS_SHOWN);
  const hidden = flag.reasons.length - shown.length;
  const panelId = `flag-${flag.id}-options`;

  return (
    <div className={`group relative w-full border-b border-line px-4 py-3 pl-5 text-left transition-colors last:border-0 ${open ? "bg-surface-2" : "hover:bg-surface-2"}`}>
      {/* A severity-coloured spine, so the shape of the backlog is legible
          from across the room before any text is read. */}
      <span
        aria-hidden="true"
        className={`absolute inset-y-0 left-0 w-0.5 ${severityAccent(flag.severity)}`}
      />

      <div className="flex items-baseline gap-3">
        <SeverityBadge severity={flag.severity} />
        <span className="nums font-mono text-xs text-muted">
          score {flag.score}
        </span>
        <ChevronDown
          size={16}
          aria-hidden="true"
          className={`ml-auto shrink-0 self-center text-faint transition-transform group-hover:text-ink ${open ? "rotate-180" : ""}`}
        />
      </div>

      <p className="mt-1.5 leading-snug font-medium text-ink">
        {/* The ::after stretches this button over the closed row, so the whole
            row is the hit area. Once open it shrinks back to the title, or it
            would sit on top of the option links below. */}
        <button
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
            {reason.detail}
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
            <button type="button" onClick={() => onSelect(flag)} className={`${OPTION} border-accent bg-accent font-semibold text-on-accent hover:bg-accent-hover`}>
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
            <Sparkles size={14} aria-hidden="true" /> Ask the assistant
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
}: {
  flags: Flag[];
  total?: number;
  delay?: number;
  onSelect?: (flag: Flag) => void;
}) {
  const list = useRef<HTMLDivElement>(null);
  const [visibleHeight, setVisibleHeight] = useState<number>();
  const [openId, setOpenId] = useState<string | null>(null);

  useEffect(() => {
    const rows = Array.from(list.current?.children ?? []).slice(0, 2) as HTMLElement[];
    if (!rows.length) return;
    const measure = () => setVisibleHeight(rows.reduce((height, row) => height + row.offsetHeight, 0));
    measure();
    const observer = new ResizeObserver(measure);
    rows.forEach((row) => observer.observe(row));
    return () => observer.disconnect();
  }, [flags]);

  return (
    <Panel
      title="Needs attention"
      aside={total > flags.length ? `${flags.length} of ${total} open flags` : `${total} open flags`}
      bodyClassName=""
      className="h-full"
      delay={delay}
    >
      {flags.length === 0 ? (
        <p className="px-4 py-8 text-center text-sm text-muted">
          No open escalated flags need attention.
        </p>
      ) : (
        /* Fit exactly two complete rows, including wrapped text; remaining
           flags scroll here. */
        <div ref={list} className="max-h-96 overflow-y-auto" style={{ maxHeight: visibleHeight }}>
          {flags.map((flag) => (
            <QueueRow key={flag.id} flag={flag} open={openId === flag.id} onToggle={() => setOpenId(openId === flag.id ? null : flag.id)} onSelect={onSelect} />
          ))}
        </div>
      )}
    </Panel>
  );
}
