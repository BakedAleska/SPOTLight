import { Suspense } from "react";
import { BatteryCliff } from "@/components/BatteryCliff";
import { DashboardSkeleton } from "@/components/DashboardSkeleton";
import { DegradedNotice } from "@/components/DegradedNotice";
import { PriorityQueue } from "@/components/PriorityQueue";
import { StatTile } from "@/components/StatTile";
import { WelcomeBanner } from "@/components/WelcomeBanner";
import { getDashboard } from "@/lib/dashboard";

/* The operator's shift-start briefing. One question: do I need to go look at
   anything? The queue answers it; everything else is supporting context.

   Every number on this page comes from the backend API — flags the backend
   triaged with Laya and the policy validator, and insights it computed in SQL
   over Neon — mapped in @/lib/dashboard. This page is the only place that
   touches that module: the panels below take plain props, so none of them
   knows whether the numbers came from Neon, from the kit CSVs, or from a
   fixture in a test. */

/* The load cascade lives here, not inside the panels, because it is a fact
   about this page's reading order rather than about any one card. Six panels
   at 55ms apart settle by ~780ms: long enough to read as deliberate, short
   enough that an operator opening this at shift start is never waiting on it. */
const STAGGER = 55;

/* Everything here is request-time: an operator console that cached its flags
   would be lying about the fleet. Under `cacheComponents` an uncached read has
   to sit inside <Suspense>, which is also exactly what we want on stage — the
   chrome and the panel titles are prerendered and painted immediately, and the
   numbers stream in behind them rather than holding the document. */
async function Briefing() {
  const { stats, flags, batteryBands, headline, degraded } = await getDashboard();

  return (
    <div className="flex flex-col gap-4">
      {degraded && <DegradedNotice reason={degraded} />}

      <WelcomeBanner hiddenFailures={headline.hiddenFailures} />

      <section aria-label="Headline metrics">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {stats.map((stat, i) => (
            <StatTile key={stat.label} {...stat} delay={STAGGER * i} />
          ))}
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-5">
        <div id="queue" className="scroll-mt-4 xl:col-span-3">
          <PriorityQueue flags={flags} delay={STAGGER * stats.length} />
        </div>
        <div className="xl:col-span-2">
          <BatteryCliff
            bands={batteryBands}
            caption={headline.batteryCliff}
            benchmark={headline.aboveCliffRate}
            delay={STAGGER * (stats.length + 1)}
          />
        </div>
      </div>
    </div>
  );
}

export default function Dashboard() {
  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
      <h1 className="sr-only">SPOT operator overview</h1>

      <Suspense fallback={<DashboardSkeleton />}>
        <Briefing />
      </Suspense>
    </div>
  );
}
