"use client";

import useSWR from "swr";
import { useState } from "react";
import { FlagDetails } from "@/components/FlagDetails";
import type { Flag } from "@/lib/types";
import { BatteryCliff } from "@/components/BatteryCliff";
import { PriorityQueue } from "@/components/PriorityQueue";
import { StatTile } from "@/components/StatTile";
import { WelcomeBanner } from "@/components/WelcomeBanner";
import { LoadingIndicator } from "@/components/LoadingIndicator";
import type { DashboardData } from "@/lib/dashboard";

async function fetchDashboard(url: string): Promise<DashboardData> {
  const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(15_000) });
  if (!response.ok) throw new Error("Dashboard data is unavailable.");
  return response.json() as Promise<DashboardData>;
}

/* The load cascade lives here, not inside the panels, because it is a fact
   about this page's reading order rather than about any one card. Six panels
   at 55ms apart settle by ~780ms: long enough to read as deliberate, short
   enough that an operator opening this at shift start is never waiting on it. */
const STAGGER = 55;
export default function Dashboard() {
  const [selectedFlag, setSelectedFlag] = useState<Flag | null>(() => {
    if (typeof window === "undefined") return null;
    const id = new URLSearchParams(window.location.search).get("flag");
    return id && /^[1-9]\d*$/.test(id) ? { id, title: `Flag #${id}`, missionId: "", assetName: "", locationName: "", severity: "LOW", score: 0, reasons: [] } : null;
  });
  function selectFlag(flag: Flag | null) {
    setSelectedFlag(flag);
    const url = new URL(window.location.href);
    if (flag) url.searchParams.set("flag", flag.id);
    else url.searchParams.delete("flag");
    window.history.replaceState(null, "", url);
  }
  const { data, error, isValidating, mutate } = useSWR<DashboardData>("/api/dashboard", fetchDashboard, {
    refreshInterval: (current) => current ? 10_000 : 0,
    shouldRetryOnError: false,
    revalidateOnFocus: false,
  });
  if (!data) {
    if (!error || isValidating) return <LoadingIndicator label="Loading dashboard…" />;
    return (
      <div className="flex min-h-[60dvh] flex-col items-center justify-center gap-3 p-6 text-center" role="alert">
        <p className="font-medium">Dashboard unavailable</p>
        <p className="text-sm text-muted">The backend isn’t responding. Check its connection, then try again.</p>
        <button type="button" onClick={() => void mutate()} className="rounded bg-accent px-4 py-2 text-on-accent">Retry</button>
      </div>
    );
  }
  const { stats, flags, batteryBands } = data;
  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
      <h1 className="sr-only">SPOT operator overview</h1>

      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <p role="status">{error ? "Connection interrupted. Showing the last successful update." : "Live backend data · refreshes every 10 seconds"}</p>
        <button type="button" onClick={() => void mutate()} disabled={isValidating} className="rounded border border-line px-3 py-1 disabled:opacity-50">
          {isValidating ? "Refreshing…" : "Refresh now"}
        </button>
      </div>
      <WelcomeBanner hiddenFailures={data.hiddenFailures} />

      <section aria-label="Headline metrics">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {stats.map((stat, i) => (
            <StatTile key={stat.label} {...stat} delay={STAGGER * i} />
          ))}
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-5">
        <div id="queue" className="scroll-mt-4 xl:col-span-3">
          <PriorityQueue flags={flags} total={data.flagCount} delay={STAGGER * stats.length} onSelect={data.features.flagDetails ? selectFlag : undefined} />
        </div>
        <div className="xl:col-span-2">
          <BatteryCliff
            bands={batteryBands}
            caption={data.batteryCaption}
            benchmark={data.aboveCliffRate}
            delay={STAGGER * (stats.length + 1)}
          />
        </div>
      </div>
      {selectedFlag && data.features.flagDetails && <FlagDetails key={selectedFlag.id} flag={selectedFlag} features={data.features} onClose={() => selectFlag(null)} onChanged={() => void mutate()} />}
    </div>
  );
}
