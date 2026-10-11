"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";

/* Panel's collapsible variant: the same chrome, plus a chevron that folds the
   body away. Lives in its own client module so Panel stays a Server Component
   everywhere it isn't asked to collapse.

   MOTION. Folding animates the panel's height from whatever it is laid out at
   to the header's, with the body fading out over it; unfolding does the
   reverse, measured after the body is back in the layout (FLIP), so it lands
   on the real height whether the panel is sized by its content or stretched
   to a row partner. Only height ever moves — never width — so a full-width
   panel stays full-width while folded.

   The body is unmounted once folded, so a chart inside re-mounts on unfold and
   plays its own entrance animation again.

   The choice is remembered per panel in localStorage — a convenience only,
   so a blocked or empty store just means every panel starts open. */

const DURATION = 460;
const EASE = "cubic-bezier(0.65, 0, 0.35, 1)";   // ease-in-out: the motion reads across its whole length

function reducedMotion() {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

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
  /** True while folding, so the chevron turns with the motion, not after it. */
  const [folding, setFolding] = useState(false);
  const panel = useRef<HTMLElement>(null);
  const header = useRef<HTMLElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const running = useRef<Animation | null>(null);
  /** Height to unfold from, set by the click and consumed after the commit. */
  const expandFrom = useRef<number | null>(null);

  useEffect(() => {
    try {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- localStorage is only readable after hydration
      if (localStorage.getItem(key) === "1") setCollapsed(true);
    } catch { /* storage unavailable: start open */ }
  }, [key]);

  const remember = (folded: boolean) => {
    try {
      if (folded) localStorage.setItem(key, "1");
      else localStorage.removeItem(key);
    } catch { /* not remembered, still toggles */ }
  };

  // Unfold: the body is mounted again by now, so the panel's laid-out height
  // is the target. Animate up to it from the folded height.
  useLayoutEffect(() => {
    const el = panel.current;
    const from = expandFrom.current;
    expandFrom.current = null;
    if (!el || from === null || collapsed) return;
    const to = el.getBoundingClientRect().height;
    running.current = el.animate([{ height: `${from}px` }, { height: `${to}px` }], { duration: DURATION, easing: EASE });
    body.current?.animate(
      [{ opacity: 0, transform: "translateY(-6px)" }, { opacity: 1, transform: "none" }],
      { duration: DURATION, easing: EASE, delay: 60, fill: "backwards" },
    );
  }, [collapsed]);

  const toggle = () => {
    const el = panel.current;
    const from = el?.getBoundingClientRect().height ?? 0;
    running.current?.cancel();
    running.current = null;

    if (!collapsed) {
      remember(true);
      if (!el || reducedMotion()) { setCollapsed(true); return; }
      // Fold: shrink to the header (plus the panel's 1px borders), fade the
      // body over it, and only then drop the body from the tree.
      setFolding(true);
      const to = (header.current?.getBoundingClientRect().height ?? 36) + 2;
      body.current?.animate([{ opacity: 1 }, { opacity: 0 }], { duration: DURATION * 0.6, easing: "ease-out", fill: "forwards" });
      const anim = el.animate([{ height: `${from}px` }, { height: `${to}px` }], { duration: DURATION, easing: EASE, fill: "forwards" });
      running.current = anim;
      anim.onfinish = () => {
        setCollapsed(true);
        setFolding(false);
        // Hand the height back to CSS on the next frame, once the folded
        // layout is committed — cancelling now would flash the full height.
        requestAnimationFrame(() => anim.cancel());
      };
      return;
    }

    remember(false);
    setFolding(false);
    if (!reducedMotion()) expandFrom.current = from;
    setCollapsed(false);
  };

  return (
    <section
      ref={panel}
      aria-labelledby={id}
      // Folded, the panel takes its content height instead of stretching to
      // its row partner, or it reads as an empty box rather than a closed one.
      // `height`, not `align-self`: in a flex column align-self is the
      // horizontal axis, and the panel would shrink sideways.
      style={collapsed ? { ...style, height: "fit-content" } : style}
      className={`anim-float panel-shadow flex flex-col overflow-hidden rounded-panel border border-line bg-surface ${className}`}
    >
      <header ref={header} className={`flex h-9 shrink-0 items-center gap-2 px-2 pr-4 ${collapsed ? "" : "border-b border-line"}`}>
        <button
          type="button"
          onClick={toggle}
          aria-expanded={!collapsed}
          aria-controls={`${id}-body`}
          aria-label={`${collapsed ? "Expand" : "Collapse"} ${title}`}
          className="grid size-6 shrink-0 place-items-center rounded text-muted transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <ChevronDown size={15} className={`transition-transform duration-300 ${collapsed || folding ? "-rotate-90" : ""}`} />
        </button>
        <h2 id={id} className="truncate text-sm font-medium text-ink">
          <button type="button" onClick={toggle} className="truncate text-left" tabIndex={-1}>{title}</button>
        </h2>
        {aside && <div className="nums ml-auto shrink-0 text-xs text-muted">{aside}</div>}
      </header>
      {!collapsed && (
        <div ref={body} id={`${id}-body`} className={`flex min-h-0 flex-1 flex-col ${bodyClassName}`}>
          {children}
        </div>
      )}
    </section>
  );
}
