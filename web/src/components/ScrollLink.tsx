"use client";

/* An in-page link that glides to its target instead of jumping. The page
   scrolls inside <main>, not the document, so `scroll-behavior` on html never
   applies to it; this does the scroll itself. Reduced motion jumps, and the
   href still works without JavaScript. Focus moves to the target, as a plain
   anchor's would, so keyboard users land where they were sent. */
export function ScrollLink({ href, className, children }: { href: `#${string}`; className?: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      className={className}
      onClick={(event) => {
        const target = document.getElementById(href.slice(1));
        if (!target) return;
        event.preventDefault();
        const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        target.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
        if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
        target.focus({ preventScroll: true });
        window.history.replaceState(null, "", href);
      }}
    >
      {children}
    </a>
  );
}
