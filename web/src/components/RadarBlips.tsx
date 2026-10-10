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
          style={{ "--blip-delay": `${phase * PERIOD}s`, "--symbol-period": `${PERIOD * (index % 2 + 2)}s`, "--symbol-delay": `${phase * PERIOD - index * PERIOD}s` } as CSSProperties}>
          <g className="spotlight-radar-dot">
            <circle cx={x} cy={y} r="62" opacity="0.3" />
            <circle cx={x} cy={y} r="32" />
          </g>
          <g className="spotlight-radar-caution" fill="#ffca44" transform={`translate(${x} ${y})`}>
            <path d="M 0 -48 L 44 34 Q 47 40 39 40 H -39 Q -47 40 -44 34 Z"
              fillOpacity="0.18" stroke="#ffca44" strokeWidth="7" strokeLinejoin="round" />
            <path d="M 0 -18 V 10" fill="none" stroke="#ffca44" strokeWidth="8" strokeLinecap="round" />
            <circle cy="25" r="5" />
          </g>
        </g>;
      })}
    </g>
  );
}
