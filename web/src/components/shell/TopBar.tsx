"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ChevronRight, CircleHelp, PanelLeft, Search } from "lucide-react";
import { ThemeToggle } from "@/components/ThemeToggle";
import type { SearchResult } from "@/lib/search";

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
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [open, setOpen] = useState(false);
  // -1 is "nothing highlighted": Enter then runs the full search page.
  const [active, setActive] = useState(-1);

  // Suggestions follow the query, debounced so a fast typist sends one
  // request per pause rather than one per key, and aborted when superseded so
  // a slow answer to "w" can never overwrite the answer to "wp05".
  useEffect(() => {
    const q = query.trim();
    // An empty box hides the list (see showList) rather than clearing it, so
    // there is nothing to fetch.
    if (!q) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const response = await fetch(`/api/search?q=${encodeURIComponent(q)}`, { signal: controller.signal });
        const body = (await response.json()) as { results?: SearchResult[] };
        setResults(body.results ?? []);
        setActive(-1);
      } catch {
        // Aborted or offline: keep whatever was showing.
      }
    }, 120);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  /* Autofill: when the best match's ID or name starts with what's typed, the
     rest of it is shown ghosted after the caret, and Tab or → takes it. The
     typed characters keep the user's own casing; only the tail is filled. */
  const completion = (() => {
    const q = query;
    const top = results[0];
    if (!q.trim() || !top) return "";
    for (const candidate of [top.name, top.id]) {
      if (candidate.length > q.length && candidate.toLowerCase().startsWith(q.toLowerCase())) {
        return candidate.slice(q.length);
      }
    }
    return "";
  })();

  const showList = open && query.trim() !== "" && results.length > 0;

  function go(result: SearchResult) {
    setOpen(false);
    setQuery("");
    searchRef.current?.blur();
    router.push(result.href);
  }

  function onSearchKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if ((e.key === "Tab" || e.key === "ArrowRight") && completion && !e.shiftKey) {
      const input = e.currentTarget;
      // → only accepts from the end of the text, so it still moves the caret
      // when the user is editing the middle of the query.
      if (e.key === "Tab" || input.selectionStart === query.length) {
        e.preventDefault();
        setQuery(query + completion);
        return;
      }
    }
    if (!showList) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const delta = e.key === "ArrowDown" ? 1 : -1;
      // Cycles through -1 (back in the text box) and each suggestion.
      const slots = results.length + 1;
      setActive((i) => ((i + 1 + delta + slots) % slots) - 1);
    } else if (e.key === "Enter" && active >= 0 && results[active]) {
      e.preventDefault();
      go(results[active]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    }
  }

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

      <form
        action="/search"
        role="search"
        className="relative ml-auto min-w-0 flex-1 sm:flex-none sm:w-64 lg:w-80"
        onSubmit={() => setOpen(false)}
      >
        <Search
          size={14}
          aria-hidden="true"
          className="pointer-events-none absolute top-1/2 left-2.5 z-10 -translate-y-1/2 text-faint"
        />
        <input
          ref={searchRef}
          name="q"
          type="search"
          autoComplete="off"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          // Deferred so a click on a suggestion lands before the list goes.
          onBlur={() => setTimeout(() => setOpen(false), 120)}
          onKeyDown={onSearchKeyDown}
          role="combobox"
          aria-autocomplete="both"
          aria-expanded={showList}
          aria-controls="search-suggestions"
          aria-activedescendant={showList && active >= 0 ? `search-option-${active}` : undefined}
          placeholder="Search missions, assets, flags…"
          aria-label="Search missions, assets and flags"
          className="h-8 w-full rounded border border-line-strong bg-surface-3 pr-12 pl-8 text-sm text-ink transition-colors placeholder:text-muted hover:border-ink"
        />
        {/* The ghosted completion. The typed part is drawn transparent so the
            tail lands exactly where the input's own text ends. */}
        {completion && open && (
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-y-0 right-12 left-8 flex items-center overflow-hidden text-sm whitespace-pre"
          >
            <span className="text-transparent">{query}</span>
            <span className="text-faint">{completion}</span>
          </span>
        )}
        <kbd className="nums pointer-events-none absolute top-1/2 right-2 -translate-y-1/2 rounded border border-line-strong px-1 py-0.5 font-mono text-[10px] text-muted">
          {completion && open ? "Tab" : "⌘K"}
        </kbd>

        {showList && (
          <ul
            id="search-suggestions"
            role="listbox"
            aria-label="Search suggestions"
            className="panel-shadow absolute inset-x-0 top-full z-50 mt-1 max-h-80 overflow-y-auto rounded border border-line bg-surface py-1"
          >
            {results.map((result, i) => (
              <li
                key={`${result.kind}-${result.id}`}
                id={`search-option-${i}`}
                role="option"
                aria-selected={i === active}
                // mousedown, not click: it fires before the input's blur.
                onMouseDown={(e) => {
                  e.preventDefault();
                  go(result);
                }}
                onMouseEnter={() => setActive(i)}
                className={`flex cursor-pointer items-center gap-2 px-2.5 py-1.5 text-sm ${i === active ? "bg-surface-2" : ""}`}
              >
                <span className="w-14 shrink-0 text-[10px] font-medium tracking-wide text-muted uppercase">
                  {result.kind}
                </span>
                <span className="nums shrink-0 font-mono text-xs text-ink">{result.id}</span>
                <span className="min-w-0 truncate text-muted">{result.name}</span>
              </li>
            ))}
            <li
              role="option"
              aria-selected={false}
              onMouseDown={(e) => {
                e.preventDefault();
                setOpen(false);
                router.push(`/search?q=${encodeURIComponent(query.trim())}`);
              }}
              className="cursor-pointer border-t border-line px-2.5 pt-2 pb-1 text-xs text-muted hover:text-ink"
            >
              See all results for “{query.trim()}” ↵
            </li>
          </ul>
        )}
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
