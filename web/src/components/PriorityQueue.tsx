"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronRight } from "lucide-react";
import { Panel } from "./Panel";
import { SeverityBadge, severityAccent } from "./SeverityBadge";
import type { Flag } from "@/lib/types";

/* Each enabled row has a keyboard-accessible button with a full-row hit area.
   Without details, the same evidence remains available as a static list. */

/* A flag carries every rule that fired — often eight or nine. A row can't show eight bullets
   and still be scannable, so it shows the heaviest few and says how many it
   is holding back. The backend score also accounts for priority and confidence. */
const REASONS_SHOWN = 2;

function QueueRow({ flag, onSelect }: { flag: Flag; onSelect?: (flag: Flag) => void }) {
  const shown = flag.reasons.slice(0, REASONS_SHOWN);
  const hidden = flag.reasons.length - shown.length;

  return (
    <div
      className="group relative w-full border-b border-line px-4 py-3 pl-5 text-left transition-colors last:border-0"
    >
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
        {onSelect && <ChevronRight
          size={16}
          aria-hidden="true"
          className="ml-auto shrink-0 self-center text-faint transition-colors group-hover:text-ink"
        />}
      </div>

      <p className="mt-1.5 leading-snug font-medium text-ink">
        {onSelect ? <button type="button" onClick={() => onSelect(flag)} className="cursor-pointer text-left after:absolute after:inset-0 hover:underline focus-visible:outline-2 focus-visible:outline-accent">{flag.title}</button> : flag.title}
      </p>
      <p className="mt-0.5 text-sm text-muted">
        {flag.assetName} · {flag.locationName}
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
            <QueueRow key={flag.id} flag={flag} onSelect={onSelect} />
          ))}
        </div>
      )}
    </Panel>
  );
}
