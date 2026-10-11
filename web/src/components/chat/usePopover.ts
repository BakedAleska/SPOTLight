"use client";

import { useEffect, useRef, useState } from "react";

/** Open/close state for a click-to-open popover: closes on outside pointer
    down and on Escape (returning focus to the trigger). */
export function usePopover<T extends HTMLElement = HTMLDivElement>() {
  const [open, setOpen] = useState(false);
  const root = useRef<T>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    function onPointer(event: PointerEvent) {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    }
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return { open, setOpen, root, trigger };
}

/** Arrow-key movement between the menu items inside `container`. */
export function moveMenuFocus(container: HTMLElement | null, event: React.KeyboardEvent) {
  if (!container || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const items = Array.from(container.querySelectorAll<HTMLElement>("[role^='menuitem']:not([disabled])"));
  if (items.length === 0) return;
  event.preventDefault();
  const index = items.indexOf(document.activeElement as HTMLElement);
  const next =
    event.key === "Home" ? 0
    : event.key === "End" ? items.length - 1
    : event.key === "ArrowDown" ? (index + 1) % items.length
    : (index - 1 + items.length) % items.length;
  items[next]?.focus();
}
