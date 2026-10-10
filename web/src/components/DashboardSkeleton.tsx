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
            {/* Mirrors BatteryCliff's geometry — 18px of cap-label headroom,
                a 168px plot, then the axis band — so the chart lands where its
                placeholder was instead of shifting the caption. */}
            <div className="mt-[18px] ml-9 flex items-end gap-2" style={{ height: 168 }}>
              {["h-[46%]", "h-[88%]", "h-[92%]", "h-[96%]", "h-[90%]"].map((height) => (
                <Bar key={height} className={`max-w-6 flex-1 ${height}`} />
              ))}
            </div>
            <div className="ml-9 flex gap-2 border-t border-line pt-1.5">
              {[0, 1, 2, 3, 4].map((band) => (
                <div key={band} className="flex-1">
                  <Bar className="h-3 w-full" />
                  <Bar className="mt-1 h-2.5 w-2/3" />
                </div>
              ))}
            </div>
            <Bar className="mt-3 h-3 w-full" />
            <Bar className="mt-1.5 h-3 w-4/5" />
          </Panel>
        </div>
      </div>
    </div>
  );
}
