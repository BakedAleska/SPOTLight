/** The route still ahead of SPOT: dashes on a dark halo so they read over the blueprint grid, flowing toward the
    destination. */
export function RouteAhead({ d }: { d: string }) {
  if (!d) return null;
  return <g aria-hidden="true" fill="none" strokeLinecap="round" strokeLinejoin="round">
    <path d={d} stroke="black" strokeOpacity="0.35" strokeWidth="7" />
    <path d={d} className="route-ahead" stroke="white" strokeWidth="3" strokeDasharray="6 8" />
  </g>;
}
