"use client";

import { Suspense, useState } from "react";
import { usePathname } from "next/navigation";
import { useChatTitle } from "@/lib/chat/title-store";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

/* Maps a route to its breadcrumb trail. Keeping it here rather than passing
   crumbs down from each page means a new route gets a correct breadcrumb by
   adding one line, and never gets a stale one. */
const CRUMBS: Record<string, string[]> = {
  "/missions/replay": ["SPOTlight", "Missions", "Path preview"],
  "/": ["SPOTlight", "Overview"],
  "/assets": ["SPOTlight", "Assets", "Inspection imagery"],
  "/assistant": ["SPOTlight", "AI Assistance"],
};

/** The conversation id in /assistant/<id>, or null on any other route. */
function chatIdFrom(pathname: string) {
  return /^\/assistant\/([^/]+)\/?$/.exec(pathname)?.[1] ?? null;
}

function crumbsFor(pathname: string, chatTitle: string | null): string[] {
  if (CRUMBS[pathname]) return CRUMBS[pathname];
  // A chat shows its title, never its uuid. The chat page publishes the
  // title once it has loaded; until then (or if it never does) it's "Chat".
  if (chatIdFrom(pathname)) return ["SPOTlight", "AI Assistance", chatTitle ?? "Chat"];
  // Fall back to the path itself, title-cased, so an unmapped route still
  // orients the user instead of showing nothing.
  return [
    "SPOTlight",
    ...pathname
      .split("/")
      .filter(Boolean)
      .map((seg) => seg.charAt(0).toUpperCase() + seg.slice(1).replace(/-/g, " ")),
  ];
}

type TopBarProps = Omit<React.ComponentProps<typeof TopBar>, "crumbs">;

/* The breadcrumb is the shell's only pathname read. Under Cache Components
   usePathname() suspends while a route with an unknown dynamic param (like
   /assistant/[conversationId]) is prerendered, so the read lives in this leaf
   behind Suspense instead of at the top of AppShell — otherwise the whole
   shell, and the build, would block on it. */
function RoutedTopBar(props: TopBarProps) {
  const pathname = usePathname();
  const chatTitle = useChatTitle(chatIdFrom(pathname));
  return <TopBar crumbs={crumbsFor(pathname, chatTitle)} {...props} />;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  /* Two states, because the rail means two different things at two sizes and
     the breakpoint is a CSS fact, not a React one. Deriving one boolean from
     matchMedia would mean reading the viewport during render — which the
     server can't do, and which costs a hydration mismatch to paper over.

     `docked` is the desktop dock/undock; `drawer` is the mobile overlay.
     Each is driven by its own button, shown at its own breakpoint. */
  const [docked, setDocked] = useState(true);
  const [drawer, setDrawer] = useState(false);

  function closeRail() {
    setDocked(false);
    setDrawer(false);
  }

  return (
    <div className="flex h-dvh overflow-hidden">
      {/* Below lg the rail floats over the content, so it needs a scrim to
          dismiss it and to mute what's behind. */}
      {drawer && (
        <button
          type="button"
          tabIndex={-1}
          aria-hidden="true"
          onClick={() => setDrawer(false)}
          className="fixed inset-0 z-10 cursor-default bg-black/50 lg:hidden"
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-20 lg:static lg:z-auto ${
          drawer ? "flex" : "hidden"
        } ${docked ? "lg:flex" : "lg:hidden"}`}
      >
        <Sidebar onClose={closeRail} onNavigate={() => setDrawer(false)} />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <Suspense
          fallback={
            <TopBar
              crumbs={["SPOTlight"]}
              docked={docked}
              onOpenDrawer={() => setDrawer(true)}
              onDock={() => setDocked(true)}
            />
          }
        >
          <RoutedTopBar
            docked={docked}
            onOpenDrawer={() => setDrawer(true)}
            onDock={() => setDocked(true)}
          />
        </Suspense>
        <main id="main" className="flex-1 overflow-y-auto bg-canvas">
          {children}
        </main>
      </div>
    </div>
  );
}
