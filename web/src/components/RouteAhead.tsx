/** The route still ahead of SPOT: dashes on a dark halo so they read over the blueprint grid, flowing toward the
    destination. */
export function RouteAhead({ d }: { d: string }) {
  if (!d) return null;
  return <g aria-hidden="true" fill="none" strokeLinecap="round" strokeLinejoin="round">
    <path d={d} stroke="black" strokeOpacity="0.35" strokeWidth="7" />
    <path d={d} className="route-ahead" stroke="white" strokeWidth="3" strokeDasharray="6 8" />
  </g>;
}

/** The map key as swatches rather than a sentence: the visited route, the next leg, then one entry per stop marker. */
export function MapLegend({ marks }: { marks: { glyph: string; tone: "bad" | "ok"; label: string }[] }) {
  return <ul className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted" aria-label="Map key">
    <li className="flex items-center gap-1.5"><span className="h-1 w-5 rounded-full bg-accent-ink" />Visited</li>
    <li className="flex items-center gap-1.5"><span className="w-5 border-t-2 border-dashed border-muted" />Next leg</li>
    {marks.map(m => <li key={m.label} className="flex items-center gap-1.5">
      <span className={`grid size-3.5 place-items-center rounded-full text-[9px] font-bold text-white ${m.tone === "bad" ? "bg-bad" : "bg-ok"}`}>{m.glyph}</span>{m.label}
    </li>)}
  </ul>;
}
