"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";

type Blip = { id: number; x: number; y: number; lifetime: number; expires: number };
const TRAIL_DEGREES = 90;
const ARROW_ANGLE = Math.atan2(609.22, 760.05);

/** Test the whole dot against the viewport and every clipping ancestor. */
function visible(svg: SVGSVGElement, x: number, y: number) {
  const matrix = svg.getScreenCTM();
  if (!matrix) return false;
  const point = new DOMPoint(x, y).matrixTransform(matrix);
  const margin = 52 * Math.hypot(matrix.a, matrix.b);
  let left = 0, top = 0, right = window.innerWidth, bottom = window.innerHeight;
  for (let element: Element | null = svg; element; element = element.parentElement) {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    if (element === svg || /hidden|clip|auto|scroll/.test(style.overflowX)) {
      left = Math.max(left, rect.left); right = Math.min(right, rect.right);
    }
    if (element === svg || /hidden|clip|auto|scroll/.test(style.overflowY)) {
      top = Math.max(top, rect.top); bottom = Math.min(bottom, rect.bottom);
    }
  }
  return point.x - margin >= left && point.x + margin <= right &&
    point.y - margin >= top && point.y + margin <= bottom;
}

/** Decorative returns appear under the sweep and live until its tail passes. */
export function RadarBlips() {
  const layer = useRef<SVGGElement>(null);
  const [blips, setBlips] = useState<Blip[]>([]);

  useEffect(() => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let timer: ReturnType<typeof setInterval>;
    let id = 0;
    function tick() {
      const now = Date.now();
      const svg = layer.current?.ownerSVGElement;
      const sweep = svg?.querySelector<SVGGElement>(".spotlight-radar-sweep");
      let fresh: Blip | null = null;
      if (svg && sweep && !document.hidden && Math.random() < 0.15) {
        const style = getComputedStyle(sweep);
        const matrix = new DOMMatrixReadOnly(style.transform);
        const angle = Math.atan2(matrix.b, matrix.a) + ARROW_ANGLE;
        const lifetime = parseFloat(style.animationDuration) * 1000 * TRAIL_DEGREES / 360;
        for (let attempt = 0; attempt < 24; attempt++) {
          const radius = Math.sqrt(180 ** 2 + Math.random() * (900 ** 2 - 180 ** 2));
          const x = 1008 + Math.cos(angle) * radius;
          const y = 1042.42 + Math.sin(angle) * radius;
          if (visible(svg, x, y)) {
            fresh = { id: ++id, x, y, lifetime, expires: now + lifetime + 400 };
            break;
          }
        }
      }
      setBlips((current) => {
        const remaining = current.filter((blip) => blip.expires > now);
        return fresh ? [...remaining, fresh] : remaining.length === current.length ? current : remaining;
      });
    }
    function reset() {
      clearInterval(timer);
      setBlips([]);
      if (!reducedMotion.matches) timer = setInterval(tick, 200);
    }
    reset();
    reducedMotion.addEventListener("change", reset);
    return () => {
      clearInterval(timer);
      reducedMotion.removeEventListener("change", reset);
    };
  }, []);

  return (
    <g ref={layer} className="text-bad" fill="currentColor">
      {blips.map((blip) => (
        <g key={blip.id} className="spotlight-radar-blip"
          style={{ "--blip-life": `${blip.lifetime}ms` } as CSSProperties}>
          <circle cx={blip.x} cy={blip.y} r="52" opacity="0.2" />
          <circle cx={blip.x} cy={blip.y} r="22" />
        </g>
      ))}
    </g>
  );
}
