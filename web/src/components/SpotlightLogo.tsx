import Image from "next/image";
import { RadarBlips } from "./RadarBlips";

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
          <image href="/spotlight-mark.svg" x="37.474" y="23.805" width="723" height="586" />
        </g>
      </g>
      {blips && <RadarBlips />}
    </svg>
  );
}
