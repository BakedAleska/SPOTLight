import { useId } from "react";

/** The route still ahead of SPOT: dashes on a dark halo so they read over the blueprint grid, flowing toward the
    destination and ending in an arrowhead at it. */
export function RouteAhead({ d }: { d: string }) {
  const arrow = useId();
  if (!d) return null;
  return <g aria-hidden="true" fill="none" strokeLinecap="round" strokeLinejoin="round">
    <defs><marker id={arrow} viewBox="0 0 10 10" refX="7" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M1,1 L9,5 L1,9 Z" fill="white" stroke="black" strokeOpacity="0.5" strokeWidth="1" strokeLinejoin="round" /></marker></defs>
    <path d={d} stroke="black" strokeOpacity="0.35" strokeWidth="7" />
    <path d={d} className="route-ahead" stroke="white" strokeWidth="3" strokeDasharray="6 8" markerEnd={`url(#${arrow})`} />
  </g>;
}
