import { Suspense } from "react";
import { connection } from "next/server";
import { ActionSuccessChart } from "@/components/ActionSuccessChart";
import { AnalyticsCharts } from "@/components/AnalyticsCharts";
import { getDashboard } from "@/lib/dashboard-loader";
import { fleetAnalytics } from "@/lib/fleet-analytics";

/* The fleet-wide success chart lives here rather than on the Overview, which
   keeps to the banner and the queue so an operator at shift start reads one
   question and one list. */
async function FleetSuccess() {
  await connection();
  const { actionSuccess } = await getDashboard();
  return <ActionSuccessChart groups={actionSuccess.groups} caption={actionSuccess.caption} benchmark={actionSuccess.fleetRate} collapsible />;
}

export default function Analytics() {
  const data = fleetAnalytics();
  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-6 p-4">
      <header className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline gap-x-3">
          <h1 className="text-2xl font-semibold text-ink">Analytics</h1>
          <p className="nums text-xs text-muted">Challenge kit · {data.coverage}</p>
        </div>
        <p className="text-xs text-muted">Click any chart to see the records behind it.</p>
      </header>
      <Suspense fallback={null}><FleetSuccess /></Suspense>
      <AnalyticsCharts data={data} />
    </div>
  );
}
