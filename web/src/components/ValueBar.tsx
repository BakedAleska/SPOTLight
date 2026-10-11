"use client";

import { Rectangle, useXAxisInverseScale, useYAxisInverseScale } from "recharts";

/* A bar that carries its own value label. Recharts holds a Bar's `label` back
   until the entrance animation ends, so the numbers popped in after the bars
   had grown. Drawn inside the shape instead, the label is there from the
   first frame, rides on the bar's end as it grows, and reads the value off
   the axis at that end, so it counts up with the bar and lands on the real
   number. Use it as `shape={valueBar({ ... })}` in place of `label`. */

type Position = "top" | "right";

interface LabelOptions {
  position: Position;
  format?: (value: number) => string;
  /** Gap between the bar's end and the text, in px. */
  offset?: number;
  fontSize?: number;
  hideZero?: boolean;
}

interface ShapeProps {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  value?: unknown;
  [key: string]: unknown;
}

/** Decimal places in the bar's final value, so the count-up never shows more precision than the data has. */
function decimals(value: number) {
  const text = String(value);
  const dot = text.indexOf(".");
  return dot < 0 ? 0 : text.length - dot - 1;
}

function ValueBarShape({ label, ...props }: ShapeProps & { label: LabelOptions }) {
  const invertX = useXAxisInverseScale();
  const invertY = useYAxisInverseScale();
  const { x = 0, y = 0, width = 0, height = 0 } = props;
  const final = Number(Array.isArray(props.value) ? props.value[1] : props.value);
  const { position, format = String, offset = 6, fontSize = 10, hideZero = false } = label;

  const live = position === "top" ? invertY?.(y) : invertX?.(x + width);
  const places = Number.isFinite(final) ? decimals(final) : 0;
  const value = Number.isFinite(Number(live)) ? Math.max(0, Number(Number(live).toFixed(places))) : final;
  const show = Number.isFinite(final) && !(hideZero && final === 0);

  return (
    <g>
      <Rectangle {...(props as React.ComponentProps<typeof Rectangle>)} />
      {show && (
        <text
          x={position === "top" ? x + width / 2 : x + width + offset}
          y={position === "top" ? y - offset : y + height / 2}
          textAnchor={position === "top" ? "middle" : "start"}
          dominantBaseline={position === "top" ? "auto" : "central"}
          fill="var(--c-muted)"
          // A halo in the panel colour keeps the number legible where an
          // error bar's whisker runs through it.
          stroke="var(--c-surface)"
          strokeWidth={3}
          paintOrder="stroke"
          fontSize={fontSize}
          className="nums pointer-events-none"
        >
          {format(value)}
        </text>
      )}
    </g>
  );
}

export function valueBar(label: LabelOptions) {
  return function ValueBar(props: unknown) {
    return <ValueBarShape {...(props as ShapeProps)} label={label} />;
  };
}
