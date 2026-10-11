"use client";

import { useEffect, useRef } from "react";
import { ChevronRight, CircleHelp, PanelLeft, Search } from "lucide-react";
import { ThemeToggle } from "@/components/ThemeToggle";

const ICON_BUTTON =
  "grid size-8 cursor-pointer place-items-center rounded text-muted transition-colors hover:bg-surface-2 hover:text-ink";

export function TopBar({
  crumbs,
  docked,
  onOpenDrawer,
  onDock,
}: {
  crumbs: string[];
  docked: boolean;
  onOpenDrawer: () => void;
  onDock: () => void;
}) {
  const searchRef = useRef<HTMLInputElement>(null);

  // ⌘K / Ctrl-K focuses search, the shortcut every operator already has in
  // their fingers from Grafana and the Orbit console.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        searchRef.current?.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-line bg-chrome px-3">
      {/* One button per breakpoint rather than one button with a branch: the
          rail is an overlay below lg and a dock above it, and CSS is what
          knows which. */}
      <button
        type="button"
        onClick={onOpenDrawer}
        aria-label="Open menu"
        className={`${ICON_BUTTON} lg:hidden`}
      >
        <PanelLeft size={16} aria-hidden="true" />
      </button>
      {!docked && (
        <button
          type="button"
          onClick={onDock}
          aria-label="Dock menu"
          className={`${ICON_BUTTON} hidden lg:grid`}
        >
          <PanelLeft size={16} aria-hidden="true" />
        </button>
      )}

      <nav aria-label="Breadcrumb" className="min-w-0">
        <ol className="flex items-center gap-1 text-sm">
          {crumbs.map((crumb, i) => {
            const last = i === crumbs.length - 1;
            return (
              <li key={crumb} className="flex min-w-0 items-center gap-1">
                {i > 0 && (
                  <ChevronRight
                    size={14}
                    aria-hidden="true"
                    className="shrink-0 text-faint"
                  />
                )}
                <span
                  aria-current={last ? "page" : undefined}
                  className={`truncate ${last ? "text-ink" : "text-muted"}`}
                >
                  {crumb}
                </span>
              </li>
            );
          })}
        </ol>
      </nav>

      <form action="/search" className="relative ml-auto min-w-0 flex-1 sm:flex-none sm:w-64 lg:w-80">
        <Search
          size={14}
          aria-hidden="true"
          className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-faint"
        />
        <input
          ref={searchRef}
          name="q"
          type="search"
          placeholder="Search missions, assets, flags…"
          aria-label="Search missions, assets and flags"
          className="h-8 w-full rounded border border-line-strong bg-surface-3 pr-12 pl-8 text-sm text-ink transition-colors placeholder:text-muted hover:border-ink"
        />
        <kbd className="nums pointer-events-none absolute top-1/2 right-2 -translate-y-1/2 rounded border border-line-strong px-1 py-0.5 font-mono text-[10px] text-muted">
          ⌘K
        </kbd>
      </form>

      <div className="ml-auto flex items-center gap-1 sm:ml-0">
        <ThemeToggle />
        <a
          href="/help"
          aria-label="Project help and documentation"
          className={ICON_BUTTON}
        >
          <CircleHelp size={16} aria-hidden="true" />
        </a>
      </div>
    </header>
  );
}
