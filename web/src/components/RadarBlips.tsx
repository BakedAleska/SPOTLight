"use client";

import { useEffect, useState } from "react";

/** Decorative returns, unrelated to real flags or asset locations. */
export function RadarBlips() {
  const [blip, setBlip] = useState<{ x: number; y: number } | null>(null);

  useEffect(() => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let next: ReturnType<typeof setTimeout>;
    let hide: ReturnType<typeof setTimeout>;
    function schedule() {
      next = setTimeout(() => {
        // Uniform area distribution within the radar, clear of its center.
        const angle = Math.random() * Math.PI * 2;
        const radius = Math.sqrt(180 ** 2 + Math.random() * (850 ** 2 - 180 ** 2));
        setBlip({ x: 1008 + Math.cos(angle) * radius, y: 1042.42 + Math.sin(angle) * radius });
        hide = setTimeout(() => setBlip(null), 1000);
        schedule();
      }, 1800 + Math.random() * 2800);
    }
    function reset() {
      clearTimeout(next);
      clearTimeout(hide);
      setBlip(null);
      if (!reducedMotion.matches) schedule();
    }
    reset();
    reducedMotion.addEventListener("change", reset);
    return () => {
      clearTimeout(next);
      clearTimeout(hide);
      reducedMotion.removeEventListener("change", reset);
    };
  }, []);

  if (!blip) return null;
  return (
    <g className="spotlight-radar-blip text-bad" fill="currentColor">
      <circle cx={blip.x} cy={blip.y} r="52" opacity="0.2" />
      <circle cx={blip.x} cy={blip.y} r="22" />
    </g>
  );
}
