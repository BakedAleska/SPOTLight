"use client";

import useSWR from "swr";
import { useState } from "react";
import { FlagDetails } from "@/components/FlagDetails";
import type { Flag } from "@/lib/types";
import { PriorityQueue } from "@/components/PriorityQueue";
import { QueueFilters } from "@/components/QueueFilters";
import { filterFlags, isFiltered, NO_FILTER, type QueueFilter } from "@/lib/queue-filter";
import { WelcomeBanner } from "@/components/WelcomeBanner";
import { LoadingIndicator } from "@/components/LoadingIndicator";
import type { DashboardData } from "@/lib/dashboard";

async function fetchDashboard(url: string): Promise<DashboardData> {
  const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(15_000) });
  if (!response.ok) throw new Error("Dashboard data is unavailable.");
  return response.json() as Promise<DashboardData>;
}

/* The load cascade lives here, not inside the panels, because it is a fact
   about this page's reading order rather than about any one card. */
const STAGGER = 55;
export default function Dashboard({ initialData }: { initialData: DashboardData }) {
  const [filter, setFilter] = useState<QueueFilter>(NO_FILTER);
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
    fallbackData: initialData,
    refreshInterval: 2500,
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
  const { flags } = data;
  const shown = filterFlags(flags, filter);
  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
      <h1 className="sr-only">SPOT operator overview</h1>

      <WelcomeBanner hiddenFailures={data.hiddenFailures} />

      {/* Overview is deliberately two things: the banner's question and the
          list that answers it, with search and filters over that list. The
          fleet figures and charts live on /analytics. */}
      <QueueFilters flags={flags} value={filter} onChange={setFilter} shown={shown.length} />

      <div id="queue" className="scroll-mt-4">
        <PriorityQueue flags={shown} total={isFiltered(filter) ? flags.length : data.flagCount} delay={STAGGER}
          emptyMessage={isFiltered(filter) ? "No open flags match these filters." : undefined}
          onSelect={data.features.flagDetails ? selectFlag : undefined} />
      </div>
      {selectedFlag && data.features.flagDetails && <FlagDetails key={selectedFlag.id} flag={selectedFlag} features={data.features} onClose={() => selectFlag(null)} onChanged={() => void mutate()} />}
    </div>
  );
}
