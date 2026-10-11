"use client";

import { useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";

/* Panel's collapsible variant: the same chrome, plus a chevron that folds the
   body away. Lives in its own client module so Panel stays a Server Component
   everywhere it isn't asked to collapse.

   The body is unmounted rather than hidden while folded, so a chart inside
   re-measures from scratch when it comes back instead of reading a 0px box.
   The choice is remembered per panel in localStorage — a convenience only,
   so a blocked or empty store just means every panel starts open. */
export function CollapsiblePanel({
  id,
  title,
  aside,
  children,
  className,
  bodyClassName,
  style,
}: {
  id: string;
  title: string;
  aside?: React.ReactNode;
  children: React.ReactNode;
  className: string;
  bodyClassName: string;
  style?: React.CSSProperties;
}) {
  const key = `spotlight:collapsed:${id}`;
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    try {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- localStorage is only readable after hydration
      if (localStorage.getItem(key) === "1") setCollapsed(true);
    } catch { /* storage unavailable: start open */ }
  }, [key]);

  const toggle = () => {
    setCollapsed((was) => {
      try {
        if (was) localStorage.removeItem(key);
        else localStorage.setItem(key, "1");
      } catch { /* not remembered, still toggles */ }
      return !was;
    });
  };

  return (
    <section
      aria-labelledby={id}
      style={style}
      // A folded panel must not be stretched to its row partner's height, or
      // it reads as an empty box rather than a closed one.
      className={`anim-float panel-shadow flex flex-col overflow-hidden rounded-panel border border-line bg-surface ${collapsed ? "self-start" : ""} ${className}`}
    >
      <header className={`flex h-9 shrink-0 items-center gap-2 px-2 pr-4 ${collapsed ? "" : "border-b border-line"}`}>
        <button
          type="button"
          onClick={toggle}
          aria-expanded={!collapsed}
          aria-controls={`${id}-body`}
          aria-label={`${collapsed ? "Expand" : "Collapse"} ${title}`}
          className="grid size-6 shrink-0 place-items-center rounded text-muted transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <ChevronDown size={15} className={`transition-transform duration-150 ${collapsed ? "-rotate-90" : ""}`} />
        </button>
        <h2 id={id} className="truncate text-sm font-medium text-ink">
          <button type="button" onClick={toggle} className="truncate text-left" tabIndex={-1}>{title}</button>
        </h2>
        {aside && <div className="nums ml-auto shrink-0 text-xs text-muted">{aside}</div>}
      </header>
      {!collapsed && (
        <div id={`${id}-body`} className={`flex min-h-0 flex-1 flex-col ${bodyClassName}`}>
          {children}
        </div>
      )}
    </section>
  );
}
