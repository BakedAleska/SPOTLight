import { Panel } from "./Panel";

/* The static shell the page streams before the backend answers.
   `cacheComponents` prerenders everything outside <Suspense>, so this is what
   an operator sees the instant the document arrives — and the panel titles are
   the part of the dashboard that never depends on data, so they are real here
   rather than grey boxes. Only the numbers pulse. */

function Bar({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded bg-surface-2 ${className}`} aria-hidden="true" />;
}

const TILES = ["Missions", "Hiding failures", "Success below 25% battery", "V2 vs V1"];

export function DashboardSkeleton() {
  return (
    <div className="flex flex-col gap-4" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading the latest triage from the backend…</span>

      {/* Matches the banner's height so the panels below don't jump when the
          real card replaces it. */}
      <Bar className="h-[164px] rounded-panel" />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {TILES.map((title) => (
          <Panel key={title} title={title} bodyClassName="px-4 py-3">
            <Bar className="h-8 w-24" />
            <Bar className="mt-3 h-3 w-full" />
            <Bar className="mt-1.5 h-3 w-2/3" />
          </Panel>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-5">
        <div className="xl:col-span-3">
          <Panel title="Needs attention" className="h-full">
            {[0, 1, 2, 3].map((row) => (
              <div key={row} className="border-b border-line py-3 first:pt-0 last:border-0">
                <Bar className="h-3 w-32" />
                <Bar className="mt-2 h-4 w-3/4" />
                <Bar className="mt-2 h-3 w-1/2" />
              </div>
            ))}
          </Panel>
        </div>
        <div className="xl:col-span-2">
          <Panel title="Action success by battery level" className="h-full">
            <div className="flex flex-1 items-end gap-3" style={{ minHeight: 160 }}>
              {["h-[70%]", "h-[95%]", "h-[90%]", "h-full", "h-[85%]"].map((height) => (
                <Bar key={height} className={`flex-1 ${height}`} />
              ))}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}
