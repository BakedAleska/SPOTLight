import type { CSSProperties } from "react";

// Irregular positions in the banner's exposed upper-left radar area. The
// banner and SVG clip these together with the artwork at every viewport size.
const RETURNS = [
  [390, 710], [1020, 350], [1290, 940], [630, 1300],
];
const HEAD = Math.atan2(609.22, 760.05);
const PERIOD = 12;

/** CSS shares the sweep's clock, so returns work before hydration too. */
export function RadarBlips() {
  return (
    <g className="spotlight-radar-returns" fill="#cf6262">
      {RETURNS.map(([x, y], index) => {
        const angle = Math.atan2(y - 1042.42, x - 1008);
        const phase = ((angle - HEAD + Math.PI * 2) % (Math.PI * 2)) / (Math.PI * 2);
        return <g key={index} className="spotlight-radar-blip"
          style={{ "--blip-delay": `${phase * PERIOD}s` } as CSSProperties}>
          <circle cx={x} cy={y} r="62" opacity="0.3" />
          <circle cx={x} cy={y} r="32" />
        </g>;
      })}
    </g>
  );
}
