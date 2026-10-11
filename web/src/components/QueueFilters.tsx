"use client";

import { Search, X } from "lucide-react";
import type { Flag } from "@/lib/types";
import { filterOptions, isFiltered, NO_FILTER, type QueueFilter } from "@/lib/queue-filter";

/* Search and filters for the "Needs attention" queue on the overview. Filters
   narrow the open flags in place; the count says how many of the queue match. */

const FIELD =
  "h-9 rounded border border-line-strong/60 bg-surface-3 text-sm text-ink transition-colors hover:border-ink focus:border-focus focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus/30";
const PILL = "h-7 rounded px-2.5 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-accent";
const PILL_ON = `${PILL} bg-surface-2 font-semibold text-ink`;
const PILL_OFF = `${PILL} text-muted hover:text-ink`;

const SEVERITY_LABEL = { CRITICAL: "Critical", HIGH: "High", MEDIUM: "Medium", LOW: "Low" } as const;

export function QueueFilters({ flags, value, onChange, shown }: {
  flags: Flag[];
  value: QueueFilter;
  onChange: (next: QueueFilter) => void;
  shown: number;
}) {
  const options = filterOptions(flags);
  const set = (patch: Partial<QueueFilter>) => onChange({ ...value, ...patch });

  return (
    <div role="search" aria-label="Filter the priority queue" className="flex flex-wrap items-center gap-2 px-1">
      <label className="relative min-w-56 flex-1 sm:max-w-md">
        <span className="sr-only">Search open flags</span>
        <Search size={15} aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-muted" />
        <input
          type="search"
          value={value.q}
          onChange={(event) => set({ q: event.target.value })}
          placeholder="Search flags, assets, waypoints, missions…"
          className={`${FIELD} w-full pr-3 pl-8 placeholder:text-muted`}
        />
      </label>

      <div role="group" aria-label="Severity" className="flex items-center gap-0.5 rounded border border-line bg-surface p-0.5">
        <button type="button" aria-pressed={value.severity === "ALL"} onClick={() => set({ severity: "ALL" })}
          className={value.severity === "ALL" ? PILL_ON : PILL_OFF}>All</button>
        {options.severities.map((s) => (
          <button key={s.value} type="button" aria-pressed={value.severity === s.value} onClick={() => set({ severity: s.value })}
            className={value.severity === s.value ? PILL_ON : PILL_OFF}>
            {SEVERITY_LABEL[s.value]} <span className="nums font-mono text-faint">{s.n}</span>
          </button>
        ))}
      </div>

      <label className="sr-only" htmlFor="queue-mission">Mission</label>
      <select id="queue-mission" value={value.mission} onChange={(event) => set({ mission: event.target.value })} className={`${FIELD} px-2`}>
        <option value="">All missions</option>
        {options.missions.map((m) => <option key={m.value} value={m.value}>{m.value} ({m.n})</option>)}
      </select>

      <label className="sr-only" htmlFor="queue-location">Waypoint</label>
      <select id="queue-location" value={value.location} onChange={(event) => set({ location: event.target.value })} className={`${FIELD} px-2`}>
        <option value="">All waypoints</option>
        {options.locations.map((l) => <option key={l.value} value={l.value}>{l.value} ({l.n})</option>)}
      </select>

      {isFiltered(value) && (
        <button type="button" onClick={() => onChange(NO_FILTER)}
          className="inline-flex h-9 items-center gap-1 rounded px-2 text-sm text-muted transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-accent">
          <X size={14} aria-hidden="true" /> Clear
        </button>
      )}

      <p role="status" className="sr-only">
        {isFiltered(value) ? `${shown} of ${flags.length} flags match` : `${flags.length} open flags`}
      </p>
    </div>
  );
}
