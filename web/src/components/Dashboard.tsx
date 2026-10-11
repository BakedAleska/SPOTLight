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
import { OverviewAnalytics } from "@/components/OverviewAnalytics";
import type { DashboardData } from "@/lib/dashboard";
import type { OverviewAnalyticsData } from "@/lib/overview";

async function fetchDashboard(url: string): Promise<DashboardData> {
  const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(15_000) });
  if (!response.ok) throw new Error("Dashboard data is unavailable.");
  return response.json() as Promise<DashboardData>;
}

/* The load cascade lives here, not inside the panels, because it is a fact
   about this page's reading order rather than about any one card. */
const STAGGER = 55;
export default function Dashboard({ initialData, analytics }: {
  initialData: DashboardData;
  /** The Overview's charts beside the queue. /review renders the queue alone. */
  analytics?: OverviewAnalyticsData;
}) {
  const [filter, setFilter] = useState<QueueFilter>(NO_FILTER);
  /** The queue row that is open. */
  const [openId, setOpenId] = useState<string | null>(null);
  /** The flag the analytics beside the queue are about: the last row opened.
      Closing the row keeps it, so the charts only turn when another row is
      opened or the operator clears the context. */
  const [contextId, setContextId] = useState<string | null>(null);
  function openRow(id: string | null) {
    setOpenId(id);
    if (id) setContextId(id);
  }
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
  // A row hidden by the filters (or resolved) is no longer the charts' context.
  const contextFlag = shown.find((flag) => flag.id === contextId) ?? null;
  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
      <h1 className="sr-only">SPOT operator overview</h1>

      <WelcomeBanner hiddenFailures={data.hiddenFailures} />

      {/* The banner's question, the list that answers it (severity pills in
          its header), and beside the list a few fleet charts that turn to
          the flag opened last. On a wide container the queue takes the left
          ~60% and the charts the rest, both panels the same height. Narrower,
          everything stacks, queue first. The full chart catalogue is
          /analytics. The minimum height leaves <main> room to scroll the
          queue's top to the top bar ("Go to the priority queue"): the
          viewport less the 3rem top bar, the queue's 1rem scroll margin and
          the page's 1rem bottom padding. Without it a short page runs out of
          scroll first and the banner's foot stays on screen. */}
      <div className="@container min-h-[calc(100dvh-5rem)]">
        <div className={`grid gap-4 ${analytics ? "@4xl:grid-cols-[minmax(0,3fr)_minmax(22rem,2fr)]" : ""}`}>
          <div id="queue" className="min-w-0 scroll-mt-4">
            <PriorityQueue flags={shown} total={isFiltered(filter) ? flags.length : data.flagCount} delay={STAGGER}
              filters={<QueueFilters flags={flags} total={data.flagCount} value={filter} onChange={setFilter} shown={shown.length} />}
              emptyMessage={isFiltered(filter) ? "No open flags match these filters." : undefined}
              openId={openId} onOpenChange={openRow}
              onSelect={data.features.flagDetails ? selectFlag : undefined} />
          </div>

          {/* Side by side, the queue sets the row's height and the charts
              fill it (absolutely, so a taller chart never stretches the queue
              into a gap under its last row); 30rem keeps them readable when
              a filter shrinks the queue to a row or two. */}
          {analytics && (
            <div className="relative min-w-0 @4xl:min-h-[30rem]">
              <div className="h-full @4xl:absolute @4xl:inset-0">
                <OverviewAnalytics data={analytics} flag={contextFlag} onClear={() => setContextId(null)} delay={STAGGER * 2} />
              </div>
            </div>
          )}
        </div>
      </div>
      {selectedFlag && data.features.flagDetails && <FlagDetails key={selectedFlag.id} flag={selectedFlag} features={data.features} onClose={() => selectFlag(null)} onChanged={() => void mutate()} />}
    </div>
  );
}
