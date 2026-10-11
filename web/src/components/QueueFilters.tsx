"use client";

import type { Flag } from "@/lib/types";
import { filterOptions, type QueueFilter } from "@/lib/queue-filter";

/* Severity pills for the "Needs attention" queue, sized to sit in its panel
   header. "All" carries the queue's total, so the header needs no separate
   count; the status line says how many of the queue match. */

const PILL = "h-6 rounded px-2 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-accent";
const PILL_ON = `${PILL} bg-surface-2 font-semibold text-ink`;
const PILL_OFF = `${PILL} text-muted hover:text-ink`;

const SEVERITY_LABEL = { CRITICAL: "Critical", HIGH: "High", MEDIUM: "Medium", LOW: "Low" } as const;

export function QueueFilters({ flags, total, value, onChange, shown }: {
  flags: Flag[];
  /** Open flags in the backend, which can exceed the flags loaded. */
  total: number;
  value: QueueFilter;
  onChange: (next: QueueFilter) => void;
  shown: number;
}) {
  const options = filterOptions(flags);
  const set = (patch: Partial<QueueFilter>) => onChange({ ...value, ...patch });

  return (
    <div role="group" aria-label="Filter the queue by severity" className="flex items-center gap-0.5 rounded border border-line bg-surface p-px">
      <button type="button" aria-pressed={value.severity === "ALL"} onClick={() => set({ severity: "ALL" })}
        className={value.severity === "ALL" ? PILL_ON : PILL_OFF}>
        All <span className="nums font-mono text-faint">{total}</span>
      </button>
      {options.severities.map((s) => (
        <button key={s.value} type="button" aria-pressed={value.severity === s.value} onClick={() => set({ severity: s.value })}
          className={value.severity === s.value ? PILL_ON : PILL_OFF}>
          {SEVERITY_LABEL[s.value]} <span className="nums font-mono text-faint">{s.n}</span>
        </button>
      ))}
      <p role="status" className="sr-only">
        {value.severity === "ALL" ? `${total} open flags` : `${shown} of ${flags.length} flags match`}
      </p>
    </div>
  );
}
