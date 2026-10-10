import Image from "next/image";
import { RadarBlips } from "./RadarBlips";

/** Angular opacity ramp behind the arrow; every slice shares its rotation. */
function RadarTrail() {
  const head = Math.atan2(609.22, 760.05);
  const span = Math.PI / 2;
  const slices = 72;
  const radius = 974;
  return <g className="spotlight-radar-trail text-accent" fill="currentColor">
    {Array.from({ length: slices }, (_, index) => {
      const start = head - span + index * span / slices;
      const end = start + span / slices + 0.0005;
      const point = (angle: number) => `${radius * Math.cos(angle)} ${radius * Math.sin(angle)}`;
      return <path key={index} d={`M 0 0 L ${point(start)} A ${radius} ${radius} 0 0 1 ${point(end)} Z`}
        opacity={0.35 * (index / (slices - 1)) ** 1.8} />;
    })}
  </g>;
}

/** The supplied artwork shares one coordinate system; the cropped mark is
 * offset back into it before rotating about the radar's actual center. */
export function SpotlightLogo({ animated = false, blips = false, small = false, className = "" }: {
  animated?: boolean;
  blips?: boolean;
  small?: boolean;
  className?: string;
}) {
  if (!animated) {
    return <Image src={small ? "/spotlight-logo-small.svg" : "/spotlight-logo.svg"} alt="" aria-hidden="true" width={2016} height={2051} className={className} />;
  }

  return (
    <svg viewBox="0 0 2016 2051" fill="none" aria-hidden="true" focusable="false" className={className}>
      <image href={small ? "/spotlight-logo-small.svg" : "/spotlight-arrow.svg"} width="2016" height="2051" />
      <g transform="translate(1008 1042.42)">
        <g className="spotlight-radar-sweep">
          <RadarTrail />
          <image href="/spotlight-mark.svg" x="37.474" y="23.805" width="723" height="586" />
        </g>
      </g>
      {blips && <RadarBlips />}
    </svg>
  );
}
