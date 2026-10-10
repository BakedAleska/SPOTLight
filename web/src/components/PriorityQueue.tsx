import { ChevronRight } from "lucide-react";
import { Panel } from "./Panel";
import { SeverityBadge, severityAccent } from "./SeverityBadge";
import type { Flag } from "@/lib/types";

/* Each enabled row has a keyboard-accessible button with a full-row hit area.
   Without details, the same evidence remains available as a static list. */

/* A flag carries every rule that fired — often eight or nine. A row can't show eight bullets
   and still be scannable, so it shows the heaviest few and says how many it
   is holding back. The backend score also accounts for priority and confidence. */
const REASONS_SHOWN = 4;

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
        /* Bounded, not h-full: the real queue is dozens of rows deep, and an
           unbounded list would set the height of the whole dashboard grid and
           drag the chart beside it down with it. The list scrolls inside the
           panel instead. */
        <div className="max-h-[min(70vh,44rem)] overflow-y-auto">
          {flags.map((flag) => (
            <QueueRow key={flag.id} flag={flag} onSelect={onSelect} />
          ))}
        </div>
      )}
    </Panel>
  );
}
