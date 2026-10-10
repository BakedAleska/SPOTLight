import type { CSSProperties } from "react";

// Irregular positions in the banner's exposed upper-left radar area. The
// banner and SVG clip these together with the artwork at every viewport size.
const RETURNS = [
  [440, 670], [730, 440], [1130, 530], [510, 1040],
  [850, 730], [1220, 860], [690, 1260], [1090, 1280],
  [350, 870], [960, 360], [610, 820], [1320, 620],
];
const HEAD = Math.atan2(609.22, 760.05);
const PERIOD = 12;

/** CSS shares the sweep's clock, so returns work before hydration too. */
export function RadarBlips() {
  return (
    <g className="spotlight-radar-returns" fill="#ff3b3b">
      {RETURNS.map(([x, y], index) => {
        const angle = Math.atan2(y - 1042.42, x - 1008);
        const phase = ((angle - HEAD + Math.PI * 2) % (Math.PI * 2)) / (Math.PI * 2);
        return <g key={index} className="spotlight-radar-blip"
          style={{ "--blip-delay": `${phase * PERIOD}s` } as CSSProperties}>
          <circle cx={x} cy={y} r="62" opacity="0.3" />
          <circle cx={x} cy={y} r="32" />
          <circle cx={x} cy={y} r="12" fill="#ffd3d3" />
        </g>;
      })}
    </g>
  );
}
