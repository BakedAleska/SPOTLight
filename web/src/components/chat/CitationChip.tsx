"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Source } from "@/lib/chat/types";
import { OriginLabel } from "./SourceMeta";

const WIDTH = 288;
/** Space kept between the preview and the viewport edge. */
const EDGE = 8;
/** Gap between the chip and the preview. */
const GAP = 6;

/** Where the preview goes: above the chip when it fits, else below, and
    always clamped inside the viewport on all four sides. */
export function placeTooltip(
  chip: { left: number; top: number; bottom: number; width: number },
  tip: { width: number; height: number },
  viewport: { width: number; height: number },
): { left: number; top: number } {
  const maxLeft = Math.max(EDGE, viewport.width - tip.width - EDGE);
  const left = Math.min(Math.max(EDGE, chip.left + chip.width / 2 - tip.width / 2), maxLeft);
  const above = chip.top - GAP - tip.height;
  const below = chip.bottom + GAP;
  const fitsAbove = above >= EDGE;
  const fitsBelow = below + tip.height <= viewport.height - EDGE;
  let top = fitsAbove ? above : fitsBelow ? below : Math.max(EDGE, viewport.height - EDGE - tip.height);
  top = Math.min(Math.max(EDGE, top), Math.max(EDGE, viewport.height - EDGE - tip.height));
  return { left, top };
}

/* A numbered chip for one cited source. Hover or keyboard focus shows the
   source's title and snippet; click opens it in the Sources panel.

   The preview is portaled to <body> and position:fixed, so neither the
   scrolling thread nor a table's overflow-x wrapper nor the chip group's
   nowrap can clip or stretch it. It is measured before it shows, then placed
   by placeTooltip. It closes on scroll, blur, mouse-out or Esc (WCAG 1.4.13:
   dismissible without moving focus). */
function CitationChip({ source, number, onOpen }: { source: Source; number: number; onOpen?: (id: string) => void }) {
  const ref = useRef<HTMLButtonElement>(null);
  const tipRef = useRef<HTMLSpanElement>(null);
  const tipId = useId();
  const [open, setOpen] = useState(false);

  // Measure the hidden preview, then place and reveal it before paint.
  useLayoutEffect(() => {
    const tip = tipRef.current;
    const chip = ref.current?.getBoundingClientRect();
    if (!open || !tip || !chip) return;
    const root = document.documentElement;
    const { left, top } = placeTooltip(chip, tip.getBoundingClientRect(), { width: root.clientWidth, height: root.clientHeight });
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
    tip.style.visibility = "visible";
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const hide = () => setOpen(false);
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") hide(); };
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <>
      <button
        ref={ref}
        type="button"
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => onOpen?.(source.id)}
        aria-label={`Source ${number}: ${source.title}`}
        aria-describedby={open ? tipId : undefined}
        className="nums inline-grid h-[1.15rem] min-w-[1.15rem] cursor-pointer place-items-center rounded-full border border-line bg-surface-2 px-1 align-[0.12em] font-mono text-[0.68rem] leading-none font-medium text-muted transition-colors hover:border-accent hover:bg-accent hover:text-on-accent focus-visible:border-accent"
      >
        {number}
      </button>
      {open && createPortal(
        <span
          ref={tipRef}
          id={tipId}
          role="tooltip"
          style={{ left: 0, top: 0, width: `min(${WIDTH}px, calc(100vw - ${EDGE * 2}px))`, visibility: "hidden" }}
          className="panel-shadow pointer-events-none fixed z-50 block rounded-lg border border-line bg-surface p-3 text-left text-xs leading-relaxed whitespace-normal text-ink shadow-lg [overflow-wrap:anywhere]"
        >
          <span className="flex items-center gap-2 text-[11px] text-muted">
            <span className="nums font-mono font-semibold text-accent-ink">[{number}]</span>
            <OriginLabel origin={source.origin} kind={source.kind} />
          </span>
          <span className="mt-1 block text-[13px] font-medium">{source.title}</span>
          {source.snippet && <span className="mt-1 line-clamp-4 block text-muted">{source.snippet}</span>}
        </span>,
        document.body,
      )}
    </>
  );
}

export function CitationGroup({ items, onOpen }: { items: { source: Source; number: number }[]; onOpen?: (id: string) => void }) {
  if (items.length === 0) return null;
  return (
    <span className="mx-0.5 inline-flex gap-0.5 whitespace-nowrap">
      {items.map(({ source, number }) => <CitationChip key={source.id} source={source} number={number} onOpen={onOpen} />)}
    </span>
  );
}
